"""标签管理接口：标签定义、版本发布、引用分群反查与版本差异审计。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.tagmgr import TagmgrService

router = APIRouter(prefix="/api/tagmgr", tags=["标签管理"])

service = TagmgrService()

LIST_FIELDS = [
    "标签编号", "标签名称", "标签分类", "计算逻辑", "值类型", "刷新周期",
    "覆盖对象数", "最近计算时间", "版本号", "引用分群",
]
STATUSES = ["草稿", "逻辑评审中", "已发布", "已下线"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按标签编号检索"),
    status: str | None = Query(default=None, description="草稿、逻辑评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按标签编号与状态过滤；返回每条标签当前版本和引用它的分群。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    for item in items:
        item["引用分群"] = service.reference_segments(str(item.get("标签编号")))
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/audit", response_model=dict)
def list_audit(tag: str | None = Query(default=None, description="按标签编号过滤审计记录")) -> dict[str, Any]:
    """版本差异审计：谁从哪个版本发布到哪个版本，影响了哪些分群和重算批次。"""
    rows = service.audit(tag_code=tag)
    return {"total": len(rows), "items": rows}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出标签管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "tagmgr", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条标签定义明细，包含版本记录和反查到的引用分群。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"标签定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条标签定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="标签定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交逻辑评审、发布标签、下线标签；发布时会同步写入版本快照与审计。"""
    action = str(payload.values.get("action") or "").strip()
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"标签定义 {entry_id} 不存在或已归档")
    if action == "发布标签":
        published, message = service.publish(entry, payload.values)
        if published is None:
            return ActionResult(ok=False, message=message)
        return ActionResult(ok=True, message=message, entry=service.get_entry(entry_id))
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_entry(entry_id))


@router.post("/{entry_id}/publish", response_model=ActionResult)
def publish_entry(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """显式发布新版本：可指定版本号、覆盖比例、对象偏移与发布时间。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=f"标签定义 {entry_id} 不存在或已归档")
    values = payload.values if payload is not None else {}
    published, message = service.publish(entry, values)
    if published is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=service.get_entry(entry_id))
