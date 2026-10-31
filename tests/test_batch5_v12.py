"""V1.2 第五批验收：事件级去重 / 真实请求统计 / 429 限流 / 退出码（任务 14–17）。"""
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

import ai_provider as ai  # noqa: E402
import bot  # noqa: E402
import evidence as ev  # noqa: E402
import state_store as store  # noqa: E402

BBC = "https://www.bbc.co.uk/sport/football/articles/c123"
SKY = "https://www.skysports.com/football/news/11095/arsenal"
BBC_TITLE = "Arsenal lose appeal against Dowman red card"
SKY_TITLE = "FA rejects Arsenal appeal over Dowman dismissal"


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def item(i=0, title=None, link=None, age_hours=1.0):
    return {
        "title": title or f"Arsenal transfer update number {i}",
        "link": link or f"https://example.com/news/{i}",
        "desc": "desc", "source": "s",
        "published": now() - datetime.timedelta(hours=age_hours),
    }


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


# ==================== 任务 14：事件级去重 ====================
class TestTask14EventLevelDedupe(unittest.TestCase):
    def setUp(self):
        self.state = store.ensure_shape({})

    def _delivered(self, url, article_title, event_title="事件"):
        event = store.record_event(self.state, url, event_title, "MULTIPLE_INDEPENDENT_SOURCES",
                                   store.VERIFIED, article_title=article_title)
        store.begin_delivery(event)
        store.mark_delivered(event, 1)
        store.transition(event, store.COMMITTED)
        return event

    def test_same_story_other_outlet_is_found(self):
        event = self._delivered(BBC, BBC_TITLE)
        found = bot._find_same_event(self.state, SKY_TITLE, SKY,
                                     statuses=bot.BLOCKING_DELIVERY_STATUSES)
        self.assertIs(found, event, "同一件事换个媒体必须能认出来")

    def test_different_club_not_merged(self):
        self._delivered(BBC, "Arsenal sign striker Viktor Gyokeres")
        found = bot._find_same_event(self.state, "Chelsea sign striker Viktor Gyokeres",
                                     "https://x.com/2", statuses=bot.BLOCKING_DELIVERY_STATUSES)
        self.assertIsNone(found)

    def test_different_action_not_merged(self):
        self._delivered(BBC, "Arsenal sign striker Viktor Gyokeres")
        found = bot._find_same_event(self.state, "Arsenal sack manager Mikel Arteta",
                                     "https://x.com/2", statuses=bot.BLOCKING_DELIVERY_STATUSES)
        self.assertIsNone(found)

    def test_different_score_is_not_same_match(self):
        """比分不同就是两场不同的比赛，不能被当成同一件事拦掉。"""
        self._delivered(BBC, "Arsenal beat Chelsea 2-1 at Emirates")
        found = bot._find_same_event(self.state, "Arsenal beat Chelsea 3-0 at Emirates",
                                     "https://x.com/2", statuses=bot.BLOCKING_DELIVERY_STATUSES)
        self.assertIsNone(found)

    def test_same_score_still_merged(self):
        self._delivered(BBC, "Arsenal beat Chelsea 2-1 at Emirates")
        found = bot._find_same_event(self.state, "Chelsea 2-1 Arsenal: report",
                                     "https://x.com/2", statuses=bot.BLOCKING_DELIVERY_STATUSES)
        self.assertIsNotNone(found)

    def test_candidate_from_other_outlet_is_dropped(self):
        self._delivered(BBC, BBC_TITLE)
        candidates, prep = bot._prepare_ai_candidates(self.state, [item(0, title=SKY_TITLE, link=SKY)])
        self.assertEqual(candidates, [])
        self.assertEqual(prep["dropped_same_event"], 1)

    def test_retryable_event_does_not_block_candidate(self):
        """可重试失败的事件不能被当成「已投递」拦掉，否则失败的新闻又发不出去了。"""
        event = store.record_event(self.state, BBC, "事件", "SINGLE_SOURCE", store.VERIFIED,
                                   article_title=BBC_TITLE)
        store.begin_delivery(event)
        store.mark_retryable(event, "HTTP 500")
        event["next_retry_at"] = (now() - datetime.timedelta(minutes=1)).isoformat()

        candidates, prep = bot._prepare_ai_candidates(self.state, [item(0, title=SKY_TITLE, link=SKY)])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(prep["dropped_same_event"], 0)

    def test_record_event_reuses_given_event_id(self):
        first = store.record_event(self.state, BBC, "事件", "SINGLE_SOURCE", store.VERIFIED,
                                   article_title=BBC_TITLE)
        second = store.record_event(self.state, SKY, "事件", "SINGLE_SOURCE", store.VERIFIED,
                                    event_id=first["event_id"], article_title=SKY_TITLE)
        self.assertIs(second, first)
        self.assertEqual(len(self.state["events"]), 1, "同一件事不得占两条记录")

    def test_evidence_title_is_extracted_for_dedupe(self):
        ok, data, _reason, _code = bot._finalize_result(base_result(), [])
        self.assertTrue(ok)
        self.assertEqual(data["evidence_title"], BBC_TITLE,
                         "必须留下英文原始标题，否则事件级去重拿中文标题比对必然失败")


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self._saved = (bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID)
        bot.STATE_FILE = self.state_path
        bot.LOG_FILE = os.path.join(self.dir, "run_telemetry.log")
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        self._sleep = mock.patch("time.sleep", return_value=None)
        self._sleep.start()

    def tearDown(self):
        self._sleep.stop()
        bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID = self._saved
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_state(self, state):
        store.save_state_atomic(store.ensure_shape(state), self.state_path)


class TestTask14PipelineDedupe(DeliveryBase):
    def test_second_outlet_is_not_pushed_again(self):
        state = store.ensure_shape({})
        event = store.record_event(state, BBC, "阿森纳就红牌提出申诉",
                                   "MULTIPLE_INDEPENDENT_SOURCES", store.VERIFIED,
                                   article_title=BBC_TITLE)
        store.begin_delivery(event)
        store.mark_delivered(event, 900)
        store.transition(event, store.COMMITTED)
        self.write_state(state)

        sent = []
        with mock.patch.object(bot, "fetch_rss_sources",
                               return_value=([item(0, title=SKY_TITLE, link=SKY)], {"https://f/rss": 200})), \
                mock.patch.object(bot, "evaluate_with_gemini", return_value=(base_result(), None)), \
                mock.patch.object(bot, "send_telegram_ex",
                                  side_effect=lambda t, *a, **k: (sent.append(t), (True, [1], ""))[1]), \
                contextlib_redirect():
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0)
        self.assertEqual(sent, [], "同一件事换了家媒体，不得再推一次")


class contextlib_redirect:
    """把 stdout 吞掉，避免测试输出被用例日志淹没。"""

    def __enter__(self):
        import contextlib
        self._ctx = contextlib.redirect_stdout(io.StringIO())
        self._ctx.__enter__()
        return self

    def __exit__(self, *exc):
        return self._ctx.__exit__(*exc)


# ==================== 任务 15：真实 API 请求统计 ====================
class Resp:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        return self._payload


def ok_payload():
    return {
        "choices": [{"message": {"content": json.dumps({"has_valid_news": True}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


class SeqHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append({"url": url})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get(self, url, **kw):
        self.calls.append({"url": url})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def make_router(state, http, **cfg_over):
    cfg = ai.AIConfig(**cfg_over)
    providers = ai.build_providers(cfg, http=http, sleep=lambda s: None)
    providers["deepseek"].api_key = "test-key"
    return ai.AIRouter(state, config=cfg, providers=providers), providers


class TestTask15RequestStats(unittest.TestCase):
    def test_retries_are_counted_as_real_requests(self):
        http = SeqHTTP([Resp(500, text="boom"), Resp(200, ok_payload())])
        router, _p = make_router({}, http)
        res = router.analyze_news("prompt")
        self.assertTrue(res.ok)
        self.assertEqual(res.http_requests, 2, "一次逻辑调用打了 2 次 HTTP")
        stats = ai.stats_snapshot(router.state)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["requests"], 2, "requests 必须反映真实请求数")
        self.assertEqual(stats["by_provider"]["deepseek"]["requests"], 2)

    def test_single_request_counts_once(self):
        http = SeqHTTP([Resp(200, ok_payload())])
        router, _p = make_router({}, http)
        router.analyze_news("prompt")
        stats = ai.stats_snapshot(router.state)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["requests"], 1)

    def test_old_state_without_requests_field_is_upgraded(self):
        state = {"ai_stats": {"date": ai.datetime.datetime.now(
            ai.datetime.timezone.utc).astimezone(
            ai.datetime.timezone(ai.datetime.timedelta(hours=8))).strftime("%Y-%m-%d"),
            "calls": 5, "errors": 0, "input_tokens": 1, "output_tokens": 1, "by_provider": {}}}
        http = SeqHTTP([Resp(200, ok_payload())])
        router, _p = make_router(state, http)
        router.analyze_news("prompt")
        stats = ai.stats_snapshot(state)
        self.assertEqual(stats["calls"], 6)
        self.assertEqual(stats["requests"], 1)

    def test_disabled_provider_makes_no_request(self):
        http = SeqHTTP([Resp(200, ok_payload())])
        router, providers = make_router({}, http)
        providers["deepseek"].api_key = ""
        res = providers["deepseek"].generate_content("p")
        self.assertEqual(res.status, ai.DISABLED)
        self.assertEqual(res.http_requests, 0)
        self.assertEqual(len(http.calls), 0)


# ==================== 任务 16：429 限流 ====================
class TestTask16RateLimit(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(ai.parse_retry_after("120"), 120.0)
        self.assertEqual(ai.parse_retry_after(" 7 "), 7.0)
        self.assertEqual(ai.parse_retry_after("0"), 0.0)

    def test_retry_after_http_date(self):
        future = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(seconds=90))
        text = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        secs = ai.parse_retry_after(text)
        self.assertIsNotNone(secs)
        self.assertTrue(80 <= secs <= 95, f"HTTP 日期解析偏差过大: {secs}")

    def test_retry_after_garbage_returns_none(self):
        self.assertIsNone(ai.parse_retry_after("soon"))
        self.assertIsNone(ai.parse_retry_after(None))
        self.assertIsNone(ai.parse_retry_after(""))

    def test_429_honors_retry_after(self):
        """服务端说 5 秒后，就必须等 5 秒，而不是自作聪明等 2 秒再敲一次。"""
        http = SeqHTTP([Resp(429, text="rate limited", headers={"Retry-After": "5"}),
                        Resp(200, ok_payload())])
        slept = []
        cfg = ai.AIConfig()
        providers = ai.build_providers(cfg, http=http, sleep=slept.append)
        providers["deepseek"].api_key = "test-key"
        router = ai.AIRouter({}, config=cfg, providers=providers)

        res = router.analyze_news("prompt")
        self.assertTrue(res.ok)
        self.assertEqual(slept, [5.0], f"应按 Retry-After 等待，实际 {slept}")

    def test_429_with_huge_retry_after_fails_fast(self):
        """服务端要求等 600 秒：等不起就立刻失败，别把 Actions 拖到超时。"""
        http = SeqHTTP([Resp(429, text="rate limited", headers={"Retry-After": "600"})])
        slept = []
        cfg = ai.AIConfig()
        providers = ai.build_providers(cfg, http=http, sleep=slept.append)
        providers["deepseek"].api_key = "test-key"

        res = providers["deepseek"].generate_content("prompt")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, ai.RATE_LIMITED)
        self.assertEqual(slept, [], "不得真的等 600 秒")
        self.assertEqual(len(http.calls), 1, "超过上限就不该再重试")
        self.assertEqual(res.retry_after, 600.0)

    def test_429_without_retry_after_uses_backoff(self):
        http = SeqHTTP([Resp(429, text="rate limited"), Resp(200, ok_payload())])
        slept = []
        cfg = ai.AIConfig(retry_base_delay=2.0)
        providers = ai.build_providers(cfg, http=http, sleep=slept.append)
        providers["deepseek"].api_key = "test-key"
        res = providers["deepseek"].generate_content("prompt")
        self.assertTrue(res.ok)
        self.assertEqual(slept, [2.0])

    def test_429_trips_breaker_with_cooldown(self):
        http = SeqHTTP([Resp(429, text="rate limited", headers={"Retry-After": "180"})])
        router, _p = make_router({}, http)
        res = router.analyze_news("prompt")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, ai.RATE_LIMITED)
        breaker = router.state["circuit_breakers"]["deepseek"]
        self.assertEqual(breaker["state"], "open")
        self.assertEqual(breaker["error_type"], ai.RATE_LIMITED)

    def test_open_breaker_prevents_further_requests(self):
        http = SeqHTTP([Resp(429, text="rate limited", headers={"Retry-After": "180"})])
        router, _p = make_router({}, http)
        router.analyze_news("prompt")
        before = len(http.calls)
        res = router.analyze_news("prompt again")
        self.assertEqual(res.status, ai.CIRCUIT_OPEN)
        self.assertEqual(len(http.calls), before, "熔断期间不得再发请求")


# ==================== 任务 17：扫描 Job 退出码 ====================
class TestTask17ExitCodes(DeliveryBase):
    def _run(self, items, status, model=(None, None), send=(True, [1], "")):
        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, status)), \
                mock.patch.object(bot, "evaluate_with_gemini", return_value=model), \
                mock.patch.object(bot, "send_telegram_ex",
                                  side_effect=lambda t, *a, **k: send), \
                contextlib_redirect():
            return bot.run_scan_mode(bot.load_state(), bot._now_iso())

    def test_all_sources_failed_exits_1(self):
        rc = self._run([], {"https://a/rss": 403, "https://b/rss": "timeout"})
        self.assertEqual(rc, 1, "所有源都挂了必须让 Job 失败，不能伪装成「今天没新闻」")

    def test_partial_source_failure_is_not_fatal(self):
        rc = self._run([item(0)], {"https://a/rss": 200, "https://b/rss": 403},
                       model=(base_result(), None))
        self.assertEqual(rc, 0)

    def test_empty_sources_list_does_not_fail(self):
        """source_status 为空（测试桩/未配置）时不误判为全挂。"""
        rc = self._run([item(0)], {}, model=(base_result(), None))
        self.assertEqual(rc, 0)

    def test_no_new_article_exits_0(self):
        state = store.ensure_shape({"processed_urls": ["https://example.com/news/0"]})
        self.write_state(state)
        rc = self._run([item(0)], {"https://a/rss": 200})
        self.assertEqual(rc, 0, "确实没新闻属于正常空转")

    def test_delivery_failure_exits_1(self):
        rc = self._run([item(0)], {"https://a/rss": 200},
                       model=(base_result(), None), send=(False, [], "HTTP 500"))
        self.assertEqual(rc, 1, "消息没送达，Job 不能报成功")

    def test_quota_exhausted_exits_0(self):
        """配额耗尽是自愈型状态（已 warning + 熔断），不该把 Job 打成红叉。"""
        rc = self._run([item(0)], {"https://a/rss": 200},
                       model=(None, f"{ai.QUOTA_EXHAUSTED}: 余额不足"))
        self.assertEqual(rc, 0)

    def test_rate_limited_exits_0(self):
        rc = self._run([item(0)], {"https://a/rss": 200},
                       model=(None, f"{ai.RATE_LIMITED}: 429"))
        self.assertEqual(rc, 0)

    def test_auth_failure_exits_1(self):
        rc = self._run([item(0)], {"https://a/rss": 200},
                       model=(None, f"{ai.AUTH_FAILED}: 401 无效 key"))
        self.assertEqual(rc, 1, "鉴权失败需要人工处理，必须让 Job 失败")

    def test_parse_error_exits_1(self):
        rc = self._run([item(0)], {"https://a/rss": 200},
                       model=(None, f"{ai.PARSE_ERROR}: JSON 解析失败"))
        self.assertEqual(rc, 1)

    def test_success_exits_0(self):
        rc = self._run([item(0)], {"https://a/rss": 200}, model=(base_result(), None))
        self.assertEqual(rc, 0)

    def test_source_health_classification(self):
        health = bot._source_health({
            "a": 200, "b": "FALLBACK_OK(200, 5 items)", "c": "200_EMPTY(0 items)",
            "d": "FALLBACK_EMPTY(0 items)", "e": 403, "f": "timeout", "g": "MISSING",
        })
        self.assertEqual(health, {"ok": 2, "degraded": 2, "failed": 3, "total": 7})


if __name__ == "__main__":
    unittest.main(verbosity=2)
