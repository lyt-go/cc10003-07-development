"""分群分析业务规则：规则驱动重算、批次状态、规模校验与重算计划都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.segmentation_service import SegmentationService, segmentation_service
from app.store import store

MODULE = "segment"
REQUIRED_FIELDS = ["分群编号", "分群名称", "筛选规则"]
STATUS_ORDER = ["草稿", "规则校验中", "已生效", "已失效"]
ACTION_RULES = {"提交规则校验": "规则校验中", "生效分群": "已生效", "失效分群": "已失效"}
NEGATIVE_ACTIONS = []


class SegmentService:
    def __init__(self, segmentation: SegmentationService | None = None) -> None:
        self.segmentation = segmentation or segmentation_service

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        tag_code: str | None = None,
        batch: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """分群列表过滤：分群编号、状态、引用标签、所属批次可组合，分页前先定位。"""
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("分群编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if tag_code:
            rows = [row for row in rows if tag_code in self._referenced_tags(row)]
        if batch:
            rows = [row for row in rows if batch in str(row.get("计算批次", ""))]
        total = len(rows)
        returned = []
        for row in rows:
            item = dict(row)
            batch_code = str(row.get("计算批次") or "")
            item["所属批次"] = batch_code
            item["标签版本列表"] = [
                {"标签编号": code, "版本": version}
                for code, version in self._tag_versions(row).items()
            ]
            returned.append(item)
        start = max(page - 1, 0) * size
        return returned[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        item = dict(entry)
        item["所属批次"] = entry.get("计算批次")
        item["标签版本列表"] = [
            {"标签编号": code, "版本": version}
            for code, version in self._tag_versions(entry).items()
        ]
        item["引用标签"] = sorted(self._referenced_tags(entry))
        return item

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
        entry["最小规模"] = values.get("最小规模") or 0
        entry["最大规模"] = values.get("最大规模") or 100000
        entry["刷新周期"] = values.get("刷新周期") or "weekly"
        entry["规则版本"] = values.get("规则版本") or "r1"
        entry["规则定义"] = values.get("规则定义") or {"版本": entry["规则版本"], "条件": []}
        entry["最终规模"] = 0
        entry["覆盖对象数"] = 0
        entry["标签版本"] = ""
        entry["计算批次"] = ""
        entry["最近计算时间"] = ""
        entry["校验状态"] = "待校验"
        entry["校验说明"] = "尚未执行版本驱动重算"
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分群分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "生效分群" and entry.get("校验状态") == "待校验":
            return None, f"分群 {entry.get('分群编号')} 当前为待校验（{entry.get('校验说明') or '规模未通过'}），不能直接生效"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target == "已生效":
            # 分群生效后同步标签侧的引用反查，标签列表立刻能看到引用关系。
            self.segmentation.refresh_tag_references()
        return entry, f"人群分群已{action}"

    def refresh(self, entry_id: int, *, fail_at_clause: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """手动触发一次重算；规模越界时停待校验并保留旧结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"人群分群 {entry_id} 不存在或已归档"
        try:
            batch = self.segmentation.recalculate_segment(
                entry,
                trigger_tag=None,
                fail_at_clause=fail_at_clause,
            )
        except Exception as error:  # noqa: BLE001 - 并发冲突按说明返回
            return None, str(error)
        if batch.get("状态") == "失败":
            return batch, f"重算失败：{batch.get('校验说明')}，已保留检查点可续算"
        if batch.get("校验状态") != "通过":
            return batch, f"重算完成但规模待校验：{batch.get('校验说明')}，已保留旧结果"
        return batch, f"重算完成，批次 {batch.get('批次号')}，覆盖对象数 {batch.get('结果对象数')}"

    def retry(self, batch_code: str) -> tuple[dict[str, Any] | None, str]:
        try:
            batch = self.segmentation.retry_batch(batch_code)
        except Exception as error:  # noqa: BLE001 - 续算前置条件按说明返回
            return None, str(error)
        if batch.get("状态") == "失败":
            return None, f"续算仍失败：{batch.get('校验说明')}"
        return batch, f"批次 {batch_code} 续算完成，新批次 {batch.get('批次号')}"

    def locate(
        self,
        *,
        keyword: str,
        status: str | None = None,
        tag_code: str | None = None,
        batch: str | None = None,
        size: int = 20,
    ) -> dict[str, Any] | None:
        """在组合过滤结果里定位一条分群，返回它所在页码与所属批次。"""
        items, _total = self.list_entries(
            keyword=keyword, status=status, tag_code=tag_code, batch=batch, page=1, size=10000
        )
        for index, item in enumerate(items):
            if item.get("分群编号") == keyword:
                return {
                    "entry": item,
                    "page": index // size + 1,
                    "index": index,
                    "size": size,
                }
        return None

    def batches(self, *, segment_code: str | None = None) -> list[dict[str, Any]]:
        return self.segmentation.list_batches(segment_code=segment_code)

    def recalculate_plan(self, tag_code: str) -> dict[str, Any] | None:
        tag = self.segmentation.find_tag(tag_code)
        if tag is None:
            return None
        return self.segmentation.build_plan(tag)

    def _referenced_tags(self, segment: dict[str, Any]) -> set[str]:
        return set(self._tag_versions(segment))

    def _tag_versions(self, segment: dict[str, Any]) -> dict[str, str]:
        """展示用标签版本：以分群结果记录的版本为准，规则里固定版本的标签也带上。"""
        versions: dict[str, str] = {}
        for item in str(segment.get("标签版本") or "").split(","):
            code, _, version = item.strip().partition("@")
            if code and version:
                versions[code] = version
        for condition in (segment.get("规则定义") or {}).get("条件") or []:
            code = condition.get("标签编号")
            if not code:
                continue
            code = str(code)
            versions.setdefault(code, str(condition.get("版本") or ""))
        return versions
