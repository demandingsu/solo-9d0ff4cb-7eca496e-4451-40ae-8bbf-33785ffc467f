"""Domain logic: event ingestion, paths, consent withdrawal, catalog.

All functions take an open :class:`sqlite3.Connection`; transaction
boundaries are controlled by the caller (see :mod:`app.db`) so that an
API request is always one atomic unit of work.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .models import (
    CatalogEditResult,
    CatalogEntry,
    CatalogEntryIn,
    CatalogEntryPatch,
    CatalogFilterRequest,
    CatalogFilterResult,
    EventIn,
    FacetCount,
    IngestResult,
    Visit,
    VisitPath,
    WithdrawResult,
)


class BadEventTime(ValueError):
    """Raised when event_time is not a timezone-aware ISO-8601 timestamp."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC.

    Naive timestamps are rejected: the API contract requires explicit UTC
    so that open/close durations are never computed on ambiguous local time.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadEventTime(f"event_time 不是合法 ISO-8601 时间: {value!r}") from exc
    if dt.tzinfo is None:
        raise BadEventTime(f"event_time 必须带时区（UTC），收到无时区时间: {value!r}")
    return dt.astimezone(timezone.utc)


def is_withdrawn(conn: sqlite3.Connection, visitor_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM withdrawals WHERE visitor_id = ? LIMIT 1",
        (visitor_id,),
    ).fetchone()
    return row is not None


def ingest_event(conn: sqlite3.Connection, event: EventIn) -> IngestResult:
    """Store one event. Must be called inside a write transaction.

    Returns ``duplicate`` when ``event_id`` already exists (idempotent
    replay, payload differences are ignored), and ``rejected_withdrawn``
    when the visitor has withdrawn consent — such events are never stored
    and therefore can never resurrect a deleted path.
    """
    if is_withdrawn(conn, event.visitor_id):
        return IngestResult(event_id=event.event_id, status="rejected_withdrawn")

    existing = conn.execute(
        "SELECT 1 FROM events WHERE event_id = ? LIMIT 1",
        (event.event_id,),
    ).fetchone()
    if existing is not None:
        return IngestResult(event_id=event.event_id, status="duplicate")

    # Validated before INSERT so a bad timestamp fails the whole transaction
    # instead of leaving a partial write behind.
    normalized = parse_utc(event.event_time).isoformat()
    conn.execute(
        """
        INSERT INTO events
            (event_id, visitor_id, page_view_id, exhibit_id,
             event_type, event_time, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.visitor_id,
            event.page_view_id,
            event.exhibit_id,
            event.event_type.value,
            normalized,
            utc_now_iso(),
        ),
    )
    return IngestResult(event_id=event.event_id, status="stored")


def _pair_visits(rows: list[sqlite3.Row]) -> list[Visit]:
    """Pair open/close rows by page_view_id and order the resulting path."""
    opens: dict[str, sqlite3.Row] = {}
    closes: dict[str, list[sqlite3.Row]] = {}

    for row in rows:
        bucket = opens if row["event_type"] == "open" else closes
        if row["event_type"] == "open":
            # Keep the earliest open if a page view ever reports more than one.
            if row["page_view_id"] not in bucket or row["event_time"] < bucket[row["page_view_id"]]["event_time"]:
                bucket[row["page_view_id"]] = row
        else:
            closes.setdefault(row["page_view_id"], []).append(row)

    visits: list[Visit] = []
    for page_view_id, open_row in opens.items():
        opened_at = open_row["event_time"]
        close_rows = closes.get(page_view_id, [])
        # Earliest close for this page view.
        close_row = min(close_rows, key=lambda r: r["event_time"]) if close_rows else None

        closed_at = close_row["event_time"] if close_row is not None else None
        dwell_seconds: float | None = None
        dwell_known = False

        if closed_at is not None:
            open_dt = datetime.fromisoformat(opened_at)
            close_dt = datetime.fromisoformat(closed_at)
            delta = (close_dt - open_dt).total_seconds()
            if delta >= 0:
                dwell_seconds = round(delta, 3)
                dwell_known = True
            # delta < 0: close earlier than its own open — anomalous input,
            # closed_at is returned but stay duration is treated as unknown.

        visits.append(
            Visit(
                page_view_id=page_view_id,
                exhibit_id=open_row["exhibit_id"],
                opened_at=opened_at,
                closed_at=closed_at,
                dwell_seconds=dwell_seconds,
                dwell_known=dwell_known,
                sequence=0,  # assigned after sorting
            )
        )

    # Path order = open time. A late-arriving earlier open therefore simply
    # re-sorts the path on the next query; nothing about the order is stored.
    visits.sort(key=lambda v: (v.opened_at, v.page_view_id))
    for index, visit in enumerate(visits, start=1):
        visit.sequence = index
    return visits


def get_visitor_path(conn: sqlite3.Connection, visitor_id: str) -> VisitPath:
    """Reconstruct a visitor's path from committed events.

    Runs in a single read transaction so the withdrawal check and the event
    scan observe one consistent snapshot.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        withdrawn = is_withdrawn(conn, visitor_id)
        if withdrawn:
            # Consent withdrawn: the path must disappear and stay empty.
            conn.rollback()
            return VisitPath(
                visitor_id=visitor_id,
                consent_withdrawn=True,
                visits=[],
                total_visits=0,
            )

        rows = conn.execute(
            """
            SELECT page_view_id, exhibit_id, event_type, event_time
            FROM events
            WHERE visitor_id = ?
            """,
            (visitor_id,),
        ).fetchall()
        visits = _pair_visits(rows)
        conn.rollback()  # read-only snapshot; nothing to commit
        return VisitPath(
            visitor_id=visitor_id,
            consent_withdrawn=False,
            visits=visits,
            total_visits=len(visits),
        )
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Catalog: versioned exhibit entries and facet filtering
#
# Storage model: one row per catalog *revision*. An edit closes the previous
# row (valid_to = new version) and inserts a single replacement row
# (valid_from = new version). Version V therefore means "the catalog after
# the edit that created V", queryable with a half-open window
#   valid_from <= V AND (valid_to IS NULL OR valid_to > V)
# No historical copy of the whole catalog is ever written, and an estimate
# pinned to V keeps returning the same exhibit set even after later edits.
# ---------------------------------------------------------------------------
class CatalogError(ValueError):
    """Base class for catalog request errors (mapped to HTTP 4xx)."""


class CatalogEntryNotFound(CatalogError):
    """The exhibit id does not exist in the current catalog."""


class CatalogVersionNotFound(CatalogError):
    """The requested catalog version does not exist."""


class CatalogConflict(CatalogError):
    """The exhibit already exists; use the correction endpoint."""


def _current_version(conn: sqlite3.Connection) -> int:
    """Current catalog version; 0 denotes an empty catalog."""
    row = conn.execute("SELECT MAX(version) FROM catalog_versions").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _resolve_version(conn: sqlite3.Connection, requested: int | None) -> int:
    """Pin a query to ``requested`` (validated) or to the current version."""
    current = _current_version(conn)
    if requested is None:
        return current
    if requested < 0 or requested > current:
        raise CatalogVersionNotFound(
            f"目录版本 {requested} 不存在（现有版本 0..{current}）"
        )
    return requested


def _next_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM catalog_versions").fetchone()
    return int(row[0] or 0) + 1


def _entry_from_row(row: sqlite3.Row) -> CatalogEntry:
    return CatalogEntry(
        exhibit_id=row["exhibit_id"],
        status=row["status"],
        materials=json.loads(row["materials"]),
        themes=json.loads(row["themes"]),
        since_version=row["valid_from"],
    )


def _current_entry_row(
    conn: sqlite3.Connection, exhibit_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT exhibit_id, status, materials, themes, valid_from
        FROM catalog_entries
        WHERE exhibit_id = ? AND valid_to IS NULL
        """,
        (exhibit_id,),
    ).fetchone()


def _open_new_version(conn: sqlite3.Connection) -> int:
    version = _next_version(conn)
    conn.execute(
        "INSERT INTO catalog_versions (version, created_at) VALUES (?, ?)",
        (version, utc_now_iso()),
    )
    return version


def _write_revision(
    conn: sqlite3.Connection,
    *,
    exhibit_id: str,
    status: str,
    materials: list[str],
    themes: list[str],
    version: int,
) -> None:
    # Close the previous revision exactly at the new version boundary, then
    # open the replacement there. Both statements share this write
    # transaction, so readers never observe an exhibit with no live row.
    conn.execute(
        """
        UPDATE catalog_entries SET valid_to = ?
        WHERE exhibit_id = ? AND valid_to IS NULL
        """,
        (version, exhibit_id),
    )
    conn.execute(
        """
        INSERT INTO catalog_entries
            (exhibit_id, status, materials, themes,
             valid_from, valid_to, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?)
        """,
        (
            exhibit_id,
            status,
            json.dumps(sorted(set(materials))),
            json.dumps(sorted(set(themes))),
            version,
            utc_now_iso(),
        ),
    )


def add_catalog_entry(
    conn: sqlite3.Connection, entry: CatalogEntryIn
) -> CatalogEditResult:
    """录入 a new exhibit. Must run inside a write transaction.

    Identity, labels and status are written as one revision at a fresh
    catalog version: there is no window in which a half-created entry is
    visible to a reader.
    """
    if _current_entry_row(conn, entry.exhibit_id) is not None:
        raise CatalogConflict(
            f"展品 {entry.exhibit_id!r} 已存在，请使用更正接口"
        )

    version = _open_new_version(conn)
    _write_revision(
        conn,
        exhibit_id=entry.exhibit_id,
        status=entry.status,
        materials=entry.materials,
        themes=entry.themes,
        version=version,
    )
    row = _current_entry_row(conn, entry.exhibit_id)
    assert row is not None
    return CatalogEditResult(
        catalog_version=version, changed=True, entry=_entry_from_row(row)
    )


def update_catalog_entry(
    conn: sqlite3.Connection, exhibit_id: str, patch: CatalogEntryPatch
) -> CatalogEditResult:
    """更正 labels / status, or 下架. Must run inside a write transaction.

    Only the changed exhibit is rewritten. A failed edit rolls the whole
    transaction back, so the previous catalog version — and every estimate
    pinned to it — remains exactly as before.
    """
    current = _current_entry_row(conn, exhibit_id)
    if current is None:
        raise CatalogEntryNotFound(f"现行目录中没有展品 {exhibit_id!r}")

    materials = (
        patch.materials
        if patch.materials is not None
        else json.loads(current["materials"])
    )
    themes = (
        patch.themes if patch.themes is not None else json.loads(current["themes"])
    )
    status = patch.status if patch.status is not None else current["status"]

    if (
        status == current["status"]
        and materials == json.loads(current["materials"])
        and themes == json.loads(current["themes"])
    ):
        # Idempotent no-op (e.g. taking an offline exhibit offline again):
        # no version bump, no catalog rewrite.
        return CatalogEditResult(
            catalog_version=_current_version(conn),
            changed=False,
            entry=_entry_from_row(current),
        )

    version = _open_new_version(conn)
    _write_revision(
        conn,
        exhibit_id=exhibit_id,
        status=status,
        materials=materials,
        themes=themes,
        version=version,
    )
    row = _current_entry_row(conn, exhibit_id)
    assert row is not None
    return CatalogEditResult(
        catalog_version=version, changed=True, entry=_entry_from_row(row)
    )


def _matches(
    entry: CatalogEntry, materials: set[str], themes: set[str]
) -> bool:
    """Same-category tags are OR-ed; the two categories are AND-ed.

    An empty category imposes no restriction.
    """
    if materials and not materials.intersection(entry.materials):
        return False
    if themes and not themes.intersection(entry.themes):
        return False
    return True


def _entries_at_version(
    conn: sqlite3.Connection, version: int
) -> list[CatalogEntry]:
    """All exhibits as of ``version``: the state after that edit.

    Rows at later revisions of the same exhibit carry a later valid_from and
    are excluded by the window; the open-ended row for an unchanged exhibit
    is included. Result is ordered by exhibit id.
    """
    rows = conn.execute(
        """
        SELECT exhibit_id, status, materials, themes, valid_from
        FROM catalog_entries
        WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY exhibit_id
        """,
        (version, version),
    ).fetchall()
    return [_entry_from_row(row) for row in rows]


def filter_catalog(
    conn: sqlite3.Connection, request: CatalogFilterRequest
) -> CatalogFilterResult:
    """Return matching exhibits + per-tag toggle counts at one version.

    Everything is derived from a single scan of the catalog rows for the
    pinned version: per-tag counts are accumulated in the same pass, so the
    number of tags never causes a rescan, and no catalog copy is written.
    The version lookup and the scan share one read snapshot, so an estimate
    computed while another request edits or delists an exhibit still
    reflects the submitted version exactly.
    """
    conn.execute("BEGIN DEFERRED")
    try:
        version = _resolve_version(conn, request.catalog_version)
        entries = _entries_at_version(conn, version)
        current_version = _current_version(conn)
        conn.rollback()  # read-only snapshot; nothing to commit
    except Exception:
        conn.rollback()
        raise

    # Only online exhibits can be selected for the exhibition; a delisted
    # exhibit never matches, no matter which labels it still carries.
    candidates = [entry for entry in entries if entry.status == "online"]
    selected_materials = set(request.materials)
    selected_themes = set(request.themes)
    unrestricted_materials = not selected_materials
    unrestricted_themes = not selected_themes

    matched = [
        entry
        for entry in candidates
        if _matches(entry, selected_materials, selected_themes)
    ]

    # Toggle estimates in a single pass over the candidates.
    #
    # Let M/T be the selected tag sets and G the number of exhibits matching
    # both current filters (== len(matched)). If M is empty every exhibit
    # passes the material filter; symmetric for T. Deselecting one tag only
    # drops exhibits for which it was the *sole* material hit (or lifts the
    # filter entirely when it was the only selection), and selecting a new
    # tag only adds exhibits that currently fail the category but carry it.
    # Per-exhibit tallies are accumulated here, so the number of tags never
    # triggers a rescan and no catalog copy is ever written.
    theme_pass_count = 0          # H_t: exhibits passing the theme filter
    material_pass_count = 0       # H_m: exhibits passing the material filter
    sole_material_hits: dict[str, int] = {}   # S: sole material hit == t
    sole_theme_hits: dict[str, int] = {}      # S: sole theme hit == s
    missing_material_hits: dict[str, int] = {}  # U: fails M, carries t
    missing_theme_hits: dict[str, int] = {}     # U: fails T, carries s

    for entry in candidates:
        entry_materials = set(entry.materials)
        entry_themes = set(entry.themes)
        material_hits = entry_materials & selected_materials
        theme_hits = entry_themes & selected_themes
        theme_ok = unrestricted_themes or bool(theme_hits)
        material_ok = unrestricted_materials or bool(material_hits)

        if theme_ok:
            theme_pass_count += 1
            if len(material_hits) == 1:
                (tag,) = material_hits
                sole_material_hits[tag] = sole_material_hits.get(tag, 0) + 1
            elif not material_hits:
                for tag in entry_materials - selected_materials:
                    missing_material_hits[tag] = (
                        missing_material_hits.get(tag, 0) + 1
                    )
        if material_ok:
            material_pass_count += 1
            if len(theme_hits) == 1:
                (tag,) = theme_hits
                sole_theme_hits[tag] = sole_theme_hits.get(tag, 0) + 1
            elif not theme_hits:
                for tag in entry_themes - selected_themes:
                    missing_theme_hits[tag] = (
                        missing_theme_hits.get(tag, 0) + 1
                    )

    matched_count = len(matched)

    def _toggle_count(
        tag: str,
        selected: set[str],
        sole_hits: dict[str, int],
        missing_hits: dict[str, int],
        pass_count: int,
    ) -> int:
        if tag in selected:
            # Deselecting the only selected tag lifts that filter entirely;
            # otherwise only exhibits for which it was the sole hit drop out.
            if len(selected) == 1:
                return pass_count
            return matched_count - sole_hits.get(tag, 0)
        # Selecting the first tag of a category applies that filter: only
        # exhibits already passing the other category and carrying the tag
        # remain. Adding to a non-empty selection keeps every current match
        # and adds exhibits that failed the category but carry this tag.
        if not selected:
            return missing_hits.get(tag, 0)
        return matched_count + missing_hits.get(tag, 0)

    # Offer every tag of this version, selected or not, so each single
    # toggle can be previewed. Filter tags absent from the version still
    # appear (with a zero estimate) and their deselection is computed above.
    material_tags: set[str] = set(selected_materials)
    theme_tags: set[str] = set(selected_themes)
    for entry in candidates:
        material_tags.update(entry.materials)
        theme_tags.update(entry.themes)

    material_facets = [
        FacetCount(
            tag=tag,
            selected=tag in selected_materials,
            count_if_toggled=_toggle_count(
                tag, selected_materials, sole_material_hits,
                missing_material_hits, theme_pass_count,
            ),
        )
        for tag in sorted(material_tags)
    ]
    theme_facets = [
        FacetCount(
            tag=tag,
            selected=tag in selected_themes,
            count_if_toggled=_toggle_count(
                tag, selected_themes, sole_theme_hits,
                missing_theme_hits, material_pass_count,
            ),
        )
        for tag in sorted(theme_tags)
    ]

    return CatalogFilterResult(
        catalog_version=version,
        is_current_version=version == current_version,
        matched_count=matched_count,
        matched_exhibits=matched,
        material_facets=material_facets,
        theme_facets=theme_facets,
    )


def withdraw_consent(conn: sqlite3.Connection, visitor_id: str) -> WithdrawResult:
    """Record withdrawal and delete the visitor's events atomically.

    Must be called inside a write transaction. The withdrawal marker
    persists, so any late upload or replayed old event is rejected
    afterwards and the path can never reappear.
    """
    already = is_withdrawn(conn, visitor_id)
    deleted = conn.execute(
        "DELETE FROM events WHERE visitor_id = ?", (visitor_id,)
    ).rowcount
    conn.execute(
        """
        INSERT INTO withdrawals (visitor_id, withdrawn_at)
        VALUES (?, ?)
        ON CONFLICT(visitor_id) DO UPDATE SET withdrawn_at = withdrawn_at
        """,
        (visitor_id, utc_now_iso()),
    )
    return WithdrawResult(
        visitor_id=visitor_id,
        consent_withdrawn=True,
        already_withdrawn=already,
        events_deleted=deleted,
    )
