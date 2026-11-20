"""Pydantic request/response models."""
from __future__ import annotations

from enum import Enum
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventType(str, Enum):
    OPEN = "open"
    CLOSE = "close"


class EventIn(BaseModel):
    """One browser open/close event reported by a client."""

    # Extra fields are rejected so a malformed report fails loudly instead
    # of being silently dropped.
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，重传使用同一事件号")
    visitor_id: str = Field(..., min_length=1, description="匿名观众号")
    page_view_id: str = Field(..., min_length=1, description="页面浏览号，同一浏览的开/关共享")
    exhibit_id: str = Field(..., min_length=1, description="展品号")
    event_type: EventType = Field(..., description="open=打开页面，close=关闭页面")
    # UTC ISO-8601, e.g. 2026-11-18T08:30:00Z or with +00:00 / fractional seconds.
    event_time: str = Field(..., min_length=1, description="事件 UTC 时间（ISO-8601）")


class EventBatchIn(BaseModel):
    events: list[EventIn] = Field(..., min_length=1)


class StoredEvent(BaseModel):
    event_id: str
    visitor_id: str
    page_view_id: str
    exhibit_id: str
    event_type: EventType
    event_time: str
    received_at: str


class IngestResult(BaseModel):
    event_id: str
    # stored=true on first acceptance; duplicate=false when the same event_id
    # was already committed (idempotent replay).
    status: Literal["stored", "duplicate", "rejected_withdrawn"]


class IngestBatchResult(BaseModel):
    accepted: int
    duplicates: int
    rejected_withdrawn: int
    results: list[IngestResult]


class Visit(BaseModel):
    """One stop on a visitor's reconstructed path."""

    page_view_id: str
    exhibit_id: str
    opened_at: str
    closed_at: str | None
    # Seconds between open and close; None when no close has been reported
    # ("停留未知"). Negative means a close was reported earlier than its open.
    dwell_seconds: float | None
    dwell_known: bool
    sequence: int


class VisitPath(BaseModel):
    visitor_id: str
    consent_withdrawn: bool
    # After withdrawal the path is empty and never recovers, even if old
    # events are replayed afterwards.
    visits: list[Visit]
    total_visits: int


class WithdrawRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_id: str = Field(..., min_length=1)


class WithdrawResult(BaseModel):
    visitor_id: str
    consent_withdrawn: bool
    # True for the first withdrawal, False on repeated withdrawals (idempotent).
    already_withdrawn: bool
    events_deleted: int


# ---------------------------------------------------------------------------
# Catalog (exhibit selection for the online exhibition)
# ---------------------------------------------------------------------------
def _normalize_tags(values: Iterable[str]) -> list[str]:
    """Trim, reject blanks, de-duplicate, and sort tags deterministically.

    Tags are exact, case-sensitive labels. Keeping them sorted and unique
    means a corrected entry with the same labels produces the same facets.
    """
    normalized: list[str] = []
    for raw in values:
        tag = raw.strip()
        if not tag:
            raise ValueError("标签不能为空字符串")
        if len(tag) > 64:
            raise ValueError(f"标签过长（最多 64 字符）: {tag!r}")
        normalized.append(tag)
    return sorted(set(normalized))


class CatalogEntryIn(BaseModel):
    """A new catalog entry (录入). ``exhibit_id`` is the identity."""

    model_config = ConfigDict(extra="forbid")

    exhibit_id: str = Field(..., min_length=1, max_length=128, description="展品号")
    materials: list[str] = Field(default_factory=list, description="材质标签")
    themes: list[str] = Field(default_factory=list, description="主题标签")
    status: Literal["online", "offline"] = Field(
        default="online", description="online=在展，offline=已下架"
    )

    @field_validator("exhibit_id")
    @classmethod
    def _clean_exhibit_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("exhibit_id 不能为空")
        return value

    @field_validator("materials", "themes")
    @classmethod
    def _clean_tags(cls, values: list[str]) -> list[str]:
        return _normalize_tags(values)


class CatalogEntryPatch(BaseModel):
    """A correction (更正). Only supplied fields change."""

    model_config = ConfigDict(extra="forbid")

    materials: list[str] | None = Field(default=None, description="材质标签（整体替换）")
    themes: list[str] | None = Field(default=None, description="主题标签（整体替换）")
    status: Literal["online", "offline"] | None = Field(
        default=None, description="online=在展，offline=已下架"
    )

    @field_validator("materials", "themes")
    @classmethod
    def _clean_tags(cls, values: list[str] | None) -> list[str] | None:
        return None if values is None else _normalize_tags(values)

    def has_changes(self) -> bool:
        return any(
            value is not None
            for value in (self.materials, self.themes, self.status)
        )


class CatalogEntry(BaseModel):
    exhibit_id: str
    status: Literal["online", "offline"]
    materials: list[str]
    themes: list[str]
    # Catalog version at which this revision became visible.
    since_version: int


class CatalogEditResult(BaseModel):
    catalog_version: int
    # False when the request was a no-op (e.g. taking an offline exhibit
    # offline again): the catalog and its version stay untouched.
    changed: bool
    entry: CatalogEntry


class CatalogFilterRequest(BaseModel):
    """Facet filter. Same-category tags are OR-ed, the two categories AND-ed,
    an empty category is unrestricted."""

    model_config = ConfigDict(extra="forbid")

    materials: list[str] = Field(default_factory=list, description="材质标签（任一命中即可）")
    themes: list[str] = Field(default_factory=list, description="主题标签（任一命中即可）")
    catalog_version: int | None = Field(
        default=None,
        ge=0,
        description=(
            "固定目录版本；省略则使用现行目录。切换标签做预估时请沿用上一次"
            "响应中的 catalog_version，保证估计与命中一致。"
        ),
    )

    @field_validator("materials", "themes")
    @classmethod
    def _clean_tags(cls, values: list[str]) -> list[str]:
        return _normalize_tags(values)


class FacetCount(BaseModel):
    tag: str
    selected: bool
    # Number of distinct exhibits that would match if this one tag were
    # toggled and every other selection kept as submitted.
    count_if_toggled: int


class CatalogFilterResult(BaseModel):
    catalog_version: int
    is_current_version: bool
    matched_count: int
    # Distinct exhibits (one row per exhibit id), sorted by exhibit id.
    matched_exhibits: list[CatalogEntry]
    material_facets: list[FacetCount]
    theme_facets: list[FacetCount]


class ErrorResponse(BaseModel):
    detail: str
