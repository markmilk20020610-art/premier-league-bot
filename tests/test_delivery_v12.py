"""V1.2 第三批验收：Telegram 投递安全与恢复（任务 8 / 9 / 10 / 11）。

覆盖四件事：
  8. Token 绝不进日志（stdout / stderr / run_telemetry.log / state.last_error）
  9. 投递结果必须分清「确定失败 / 可重试 / 结果未知」，不能一律当失败
 10. 失败的新闻必须能重新排队，不能因为「失败」被永久去重
 11. 长消息分段独立记账：一段成功不重发，部分成功不冒充全部成功
"""
import contextlib
import datetime
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _stubs  # noqa: E402

_stubs.install()

import requests  # noqa: E402

import bot  # noqa: E402
import redact  # noqa: E402
import state_store as store  # noqa: E402

BBC = "https://www.bbc.co.uk/sport/football/articles/c123"
SKY = "https://www.skysports.com/football/news/11095/arsenal"
BBC_TITLE = "Arsenal lose appeal against Dowman red card"
SKY_TITLE = "FA rejects Arsenal appeal over Dowman dismissal"

TEST_TOKEN = "TEST_TOKEN"


# ==================== 假 Telegram ====================
class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakePost:
    """按脚本依次返回：Exception 实例 → 抛出；否则 (status, payload)。

    脚本用尽后重复最后一项（而不是悄悄变成成功），
    否则「一直失败」的场景会被默认成功掩盖掉。
    """

    def __init__(self, script=None, default=None):
        self.script = list(script or [])
        if default is not None:
            self.fallback = default
        elif self.script:
            self.fallback = self.script[-1]
        else:
            self.fallback = ok_resp(1)
        self.calls = []

    def __call__(self, url, json=None, timeout=None, **kwargs):  # noqa: A002
        self.calls.append({"url": url, "text": (json or {}).get("text", "")})
        item = self.script.pop(0) if self.script else self.fallback
        if isinstance(item, BaseException):
            raise item
        status, payload = item
        return FakeResp(status, payload)

    @property
    def count(self):
        return len(self.calls)


def ok_resp(mid=1):
    return (200, {"ok": True, "result": {"message_id": mid}})


def base_result(**over):
    data = {
        "has_valid_news": True,
        "var_rating": "⚡【闪电突袭·跟队独家】(Tier 2)",
        "var_reason": "跟队记者爆料",
        "event_title": "阿森纳就红牌提出申诉",
        "event_time": "2小时前",
        "fact_summary": "英足总确认收到阿森纳关于道曼红牌的正式申诉。",
        "evidence_url": BBC,
        "draft_a": "你站哪一边？这次红牌到底冤不冤？",
        "draft_b": "英足总官方公告已确认收到申诉，BBC 与天空体育同步报道。",
        "draft_c": "从纪律条例看，申诉窗口只有 48 小时，逾期不予受理。",
        "cover_blueprint": "深色底 + 红色警示条",
        "image_prompt": "3d caricature style referee holding a red card",
        "banner_overlay_text": "申诉窗口",
        "grounding_records": [
            {"url": BBC, "title": BBC_TITLE},
            {"url": SKY, "title": SKY_TITLE},
        ],
        "verification_status": "GEMINI_GROUNDED",
    }
    data.update(over)
    return data


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")

        self._saved = (bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID)
        bot.STATE_FILE = self.state_path
        bot.LOG_FILE = os.path.join(self.dir, "run_telemetry.log")
        bot.TELEGRAM_BOT_TOKEN = TEST_TOKEN
        bot.TELEGRAM_CHAT_ID = "123456"

        # 重试退避 2/4/8 秒，测试里不能真等
        self._sleep = mock.patch("time.sleep", return_value=None)
        self._sleep.start()

        self.state = store.ensure_shape({})

    def tearDown(self):
        self._sleep.stop()
        bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID = self._saved
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def patch_post(self, fake):
        p = mock.patch.object(requests, "post", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def read_log(self):
        if not os.path.exists(bot.LOG_FILE):
            return ""
        with open(bot.LOG_FILE, "r", encoding="utf-8") as f:
            return f.read()

    def new_event(self, url=BBC, title="Arsenal news", status=store.VERIFIED):
        event = store.record_event(self.state, url, title, "MULTIPLE_INDEPENDENT_SOURCES", status)
        return event


# ==================== 任务 8：Token 日志脱敏 ====================
class TestTask8TokenRedaction(DeliveryBase):
    LEAKY = f"failed request https://api.telegram.org/bot{TEST_TOKEN}/sendMessage"

    def test_token_absent_from_stdout_stderr_and_error(self):
        fake = self.patch_post(FakePost([requests.exceptions.ConnectionError(self.LEAKY)]))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ok, ids, reason = bot.send_telegram_ex("hello")
        self.assertFalse(ok)
        self.assertEqual(ids, [])
        self.assertGreaterEqual(fake.count, 1)
        for blob, label in ((out.getvalue(), "stdout"), (err.getvalue(), "stderr"), (reason, "返回值")):
            self.assertNotIn(TEST_TOKEN, blob, f"{label} 泄露了 Token")
            self.assertNotIn(f"bot{TEST_TOKEN}", blob, f"{label} 泄露了 Bot API URL")

    def test_token_absent_from_telemetry_log(self):
        self.patch_post(FakePost([requests.exceptions.ConnectionError(self.LEAKY)]))
        with contextlib.redirect_stdout(io.StringIO()):
            _ok, _ids, reason = bot.send_telegram_ex("hello")
            bot.log_telemetry({"status": "PUSH_FAILED", "error": reason, "raw": self.LEAKY})
        log = self.read_log()
        self.assertIn("PUSH_FAILED", log, "遥测必须照常写入，只是去掉凭证")
        self.assertNotIn(TEST_TOKEN, log)

    def test_token_absent_from_state_last_error(self):
        event = self.new_event()
        store.begin_delivery(event)
        store.mark_failed(event, f"请求异常 ConnectionError: {self.LEAKY}")
        self.assertNotIn(TEST_TOKEN, event["last_error"])
        self.assertIn("redacted", event["last_error"])

    def test_sanitizer_keeps_diagnostic_context(self):
        """脱敏不能把日志变成一团空白——还得能看出是哪个块、什么错。"""
        raw = f"分块 1/3 第 2 次失败: ConnectionError({self.LEAKY})"
        clean = redact.sanitize(raw)
        self.assertIn("分块 1/3", clean)
        self.assertIn("第 2 次失败", clean)
        self.assertIn("ConnectionError", clean)
        self.assertNotIn(TEST_TOKEN, clean)

    def test_sanitizer_covers_other_secret_shapes(self):
        cases = [
            "Authorization: Bearer abcdefghijklmnop1234",
            "key is sk-abcdefghijklmnopqrstuvwx",
            "https://example.com/x?token=supersecretvalue&a=1",
        ]
        for raw in cases:
            clean = redact.sanitize(raw)
            for secret in ("abcdefghijklmnop1234", "sk-abcdefghijklmnopqrstuvwx",
                           "supersecretvalue"):
                self.assertNotIn(secret, clean, f"{raw} 未脱敏")

    def test_token_not_leaked_through_real_pipeline(self):
        """端到端：run_scan_mode 走真实推送路径时，Token 也不能进日志/state。"""
        self.patch_post(FakePost([requests.exceptions.ConnectionError(self.LEAKY)] * 8))
        item = {
            "title": "Arsenal transfer update number 0",
            "link": "https://example.com/news/0",
            "desc": "desc", "source": "s",
            "published": datetime.datetime.now(datetime.timezone.utc),
        }
        out = io.StringIO()
        with mock.patch.object(bot, "fetch_rss_sources", return_value=([item], {})), \
                mock.patch.object(bot, "evaluate_with_gemini", return_value=(base_result(), None)), \
                contextlib.redirect_stdout(out):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        printed = out.getvalue()
        self.assertNotIn(TEST_TOKEN, printed, "run_scan_mode 输出泄露了 Token")
        self.assertNotIn(TEST_TOKEN, self.read_log(), "run_telemetry.log 泄露了 Token")
        with open(self.state_path, "r", encoding="utf-8") as f:
            self.assertNotIn(TEST_TOKEN, f.read(), "state.json 泄露了 Token")


# ==================== 任务 9：投递结果必须分类 ====================
class TestTask9DeliveryOutcome(DeliveryBase):
    def test_500_is_retryable_not_failed(self):
        self.patch_post(FakePost([(500, {"ok": False, "description": "internal"})] * 8))
        event = self.new_event()
        store.begin_delivery(event)
        ok, _ids, _err = bot._deliver_card(self.state, event, "hi")
        self.assertFalse(ok)
        self.assertEqual(event["delivery_status"], store.RETRYABLE_FAILURE)
        self.assertTrue(event["next_retry_at"], "可重试失败必须排重试时间")
        self.assertEqual(event["chunks"][0]["delivery_status"], store.CHUNK_RETRYABLE)

    def test_403_is_deterministic_failure(self):
        self.patch_post(FakePost([(403, {"ok": False, "description": "bot was blocked"})] * 4))
        event = self.new_event()
        store.begin_delivery(event)
        ok, _ids, _err = bot._deliver_card(self.state, event, "hi")
        self.assertFalse(ok)
        self.assertEqual(event["delivery_status"], store.DELIVERY_FAILED)
        self.assertEqual(event["next_retry_at"], "", "确定性失败不该排重试")

    def test_read_timeout_is_uncertain(self):
        """读超时说明请求已发出，消息可能已经进群 → 不能当确定失败。"""
        self.patch_post(FakePost([requests.exceptions.ReadTimeout("read timed out")] * 8))
        event = self.new_event()
        store.begin_delivery(event)
        ok, _ids, _err = bot._deliver_card(self.state, event, "hi")
        self.assertFalse(ok)
        self.assertEqual(event["delivery_status"], store.DELIVERY_UNCERTAIN)
        self.assertEqual(event["chunks"][0]["delivery_status"], store.CHUNK_UNCERTAIN)

    def test_connect_timeout_is_retryable(self):
        """连接都没建立 → 确定没送达，可以放心重试。"""
        self.patch_post(FakePost([requests.exceptions.ConnectTimeout("connect timed out")] * 8))
        event = self.new_event()
        store.begin_delivery(event)
        bot._deliver_card(self.state, event, "hi")
        self.assertEqual(event["delivery_status"], store.RETRYABLE_FAILURE)

    def test_uncertain_goes_to_recovery_queue_not_retry(self):
        self.patch_post(FakePost([requests.exceptions.ReadTimeout("read timed out")] * 8))
        event = self.new_event()
        store.begin_delivery(event)
        bot._deliver_card(self.state, event, "hi")

        self.assertIn(event, store.uncertain_queue(self.state))
        actions = {a["event_id"]: a["action"] for a in store.recovery_actions(self.state)}
        self.assertEqual(actions[event["event_id"]], "UNCERTAIN_REPORT")

    def test_startup_reports_uncertain_queue(self):
        """启动时结果未知的事件必须被明确报出来，不能静默留着。"""
        event = self.new_event(title="Chelsea keeper injury")
        store.begin_delivery(event)
        store.mark_uncertain(event, "读超时，结果未知")
        store.save_state_atomic(self.state, self.state_path)

        out = io.StringIO()
        with mock.patch.object(bot, "run_scan_mode", return_value=0), contextlib.redirect_stdout(out):
            bot.main()
        printed = out.getvalue()
        self.assertIn("Delivery Uncertain", printed)
        self.assertIn(event["event_id"], printed)
        # 报告之后状态不变：仍然未知，仍然不重发
        self.assertEqual(bot.load_state()["events"][event["event_id"]]["delivery_status"],
                         store.DELIVERY_UNCERTAIN)

    def test_stuck_pending_becomes_uncertain_not_failed(self):
        """上次发到一半进程被杀：可能已送达，必须记 UNCERTAIN。"""
        event = self.new_event()
        store.begin_delivery(event)
        store.apply_recovery(self.state)
        self.assertEqual(event["delivery_status"], store.DELIVERY_UNCERTAIN)

    def test_retry_backoff_grows_with_attempts(self):
        self.assertEqual(store.retry_delay_minutes(1), store.RETRY_BACKOFF_MINUTES[0])
        self.assertEqual(store.retry_delay_minutes(2), store.RETRY_BACKOFF_MINUTES[1])
        self.assertEqual(store.retry_delay_minutes(99), store.RETRY_BACKOFF_MINUTES[-1])

    def test_missing_credentials_is_deterministic(self):
        """凭证没配不是「可重试」——无限重试只会把日志刷满。"""
        bot.TELEGRAM_BOT_TOKEN = ""
        event = self.new_event()
        store.begin_delivery(event)
        ok, _ids, err = bot._deliver_card(self.state, event, "hi")
        self.assertFalse(ok)
        self.assertEqual(err, bot.MISSING_CREDENTIALS)
        self.assertEqual(event["delivery_status"], store.DELIVERY_FAILED)


# ==================== 任务 10：失败新闻不得被永久去重 ====================
class TestTask10FailedNewsRequeue(DeliveryBase):
    def _item(self, i=0, title="Arsenal transfer update number 0"):
        return {
            "title": title,
            "link": f"https://example.com/news/{i}",
            "desc": "desc", "source": "s",
            "published": datetime.datetime.now(datetime.timezone.utc),
        }

    def _seed_event(self, item, status):
        event = store.record_event(self.state, item["link"], item["title"], "SINGLE_SOURCE", store.VERIFIED)
        if status == store.COMMITTED:
            store.begin_delivery(event)
            store.mark_delivered(event, 1)
            store.transition(event, store.COMMITTED)
        elif status == store.RETRYABLE_FAILURE:
            store.begin_delivery(event)
            store.mark_retryable(event, "HTTP 500")
        elif status == store.DELIVERY_UNCERTAIN:
            store.begin_delivery(event)
            store.mark_uncertain(event, "读超时")
        elif status == store.DELIVERY_FAILED:
            store.begin_delivery(event)
            store.mark_failed(event, "HTTP 403")
        return event

    def test_committed_news_is_not_reprocessed(self):
        item = self._item()
        self._seed_event(item, store.COMMITTED)
        candidates, _prep = bot._prepare_ai_candidates(self.state, [item])
        self.assertEqual(candidates, [])

    def test_retryable_failure_reenters_pool_when_due(self):
        item = self._item()
        event = self._seed_event(item, store.RETRYABLE_FAILURE)
        event["next_retry_at"] = (datetime.datetime.now(datetime.timezone.utc)
                                  - datetime.timedelta(minutes=1)).isoformat()
        candidates, prep = bot._prepare_ai_candidates(self.state, [item])
        self.assertEqual(len(candidates), 1, "到点的可重试失败必须重新排队")
        self.assertEqual(prep["revived_retry"], 1)

    def test_retryable_failure_waits_before_backoff_expires(self):
        item = self._item()
        self._seed_event(item, store.RETRYABLE_FAILURE)
        candidates, prep = bot._prepare_ai_candidates(self.state, [item])
        self.assertEqual(candidates, [], "退避期内不该重投")
        self.assertEqual(prep["waiting_retry"], 1)

    def test_uncertain_is_blocked_and_counted(self):
        item = self._item()
        self._seed_event(item, store.DELIVERY_UNCERTAIN)
        candidates, prep = bot._prepare_ai_candidates(self.state, [item])
        self.assertEqual(candidates, [], "结果未知不得自动重发")
        self.assertEqual(prep["blocked_uncertain"], 1)

    def test_delivery_failed_gives_up_after_max_attempts(self):
        item = self._item()
        event = self._seed_event(item, store.DELIVERY_FAILED)
        self.assertFalse(store.blocks_rediscovery(event), "确定性失败也应给修复凭证后的机会")
        event["attempts"] = store.MAX_DELIVERY_ATTEMPTS
        self.assertTrue(store.blocks_rediscovery(event), "超过上限必须彻底放弃")

    def test_end_to_end_500_then_recovered_next_round(self):
        """500 → RETRYABLE_FAILURE → 下一轮真的把同一条新闻发出去了。"""
        item = self._item()

        fake_fail = self.patch_post(FakePost([(500, {"ok": False})] * 8))
        with mock.patch.object(bot, "fetch_rss_sources", return_value=([item], {})), \
                mock.patch.object(bot, "evaluate_with_gemini", return_value=(base_result(), None)), \
                contextlib.redirect_stdout(io.StringIO()):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        state = bot.load_state()
        events = list(state["events"].values())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["delivery_status"], store.RETRYABLE_FAILURE)
        self.assertGreater(fake_fail.count, 0)

        # 模拟退避到期
        events[0]["next_retry_at"] = (datetime.datetime.now(datetime.timezone.utc)
                                      - datetime.timedelta(minutes=1)).isoformat()
        store.save_state_atomic(state, self.state_path)

        fake_ok = self.patch_post(FakePost([ok_resp(555)]))
        with mock.patch.object(bot, "fetch_rss_sources", return_value=([item], {})), \
                mock.patch.object(bot, "evaluate_with_gemini", return_value=(base_result(), None)), \
                contextlib.redirect_stdout(io.StringIO()):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertGreater(fake_ok.count, 0, "退避到期后必须重新投递")
        state = bot.load_state()
        event = list(state["events"].values())[0]
        self.assertEqual(event["delivery_status"], store.COMMITTED)
        self.assertEqual(event["telegram_message_id"], 555)


# ==================== 任务 11：分段投递记录 ====================
def long_card(paragraphs=40, width=150):
    return "\n".join(f"<b>第 {i} 段</b> " + "情" * width for i in range(paragraphs))


class TestTask11ChunkRecords(DeliveryBase):
    def test_long_message_splits_into_multiple_chunks(self):
        chunks = bot._split_html(long_card(), 4000)
        self.assertGreater(len(chunks), 1, "测试素材必须真的会被切段")

    def test_partial_success_never_reported_as_full(self):
        text = long_card()
        total = len(bot._split_html(text, 4000))
        # 第 1 段成功，第 2 段 400（确定性失败，不再重试）
        script = [ok_resp(11)] + [(400, {"ok": False, "description": "can't parse entities"})]
        self.patch_post(FakePost(script))
        event = self.new_event()
        store.begin_delivery(event)

        ok, ids, _err = bot._deliver_card(self.state, event, text)
        self.assertFalse(ok, "部分成功不得返回 True")
        self.assertEqual(ids, [11])

        chunks = event["chunks"]
        self.assertEqual(len(chunks), total)
        self.assertEqual(chunks[0]["delivery_status"], store.CHUNK_DELIVERED)
        self.assertEqual(chunks[0]["telegram_message_id"], 11)
        self.assertEqual(chunks[1]["delivery_status"], store.CHUNK_FAILED)
        self.assertEqual([c["delivery_status"] for c in chunks[2:]], [store.CHUNK_NOT_ATTEMPTED] * (total - 2))

        summary = store.delivery_summary(event)
        self.assertTrue(summary["partial"])
        self.assertFalse(summary["all_delivered"])
        self.assertEqual(summary["delivered_indexes"], [1])
        self.assertEqual(event["delivery_status"], store.DELIVERY_FAILED)

    def test_delivered_chunk_is_never_resent(self):
        text = long_card()
        total = len(bot._split_html(text, 4000))
        self.assertGreaterEqual(total, 2)

        # 上一次：第 1 段已送达，第 2 段可重试失败
        prior = [
            {"chunk_index": 1, "chunk_total": total,
             "chunk_hash": bot._chunk_hash(bot._split_html(text, 4000)[0]),
             "delivery_status": store.CHUNK_DELIVERED, "telegram_message_id": 77,
             "attempts": 1, "last_error": ""},
            {"chunk_index": 2, "chunk_total": total,
             "chunk_hash": bot._chunk_hash(bot._split_html(text, 4000)[1]),
             "delivery_status": store.CHUNK_RETRYABLE, "telegram_message_id": None,
             "attempts": 4, "last_error": "HTTP 500"},
        ]
        fake = self.patch_post(FakePost([ok_resp(88)]))
        report = []
        ok, ids, _err = bot.send_telegram_ex(text, prior, report)

        self.assertTrue(ok)
        self.assertEqual(fake.count, total - 1, "已送达的段不得重发，只补发剩下的")
        self.assertEqual(report[0]["delivery_status"], store.CHUNK_DELIVERED)
        self.assertEqual(report[0]["telegram_message_id"], 77)
        self.assertIn(77, ids)

    def test_chunk_layout_change_disables_resume(self):
        text = long_card()
        total = len(bot._split_html(text, 4000))
        prior = [{"chunk_index": 1, "chunk_total": total + 5,
                  "chunk_hash": "deadbeef",
                  "delivery_status": store.CHUNK_DELIVERED, "telegram_message_id": 99,
                  "attempts": 1, "last_error": ""}]
        fake = self.patch_post(FakePost([ok_resp(1)] * (total + 2)))
        report = []
        ok, _ids, _err = bot.send_telegram_ex(text, prior, report)
        self.assertTrue(ok)
        self.assertEqual(fake.count, total, "分段布局变了就不能沿用旧记录")

    def test_record_chunks_never_downgrades_delivered(self):
        event = self.new_event()
        store.record_chunks(event, [{
            "chunk_index": 1, "chunk_total": 2, "chunk_hash": "h1",
            "delivery_status": store.CHUNK_DELIVERED, "telegram_message_id": 5,
            "attempts": 1, "last_error": "",
        }])
        store.record_chunks(event, [{
            "chunk_index": 1, "chunk_total": 2, "chunk_hash": "h1",
            "delivery_status": store.CHUNK_FAILED, "telegram_message_id": None,
            "attempts": 2, "last_error": "HTTP 400",
        }])
        self.assertEqual(event["chunks"][0]["delivery_status"], store.CHUNK_DELIVERED)
        self.assertEqual(event["chunks"][0]["telegram_message_id"], 5)

    def test_all_chunks_delivered_marks_event_delivered(self):
        text = long_card()
        self.patch_post(FakePost([ok_resp(i) for i in range(1, 20)]))
        event = self.new_event()
        store.begin_delivery(event)
        ok, ids, _err = bot._deliver_card(self.state, event, text)
        self.assertTrue(ok)
        self.assertEqual(event["delivery_status"], store.DELIVERED)
        self.assertEqual(event["telegram_message_id"], 1)
        self.assertTrue(store.delivery_summary(event)["all_delivered"])
        self.assertEqual(event["pending_text"], "", "送达后不应再占着原文")

    def test_retry_reuses_original_text_so_no_duplicate_chunk(self):
        """下一轮模型会重新写卡片，续传必须沿用原文，否则分段对不上只能整条重发。"""
        text = long_card()
        chunks = bot._split_html(text, 4000)
        total = len(chunks)

        self.patch_post(FakePost([ok_resp(11)] + [(500, {"ok": False})] * 4))
        event = self.new_event()
        store.begin_delivery(event)
        bot._deliver_card(self.state, event, text)
        self.assertEqual(event["delivery_status"], store.RETRYABLE_FAILURE)
        self.assertEqual(event["chunks"][0]["delivery_status"], store.CHUNK_DELIVERED)
        self.assertTrue(event["pending_text"], "失败后必须保留原文以便续传")

        fake = self.patch_post(FakePost([ok_resp(22)]))
        store.begin_delivery(event)          # RETRYABLE_FAILURE → PENDING_DELIVERY
        ok, ids, _err = bot._deliver_card(self.state, event, text + "\n<b>补充一句</b>")

        self.assertTrue(ok)
        self.assertEqual(fake.count, total - 1, "只补发未送达的段")
        self.assertEqual(fake.calls[0]["text"], chunks[1], "必须沿用上次原文续传")
        self.assertEqual(ids[0], 11, "已送达段的 message_id 仍要留在记录里")
        self.assertEqual(event["pending_text"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
