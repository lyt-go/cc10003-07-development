"""分群分析接口：批次驱动重算、规模校验、失败续算与列表定位。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.segment import SegmentService

router = APIRouter(prefix="/api/segment", tags=["分群分析"])

service = SegmentService()

LIST_FIELDS = [
    "分群编号", "分群名称", "筛选规则", "最小规模", "最大规模", "刷新周期",
    "最近计算时间", "最终规模", "覆盖对象数", "标签版本", "计算批次", "校验状态",
]
STATUSES = ["草稿", "规则校验中", "已生效", "已失效"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按分群编号检索"),
    status: str | None = Query(default=None, description="草稿、规则校验中、已生效、已失效"),
    tag: str | None = Query(default=None, description="按引用的标签编号过滤"),
    batch: str | None = Query(default=None, description="按所属计算批次过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按分群编号、状态、引用标签与批次过滤；分页只影响返回窗口，不影响定位结果。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, tag_code=tag, batch=batch, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches", response_model=dict)
def list_batches(
    segment: str | None = Query(default=None, description="按分群编号过滤批次"),
) -> dict[str, Any]:
    """批次清单：按开始时间倒序，接口返回每批的规模、校验状态和检查点对象数。"""
    rows = service.batches(segment_code=segment)
    return {"total": len(rows), "items": rows}


@router.get("/refresh-plan", response_model=dict)
def refresh_plan(tag: str = Query(description="标签编号，例如 TAGM-0003")) -> dict[str, Any]:
    """查看某标签当前版本会触发哪些分群重算、哪些继续复用。"""
    plan = service.recalculate_plan(tag)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"标签 {tag} 不存在或已归档")
    return plan


@router.get("/locate", response_model=dict)
def locate_entry(
    keyword: str = Query(description="分群编号，例如 SEGM-0002"),
    status: str | None = Query(default=None),
    tag: str | None = Query(default=None, description="按引用的标签编号过滤"),
    batch: str | None = Query(default=None, description="按所属批次过滤"),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """在过滤条件与分页结果中定位同一条分群，返回页码和所属批次。"""
    located = service.locate(keyword=keyword, status=status, tag_code=tag, batch=batch, size=size)
    if located is None:
        raise HTTPException(status_code=404, detail=f"在当前过滤条件下未定位到分群 {keyword}")
    return located


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分群分析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "segment", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条人群分群明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"人群分群 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条人群分群，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="人群分群已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交规则校验、生效分群、失效分群；待校验且规模越界的分群不允许直接生效。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/refresh", response_model=ActionResult)
def refresh_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """手动触发一次规模重算；越界时停在待校验并保留旧结果。"""
    fail_at = None
    if payload is not None and payload.values.get("fail_at_clause") is not None:
        fail_at = int(payload.values["fail_at_clause"])
    batch, message = service.refresh(entry_id, fail_at_clause=fail_at)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=batch.get("校验状态") == "通过", message=message, entry=batch)


@router.post("/batches/{batch_code}/retry", response_model=ActionResult)
def retry_batch(batch_code: str) -> ActionResult:
    """失败批次续算：从失败条件与检查点继续，按对象去重不重复计入。"""
    batch, message = service.retry(batch_code)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)
