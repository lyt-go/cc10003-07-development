"""人群规模计算引擎：用稳定的对象集合语义重算分群规模。

真实项目里这一步会落到数仓（标签物化表 + 规则 SQL / 检索引擎）。这里用
确定性的对象编号模拟同一份对象全集，保证：

* 同一个标签版本 + 同一份规则算出的对象集合稳定可复现；
* 计数按对象去重，重试或续算不会把对象重复计入；
* 规则按条件逐段执行，失败时可以保留「已处理条件」和检查点，
  从受影响条件继续，而不是整批重来。
"""
from __future__ import annotations

from typing import Any, Callable

# 对象全集固定为 100000 个对象，标签覆盖比例按百分比映射到对象编号上。
UNIVERSE_SIZE = 100_000
OBJECT_IDS = range(1, UNIVERSE_SIZE + 1)

REGIONS = ["华东", "华南", "华北", "西南", "东北"]
LEVELS = ["金", "银", "普"]


class RuleConditionError(RuntimeError):
    """单个筛选条件执行失败：带条件序号和已筛出的检查点，供续算使用。"""

    def __init__(
        self,
        message: str,
        *,
        clause_index: int,
        clause: dict[str, Any],
        selected: set[int] | None = None,
    ) -> None:
        super().__init__(message)
        self.clause_index = clause_index
        self.clause = clause
        self.selected = set(selected or ())  # 失败条件之前已去重的对象


def version_snapshot(tag: dict[str, Any], version: str | None = None) -> dict[str, Any]:
    """读取标签某个版本的覆盖配置；版本不在版本记录里时视为不可用。"""
    records: list[dict[str, Any]] = list(tag.get("版本记录") or [])
    wanted = version or str(tag.get("版本号") or "")
    for record in records:
        if str(record.get("版本")) == wanted:
            return dict(record)
    if not records and wanted == str(tag.get("版本号") or ""):
        # 兼容只有当前定义、没有版本记录的标签。
        return {
            "版本": wanted,
            "覆盖比例": int(tag.get("覆盖比例") or 0),
            "对象偏移": int(tag.get("对象偏移") or 0),
            "计算逻辑": tag.get("计算逻辑"),
        }
    raise RuleConditionError(
        f"标签 {tag.get('标签编号')} 不存在版本 {wanted}",
        clause_index=-1,
        clause={},
    )


def tag_object_ids(snapshot: dict[str, Any]) -> set[int]:
    """标签版本覆盖的对象集合，按对象编号去重。

    对象编号加偏移后对 100 取模小于覆盖比例即命中，因此对象数严格等于
    覆盖比例 * UNIVERSE_SIZE / 100，审计时可核对。
    """
    coverage = max(0, min(int(snapshot.get("覆盖比例") or 0), 100))
    offset = int(snapshot.get("对象偏移") or 0)
    return {
        object_id
        for object_id in OBJECT_IDS
        if ((object_id + offset) % 100) < coverage
    }


def object_attributes(object_id: int) -> dict[str, Any]:
    """对象属性由编号确定，保证跨批次、跨进程可复现。"""
    return {
        "region": REGIONS[object_id % len(REGIONS)],
        "level": LEVELS[object_id % len(LEVELS)],
        "active_days": object_id % 120 + 1,
    }


def _match_attribute(object_id: int, condition: dict[str, Any]) -> bool:
    attributes = object_attributes(object_id)
    field = str(condition.get("字段") or "")
    operator = str(condition.get("运算符") or "in")
    expected = condition.get("取值")
    actual = attributes.get(field)
    if operator in ("in", "属于"):
        values = expected if isinstance(expected, list) else [expected]
        return actual in values
    if operator in ("not_in", "不属于"):
        values = expected if isinstance(expected, list) else [expected]
        return actual not in values
    if operator in (">=", "大于等于"):
        return int(actual or 0) >= int(expected or 0)
    if operator in ("<=", "小于等于"):
        return int(actual or 0) <= int(expected or 0)
    if operator in ("==", "等于"):
        return actual == expected
    raise ValueError(f"不支持的运算符：{operator}")


def evaluate_conditions(
    conditions: list[dict[str, Any]],
    *,
    resolve_tag: Callable[[str, str], dict[str, Any] | None],
    start_index: int = 0,
    checkpoint: set[int] | None = None,
    force_failure_at: int | None = None,
) -> tuple[set[int], list[dict[str, Any]]]:
    """按顺序执行筛选条件，返回去重后的对象集合与每个条件的执行记录。

    ``start_index`` + ``checkpoint`` 用于失败续算：检查点里已经是前面条件
    筛出的去重对象，续算时直接接着往后筛，不会把对象重复计入。
    """
    selected: set[int] = set(checkpoint) if checkpoint is not None else set(OBJECT_IDS)
    logs: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions):
        if index < start_index:
            continue
        if force_failure_at is not None and index == force_failure_at:
            raise RuleConditionError(
                f"条件「{condition.get('名称') or condition.get('类型')}」执行失败",
                clause_index=index,
                clause=condition,
                selected=selected,
            )
        kind = str(condition.get("类型") or "")
        before = len(selected)
        if kind in ("标签", "排除标签"):
            tag_code = str(condition.get("标签编号") or "")
            version = str(condition.get("版本") or "")
            tag = resolve_tag(tag_code, version)
            if tag is None:
                raise RuleConditionError(
                    f"引用的标签 {tag_code} 不存在或已归档",
                    clause_index=index,
                    clause=condition,
                    selected=selected,
                )
            try:
                snapshot = version_snapshot(tag, version)
            except RuleConditionError:
                raise RuleConditionError(
                    f"标签 {tag_code} 缺少版本 {version}",
                    clause_index=index,
                    clause=condition,
                    selected=selected,
                ) from None
            members = tag_object_ids(snapshot)
            selected = selected & members if kind == "标签" else selected - members
        elif kind == "对象属性":
            selected = {object_id for object_id in selected if _match_attribute(object_id, condition)}
        elif kind == "模拟失败":
            raise RuleConditionError(
                "规则条件被标记为执行失败",
                clause_index=index,
                clause=condition,
                selected=selected,
            )
        else:
            raise RuleConditionError(
                f"未知的筛选条件类型：{kind or '空'}",
                clause_index=index,
                clause=condition,
                selected=selected,
            )
        logs.append({
            "序号": index + 1,
            "名称": condition.get("名称") or kind,
            "类型": kind,
            "输入对象数": before,
            "输出对象数": len(selected),
        })
    return selected, logs


def scale_bounds(segment: dict[str, Any]) -> tuple[int, int]:
    """读取分群的最小/最大规模；坏数据按 0 与无穷大处理。"""
    def as_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return as_int(segment.get("最小规模"), 0), as_int(segment.get("最大规模"), UNIVERSE_SIZE)


def check_scale(count: int, segment: dict[str, Any]) -> tuple[bool, str]:
    """规模校验：低于最小规模或高于最大规模都不允许直接生效。"""
    minimum, maximum = scale_bounds(segment)
    if count < minimum:
        return False, f"低于最小规模（{count} < {minimum}）"
    if count > maximum:
        return False, f"高于最大规模（{count} > {maximum}）"
    return True, "通过"
