"""PR #1 最终审核 P1-B：state 合并路径绕过了审核隐私守卫。

三条缺陷
--------
1. **从不调用 `public_review_violations()`。**
   `state_store.save_state_atomic` 有落盘前硬拦截，但合并脚本自己写文件
   （`json.dump` + `os.replace`），完全绕过那道拦截。历史 state 里只要
   残留一份审核快照，一次 rebase 冲突就会把它原样写回公开仓库。

2. **`_merge_event()` 会从另一侧补回 `article_title` / `evidence_url`。**
   对审核记录来说，这两个字段就是「哪条新闻在审」——P0 花了很大力气才把
   标题从 stub 上摘掉，一次合并就补回来了。

3. **`STATUS_RANK` 里没有 `PENDING_REVIEW` / `REVIEW_DECLINED`。**
   `_rank()` 返回 -1，比 `DISCOVERED` 还小，于是「人工拒绝」在冲突时会被
   `PENDING_DELIVERY` / `RETRYABLE_FAILURE` 静默覆盖 —— 自动重发一条被
   明确拒绝的内容，或者把一条还在审的内容直接推进投递管道。

本文件把这三条各自钉成一个用例，另外补上「无法安全裁决必须 fail-closed」
和「早退路径同样过守卫」，最后用一组回归用例确认原有合并语义没被破坏。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import merge_state  # noqa: E402
import state_store as store  # noqa: E402

TITLE = "Ronaldo announces Portugal retirement"
URL = "https://www.thesun.co.uk/sport/ronaldo-retires"


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class _MergeBase(unittest.TestCase):
    """每个用例一份独立临时目录，CLI 路径与纯函数路径都能走。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="merge-state-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def run_cli(self, local, remote):
        """按 commit_state.sh 的调用方式跑 main()，返回 (rc, local 路径, 输出)。"""
        local_path = os.path.join(self.tmp, "state.json")
        remote_path = os.path.join(self.tmp, "remote.json")
        for path, data in ((local_path, local), (remote_path, remote)):
            if data is None:
                if os.path.exists(path):
                    os.remove(path)
                continue
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["merge_state.py", local_path, remote_path]), \
                contextlib.redirect_stdout(buf):
            rc = merge_state.main()
        return rc, local_path, buf.getvalue()


# ============================================================================
# 一、隐私守卫
# ============================================================================
class TestMergePrivacyGuard(_MergeBase):
    """合并结果必须与 save_state_atomic 用同一套判定。"""

    def test_1_historical_private_fields_never_reach_merged_state(self):
        """远端残留的审核快照不得被合并进输出。"""
        remote = {"events": {"rv_deadbeef": {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "ref-1",
            "review_payload": {"title": TITLE, "cred": "E4"},
            "review_reason": "只有一家外媒",
            "article_title": TITLE,
            "evidence_url": URL,
            "fact_summary": "球员已宣布退出国家队",
        }}}
        merged = merge_state.merge({"events": {}}, remote)
        rec = merged["events"]["rv_deadbeef"]
        for leaked in ("review_payload", "review_reason", "article_title",
                       "evidence_url", "fact_summary"):
            self.assertNotIn(leaked, rec, "合并把审核私有字段写回了公开 state：{}".format(leaked))
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)
        self.assertEqual(rec["review_ref"], "ref-1", "句柄必须保留，否则审核动作失联")
        self.assertEqual(store.public_review_violations(merged), [])

    def test_2_cli_output_passes_privacy_guard(self):
        """端到端：落盘文件读回来必须干净，已送达记录的公开标题要保留。"""
        local = {"events": {"e1": {
            "delivery_status": store.DELIVERED,
            "telegram_message_id": 11,
            "article_title": TITLE,
            "review_payload": {"title": TITLE},     # 历史残留
        }}}
        remote = {"events": {"e1": {
            "delivery_status": store.DELIVERED,
            "telegram_message_id": 11,
            "article_title": TITLE,
        }}}
        rc, path, _out = self.run_cli(local, remote)
        self.assertEqual(rc, 0)
        data = json.loads(read(path))
        self.assertNotIn("review_payload", data["events"]["e1"])
        self.assertEqual(data["events"]["e1"]["article_title"], TITLE,
                         "已送达记录的标题本来就发到 Telegram 了，删掉反而破坏审计")
        self.assertEqual(store.public_review_violations(data), [])

    def test_3_review_stub_never_regains_title_or_url(self):
        """审核记录不得从另一侧补回 article_title / evidence_url。"""
        local = {"events": {"rv_x": {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "r",
            "chunks": [],
            "pending_text": "",
        }}}
        remote = {"events": {"rv_x": {
            "delivery_status": store.PENDING_REVIEW,
            "review_ref": "r",
            "article_title": TITLE,
            "evidence_url": URL,
        }}}
        merged = merge_state.merge(local, remote)
        rec = merged["events"]["rv_x"]
        self.assertNotIn("article_title", rec, "审核 stub 重新长出了标题 = 泄露「哪条在审」")
        self.assertNotIn("evidence_url", rec)
        self.assertEqual(rec["chunks"], [], "审核 stub 的 chunks 必须恒为空")
        self.assertEqual(rec["pending_text"], "")

    def test_4_unfixable_leak_blocks_write(self):
        """清理不掉越界（如 s_overflow 里）时必须拒绝写盘，不能静默通过。"""
        local = {"events": {},
                 "s_overflow": {"bucket": {"item": {"review_payload": {"t": TITLE}}}}}
        remote = {"events": {}}
        rc, path, out = self.run_cli(local, remote)
        self.assertEqual(rc, 1, "无法清理的越界必须 fail-closed")
        self.assertIn("隐私检查", out)
        self.assertEqual(json.loads(read(path)), local, "被拒绝时不得改动文件")

    def test_5_missing_remote_still_scrubs(self):
        """早退路径：远端缺失时留下的就是本地这份，同样要过守卫。"""
        local = {"events": {"e1": {
            "delivery_status": store.DELIVERED,
            "telegram_message_id": 3,
            "review_payload": {"t": TITLE},
        }}}
        rc, path, _out = self.run_cli(local, None)
        self.assertEqual(rc, 0)
        data = json.loads(read(path))
        self.assertNotIn("review_payload", data["events"]["e1"])
        self.assertEqual(store.public_review_violations(data), [])

    def test_6_missing_local_still_scrubs(self):
        """早退路径：本地缺失时采用远端，远端的历史残留同样要清掉。"""
        remote = {"events": {"e1": {
            "delivery_status": store.DELIVERED,
            "telegram_message_id": 4,
            "review_payload": {"t": TITLE},
        }}}
        rc, path, _out = self.run_cli(None, remote)
        self.assertEqual(rc, 0)
        data = json.loads(read(path))
        self.assertNotIn("review_payload", data["events"]["e1"])
        self.assertEqual(store.public_review_violations(data), [])


# ============================================================================
# 二、审核状态冲突规则
# ============================================================================
class TestReviewStatusConflict(unittest.TestCase):
    """REVIEW_DECLINED 是终态，PENDING_REVIEW 是闸门，都不能被盲目覆盖。"""

    def _ev(self, status, **over):
        ev = {"delivery_status": status, "updated_at": "2026-03-01T00:00:00+00:00"}
        ev.update(over)
        return ev

    def test_1_declined_never_overwritten_by_auto_delivery(self):
        """拒绝决定不得被 PENDING_DELIVERY / RETRYABLE_FAILURE 等覆盖。"""
        for other in (store.PENDING_DELIVERY, store.RETRYABLE_FAILURE,
                      store.DELIVERY_FAILED, store.DELIVERY_UNCERTAIN,
                      store.DISCOVERED, store.VERIFIED, store.PENDING_REVIEW):
            local = {"events": {"e1": self._ev(store.REVIEW_DECLINED, review_ref="r")}}
            remote = {"events": {"e1": self._ev(other)}}
            got = merge_state.merge(local, remote)["events"]["e1"]["delivery_status"]
            self.assertEqual(got, store.REVIEW_DECLINED,
                             "人工拒绝被 {} 覆盖了（local 在前）".format(other))
            got = merge_state.merge(remote, local)["events"]["e1"]["delivery_status"]
            self.assertEqual(got, store.REVIEW_DECLINED,
                             "人工拒绝被 {} 覆盖了（remote 在前）".format(other))

    def test_2_declined_survives_merge_metadata(self):
        """裁决为拒绝时，句柄要跟着拒绝那份走，不能被另一侧冲掉。"""
        local = {"events": {"e1": self._ev(store.REVIEW_DECLINED, review_ref="keep-me")}}
        remote = {"events": {"e1": self._ev(store.PENDING_DELIVERY,
                                            review_ref="other", telegram_message_id=8)}}
        rec = merge_state.merge(local, remote)["events"]["e1"]
        self.assertEqual(rec["delivery_status"], store.REVIEW_DECLINED)
        self.assertEqual(rec["review_ref"], "keep-me")

    def test_3_declined_vs_real_delivery_is_fail_closed(self):
        """一边拒绝、一边确实发出去了 → 无法裁决，必须抛异常中止。"""
        local = {"events": {"e1": self._ev(store.REVIEW_DECLINED, review_ref="r")}}
        remote = {"events": {"e1": self._ev(store.DELIVERED, telegram_message_id=9)}}
        with self.assertRaises(merge_state.MergeConflict) as ctx:
            merge_state.merge(local, remote)
        self.assertIn("e1", str(ctx.exception), "报错必须指出是哪一条事件冲突")

    def test_4_declined_vs_delivered_without_receipt_stays_declined(self):
        """没有 message_id 的 DELIVERED 不算真实送达证据，仍按拒绝处理。"""
        local = {"events": {"e1": self._ev(store.REVIEW_DECLINED, review_ref="r")}}
        remote = {"events": {"e1": self._ev(store.DELIVERED)}}
        merged = merge_state.merge(local, remote)
        self.assertEqual(merged["events"]["e1"]["delivery_status"], store.REVIEW_DECLINED)

    def test_5_fail_closed_cli_writes_nothing(self):
        """fail-closed 的落盘行为：返回 1，且文件逐字节保持原样。"""
        local = {"events": {"e1": self._ev(store.REVIEW_DECLINED, review_ref="r")}}
        remote = {"events": {"e1": self._ev(store.DELIVERED, telegram_message_id=9)}}
        tmp = tempfile.mkdtemp(prefix="merge-conflict-")
        try:
            lp = os.path.join(tmp, "state.json")
            rp = os.path.join(tmp, "remote.json")
            for path, data in ((lp, local), (rp, remote)):
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            before = read(lp)
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", ["merge_state.py", lp, rp]), \
                    contextlib.redirect_stdout(buf):
                rc = merge_state.main()
            self.assertEqual(rc, 1)
            self.assertIn("fail-closed", buf.getvalue())
            self.assertEqual(read(lp), before, "中止时不得写入任何内容")
            self.assertFalse(os.path.exists(lp + ".merge.tmp"), "不得留下临时文件")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_6_pending_review_loses_to_delivery_progress(self):
        """已过闸（审核通过）→ 取投递状态，避免重复送审或重复推送。"""
        local = {"events": {"e1": self._ev(store.PENDING_REVIEW, review_ref="r")}}
        remote = {"events": {"e1": self._ev(store.PENDING_DELIVERY)}}
        self.assertEqual(
            merge_state.merge(local, remote)["events"]["e1"]["delivery_status"],
            store.PENDING_DELIVERY)
        self.assertEqual(
            merge_state.merge(remote, local)["events"]["e1"]["delivery_status"],
            store.PENDING_DELIVERY)

    def test_7_pending_review_wins_over_discovered(self):
        """还没过闸 → 闸门更严，不能被「刚发现 / 刚核实」冲掉。"""
        for weaker in (store.DISCOVERED, store.VERIFIED):
            local = {"events": {"e1": self._ev(store.PENDING_REVIEW, review_ref="r")}}
            remote = {"events": {"e1": self._ev(weaker)}}
            self.assertEqual(
                merge_state.merge(local, remote)["events"]["e1"]["delivery_status"],
                store.PENDING_REVIEW, "{} 把审核闸门冲掉了".format(weaker))

    def test_8_review_statuses_are_ranked(self):
        """STATUS_RANK 必须显式包含两个审核状态（缺失时 _rank 返回 -1）。"""
        for status in (store.PENDING_REVIEW, store.REVIEW_DECLINED):
            self.assertIn(status, merge_state.STATUS_RANK)
            self.assertGreaterEqual(merge_state.STATUS_RANK[status], 0)
        self.assertGreater(merge_state.STATUS_RANK[store.REVIEW_DECLINED],
                           merge_state.STATUS_RANK[store.COMMITTED],
                           "拒绝是终态，必须排在所有投递状态之后")


# ============================================================================
# 三、原有合并语义不回归
# ============================================================================
class TestMergeSemanticsPreserved(unittest.TestCase):
    def _ev(self, status, **over):
        ev = {"delivery_status": status, "updated_at": "2026-03-01T00:00:00+00:00",
              "chunks": []}
        ev.update(over)
        return ev

    def test_1_committed_never_downgraded(self):
        local = {"events": {"e1": self._ev(store.PENDING_DELIVERY)}}
        remote = {"events": {"e1": self._ev(store.COMMITTED, telegram_message_id=5)}}
        for a, b in ((local, remote), (remote, local)):
            merged = merge_state.merge(a, b)
            self.assertEqual(merged["events"]["e1"]["delivery_status"], store.COMMITTED,
                             "已提交记录不得因合并降级（会重复投递）")
            self.assertEqual(merged["events"]["e1"]["telegram_message_id"], 5)

    def test_2_pool_focus_morning_still_merge(self):
        local = {
            "news_pool": [{"link": "https://a/1", "published": "2026-03-01T01:00:00"}],
            "focus": {"calendar_at": "2026-03-01T00:00:00",
                      "matches": {"m1": {"referee_pushed": False}}},
            "morning_delivery": {
                "date": "2026-03-01", "pending_text": "X",
                "chunks": [{"chunk_index": 0, "delivery_status": "DELIVERED",
                            "telegram_message_id": 1}],
                "status": "PARTIALLY_DELIVERED"},
            "last_morning_report_date": "2026-03-01",
        }
        remote = {
            "news_pool": [{"link": "https://a/2", "published": "2026-03-01T02:00:00"}],
            "focus": {"calendar_at": "2026-03-01T05:00:00",
                      "matches": {"m1": {"referee_pushed": True}}},
            "morning_delivery": {
                "date": "2026-03-01", "pending_text": "X",
                "chunks": [{"chunk_index": 1, "delivery_status": "DELIVERED",
                            "telegram_message_id": 2}],
                "status": "DELIVERING"},
            "last_morning_report_date": "2026-03-01",
        }
        merged = merge_state.merge(local, remote)
        self.assertEqual(len(merged["news_pool"]), 2, "新闻池必须取并集")
        self.assertTrue(merged["focus"]["matches"]["m1"]["referee_pushed"],
                        "焦点战「宁可多推一次，不能漏推」")
        self.assertEqual(merged["focus"]["calendar_at"], "2026-03-01T05:00:00")
        idx = sorted(c["chunk_index"] for c in merged["morning_delivery"]["chunks"])
        self.assertEqual(idx, [0, 1], "早报分段账本必须取并集，已送达段不得丢失")
        self.assertEqual(merged["last_morning_report_date"], "2026-03-01")

    def test_3_ordinary_delivery_status_still_progresses(self):
        """普通状态仍按进度取靠后的那份（本次改动不得把整条链路变保守）。"""
        local = {"events": {"e1": self._ev(store.VERIFIED)}}
        remote = {"events": {"e1": self._ev(store.PENDING_DELIVERY)}}
        self.assertEqual(
            merge_state.merge(local, remote)["events"]["e1"]["delivery_status"],
            store.PENDING_DELIVERY)
        local = {"events": {"e1": self._ev(store.PENDING_DELIVERY)}}
        remote = {"events": {"e1": self._ev(store.DELIVERED, telegram_message_id=2)}}
        self.assertEqual(
            merge_state.merge(local, remote)["events"]["e1"]["delivery_status"],
            store.DELIVERED)


# ============================================================================
# 四、P1-C：events 结构异常不得成为隐私守卫的免检通道
# ============================================================================
class TestMalformedEventsStructure(_MergeBase):
    """`events` 是非空非 dict 时，隐私检查不能被整体跳过。

    原缺陷（`state_store.public_review_violations`）：

        events = state.get("events") or {}
        if not isinstance(events, dict):
            return out          # ← 直接返回空列表，后面三项检查全被跳过

    于是这样一份 state：

        {"events": ["https://example.com/leak"],
         "s_overflow": {"bucket": {"item": {"review_payload": {...}}}}}

    会被判成「干净」：函数返回 []，`save_state_atomic` 照常落盘、
    `merge_state` 照常写文件。`s_overflow` 里那份审核快照就这么进了公开仓库。

    修法有两层，本类各钉一遍：
      · `public_review_violations()` 把结构异常本身记成违规，并**继续**跑
        后面的检查（这一层同时覆盖 bot 的落盘路径）；
      · `merge_state._structure_violations()` 在合并之前挡住，让拒绝是一条
        明确判定，而不是 `.items()` 抛出来的一段 traceback。
    """

    LEAKY = {
        "events": ["https://example.com/leak"],
        "s_overflow": {"bucket": {"item": {"review_payload": {"title": TITLE}}}},
    }

    def test_1_guard_reports_structure_and_keeps_checking(self):
        """隐私守卫必须同时报出「结构异常」和 s_overflow 里的越界。"""
        violations = store.public_review_violations(self.LEAKY)
        self.assertTrue(violations, "非 dict 的 events 让整个隐私检查被跳过了")
        joined = " | ".join(violations)
        self.assertIn("events", joined, "必须明确指出 events 结构异常")
        self.assertIn("review_payload", joined,
                      "s_overflow 里的审核私有字段不得因为 events 坏掉就被跳过")

    def test_2_save_state_atomic_rejects_malformed_events(self):
        """bot 的落盘路径用的是同一处守卫，同样必须拒绝。"""
        path = os.path.join(self.tmp, "state.json")
        with self.assertRaises(store.StateError):
            store.save_state_atomic(self.LEAKY, path)
        self.assertFalse(os.path.exists(path), "被拒绝时不得留下文件")

    def test_3_merge_refuses_to_write(self):
        """端到端：本地坏结构 → 拒绝写盘，原文件逐字节不变。"""
        local = dict(self.LEAKY)
        remote = {"events": {}, "news_pool": []}
        rc, path, out = self.run_cli(local, remote)
        self.assertEqual(rc, 1, "events 结构异常时合并不得返回成功")
        self.assertIn("结构异常", out)
        self.assertEqual(json.loads(read(path)), local, "被拒绝时文件必须逐字节不变")
        self.assertFalse(os.path.exists(path + ".merge.tmp"), "不得留下临时文件")

    def test_4_remote_malformed_also_refused(self):
        """远端坏结构同样拒绝，不能只看本地那一份。"""
        local = {"events": {}, "news_pool": []}
        remote = dict(self.LEAKY)
        rc, path, out = self.run_cli(local, remote)
        self.assertEqual(rc, 1)
        self.assertIn("远端", out)
        self.assertEqual(json.loads(read(path)), local)

    def test_5_early_return_path_also_refused(self):
        """早退路径（远端缺失）也不得把坏结构原样写回。"""
        local = dict(self.LEAKY)
        rc, path, out = self.run_cli(local, None)
        self.assertEqual(rc, 1, "远端缺失时也不能放过坏结构")
        self.assertIn("结构异常", out)
        self.assertEqual(json.loads(read(path)), local)

    def test_6_empty_events_list_still_tolerated(self):
        """空 list 是旧版 state 的常见形态，按「没有事件」处理，不算违规。"""
        self.assertEqual(store.public_review_violations({"events": []}), [])
        self.assertEqual(merge_state._structure_violations({"events": []}), [])

    def test_7_normal_structure_unaffected(self):
        """正常结构的合法 state 不受影响：照常合并、落盘、隐私检查通过。"""
        local = {
            "events": {"e1": {"delivery_status": store.DELIVERED,
                              "telegram_message_id": 21,
                              "article_title": TITLE}},
            "news_pool": [{"link": "https://a/1", "published": "2026-03-01T01:00:00"}],
            "last_morning_report_date": "2026-03-01",
        }
        remote = {
            "events": {"e1": {"delivery_status": store.DELIVERED,
                              "telegram_message_id": 21}},
            "news_pool": [{"link": "https://a/2", "published": "2026-03-01T02:00:00"}],
            "last_morning_report_date": "2026-03-01",
        }
        rc, path, out = self.run_cli(local, remote)
        self.assertEqual(rc, 0, out)
        data = json.loads(read(path))
        self.assertEqual(store.public_review_violations(data), [])
        self.assertEqual(len(data["news_pool"]), 2, "新闻池照常取并集")
        self.assertEqual(data["events"]["e1"]["delivery_status"], store.DELIVERED)
        self.assertEqual(data["last_morning_report_date"], "2026-03-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
