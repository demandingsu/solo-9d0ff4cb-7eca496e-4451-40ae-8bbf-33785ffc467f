"""Tests for the catalog: intake, correction/delisting, facet filtering.

Required self-test matrix:
  1. intake records id + material/theme tags, one row per exhibit
  2. same-category multi-select is OR, across categories is AND, empty is
     unrestricted
  3. toggle estimates for every tag agree with a real re-query (multi-select)
  4. estimates pinned to a catalog version stay exact after later tag edits
     and delistings; new queries follow the current catalog
  5. a failed edit leaves the previous catalog (and pinned estimates) intact
  6. no per-tag rescan and no catalog copy: storage growth is one row per
     edit, and query cost is one catalog scan regardless of tag count
  7. delisting via DELETE (soft delist) and the GET form of the filter
"""
from __future__ import annotations

import os
import tempfile
import threading

from fastapi.testclient import TestClient

from app import config, crud
from app.db import connect, init_db, write_transaction
from app.main import app
from app.models import CatalogEntryIn, CatalogFilterRequest


def _entry(client: TestClient, exhibit_id: str, materials=None, themes=None,
           status="online", expect=201):
    resp = client.post("/catalog/entries", json={
        "exhibit_id": exhibit_id,
        "materials": materials or [],
        "themes": themes or [],
        "status": status,
    })
    assert resp.status_code == expect, resp.text
    return resp.json()


def _filter(client: TestClient, materials=None, themes=None,
            version=None) -> dict:
    body: dict = {
        "materials": materials or [],
        "themes": themes or [],
    }
    if version is not None:
        body["catalog_version"] = version
    resp = client.post("/catalog/filter", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _matched_ids(body: dict) -> list[str]:
    ids = [entry["exhibit_id"] for entry in body["matched_exhibits"]]
    assert len(ids) == len(set(ids)), "an exhibit was counted twice"
    assert ids == sorted(ids)
    assert body["matched_count"] == len(ids)
    return ids


def _facet(body: dict, category: str, tag: str) -> dict:
    for facet in body[category]:
        if facet["tag"] == tag:
            return facet
    raise AssertionError(f"facet {tag!r} missing from {category}")


def _expected_toggle_ids(client, category, toggled, submitted, version):
    """Re-query with one tag toggled, at the same pinned version."""
    materials = list(submitted.get("materials", []))
    themes = list(submitted.get("themes", []))
    bucket = materials if category == "material_facets" else themes
    if toggled in bucket:
        bucket.remove(toggled)
    else:
        bucket.append(toggled)
    return sorted(_matched_ids(_filter(client, materials, themes, version)))


def _assert_toggle_estimates(client, submitted: dict, version: int) -> None:
    body = _filter(client, submitted.get("materials"), submitted.get("themes"),
                   version)
    # Both categories must offer every tag present at this version.
    expected_material_tags = set()
    expected_theme_tags = set()
    all_online = _filter(client, [], [], version)["matched_exhibits"]
    for entry in all_online:
        expected_material_tags.update(entry["materials"])
        expected_theme_tags.update(entry["themes"])
    # A submitted tag absent from the version is still offered (estimate 0).
    expected_material_tags.update(submitted.get("materials", []))
    expected_theme_tags.update(submitted.get("themes", []))
    assert {f["tag"] for f in body["material_facets"]} == expected_material_tags
    assert {f["tag"] for f in body["theme_facets"]} == expected_theme_tags

    for category in ("material_facets", "theme_facets"):
        for facet in body[category]:
            expected = _expected_toggle_ids(
                client, category, facet["tag"], submitted, version
            )
            assert facet["count_if_toggled"] == len(expected), (
                f"{category}/{facet['tag']} @v{version}: "
                f"{facet['count_if_toggled']} != {len(expected)}"
            )


# ---------------------------------------------------------------------------
# 1. Intake
# ---------------------------------------------------------------------------
def test_intake_records_tags_and_bumps_version(client):
    first = _entry(client, "EX-1", ["青铜", "木"], ["古代文明"])
    assert first["catalog_version"] == 1 and first["changed"] is True
    assert first["entry"] == {
        "exhibit_id": "EX-1",
        "status": "online",
        "materials": ["木", "青铜"],  # normalized: sorted, unique
        "themes": ["古代文明"],
        "since_version": 1,
    }
    second = _entry(client, "EX-2", ["纸"], ["现代艺术", "现代艺术"])
    assert second["catalog_version"] == 2
    assert second["entry"]["themes"] == ["现代艺术"]

    body = _filter(client)
    assert body["catalog_version"] == 2 and body["is_current_version"] is True
    assert _matched_ids(body) == ["EX-1", "EX-2"]

    dup = client.post("/catalog/entries", json={
        "exhibit_id": "EX-1", "materials": [], "themes": [],
    })
    assert dup.status_code == 409


def test_intake_validation(client):
    blank = client.post("/catalog/entries", json={
        "exhibit_id": "EX-1", "materials": ["  "], "themes": [],
    })
    assert blank.status_code == 422
    extra = client.post("/catalog/entries", json={
        "exhibit_id": "EX-1", "materials": [], "themes": [], "bogus": 1,
    })
    assert extra.status_code == 422


# ---------------------------------------------------------------------------
# 2. OR within a category, AND across categories, empty unrestricted
# ---------------------------------------------------------------------------
def test_filter_semantics_or_within_and_across(client):
    _entry(client, "A", ["wood", "metal"], ["history", "nature"])
    _entry(client, "B", ["metal"], ["history"])
    _entry(client, "C", ["paper"], ["science"])

    assert _matched_ids(_filter(client)) == ["A", "B", "C"]
    # Same category: ANY match.
    assert _matched_ids(_filter(client, materials=["wood", "paper"])) == [
        "A", "C",
    ]
    # Across categories: BOTH must match (A has nature but not science).
    assert _matched_ids(
        _filter(client, materials=["wood"], themes=["science"])
    ) == []
    assert _matched_ids(
        _filter(client, materials=["wood"], themes=["nature", "science"])
    ) == ["A"]
    # An empty category is unrestricted.
    assert _matched_ids(_filter(client, themes=["science"])) == ["C"]


def test_offline_entries_are_never_counted(client):
    _entry(client, "A", ["wood"], ["history"])
    _entry(client, "B", ["wood"], ["history"])
    resp = client.patch("/catalog/entries/B", json={"status": "offline"})
    assert resp.status_code == 200

    body = _filter(client, materials=["wood"])
    assert _matched_ids(body) == ["A"]
    assert body["matched_count"] == 1
    # Deselecting the only material tag lifts the filter: still just A.
    facet = _facet(body, "material_facets", "wood")
    assert facet["count_if_toggled"] == 1


# ---------------------------------------------------------------------------
# 3. Toggle estimates, including multi-select in both categories
# ---------------------------------------------------------------------------
def test_toggle_estimates_match_real_requeries(client):
    data = {
        "A": (["wood", "metal"], ["history"]),
        "B": (["wood"], ["history", "nature"]),
        "C": (["paper", "metal"], ["nature"]),
        "D": (["glass"], ["science"]),
        "E": (["wood", "paper"], ["history", "science"]),
        "F": ([], ["science"]),
    }
    for exhibit_id, (materials, themes) in data.items():
        _entry(client, exhibit_id, materials, themes)
    version = _filter(client)["catalog_version"]

    submissions = [
        {},
        {"materials": ["wood"]},
        {"materials": ["wood", "metal"]},
        {"themes": ["history", "nature"]},
        {"materials": ["wood", "paper"], "themes": ["history", "science"]},
        {"materials": ["glass"], "themes": ["nature"]},
        {"materials": ["wood", "metal", "paper"], "themes": ["science"]},
    ]
    for submitted in submissions:
        _assert_toggle_estimates(client, submitted, version)


def test_toggle_estimates_with_duplicate_label_entries(client):
    # An exhibit with several matching tags must be counted once even when
    # toggling one of them leaves it in the result.
    _entry(client, "A", ["wood", "metal"], ["history"])
    _entry(client, "B", ["wood"], ["history"])
    body = _filter(client, materials=["wood", "metal"])
    assert _matched_ids(body) == ["A", "B"]
    # Toggling "wood" off leaves A (metal) and drops B.
    assert _facet(body, "material_facets", "wood")["count_if_toggled"] == 1
    # Toggling "metal" off leaves A via wood and B via wood.
    assert _facet(body, "material_facets", "metal")["count_if_toggled"] == 2
    assert _facet(body, "material_facets", "metal")["selected"] is True


# ---------------------------------------------------------------------------
# 4. Version pinning: estimates stay exact after edits and delistings
# ---------------------------------------------------------------------------
def test_pinned_estimate_survives_edits_and_delisting(client):
    _entry(client, "A", ["wood", "metal"], ["history"])
    _entry(client, "B", ["wood"], ["nature"])
    _entry(client, "C", ["paper"], ["history"])

    submitted = {"materials": ["wood"], "themes": ["history"]}
    pinned = _filter(client, **submitted)
    version = pinned["catalog_version"]
    assert _matched_ids(pinned) == ["A"]
    expected = {f["tag"]: f["count_if_toggled"]
                for f in pinned["material_facets"]}

    # Meanwhile: relabel A, add an exhibit, and delist B.
    client.patch("/catalog/entries/A", json={
        "materials": ["glass"], "themes": ["science"],
    })
    _entry(client, "D", ["wood"], ["history"])
    client.patch("/catalog/entries/B", json={"status": "offline"})

    # The pinned query is unchanged, and its estimates still agree with a
    # real re-query at that version.
    again = _filter(client, **submitted, version=version)
    assert again["catalog_version"] == version
    assert again["is_current_version"] is False
    assert _matched_ids(again) == ["A"]
    assert {f["tag"]: f["count_if_toggled"]
            for f in again["material_facets"]} == expected
    _assert_toggle_estimates(client, submitted, version)

    # A new query (no version) follows the current catalog.
    current = _filter(client, **submitted)
    assert current["is_current_version"] is True
    assert current["catalog_version"] > version
    assert _matched_ids(current) == ["D"]
    # A was relabelled and B delisted in the current catalog; only D still
    # carries "wood".
    assert _matched_ids(_filter(client, materials=["wood"])) == ["D"]
    _assert_toggle_estimates(client, submitted, current["catalog_version"])


def test_delisted_exhibit_counted_once_across_versions(client):
    _entry(client, "A", ["wood"], ["history"])
    _entry(client, "B", ["wood"], ["history"])
    v1 = _filter(client)["catalog_version"]
    client.patch("/catalog/entries/B", json={"materials": ["metal"]})
    v2 = _filter(client)["catalog_version"]
    assert v2 > v1

    # B appears once at each version; no duplicate row survives into one
    # result set, and the pre-edit version still shows the old labels.
    assert _matched_ids(_filter(client, materials=["wood"], version=v1)) == [
        "A", "B",
    ]
    assert _matched_ids(_filter(client, materials=["wood"], version=v2)) == [
        "A",
    ]
    assert _matched_ids(_filter(client, materials=["metal"], version=v2)) == [
        "B",
    ]
    # Historical version 1 is untouched by the edit.
    assert _facet(_filter(client, materials=["wood"], version=v1),
                  "material_facets", "wood")["count_if_toggled"] == 2


def test_delist_via_delete_and_get_filter(client):
    _entry(client, "A", ["wood"], ["history"])
    _entry(client, "B", ["wood"], ["history"])
    version = _filter(client)["catalog_version"]

    # GET filter is the same query for /docs convenience, repeatable params.
    resp = client.get("/catalog/filter", params=[
        ("materials", "wood"), ("themes", "history"),
    ])
    assert resp.status_code == 200
    assert _matched_ids(resp.json()) == ["A", "B"]
    assert resp.json()["catalog_version"] == version

    # DELETE = soft delist: no history is removed, old estimates stay valid.
    resp = client.delete("/catalog/entries/B")
    assert resp.status_code == 200
    assert resp.json()["changed"] is True
    assert resp.json()["entry"]["status"] == "offline"
    assert resp.json()["entry"]["materials"] == ["wood"]
    assert _matched_ids(_filter(client, materials=["wood"])) == ["A"]
    assert _matched_ids(_filter(client, materials=["wood"],
                                version=version)) == ["A", "B"]

    # Delisting an already offline exhibit is an idempotent no-op.
    again = client.delete("/catalog/entries/B").json()
    assert again["changed"] is False
    assert client.delete("/catalog/entries/NOPE").status_code == 404

    # And it can be brought back online.
    client.patch("/catalog/entries/B", json={"status": "online"})
    assert _matched_ids(_filter(client, materials=["wood"])) == ["A", "B"]


def test_get_filter_rejects_empty_tag(client):
    _entry(client, "A", ["wood"], [])
    assert client.get("/catalog/filter", params={"materials": " "}
                      ).status_code == 422


def test_unknown_version_is_rejected(client):
    _entry(client, "A", ["wood"], [])
    assert client.post("/catalog/filter", json={"catalog_version": 99}
                       ).status_code == 404


def test_empty_catalog_filters_to_zero(client):
    body = _filter(client)
    assert body["catalog_version"] == 0
    assert body["matched_count"] == 0
    assert body["matched_exhibits"] == []
    assert body["material_facets"] == [] and body["theme_facets"] == []


# ---------------------------------------------------------------------------
# 5. Edits: correction, idempotent no-op, failure rollback
# ---------------------------------------------------------------------------
def test_correction_only_changes_supplied_fields(client):
    _entry(client, "A", ["wood"], ["history"])
    resp = client.patch("/catalog/entries/A", json={"themes": ["science"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert body["entry"]["materials"] == ["wood"]      # untouched
    assert body["entry"]["themes"] == ["science"]      # replaced wholesale
    assert body["entry"]["status"] == "online"

    # Delisting keeps labels, so a later correction can restore it.
    off = client.patch("/catalog/entries/A", json={"status": "offline"}).json()
    assert off["entry"]["status"] == "offline"
    assert off["entry"]["materials"] == ["wood"]
    on = client.patch("/catalog/entries/A", json={"status": "online"}).json()
    assert _matched_ids(_filter(client, materials=["wood"])) == ["A"]


def test_noop_correction_does_not_create_version(client):
    _entry(client, "A", ["wood"], ["history"])
    before = _filter(client)["catalog_version"]
    resp = client.patch("/catalog/entries/A", json={
        "materials": ["wood"], "themes": ["history"], "status": "online",
    })
    assert resp.status_code == 200
    assert resp.json()["changed"] is False
    assert resp.json()["catalog_version"] == before
    assert _filter(client)["catalog_version"] == before


def test_failed_edit_preserves_catalog_and_pinned_estimates(client):
    _entry(client, "A", ["wood"], ["history"])
    _entry(client, "B", ["wood"], ["nature"])
    version = _filter(client)["catalog_version"]
    before = _filter(client, materials=["wood"])
    before_version = before["catalog_version"]

    # Unknown exhibit -> 404.
    assert client.patch("/catalog/entries/NOPE",
                        json={"status": "offline"}).status_code == 404
    # Invalid payload -> 422.
    assert client.patch("/catalog/entries/A",
                        json={"status": "missing"}).status_code == 422
    assert client.patch("/catalog/entries/A",
                        json={"materials": [""]}).status_code == 422
    # Empty patch -> 422.
    assert client.patch("/catalog/entries/A", json={}).status_code == 422

    # Nothing moved: same current version, same pinned answers.
    assert _filter(client)["catalog_version"] == before_version
    assert _filter(client, materials=["wood"]) == before
    assert _filter(client, materials=["wood"], version=version) == before


def test_update_of_unknown_entry_is_404(client):
    assert client.patch("/catalog/entries/missing",
                        json={"themes": ["x"]}).status_code == 404


# ---------------------------------------------------------------------------
# 6. Storage/scaling guarantees
# ---------------------------------------------------------------------------
def test_edits_store_one_row_per_revision_not_catalog_copies(client):
    for i in range(5):
        _entry(client, f"EX-{i}", ["wood"], ["history"])
    conn = connect(config.DB_PATH)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
        versions = conn.execute(
            "SELECT COUNT(*) FROM catalog_versions").fetchone()[0]
    finally:
        conn.close()
    assert (rows, versions) == (5, 5)

    # A single-field correction rewrites exactly one exhibit's revision.
    client.patch("/catalog/entries/EX-3", json={"status": "offline"})
    conn = connect(config.DB_PATH)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM catalog_entries").fetchone()[0]
        per_exhibit = conn.execute(
            "SELECT exhibit_id, COUNT(*) FROM catalog_entries "
            "GROUP BY exhibit_id ORDER BY exhibit_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == 6  # one new revision, not a 5-row catalog copy
    assert [(r[0], r[1]) for r in per_exhibit] == [
        ("EX-0", 1), ("EX-1", 1), ("EX-2", 1), ("EX-3", 2), ("EX-4", 1),
    ]


def test_concurrent_edit_while_filtering_never_mixes_versions(client):
    """A filter and an edit racing on the same SQLite file must each observe
    one committed catalog state: a result is either entirely before or
    entirely after the edit, and each response reports a self-consistent
    version (matched set re-queryable at exactly that version)."""
    _entry(client, "A", ["wood"], ["history"])
    _entry(client, "B", ["wood"], ["history"])
    _entry(client, "C", ["wood"], ["history"])
    version = _filter(client)["catalog_version"]
    errors: list[str] = []

    def editor(local: TestClient) -> None:
        try:
            for i in range(20):
                local.patch("/catalog/entries/A", json={
                    "status": "offline" if i % 2 == 0 else "online",
                })
        except Exception as exc:  # pragma: no cover
            errors.append(f"editor: {exc!r}")

    def reader(local: TestClient) -> None:
        try:
            for _ in range(50):
                body = _filter(local, materials=["wood"])
                ids = [e["exhibit_id"] for e in body["matched_exhibits"]]
                if len(ids) != len(set(ids)):
                    errors.append("duplicate exhibit in result")
                if not set(ids) <= {"A", "B", "C"}:
                    errors.append(f"unexpected ids {ids}")
                again = _filter(local, materials=["wood"],
                                version=body["catalog_version"])
                if [e["exhibit_id"] for e in again["matched_exhibits"]] != ids:
                    errors.append(
                        f"version {body['catalog_version']} not stable: "
                        f"{ids} vs {[e['exhibit_id'] for e in again['matched_exhibits']]}"
                    )
        except Exception as exc:  # pragma: no cover
            errors.append(f"reader: {exc!r}")

    threads = [
        threading.Thread(target=editor, args=(TestClient(app),)),
        threading.Thread(target=reader, args=(TestClient(app),)),
        threading.Thread(target=reader, args=(TestClient(app),)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert _filter(client, version=version)["matched_count"] == 3


def test_filter_uses_one_catalog_scan_regardless_of_tag_count():
    """Per-tag toggle counts come from the same single scan: one SELECT on
    the catalog table per query, no matter how many tags are involved."""
    statements: list[str] = []

    class RecordingConnection:
        """Delegates to the real connection, recording SQL statements."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            statements.append(sql)
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    tmp = tempfile.mkdtemp(prefix="catalog-scan-")
    db_path = os.path.join(tmp, "scan.db")
    init_db(db_path)
    conn = connect(db_path)
    try:
        tags = [f"tag{i:02d}" for i in range(30)]
        for i in range(20):
            with write_transaction(conn):
                crud.add_catalog_entry(conn, CatalogEntryIn(
                    exhibit_id=f"EX-{i}",
                    materials=tags[i % 30:] + tags[: i % 4],
                    themes=[tags[(i + 5) % 30]],
                ))

        request = CatalogFilterRequest(
            materials=tags[:3], themes=tags[10:12])
        result = crud.filter_catalog(RecordingConnection(conn), request)

        catalog_selects = [
            sql for sql in statements
            if "catalog_entries" in sql
            and sql.lstrip().upper().startswith("SELECT")
        ]
        assert len(catalog_selects) == 1, catalog_selects
        assert result.matched_count >= 0
        assert len(result.material_facets) == 30  # all tags offered
        # And every estimate was produced without additional catalog reads.
    finally:
        conn.close()
