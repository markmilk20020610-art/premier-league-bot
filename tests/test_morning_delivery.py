"""V2.2 P0：早报分段投递账本。

修的问题（改动前）
------------------
早报走 `send_telegram(card)` → `send_telegram_ex(text)`，`chunk_records=None`：
分段能力有，但分段结果从不落盘。于是「第 1 段已到群里、第 2 段失败」时，
系统只知道整体失败；下一轮从第 1 段重来 → 读者收到重复内容，而且永远发不完。

这组测试锁住四条不变式：
  1. 已确认送达的段，下一次运行绝不重发；
  2. 只有全部分段送达，才写 `last_morning_report_date`；
  3. 分段结果必须落盘（否则「哪几段已送达」的记忆丢失）；
  4. 卡片原文跨运行沿用，避免模型重写导致哈希变化、退化成整条重发。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import bot                     # noqa: E402
import merge_state             # noqa: E402
import state_store as store    # noqa: E402

TODAY = "2026-11-01"
YESTERDAY = "2026-10-31"


def long_card(paragraphs=3, width=2000, ch="情"):
    """构造一张会被切成多段的卡片。

    每段一行、每行 width 字：`_split_html` 逐行聚合，两行拼起来超过 4000，
    所以会按行边界切开——正是早报的真实形态（段落之间不硬切）。

    每行必须带唯一前缀：否则三段文本完全相同，「第 1 段有没有被重发」
    这种断言根本测不出来（内容一样，看着就像重发了）。
    """
    return "\n".join(f"P{i} " + ch * width for i in range(1, paragraphs + 1))


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self._saved = (bot.STATE_FILE, bot.TELEGRAM_BOT_TOKEN,
                       bot.TELEGRAM_CHAT_ID, bot.TELEGRAM_RETRY_DELAYS)
        bot.STATE_FILE = self.state_path
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "TEST_CHAT"
        bot.TELEGRAM_RETRY_DELAYS = ()      # 测试里不睡
        self.state = store.ensure_shape({})
        self.sent = []                      # 每次真正发出的分段文本
        self.script = []                    # 逐次返回的剧本；空则一律成功
        self._patches = [mock.patch.object(bot, "_post_telegram_once",
                                           side_effect=self._fake_post)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        (bot.STATE_FILE, bot.TELEGRAM_BOT_TOKEN,
         bot.TELEGRAM_CHAT_ID, bot.TELEGRAM_RETRY_DELAYS) = self._saved
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _fake_post(self, url, payload):
        self.sent.append(payload["text"])
        if self.script:
            ok, kind, reason = self.script.pop(0)
        else:
            ok, kind, reason = True, bot.TG_OK, ""
        mid = (9000 + len(self.sent)) if ok else None
        return ok, kind, reason, mid

    def deliver(self, card, today=TODAY, force=False):
        return bot._deliver_morning(self.state, card, today, force=force)


class TestAllDelivered(DeliveryBase):
    def test_three_chunks_all_sent(self):
        card = long_card(3)
        ok, summary, err = self.deliver(card)
        self.assertTrue(ok, f"应全部送达，实际 {summary} / {err}")
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["delivered_indexes"], [1, 2, 3])
        self.assertTrue(summary["all_delivered"])
        self.assertEqual(len(self.sent), 3)

    def test_lock_written_only_after_all_delivered(self):
        card = long_card(3)
        ok, _summary, _err = self.deliver(card)
        self.assertTrue(ok)
        self.assertTrue(bot._lock_morning_sent(self.state, TODAY, "早报"))
        reloaded = bot.load_state()
        self.assertEqual(reloaded["last_morning_report_date"], TODAY)
        self.assertTrue(reloaded.get("last_summary_sent_at"))

    def test_record_persisted_with_message_ids(self):
        card = long_card(3)
        self.deliver(card)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["date"], TODAY)
        self.assertEqual(rec["status"], "DELIVERED")
        self.assertEqual([c["chunk_index"] for c in rec["chunks"]], [1, 2, 3])
        for c in rec["chunks"]:
            self.assertEqual(c["delivery_status"], store.CHUNK_DELIVERED)
            self.assertTrue(c["telegram_message_id"])
            self.assertTrue(c["chunk_hash"])


class TestPartialFailure(DeliveryBase):
    def test_second_chunk_fails_no_lock_and_partial_record(self):
        card = long_card(3)
        self.script = [(True, bot.TG_OK, ""),
                       (False, bot.TG_RETRYABLE, "timeout")]
        ok, summary, err = self.deliver(card)
        self.assertFalse(ok, "部分成功绝不能报成全部成功")
        self.assertEqual(summary["delivered_indexes"], [1])
        self.assertTrue(summary["partial"])
        self.assertEqual(err, "timeout")

        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["status"], "PARTIALLY_DELIVERED")
        statuses = {c["chunk_index"]: c["delivery_status"] for c in rec["chunks"]}
        self.assertEqual(statuses[1], store.CHUNK_DELIVERED)
        self.assertEqual(statuses[2], store.CHUNK_RETRYABLE)
        self.assertEqual(statuses[3], store.CHUNK_NOT_ATTEMPTED)

    def test_failure_does_not_lock_the_day(self):
        card = long_card(3)
        self.script = [(False, bot.TG_RETRYABLE, "boom")]
        ok, _summary, _err = self.deliver(card)
        self.assertFalse(ok)
        # 注意：ensure_shape 会预置 `last_morning_report_date: ""` 这个空键，
        # 所以不能断言「键不存在」，只能断言「值仍为空」。
        self.assertFalse(bot.load_state().get("last_morning_report_date"),
                         "发送失败绝不能写日期锁")

    def test_retry_only_sends_remaining_chunks(self):
        card = long_card(3)
        self.script = [(True, bot.TG_OK, ""),
                       (False, bot.TG_RETRYABLE, "timeout")]
        self.deliver(card)
        first_round = list(self.sent)
        self.assertEqual(len(first_round), 2)

        # 第二轮：全部恢复。第 1 段已在群里，绝不能再发一次。
        self.sent.clear()
        self.script = []
        ok, summary, _err = self.deliver(card)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 2, "只该补发第 2、3 段")
        self.assertEqual(summary["delivered_indexes"], [1, 2, 3])
        self.assertNotIn(first_round[0], self.sent, "第 1 段不得重发")

    def test_partial_then_all_delivered_writes_lock(self):
        card = long_card(3)
        self.script = [(True, bot.TG_OK, ""), (False, bot.TG_RETRYABLE, "x")]
        self.deliver(card)
        self.script = []
        ok, _summary, _err = self.deliver(card)
        self.assertTrue(ok)
        self.assertTrue(bot._lock_morning_sent(self.state, TODAY, "早报"))
        self.assertEqual(bot.load_state()["last_morning_report_date"], TODAY)


class TestFailureKinds(DeliveryBase):
    def test_429_retryable_then_success(self):
        """429 属于可重试：同一次运行内退避重试，成功后不留下失败记录。"""
        bot.TELEGRAM_RETRY_DELAYS = (0, 0, 0)
        card = long_card(2)
        self.script = [(False, bot.TG_RETRYABLE, "429 Too Many Requests"),
                       (True, bot.TG_OK, "")]
        ok, summary, _err = self.deliver(card)
        self.assertTrue(ok)
        self.assertEqual(summary["delivered_indexes"], [1, 2])
        self.assertEqual(len(self.sent), 3, "第 1 段一次失败 + 一次重试 + 第 2 段")

    def test_429_exhausted_is_retryable_not_failed(self):
        bot.TELEGRAM_RETRY_DELAYS = (0, 0, 0)
        card = long_card(2)
        self.script = [(False, bot.TG_RETRYABLE, "429")] * 4
        ok, _summary, _err = self.deliver(card)
        self.assertFalse(ok)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["chunks"][0]["delivery_status"], store.CHUNK_RETRYABLE)

    def test_read_timeout_marks_uncertain(self):
        """读超时 = 可能已送达。标 UNCERTAIN，下一轮绝不当成「没发过」重发。"""
        card = long_card(2)
        self.script = [(True, bot.TG_OK, ""),
                       (False, bot.TG_UNCERTAIN, "read timeout")]
        ok, summary, _err = self.deliver(card)
        self.assertFalse(ok)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["chunks"][1]["delivery_status"], store.CHUNK_UNCERTAIN)
        self.assertEqual(summary["delivered_indexes"], [1])

    def test_deterministic_failure_is_failed(self):
        card = long_card(2)
        self.script = [(False, bot.TG_DETERMINISTIC, "400 Bad Request")]
        ok, _summary, _err = self.deliver(card)
        self.assertFalse(ok)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["chunks"][0]["delivery_status"], store.CHUNK_FAILED)

    def test_missing_credentials_records_single_chunk(self):
        bot.TELEGRAM_BOT_TOKEN = ""
        ok, summary, err = self.deliver(long_card(2))
        self.assertFalse(ok)
        self.assertEqual(err, bot.MISSING_CREDENTIALS)
        self.assertEqual(summary["total"], 1)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["chunks"][0]["delivery_status"], store.CHUNK_RETRYABLE)


class TestInterruption(DeliveryBase):
    def test_killed_between_send_and_lock_does_not_resend(self):
        """模拟「最后一段发完、日期锁还没写」时进程被杀。

        下次启动看到 3 段全是 DELIVERED → 一段都不重发，直接补写锁。
        """
        card = long_card(3)
        ok, _summary, _err = self.deliver(card)
        self.assertTrue(ok)
        # 故意不写日期锁（模拟被杀）。空键是 ensure_shape 预置的，值为空即「未锁定」。
        self.assertFalse(bot.load_state().get("last_morning_report_date"))

        self.sent.clear()
        ok2, summary2, _err2 = self.deliver(card)
        self.assertTrue(ok2)
        self.assertEqual(self.sent, [], "全部已送达时不得再发任何一段")
        self.assertEqual(summary2["delivered_indexes"], [1, 2, 3])

    def test_killed_mid_send_resumes_from_next_chunk(self):
        """模拟「第 2 段发出去之前进程被杀」：第 1 段已记账，续传只发 2、3。"""
        card = long_card(3)
        self.script = [(True, bot.TG_OK, ""), (False, bot.TG_RETRYABLE, "killed")]
        self.deliver(card)
        self.sent.clear()
        self.script = []
        ok, _summary, _err = self.deliver(card)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 2)

    def test_state_write_failure_before_send_does_not_block(self):
        card = long_card(2)

        def boom(_state):
            raise store.StateError("模拟写盘失败")

        with mock.patch.object(bot, "save_state", side_effect=boom):
            ok, _summary, _err = self.deliver(card)
        self.assertTrue(ok, "记账失败不该阻断发送")


class TestTextContinuity(DeliveryBase):
    def test_reuses_stored_text_when_card_regenerated(self):
        """卡片被模型重新生成（内容有细微差异）时，必须沿用上次原文续传。

        否则分段哈希全变，断点续传失效 → 整条重发 → 读者收到重复内容。
        """
        first = long_card(3, width=2000)
        self.script = [(True, bot.TG_OK, ""), (False, bot.TG_RETRYABLE, "x")]
        self.deliver(first)
        sent_chunk1 = self.sent[0]

        # 第二次生成出来的卡片换了个说法（每个字都不同）
        second = "\n".join("报" * 2000 for _ in range(3))
        self.sent.clear()
        self.script = []
        ok, summary, _err = self.deliver(second)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 2, "只补发第 2、3 段")
        self.assertEqual(summary["delivered_indexes"], [1, 2, 3])
        # 第 2、3 段必须来自**旧原文**，不能一半旧一半新
        self.assertTrue(all("情" in t for t in self.sent), "续传内容必须来自上次原文")
        self.assertNotIn(sent_chunk1, self.sent)

    def test_yesterday_record_is_discarded(self):
        old = {
            "date": YESTERDAY, "chunks": [
                {"chunk_index": 1, "chunk_total": 3, "chunk_hash": "deadbeef",
                 "delivery_status": store.CHUNK_DELIVERED,
                 "telegram_message_id": 111, "attempts": 1, "last_error": ""}],
            "pending_text": "昨天的原文", "attempts": 1,
            "status": "PARTIALLY_DELIVERED", "updated_at": "",
        }
        self.state[bot.MORNING_DELIVERY_KEY] = old
        self.deliver(long_card(3))
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["date"], TODAY)
        self.assertEqual(len(self.sent), 3, "跨天必须整条重发，不能套用昨天的分段布局")

    def test_attempts_accumulate_across_runs(self):
        card = long_card(2)
        self.script = [(False, bot.TG_RETRYABLE, "x")]
        self.deliver(card)
        self.script = [(False, bot.TG_RETRYABLE, "x")]
        self.deliver(card)
        self.assertEqual(bot.load_state()[bot.MORNING_DELIVERY_KEY]["attempts"], 2)


class TestLockWriteFailure(DeliveryBase):
    def test_lock_returns_false_when_state_write_fails(self):
        def boom(_state):
            raise store.StateError("磁盘满")

        with mock.patch.object(bot, "save_state", side_effect=boom):
            self.assertFalse(bot._lock_morning_sent(self.state, TODAY, "早报"))


class TestMorningModeIntegration(DeliveryBase):
    """端到端：run_morning_mode 里分段失败时，日期锁必须不写、账本必须落盘。"""

    def _pool(self):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        return [{
            "title": f"Arsenal transfer update number {i}",
            "link": f"https://example.com/a{i}",
            "desc": "d", "source": "s", "feed_origin": "s",
            "published": (now - datetime.timedelta(hours=2)).isoformat(),
            "first_seen": now.isoformat(),
        } for i in range(8)]

    def _summary(self):
        # 正文做成两行 × 2400 字 ≈ 4800 字 → 会被切成 2 段
        body = "\n".join(["正文" * 1200] * 2)
        return {
            "has_valid_news": True, "note_title": "标题", "hook": "钩子一句话",
            "body": body, "tags": "#英超",
            "top_events": [{"rank": 1, "event_id": "E01",
                            "title": "测试事件", "one_line": "一句话"}],
            "fact_check_note": "截至 06:00", "cover_blueprint": "深色底",
            "image_prompt": "3d caricature style", "banner_overlay_text": "标题",
        }

    def _seed(self):
        store.save_state_atomic(store.ensure_shape({
            "processed_urls": [], "last_morning_report_date": "",
            "news_pool": self._pool(),
        }), self.state_path)

    def test_partial_chunk_failure_keeps_lock_unset(self):
        self._seed()
        self.script = [(True, bot.TG_OK, ""), (False, bot.TG_RETRYABLE, "boom")]
        with mock.patch.object(bot, "evaluate_summary",
                               return_value=(self._summary(), None)):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 1, "分段未全部送达必须返回非 0")
        reloaded = bot.load_state()
        self.assertEqual(reloaded.get("last_morning_report_date") or "", "",
                         "分段失败不得锁定当天早报")
        rec = reloaded.get(bot.MORNING_DELIVERY_KEY) or {}
        self.assertEqual(rec.get("status"), "PARTIALLY_DELIVERED")

    def test_full_success_sets_lock(self):
        self._seed()
        with mock.patch.object(bot, "evaluate_summary",
                               return_value=(self._summary(), None)):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        reloaded = bot.load_state()
        self.assertEqual(reloaded["last_morning_report_date"], bot._cn_date_str())
        self.assertEqual(reloaded[bot.MORNING_DELIVERY_KEY]["status"], "DELIVERED")

    def test_rerun_after_partial_does_not_duplicate(self):
        self._seed()
        self.script = [(True, bot.TG_OK, ""), (False, bot.TG_RETRYABLE, "boom")]
        with mock.patch.object(bot, "evaluate_summary",
                               return_value=(self._summary(), None)):
            bot.run_morning_mode(bot.load_state(), bot._now_iso())
        first = list(self.sent)
        self.assertEqual(len(first), 2)

        self.sent.clear()
        self.script = []
        with mock.patch.object(bot, "evaluate_summary",
                               return_value=(self._summary(), None)):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1, "只该补发第 2 段")
        self.assertNotIn(first[0], self.sent)


class TestMorningUsesLedgerPath(unittest.TestCase):
    """静态守卫：早报不得再退回无账本的 send_telegram()。"""

    def _src(self):
        path = os.path.join(ROOT, "bot.py")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def test_no_send_telegram_call_left_in_morning(self):
        src = self._src()
        self.assertNotIn("push_success = send_telegram(card)", src,
                         "早报必须走 _deliver_morning，不能再用无账本的 send_telegram")

    def test_morning_delivery_key_declared(self):
        self.assertEqual(bot.MORNING_DELIVERY_KEY, "morning_delivery")


class TestForceSummary(DeliveryBase):
    """FORCE_SUMMARY 人工强制补发必须真的发出去。

    账本的目的是「别重发已经在群里的段落」，而强制补发的诉求恰恰相反。
    两者冲突时人的指令赢——否则 FORCE_SUMMARY 会变成
    「记账显示已推送、群里什么都没有」，比原来的 bug 更难发现。
    """

    def test_normal_rerun_after_success_sends_nothing(self):
        card = long_card(3)
        ok, _s, _e = self.deliver(card)
        self.assertTrue(ok)
        self.sent.clear()
        ok2, summary2, _e2 = self.deliver(card)
        self.assertTrue(ok2)
        self.assertEqual(self.sent, [], "全部已送达且非强制补发时，一段都不该再发")
        self.assertTrue(summary2["all_delivered"])

    def test_force_resends_every_chunk(self):
        card = long_card(3)
        self.deliver(card)
        self.assertEqual(len(self.sent), 3)
        self.sent.clear()
        ok, summary, _e = self.deliver(card, force=True)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent), 3, "强制补发必须整份重发，不能被账本挡住")
        self.assertTrue(summary["all_delivered"])

    def test_force_resets_ledger_status(self):
        card = long_card(3)
        self.deliver(card)
        self.deliver(card, force=True)
        rec = bot.load_state()[bot.MORNING_DELIVERY_KEY]
        self.assertEqual(rec["status"], "DELIVERED")
        self.assertEqual(len(rec["chunks"]), 3, "强制补发后账本应只剩本轮的三段")
        self.assertEqual([c["chunk_index"] for c in rec["chunks"]], [1, 2, 3])

    def test_force_through_morning_mode_ignores_lock(self):
        """日期锁已写 + FORCE_SUMMARY=true：仍要重发，并保持锁有效。"""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        pool = [{
            "title": f"Arsenal transfer update number {i}",
            "link": f"https://example.com/a{i}",
            "desc": "d", "source": "s", "feed_origin": "s",
            "published": (now - datetime.timedelta(hours=2)).isoformat(),
            "first_seen": now.isoformat(),
        } for i in range(8)]
        summary_obj = {
            "has_valid_news": True, "note_title": "标题", "hook": "钩子一句话",
            "body": "\n".join(["正文" * 1200] * 2), "tags": "#英超",
            "top_events": [{"rank": 1, "event_id": "E01",
                            "title": "测试事件", "one_line": "一句话"}],
            "fact_check_note": "截至 06:00", "cover_blueprint": "深色底",
            "image_prompt": "3d caricature style", "banner_overlay_text": "标题",
        }
        store.save_state_atomic(store.ensure_shape({
            "processed_urls": [],
            "last_morning_report_date": bot._cn_date_str(),
            "news_pool": pool,
        }), self.state_path)

        with mock.patch.object(bot, "evaluate_summary",
                               return_value=(summary_obj, None)), \
             mock.patch.object(bot, "FORCE_SUMMARY", True):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 2, "FORCE_SUMMARY 必须真的发出分段")
        self.assertEqual(bot.load_state()["last_morning_report_date"],
                         bot._cn_date_str())


class TestMergeState(DeliveryBase):
    """CI 里 state.json 冲突会走 merge_state.py，账本必须能正确合并。"""

    def _chunk(self, idx, status, mid=None, h="h"):
        return {"chunk_index": idx, "chunk_total": 3, "chunk_hash": h,
                "delivery_status": status, "telegram_message_id": mid,
                "attempts": 1, "last_error": ""}

    def test_delivered_chunk_never_downgraded(self):
        local = {"date": TODAY, "pending_text": "X", "chunks": [
            self._chunk(1, store.CHUNK_DELIVERED, 111)], "status": "PARTIALLY_DELIVERED"}
        remote = {"date": TODAY, "pending_text": "X", "chunks": [
            self._chunk(1, store.CHUNK_RETRYABLE)], "status": "DELIVERY_FAILED"}
        out = merge_state._merge_morning_delivery(local, remote)
        self.assertEqual(out["chunks"][0]["delivery_status"], store.CHUNK_DELIVERED)
        self.assertEqual(out["chunks"][0]["telegram_message_id"], 111)

    def test_cross_day_takes_later(self):
        local = {"date": YESTERDAY, "pending_text": "old", "chunks": []}
        remote = {"date": TODAY, "pending_text": "new", "chunks": []}
        self.assertEqual(merge_state._merge_morning_delivery(local, remote)["date"], TODAY)
        self.assertEqual(merge_state._merge_morning_delivery(remote, local)["date"], TODAY)

    def test_different_text_takes_more_progress(self):
        local = {"date": TODAY, "pending_text": "A", "updated_at": "2026-11-01T01:00:00Z",
                 "chunks": [self._chunk(1, store.CHUNK_DELIVERED, 1)],
                 "status": "PARTIALLY_DELIVERED"}
        remote = {"date": TODAY, "pending_text": "B", "updated_at": "2026-11-01T02:00:00Z",
                  "chunks": [self._chunk(1, store.CHUNK_RETRYABLE)],
                  "status": "DELIVERY_FAILED"}
        out = merge_state._merge_morning_delivery(local, remote)
        self.assertEqual(out["pending_text"], "A", "原文不同时取进度更靠前的那份整份")

    def test_merge_keeps_morning_delivery_key(self):
        local = {"events": {}, "processed_urls": [], "news_pool": [],
                 "morning_delivery": {"date": YESTERDAY, "chunks": [], "pending_text": ""}}
        remote = {"events": {}, "processed_urls": [], "news_pool": [],
                  "morning_delivery": {"date": TODAY, "chunks": [], "pending_text": ""}}
        out = merge_state.merge(local, remote)
        self.assertEqual(out["morning_delivery"]["date"], TODAY,
                         "远端的新账本不能被本地旧记录盖掉")

    def test_merge_without_key_on_either_side(self):
        out = merge_state.merge({"events": {}}, {"events": {}})
        self.assertNotIn("morning_delivery", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
