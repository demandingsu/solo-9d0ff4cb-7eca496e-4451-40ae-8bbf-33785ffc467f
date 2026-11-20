"""FastAPI application: event ingestion, visit paths, withdrawal, catalog."""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import crud
from .db import get_connection, init_db, write_transaction
from .models import (
    CatalogEditResult,
    CatalogEntryIn,
    CatalogEntryPatch,
    CatalogFilterRequest,
    CatalogFilterResult,
    ErrorResponse,
    EventBatchIn,
    EventIn,
    IngestBatchResult,
    IngestResult,
    VisitPath,
    WithdrawRequest,
    WithdrawResult,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Ensure schema exists before serving the first request.
    init_db()
    yield


app = FastAPI(
    title="线上展馆 API：参观路径与目录筛选",
    description=(
        "从浏览器 open/close 事件还原观众实际参观路径（重传去重、乱序补报、"
        "缺关闭标记停留未知、数据同意撤回）；并提供带版本目录的展品录入、"
        "更正/下架与材质/主题标签筛选。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(crud.BadEventTime)
def _bad_event_time_handler(_request, exc: crud.BadEventTime) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": str(exc)},
    )


@app.exception_handler(crud.CatalogEntryNotFound)
def _catalog_not_found_handler(
    _request, exc: crud.CatalogEntryNotFound
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


@app.exception_handler(crud.CatalogVersionNotFound)
def _catalog_version_not_found_handler(
    _request, exc: crud.CatalogVersionNotFound
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": str(exc)},
    )


@app.exception_handler(crud.CatalogConflict)
def _catalog_conflict_handler(
    _request, exc: crud.CatalogConflict
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc)},
    )


@app.post(
    "/events",
    response_model=IngestResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="上报单个浏览事件（打开/关闭）",
)
def post_event(
    event: EventIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestResult:
    """同一 ``event_id`` 重传返回 duplicate，不重复计数。

    整个请求在一个 ``BEGIN IMMEDIATE`` 事务内：校验、撤回检查与插入要么
    全部生效，要么全部回滚，不会出现半份写入。
    """
    with write_transaction(conn):
        return crud.ingest_event(conn, event)


@app.post(
    "/events/batch",
    response_model=IngestBatchResult,
    status_code=status.HTTP_201_CREATED,
    responses={400: {"model": ErrorResponse}},
    summary="批量上报浏览事件（同一事务，原子提交）",
)
def post_events(
    batch: EventBatchIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> IngestBatchResult:
    """批量事件作为一个事务提交：任一事件非法则整批回滚。

    已撤回观众的事件记为 ``rejected_withdrawn``，其余事件仍可正常入库，
    二者在同一已提交状态上串行判定。
    """
    with write_transaction(conn):
        results = [crud.ingest_event(conn, event) for event in batch.events]
    accepted = sum(r.status == "stored" for r in results)
    duplicates = sum(r.status == "duplicate" for r in results)
    rejected = sum(r.status == "rejected_withdrawn" for r in results)
    return IngestBatchResult(
        accepted=accepted,
        duplicates=duplicates,
        rejected_withdrawn=rejected,
        results=results,
    )


@app.get(
    "/visitors/{visitor_id}/path",
    response_model=VisitPath,
    summary="查询观众参观路径（按打开时间排序）",
)
def get_path(
    visitor_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> VisitPath:
    """按 ``page_view_id`` 配对开关；缺关闭的展品 ``dwell_known=false``。

    路径在查询时从已提交事件实时推导，因此较早事件的补报会自动改变
    路径顺序与停留时长。撤回后返回空路径（``consent_withdrawn=true``）。
    """
    return crud.get_visitor_path(conn, visitor_id)


@app.post(
    "/visitors/{visitor_id}/withdraw",
    response_model=WithdrawResult,
    summary="撤回数据使用同意（删除已存事件，之后拒绝该观众的任何事件）",
)
def post_withdraw(
    visitor_id: str,
    payload: WithdrawRequest | None = None,
    conn: sqlite3.Connection = Depends(get_connection),
) -> WithdrawResult:
    """撤回标记与事件删除在同一事务内完成，重复撤回幂等。

    若请求带 body，其中的 visitor_id 必须与路径参数一致。
    """
    if payload is not None and payload.visitor_id != visitor_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="路径中的 visitor_id 与请求体不一致",
        )
    with write_transaction(conn):
        return crud.withdraw_consent(conn, visitor_id)


@app.get("/health", summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Catalog: exhibit intake, corrections/delisting, and facet filtering
# ---------------------------------------------------------------------------
@app.post(
    "/catalog/entries",
    response_model=CatalogEditResult,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse}},
    summary="录入展品（展品号、材质标签、主题标签、在展状态）",
)
def create_catalog_entry(
    entry: CatalogEntryIn,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CatalogEditResult:
    """录入一个新展品；展品号已存在时返回 409，请改用更正接口。

    条目与材质/主题标签在同一写事务内落成一条目录修订，并产生新的目录版本。
    """
    with write_transaction(conn):
        return crud.add_catalog_entry(conn, entry)


@app.patch(
    "/catalog/entries/{exhibit_id}",
    response_model=CatalogEditResult,
    responses={
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    summary="更正标签或在展状态（status=offline 即下架）",
)
def patch_catalog_entry(
    exhibit_id: str,
    patch: CatalogEntryPatch,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CatalogEditResult:
    """只改动被提交的字段；未提及的字段保持原值。

    与现状态完全相同的提交是幂等空操作（不产生新版本）。任何失败都会
    回滚整个事务，原目录（含按版本固定的预估）不受影响。
    """
    if not patch.has_changes():
        raise HTTPException(
            status_code=422,
            detail="请求未包含任何可更正的字段（materials / themes / status）",
        )
    with write_transaction(conn):
        return crud.update_catalog_entry(conn, exhibit_id, patch)


@app.delete(
    "/catalog/entries/{exhibit_id}",
    response_model=CatalogEditResult,
    responses={404: {"model": ErrorResponse}},
    summary="下架展品（软下架：保留标签与历史，可再设回 online）",
)
def delete_catalog_entry(
    exhibit_id: str,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CatalogEditResult:
    """下架 = ``status=offline``，不删除历史修订。

    这样按旧目录版本发出的预估仍然有效；已下架的展品不再计入任何命中。
    已下架时重复调用返回 ``changed=false``，不产生新版本。
    """
    with write_transaction(conn):
        return crud.update_catalog_entry(
            conn, exhibit_id, CatalogEntryPatch(status="offline")
        )


def _filter_request(
    materials: list[str], themes: list[str], catalog_version: int | None
) -> CatalogFilterRequest:
    try:
        return CatalogFilterRequest(
            materials=materials or [],
            themes=themes or [],
            catalog_version=catalog_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/catalog/filter",
    response_model=CatalogFilterResult,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="按材质/主题标签筛选展品（查询参数形式，便于在 /docs 直接试）",
)
def filter_catalog_get(
    materials: list[str] = Query(default=[], description="材质标签，可重复传"),
    themes: list[str] = Query(default=[], description="主题标签，可重复传"),
    catalog_version: int | None = Query(
        default=None, ge=0, description="固定目录版本；省略使用现行目录"
    ),
    conn: sqlite3.Connection = Depends(get_connection),
) -> CatalogFilterResult:
    """与 ``POST /catalog/filter`` 等价，语义见该接口说明。"""
    return crud.filter_catalog(
        conn, _filter_request(materials, themes, catalog_version)
    )


@app.post(
    "/catalog/filter",
    response_model=CatalogFilterResult,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="按材质/主题标签筛选展品，并给出逐个标签切换后的命中数",
)
def filter_catalog(
    request: CatalogFilterRequest,
    conn: sqlite3.Connection = Depends(get_connection),
) -> CatalogFilterResult:
    """同类标签多选命中任一项，两类都选须同时满足，空选的类不限。

    - 省略 ``catalog_version`` 使用现行目录；做切换预估时请沿用上一次
      响应里的 ``catalog_version``，这样即使期间有人改标签或下架，估计值
      也与该版本的实际命中一致。
    - ``matched_exhibits`` 每个展品只出现一次。
    - ``material_facets`` / ``theme_facets`` 中每一项都给出“单独切换该标签”
      后的命中件数（其余选择保持不变）。
    - 下架（``status=offline``）的展品不计入任何命中或件数。
    """
    return crud.filter_catalog(conn, request)
