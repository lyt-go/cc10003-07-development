"""标签版本驱动的分群重算回归用例。

直接调用服务层，避免依赖测试客户端；每个用例前把内存仓库还原成种子数据。
"""
from __future__ import annotations

import threading
import unittest

from app.seed import SEED_ROWS
from app.services.segment import SegmentService
from app.services.segmentation import UNIVERSE_SIZE, evaluate_conditions, tag_object_ids
from app.services.segmentation_service import SegmentationService
from app.services.tagmgr import TagmgrService
from app.store import store


def reset_store() -> None:
    for name, rows in SEED_ROWS.items():
        store._tables[name] = [dict(row) for row in rows]


class SegmentationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.segmentation = SegmentationService()
        self.segments = SegmentService(self.segmentation)
        self.tags = TagmgrService(self.segmentation)
        # 检查点集合存在服务实例上，用例之间互不干扰。
        self.segmentation._checkpoints.clear()

    # ---------- 版本驱动重算 ----------

    def test_publish_new_version_recalculates_referencing_segment(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0002")
        self.assertEqual(segment["标签版本"].split(",")[0], "TAGM-0003@v2")

        result = self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 09:00:00", "重算日期": "2026-11-13"},
        )

        self.assertEqual(result["tag"]["版本号"], "v4")
        self.assertEqual([item["分群编号"] for item in result["plan"]["重算"]], ["SEGM-0002"])
        self.assertEqual(result["batches"], ["BATCH-SEG-20261113-A"])

        self.assertIn("TAGM-0003@v4", segment["标签版本"])
        self.assertEqual(segment["覆盖对象数"], segment["最终规模"])
        self.assertEqual(segment["计算批次"], "BATCH-SEG-20261113-A")
        self.assertEqual(segment["最近计算时间"], "2026-11-13")
        self.assertEqual(segment["校验状态"], "通过")
        self.assertEqual(segment["status"], "已生效")

    def test_unaffected_segment_keeps_its_own_batch(self) -> None:
        before = self.segmentation.find_segment("SEGM-0003")["计算批次"]
        self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 09:00:00", "重算日期": "2026-11-13"},
        )
        after = self.segmentation.find_segment("SEGM-0003")["计算批次"]
        self.assertEqual(before, "BATCH-SEG-20261110-A")
        self.assertEqual(after, "BATCH-SEG-20261110-A")

    def test_reuse_when_segment_already_on_current_version(self) -> None:
        self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 09:00:00", "重算日期": "2026-11-13"},
        )
        plan = self.segmentation.build_plan(self.segmentation.find_tag("TAGM-0003"))
        self.assertEqual(plan["recalculate"], [])
        self.assertEqual([item["分群编号"] for item in plan["reuse"]], ["SEGM-0002"])

        result = self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 10:00:00", "重算日期": "2026-11-13"},
        )
        # 版本升到 v5，只有引用该标签的分群重算。
        self.assertIn("引用的标签版本变化", result["plan"]["重算"][0]["原因"])

    def test_audit_records_version_difference(self) -> None:
        result = self.segmentation.publish_tag(
            "TAGM-0003",
            {
                "发布时间": "2026-11-13 09:00:00",
                "重算日期": "2026-11-13",
                "覆盖比例": 64,
                "发布批次": "BATCH-TAG-20261113-A",
            },
        )
        audit = result["audit"]
        self.assertEqual((audit["从版本"], audit["到版本"]), ("v3", "v4"))
        self.assertEqual(audit["版本差异"]["覆盖比例"], {"旧值": 60, "新值": 64})
        self.assertEqual(audit["影响分群"], ["SEGM-0002"])
        self.assertEqual(audit["重算批次"], ["BATCH-SEG-20261113-A"])
        self.assertEqual(self.segmentation.audit_rows(tag_code="TAGM-0003")[0]["到版本"], "v4")

    def test_tag_lists_referencing_segments(self) -> None:
        items = self.tags.reference_segments("TAGM-0003")
        self.assertEqual([item["分群编号"] for item in items], ["SEGM-0002"])
        self.assertEqual(items[0]["计算批次"], "BATCH-SEG-20261106-A")
        # 未发布前规则里引用的分群也能反查到，标签列表因此可以提示影响面。
        self.assertEqual(
            [item["分群编号"] for item in self.tags.reference_segments("TAGM-0001")],
            ["SEGM-0002", "SEGM-0004"],
        )

    # ---------- 规模边界 ----------

    def test_out_of_range_count_keeps_old_size_and_pending(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0004")
        old_size = segment["最终规模"]

        batch, message = self.segments.refresh(3)

        self.assertIsNotNone(batch)
        self.assertIn("待校验", message)
        self.assertEqual(segment["校验状态"], "待校验")
        self.assertEqual(segment["status"], "规则校验中")
        self.assertEqual(segment["最终规模"], old_size)
        self.assertEqual(segment["覆盖对象数"], 8000)
        self.assertIn("最大规模", segment["校验说明"])

    def test_pending_segment_cannot_be_activated(self) -> None:
        self.segments.refresh(3)
        entry, message = self.segments.run_action(3, "生效分群")
        self.assertIsNone(entry)
        self.assertIn("不能直接生效", message)

    def test_in_range_count_activates_segment(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0004")
        segment["最大规模"] = 9000
        batch, message = self.segments.refresh(3)
        self.assertTrue(batch["校验状态"] == "通过", message)
        self.assertEqual(segment["最终规模"], 8000)
        self.assertEqual(segment["status"], "已生效")

    # ---------- 并发与失败续算 ----------

    def test_concurrent_refresh_only_one_wins(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0002")
        before = {
            row["批次号"] for row in self.segmentation.list_batches(segment_code="SEGM-0002")
        }
        errors: list[str] = []
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait()
            try:
                self.segmentation.recalculate_segment(segment, trigger_tag="TAGM-0003")
            except Exception as error:  # noqa: BLE001 - 并发冲突是预期结果
                errors.append(str(error))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 同一分群同时刷新时至多一份更新生效，其余请求被明确拒绝。
        self.assertGreaterEqual(len(errors), 1)
        self.assertTrue(all("正在刷新" in item for item in errors))
        # 只有一个批次写入了结果，且规模与覆盖对象数一致。
        new_batches = [
            row
            for row in self.segmentation.list_batches(segment_code="SEGM-0002")
            if row["批次号"] not in before
        ]
        self.assertEqual(len(new_batches), 1)
        self.assertEqual(segment["计算批次"], new_batches[0]["批次号"])
        self.assertEqual(segment["最终规模"], segment["覆盖对象数"])

    def test_failed_batch_resumes_from_affected_clause_without_duplicates(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0002")

        def resolve_tag(code: str, _version: str):
            return self.segmentation.find_tag(code)

        expected, _ = evaluate_conditions(
            [dict(item) for item in segment["规则定义"]["条件"]],
            resolve_tag=resolve_tag,
        )
        failed = self.segmentation.recalculate_segment(
            segment, trigger_tag="TAGM-0003", fail_at_clause=1
        )
        self.assertEqual(failed["状态"], "失败")
        self.assertEqual(failed["检查点条件序号"], 1)
        self.assertEqual(failed["检查点对象数"], 60000)

        resumed = self.segmentation.retry_batch(failed["批次号"])
        self.assertEqual(resumed["状态"], "成功")
        self.assertEqual(resumed["续算自"], failed["批次号"])
        self.assertEqual(resumed["结果对象数"], len(expected))
        self.assertEqual(segment["最终规模"], len(expected))
        self.assertEqual(segment["覆盖对象数"], len(expected))

    def test_retry_rejects_successful_batch(self) -> None:
        batch = self.segmentation.list_batches(segment_code="SEGM-0002")[0]
        with self.assertRaises(Exception) as context:
            self.segmentation.retry_batch(batch["批次号"])
        self.assertIn("不是失败批次", str(context.exception))

    # ---------- 列表定位 ----------

    def test_list_filters_by_tag_and_batch_across_pages(self) -> None:
        items, total = self.segments.list_entries(page=1, size=1)
        self.assertEqual(total, 3)
        self.assertEqual(len(items), 1)

        items, total = self.segments.list_entries(tag_code="TAGM-0003")
        self.assertEqual([item["分群编号"] for item in items], ["SEGM-0002"])
        self.assertEqual(items[0]["所属批次"], "BATCH-SEG-20261106-A")

        items, total = self.segments.list_entries(batch="BATCH-SEG-20261113-A")
        self.assertEqual(items, [])

        self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 09:00:00", "重算日期": "2026-11-13"},
        )
        # 切换过滤条件后仍能定位到同一条分群，并显示它最新的所属批次。
        items, _ = self.segments.list_entries(tag_code="TAGM-0003", batch="BATCH-SEG-20261113-A")
        self.assertEqual([item["分群编号"] for item in items], ["SEGM-0002"])
        self.assertEqual(items[0]["所属批次"], "BATCH-SEG-20261113-A")

    def test_locate_returns_page_and_batch_after_filter_switch(self) -> None:
        located = self.segments.locate(keyword="SEGM-0002", size=1)
        self.assertEqual(located["page"], 1)
        self.assertEqual(located["entry"]["所属批次"], "BATCH-SEG-20261106-A")

        # 切到「标签 + 批次」条件后，同一条分群仍能定位到。
        located = self.segments.locate(
            keyword="SEGM-0002", tag_code="TAGM-0003", batch="BATCH-SEG-20261113-A", size=1
        )
        self.assertIsNone(located)

        self.segmentation.publish_tag(
            "TAGM-0003",
            {"发布时间": "2026-11-13 09:00:00", "重算日期": "2026-11-13"},
        )
        located = self.segments.locate(
            keyword="SEGM-0002", tag_code="TAGM-0003", batch="BATCH-SEG-20261113-A", size=1
        )
        self.assertIsNotNone(located)
        self.assertEqual(located["entry"]["所属批次"], "BATCH-SEG-20261113-A")

        self.assertIsNone(self.segments.locate(keyword="SEGM-9999"))

    def test_activating_segment_refreshes_tag_reverse_lookup(self) -> None:
        tag = self.segmentation.find_tag("TAGM-0003")
        tag["引用分群"] = []
        segment = self.segmentation.find_segment("SEGM-0002")
        self.segments.run_action(segment["id"], "生效分群")
        self.assertEqual(tag["引用分群"], ["SEGM-0002"])

    def test_resume_keeps_failed_batch_tag_version(self) -> None:
        segment = self.segmentation.find_segment("SEGM-0002")
        failed = self.segmentation.recalculate_segment(
            segment, trigger_tag="TAGM-0003", fail_at_clause=2
        )
        # 失败后标签又发了新版本，续算仍按失败批次的版本快照继续。
        self.segmentation.publish_tag(
            "TAGM-0003",
            {
                "发布时间": "2026-11-13 09:00:00",
                "重算日期": "2026-11-13",
                "覆盖比例": 90,
            },
        )
        resumed = self.segmentation.retry_batch(failed["批次号"])
        self.assertEqual(resumed["状态"], "成功")
        self.assertIn("TAGM-0003@v3", resumed["标签版本"])

    def test_tag_object_count_matches_coverage(self) -> None:
        tag = self.segmentation.find_tag("TAGM-0003")
        snapshot = tag["版本记录"][-1]
        self.assertEqual(len(tag_object_ids(snapshot)), int(UNIVERSE_SIZE * 60 / 100))


if __name__ == "__main__":
    unittest.main()
