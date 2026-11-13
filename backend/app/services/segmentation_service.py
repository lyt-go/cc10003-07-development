"""标签版本驱动的分群规模重算。

业务约束都收在这个服务里：

* 标签发布新版本时才找出引用它的分群重算；未变化的标签继续复用已有结果；
* 每个分群同一时刻最多一个刷新能写入结果（分群级互斥）；
* 失败批次保留检查点，续算从失败条件继续，按对象去重不重复计入；
* 重算规模越界时保持「待校验」并保留旧结果，不允许把错误规模写成生效规模；
* 分群生效后反写标签的引用分群；每次发布都记录版本差异审计。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.services.segmentation import (
    RuleConditionError,
    check_scale,
    evaluate_conditions,
    tag_object_ids,
    version_snapshot,
)
from app.store import store

SEGMENT_MODULE = "segment"
TAG_MODULE = "tagmgr"
BATCH_MODULE = "segment_batch"
AUDIT_MODULE = "tag_audit"

PENDING_STATUS = "规则校验中"
RESULT_STATUS = "已生效"

# 分群级互斥：同一分群同一时刻只允许一个刷新写入结果。
_SEGMENT_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# 标签发布串行化：避免两个版本同时发布时各自算出不同的引用计划。
_PUBLISH_LOCK = threading.RLock()

# 每个标签最多保留多少个版本快照，避免内存无限增长。
MAX_TAG_VERSIONS = 50


class TagPublishError(RuntimeError):
    """标签发布前置条件不满足。"""


def _segment_lock(segment_code: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _SEGMENT_LOCKS.setdefault(segment_code, threading.Lock())


def _parse_version(version: str | None) -> int:
    text = str(version or "").strip().lower().lstrip("v")
    try:
        return int(text)
    except ValueError:
        return 0


def _next_version(tag: dict[str, Any]) -> str:
    versions = [record.get("版本") for record in tag.get("版本记录") or []]
    versions.append(tag.get("版本号"))
    latest = max((_parse_version(item) for item in versions), default=0)
    return f"v{latest + 1}"


def _next_sequence(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _batch_code(rows: list[dict[str, Any]], date_key: str) -> str:
    """BATCH-SEG-YYYYMMDD-A：同一天多个批次按 A、B、C 递增。"""
    prefix = f"BATCH-SEG-{date_key}-"
    used = {str(row.get("批次号")) for row in rows if str(row.get("批次号") or "").startswith(prefix)}
    index = 0
    while True:
        candidate = f"{prefix}{chr(ord('A') + index)}"
        if candidate not in used:
            return candidate
        index += 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SegmentationService:
    def __init__(self) -> None:
        # 失败批次的检查点对象集合：续算时从这里接着筛，按集合去重。
        self._checkpoints: dict[str, set[int]] = {}

    # ---------- 查询 ----------

    def tags(self) -> list[dict[str, Any]]:
        return store.rows(TAG_MODULE)

    def find_tag(self, tag_code: str) -> dict[str, Any] | None:
        for tag in self.tags():
            if str(tag.get("标签编号")) == tag_code:
                return tag
        return None

    def segments(self) -> list[dict[str, Any]]:
        return store.rows(SEGMENT_MODULE)

    def find_segment(self, segment_code: str) -> dict[str, Any] | None:
        for segment in self.segments():
            if str(segment.get("分群编号")) == segment_code:
                return segment
        return None

    def list_batches(self, *, segment_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        if segment_code:
            rows = [row for row in rows if row.get("分群编号") == segment_code]
        return sorted(
            rows,
            key=lambda row: (str(row.get("开始时间") or ""), int(row.get("id", 0))),
            reverse=True,
        )

    def latest_batch(self, segment_code: str) -> dict[str, Any] | None:
        batches = self.list_batches(segment_code=segment_code)
        return batches[0] if batches else None

    def referencing_segments(self, tag_code: str) -> list[dict[str, Any]]:
        """反查引用了某个标签的分群；只认规则里真实出现的标签编号。"""
        result = []
        for segment in self.segments():
            conditions = (segment.get("规则定义") or {}).get("条件") or []
            codes = {str(item.get("标签编号")) for item in conditions if item.get("标签编号")}
            if tag_code in codes:
                result.append(segment)
        return result

    def refresh_tag_references(self) -> None:
        with store.lock:
            for tag in self.tags():
                code = str(tag.get("标签编号"))
                tag["引用分群"] = sorted(
                    str(segment.get("分群编号")) for segment in self.referencing_segments(code)
                )

    def audit_rows(self, *, tag_code: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(AUDIT_MODULE)
        if tag_code:
            rows = [row for row in rows if row.get("标签编号") == tag_code]
        return sorted(rows, key=lambda row: str(row.get("发布时间") or ""), reverse=True)

    # ---------- 标签发布 ----------

    def publish_tag(self, tag_code: str, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """把标签发布成新版本：写入版本快照、审计版本差异，并触发受影响分群重算。"""
        with _PUBLISH_LOCK:
            return self._publish_tag(tag_code, values or {})

    def _publish_tag(self, tag_code: str, values: dict[str, Any]) -> dict[str, Any]:
        with store.lock:
            tag = self.find_tag(tag_code)
            if tag is None:
                raise TagPublishError(f"标签 {tag_code} 不存在或已归档")
            previous_version = str(tag.get("版本号") or "")
            if not previous_version:
                raise TagPublishError(f"标签 {tag_code} 缺少当前版本号，无法发布新版本")
            if tag.get("status") == "已下线":
                raise TagPublishError(f"标签 {tag_code} 已下线，不能再发布新版本")
            new_version = str(values.get("版本") or _next_version(tag))

            previous = version_snapshot(tag, previous_version)
            coverage = values.get("覆盖比例")
            if coverage is None:
                coverage = previous.get("覆盖比例")
            offset = values.get("对象偏移")
            if offset is None:
                offset = previous.get("对象偏移")
            try:
                coverage = max(0, min(int(coverage or 0), 100))
                offset = int(offset or 0)
            except (TypeError, ValueError):
                raise TagPublishError("覆盖比例与对象偏移必须是整数") from None
            logic = values.get("计算逻辑") or tag.get("计算逻辑") or ""

            cohort = tag_object_ids({"覆盖比例": coverage, "对象偏移": offset})
            batch_code = str(values.get("发布批次") or "").strip()
            if not batch_code:
                publish_date = str(values.get("发布日期") or values.get("发布时间") or "")[:10]
                batch_code = f"BATCH-TAG-{publish_date.replace('-', '') or 'UNKNOWN'}"
            snapshot = {
                "版本": new_version,
                "发布批次": batch_code,
                "发布时间": values.get("发布时间") or _utc_now(),
                "覆盖比例": coverage,
                "对象偏移": offset,
                "计算逻辑": logic,
                "覆盖对象数": len(cohort),
            }
            records = list(tag.get("版本记录") or [])
            records = [record for record in records if str(record.get("版本")) != new_version]
            records.append(snapshot)
            records = records[-MAX_TAG_VERSIONS:]
            tag["版本记录"] = records
            tag["版本号"] = new_version
            tag["覆盖比例"] = coverage
            tag["对象偏移"] = offset
            tag["计算逻辑"] = logic
            tag["覆盖对象数"] = len(cohort)
            tag["最近计算时间"] = str(snapshot["发布时间"])[:10]

            reference_rows = self.referencing_segments(tag_code)
            plan = self.build_plan(tag)
            publish_date = str(values.get("重算日期") or snapshot["发布时间"])[:10].replace("-", "")
            previous_snapshot = previous

        # 重算要拿分群级锁，不能在持有 store.lock 时做，否则和手动刷新互等。
        batch_ids: list[str] = []
        updated_segments: list[dict[str, Any]] = []
        pending_batches: list[str] = []
        skipped: list[dict[str, Any]] = []
        for item in plan["recalculate"]:
            segment = self.find_segment(item["分群编号"])
            if segment is None:
                continue
            try:
                batch = self.recalculate_segment(
                    segment,
                    trigger_tag=tag_code,
                    date_key=publish_date,
                )
            except TagPublishError as error:
                # 该分群正在被另一路径刷新：这一份更新不生效，留给正在执行的那次写入。
                skipped.append({"分群编号": item["分群编号"], "原因": str(error)})
                continue
            batch_ids.append(batch["批次号"])
            if batch.get("校验状态") != "通过":
                pending_batches.append(batch["批次号"])
            updated_segments.append(segment)

        with store.lock:
            self.refresh_tag_references()
            difference = self._version_diff(previous_snapshot, snapshot)
            audit = {
                "id": _next_sequence(store.rows(AUDIT_MODULE)),
                "标签编号": tag_code,
                "标签名称": tag.get("标签名称"),
                "从版本": previous_version,
                "到版本": new_version,
                "发布批次": snapshot["发布批次"],
                "发布时间": snapshot["发布时间"],
                "版本差异": difference,
                "影响分群": sorted(str(segment.get("分群编号")) for segment in reference_rows),
                "重算批次": batch_ids,
                "结果摘要": {
                    "重算": len(updated_segments),
                    "复用": len(plan["reuse"]),
                    "待校验": len(plan["pending"]) + len(pending_batches),
                    "并发跳过": len(skipped),
                },
                "跳过分群": skipped,
            }
            store.rows(AUDIT_MODULE).append(audit)
            return {
                "tag": tag,
                "plan": plan,
                "batches": batch_ids,
                "segments": updated_segments,
                "audit": audit,
            }

    def build_plan(self, tag: dict[str, Any]) -> dict[str, Any]:
        """按标签当前版本判断各分群该重算还是继续复用。"""
        tag_code = str(tag.get("标签编号"))
        version = str(tag.get("版本号") or "")
        recalculate: list[dict[str, Any]] = []
        reuse: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for segment in self.referencing_segments(tag_code):
            codes = self._segment_tag_versions(segment)
            current = codes.get(tag_code)
            if current == version:
                reuse.append({
                    "分群编号": segment.get("分群编号"),
                    "原因": f"已使用 {tag_code}@{version}，无需重算",
                })
                continue
            entry = {
                "分群编号": segment.get("分群编号"),
                "分群名称": segment.get("分群名称"),
                "当前标签版本": current or "未记录",
                "目标标签版本": version,
                "规则版本": segment.get("规则版本"),
                "刷新周期": segment.get("刷新周期"),
            }
            if segment.get("校验状态") == "待校验":
                pending.append({**entry, "原因": "已有待校验结果，等待人工复核"})
            else:
                recalculate.append({**entry, "原因": "引用的标签版本变化"})
        return {
            "标签": tag_code,
            "版本": version,
            "重算": recalculate,
            "复用": reuse,
            "待校验": pending,
            "recalculate": recalculate,
            "reuse": reuse,
            "pending": pending,
        }

    def _segment_tag_versions(self, segment: dict[str, Any]) -> dict[str, str]:
        """分群结果里记录的标签版本。

        只认 标签版本 字段：重算结果会把实际使用的版本写回这里；
        规则里的 版本 只用于固定某个标签快照，不参与「是否过期」的判断。
        """
        result: dict[str, str] = {}
        for item in str(segment.get("标签版本") or "").split(","):
            code, _, version = item.strip().partition("@")
            if code and version:
                result[code] = version
        return result

    def _version_diff(self, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        diff: dict[str, Any] = {}
        for key in ("覆盖比例", "覆盖对象数", "计算逻辑"):
            old, new = previous.get(key), current.get(key)
            if old != new:
                diff[key] = {"旧值": old, "新值": new}
        return diff

    # ---------- 分群重算 ----------

    def recalculate_segment(
        self,
        segment: dict[str, Any],
        *,
        trigger_tag: str | None = None,
        date_key: str | None = None,
        refresh: str | None = None,
        fail_at_clause: int | None = None,
        resume_from: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按筛选规则重算分群规模，并写回同一份分群结果。

        ``resume_from`` 传入失败批次时，从该批次的检查点和失败条件继续，
        不会把已筛选对象重复计入。
        """
        date_key = date_key or _utc_now()[:10].replace("-", "")
        segment_code = str(segment.get("分群编号"))
        lock = _segment_lock(segment_code)
        # 分群级互斥：并发的第二次刷新直接放弃，保证至多一份更新生效。
        if not lock.acquire(blocking=False):
            raise TagPublishError(f"分群 {segment_code} 正在刷新，本次更新未生效")
        try:
            with store.lock:
                return self._recalculate_locked(
                    segment,
                    trigger_tag=trigger_tag,
                    date_key=date_key,
                    refresh=refresh,
                    fail_at_clause=fail_at_clause,
                    resume_from=resume_from,
                )
        finally:
            lock.release()

    def _recalculate_locked(
        self,
        segment: dict[str, Any],
        *,
        trigger_tag: str | None,
        date_key: str,
        refresh: str | None,
        fail_at_clause: int | None,
        resume_from: dict[str, Any] | None,
    ) -> dict[str, Any]:
        segment_code = str(segment.get("分群编号"))
        rule = dict(segment.get("规则定义") or {})
        conditions = list(rule.get("条件") or [])
        rule_version = str(rule.get("版本") or segment.get("规则版本") or "r1")

        # 条件里显式指定 版本 时按该快照算；没指定就由 version_snapshot 回退到
        # 标签当前已发布版本，因此发布新版本后重算会自动用上新快照。
        start_index = 0
        checkpoint: set[int] | None = None
        if resume_from:
            start_index = int(resume_from.get("检查点条件序号") or 0)
            checkpoint = set(int(item) for item in resume_from.get("检查点对象") or ())
            # 续算沿用失败批次的标签版本，避免失败与重试之间标签又发新版，
            # 导致检查点和后续条件用的是两份不同的标签快照。
            pinned = dict(resume_from.get("标签版本") or {})
            if pinned:
                conditions = [
                    {**condition, "版本": pinned.get(str(condition.get("标签编号")), condition.get("版本"))}
                    if condition.get("标签编号") and str(condition.get("标签编号")) in pinned
                    else dict(condition)
                    for condition in conditions
                ]
            if start_index and not checkpoint:
                # 检查点丢失时按前缀条件重算，仍能保证不重复计入对象。
                checkpoint, _ = evaluate_conditions(
                    [{**condition} for condition in conditions[:start_index]],
                    resolve_tag=lambda code, _version: self.find_tag(code),
                )

        batch_code = _batch_code(store.rows(BATCH_MODULE), date_key)
        resolved_versions = self._resolved_tag_versions(conditions, trigger_tag=trigger_tag)
        batch: dict[str, Any] = {
            "id": _next_sequence(store.rows(BATCH_MODULE)),
            "批次号": batch_code,
            "分群编号": segment_code,
            "状态": "执行中",
            "触发标签": trigger_tag,
            "触发版本": resolved_versions.get(trigger_tag or "", ""),
            "标签版本": ",".join(f"{code}@{version}" for code, version in resolved_versions.items()),
            "规则版本": rule_version,
            "刷新周期": refresh or segment.get("刷新周期") or "weekly",
            "开始时间": f"{date_key[:4]}-{date_key[4:6]}-{date_key[6:]} 00:00:00",
            "完成时间": "",
            "结果对象数": 0,
            "校验状态": "待校验",
            "校验说明": "",
            "检查点对象数": len(checkpoint or ()),
            "检查点条件序号": start_index,
        }
        store.rows(BATCH_MODULE).append(batch)

        def resolve_tag(tag_code: str, _version: str) -> dict[str, Any] | None:
            return self.find_tag(tag_code)

        try:
            selected, logs = evaluate_conditions(
                conditions,
                resolve_tag=resolve_tag,
                start_index=start_index,
                checkpoint=checkpoint,
                force_failure_at=fail_at_clause,
            )
        except RuleConditionError as error:
            batch["状态"] = "失败"
            batch["校验说明"] = str(error)
            batch["检查点条件序号"] = error.clause_index
            batch["检查点对象数"] = len(error.selected)
            batch["完成时间"] = batch["开始时间"]
            # 检查点对象保存在服务内存里，接口只暴露条件序号与对象数。
            self._checkpoints[batch_code] = error.selected
            segment["最近批次"] = batch_code
            return batch

        count = len(selected)
        tag_versions = resolved_versions
        ok, reason = check_scale(count, segment)

        batch["结果对象数"] = count
        batch["校验状态"] = "通过" if ok else "待校验"
        batch["校验说明"] = "" if ok else reason
        batch["状态"] = "成功" if ok else "待校验"
        batch["完成时间"] = batch["开始时间"]
        batch["条件执行记录"] = logs
        batch["检查点条件序号"] = 0
        batch["检查点对象数"] = count
        self._checkpoints[batch_code] = selected

        # 覆盖对象数、最近计算时间和标签版本和最终规模写回同一份分群结果。
        segment["最近批次"] = batch_code
        segment["覆盖对象数"] = count
        segment["最近计算时间"] = batch["完成时间"][:10]
        segment["标签版本"] = ",".join(f"{code}@{version}" for code, version in tag_versions.items())
        segment["计算批次"] = batch_code

        if ok:
            # 校验通过才把重算结果写成最终规模并推进到已生效。
            segment["最终规模"] = count
            segment["校验状态"] = "通过"
            segment["校验说明"] = ""
            segment["status"] = RESULT_STATUS
            segment["pending"] = False
            segment["abnormal"] = False
        else:
            # 越界时保留旧最终规模供追溯，分群停在待校验，不能直接发布。
            segment["校验状态"] = "待校验"
            segment["校验说明"] = reason
            segment["status"] = PENDING_STATUS
            segment["pending"] = True
            segment["abnormal"] = True
        return batch

    def _resolved_tag_versions(
        self,
        conditions: list[dict[str, Any]],
        *,
        trigger_tag: str | None,
    ) -> dict[str, str]:
        versions: dict[str, str] = {}
        for condition in conditions:
            code = condition.get("标签编号")
            if not code:
                continue
            tag = self.find_tag(str(code))
            if tag is None:
                continue
            versions[str(code)] = str(condition.get("版本") or tag.get("版本号") or "")
        if trigger_tag and trigger_tag not in versions:
            tag = self.find_tag(trigger_tag)
            if tag is not None:
                versions[trigger_tag] = str(tag.get("版本号") or "")
        return versions

    def retry_batch(self, batch_code: str) -> dict[str, Any]:
        """失败批次续算：从失败条件与检查点继续，不重复计入对象。"""
        source = next(
            (row for row in store.rows(BATCH_MODULE) if row.get("批次号") == batch_code),
            None,
        )
        if source is None:
            raise TagPublishError(f"批次 {batch_code} 不存在")
        segment = self.find_segment(str(source.get("分群编号")))
        if segment is None:
            raise TagPublishError(f"分群 {source.get('分群编号')} 不存在")
        if source.get("状态") != "失败":
            raise TagPublishError(f"批次 {batch_code} 不是失败批次，无需续算")
        versions = {}
        for item in str(source.get("标签版本") or "").split(","):
            code, _, version = item.strip().partition("@")
            if code and version:
                versions[code] = version
        resume = {
            "检查点条件序号": source.get("检查点条件序号") or 0,
            "检查点对象": self._checkpoints.get(batch_code) or set(),
            "标签版本": versions,
        }
        date_key = str(source.get("开始时间") or _utc_now())[:10].replace("-", "")
        batch = self.recalculate_segment(
            segment,
            trigger_tag=str(source.get("触发标签") or "") or None,
            date_key=date_key,
            refresh=str(source.get("刷新周期") or "") or None,
            resume_from=resume,
        )
        batch["续算自"] = batch_code
        return batch


segmentation_service = SegmentationService()
