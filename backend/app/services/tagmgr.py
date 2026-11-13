"""标签管理业务规则：状态流转、字段校验、版本发布与分群引用反查。"""
from __future__ import annotations

from typing import Any

from app.services.segmentation_service import SegmentationService, segmentation_service
from app.store import store

MODULE = "tagmgr"
REQUIRED_FIELDS = ["标签编号", "标签名称", "标签分类"]
STATUS_ORDER = ["草稿", "逻辑评审中", "已发布", "已下线"]
ACTION_RULES = {"提交逻辑评审": "逻辑评审中", "发布标签": "已发布", "下线标签": "已下线"}
NEGATIVE_ACTIONS = ["下线标签"]


class TagmgrService:
    def __init__(self, segmentation: SegmentationService | None = None) -> None:
        self.segmentation = segmentation or segmentation_service

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("标签编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is not None:
            entry["引用分群"] = self.reference_segments(str(entry.get("标签编号")))
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["版本号"] = "v1"
        entry["覆盖比例"] = 0
        entry["对象偏移"] = 0
        entry["覆盖对象数"] = 0
        entry["最近计算时间"] = ""
        entry["引用分群"] = []
        entry["版本记录"] = []
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"标签定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于标签管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "发布标签":
            return self.publish(entry, {})
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"标签定义已{action}"

    def publish(self, entry: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """发布标签：生成新版本快照，触发受影响分群重算，并记录版本差异审计。"""
        try:
            result = self.segmentation.publish_tag(str(entry.get("标签编号")), values)
        except Exception as error:  # noqa: BLE001 - 业务失败按说明返回
            return None, str(error)
        plan = result["plan"]
        message = (
            f"标签已发布 {result['tag'].get('标签编号')}@{result['tag'].get('版本号')}，"
            f"重算 {len(plan['重算'])} 个分群、复用 {len(plan['复用'])} 个分群、"
            f"待校验 {len(plan['待校验'])} 个分群"
        )
        return result["tag"], message

    def reference_segments(self, tag_code: str) -> list[dict[str, Any]]:
        """反查引用了该标签的分群，带出分群当前结果与所属批次。"""
        segments = self.segmentation.referencing_segments(tag_code)
        result = []
        for segment in segments:
            result.append({
                "分群编号": segment.get("分群编号"),
                "分群名称": segment.get("分群名称"),
                "status": segment.get("status"),
                "标签版本": segment.get("标签版本"),
                "最终规模": segment.get("最终规模"),
                "覆盖对象数": segment.get("覆盖对象数"),
                "计算批次": segment.get("计算批次"),
                "最近计算时间": segment.get("最近计算时间"),
            })
        return result

    def audit(self, *, tag_code: str | None = None) -> list[dict[str, Any]]:
        return self.segmentation.audit_rows(tag_code=tag_code)
