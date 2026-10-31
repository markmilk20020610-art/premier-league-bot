"""AI Provider 层验收：任务书「八、必须执行的验证」测试 1~7。

全部用假 HTTP 响应模拟，不产生真实网络请求、不消耗任何额度。
"""
import json
import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _stubs  # noqa: E402

_stubs.install()

import ai_provider as ai  # noqa: E402
import state_store as store  # noqa: E402

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


class Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        return self._payload


def ok_payload(content=None, prompt_tokens=120, completion_tokens=80):
    body = content or {"has_valid_news": True, "event_title": "t"}
    return {
        "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


class FakeHTTP:
    """按顺序吐出预设响应，并记录所有请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append({"method": "post", "url": url, "json": kw.get("json"), "headers": kw.get("headers")})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get(self, url, **kw):
        self.calls.append({"method": "get", "url": url})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class FakeGemini:
    """假的 genai client，用于验证 Gemini 熔断，不碰真实 SDK。"""

    def __init__(self, side_effect=None):
        self.side_effect = side_effect
        self.calls = 0
        self.models = self

    def generate_content(self, **kw):
        self.calls += 1
        if self.side_effect:
            raise self.side_effect
        raise RuntimeError("不应被调用")


def make_router(state=None, http=None, gemini_enabled=False, **cfg_over):
    cfg = ai.AIConfig(gemini_enabled=gemini_enabled, **cfg_over)
    providers = ai.build_providers(cfg, http=http or FakeHTTP([Resp(200, ok_payload())]), sleep=lambda s: None)
    providers["deepseek"].api_key = "test-key"
    if gemini_enabled:
        providers["gemini"].api_key = "test-key"
    return ai.AIRouter(state if state is not None else {}, config=cfg, providers=providers)


# ==================== 测试 1：Gemini 关闭 ====================
class Test01GeminiDisabled(unittest.TestCase):
    def test_no_gemini_request_when_disabled(self):
        http = FakeHTTP([Resp(200, ok_payload())])
        router = make_router(http=http, gemini_enabled=False)

        res = router.analyze_news("prompt")
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "deepseek")

        gemini = router.providers["gemini"]
        self.assertFalse(gemini.enabled)
        self.assertEqual(gemini.call_count, 0, "Gemini 禁用时不得产生任何调用")
        self.assertTrue(all(DEEPSEEK_URL in c["url"] for c in http.calls),
                        "禁用 Gemini 时不应出现其它 endpoint")

    def test_gemini_provider_is_disabled_by_default(self):
        cfg = ai.AIConfig()
        self.assertFalse(cfg.gemini_enabled)
        self.assertEqual(cfg.provider_order(), ["deepseek"])
        self.assertFalse(cfg.openai_enabled)


# ==================== 测试 4：429 限流 ====================
class Test04RateLimit(unittest.TestCase):
    def test_retry_with_backoff_but_not_infinite(self):
        http = FakeHTTP([Resp(429, {"error": "rate limit"})])
        sleeps = []
        cfg = ai.AIConfig(max_retries=2, retry_base_delay=1.0)
        providers = ai.build_providers(cfg, http=http, sleep=lambda s: sleeps.append(s))
        providers["deepseek"].api_key = "k"
        router = ai.AIRouter({}, config=cfg, providers=providers)

        res = router.analyze_news("p")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, ai.RATE_LIMITED)
        self.assertEqual(len(http.calls), 3, "1 次首发 + 2 次重试，不得无限重试")
        self.assertEqual(sleeps, [1.0, 2.0], "退避应指数增长")
        self.assertEqual(router._stats()["errors"], 1)

    def test_429_trips_breaker_after_quota_message(self):
        http = FakeHTTP([Resp(429, {"error": "quota exceeded"})])
        router = make_router(http=http, max_retries=0)
        router.analyze_news("p")
        self.assertTrue(router._breaker("deepseek").is_open, "配额类 429 应触发熔断")


# ==================== 测试 5：余额 / 配额耗尽 ====================
class Test05QuotaExhausted(unittest.TestCase):
    def test_quota_exhausted_opens_breaker_and_stops(self):
        http = FakeHTTP([Resp(402, {"error": {"message": "Insufficient Balance"}})])
        router = make_router(http=http, max_retries=2)

        res = router.analyze_news("p")
        self.assertFalse(res.ok)
        self.assertEqual(res.status, ai.QUOTA_EXHAUSTED)
        self.assertEqual(len(http.calls), 1, "配额耗尽不应重试")

        breaker = router._breaker("deepseek")
        self.assertTrue(breaker.is_open)
        self.assertGreater(breaker.remaining_minutes(), 0)

        # 第二次调用应被熔断直接拦下，不再发请求
        res2 = router.analyze_news("p2")
        self.assertFalse(res2.ok)
        self.assertEqual(len(http.calls), 1, "熔断期间不得再次请求")

    def test_auth_failure_stops_immediately(self):
        http = FakeHTTP([Resp(401, {"error": "invalid key"})])
        router = make_router(http=http, max_retries=2)
        res = router.analyze_news("p")
        self.assertEqual(res.status, ai.AUTH_FAILED)
        self.assertEqual(len(http.calls), 1, "401 不应重试")
        self.assertTrue(router._breaker("deepseek").is_open)

    def test_server_error_retries_then_opens(self):
        http = FakeHTTP([Resp(503, {"error": "unavailable"})])
        router = make_router(http=http, max_retries=1, server_error_threshold=3)
        for _ in range(3):
            router.analyze_news("p")
        self.assertEqual(len(http.calls), 6, "每轮 1 次首发 + 1 次重试")
        self.assertTrue(router._breaker("deepseek").is_open, "连续 3 轮失败应熔断")


# ==================== 测试 6：Gemini 启用但配额耗尽 ====================
class Test06GeminiBreaker(unittest.TestCase):
    def test_gemini_quota_exhausted_trips_persistent_breaker(self):
        state = {}
        gemini_error = RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        gemini_error.code = 429

        router = make_router(state=state, gemini_enabled=True, http=FakeHTTP([Resp(402, {"error": "Insufficient Balance"})]))
        fake_client = FakeGemini(side_effect=gemini_error)
        router.providers["gemini"]._client = fake_client
        router.providers["gemini"]._types = mock.MagicMock()
        router.providers["gemini"]._ensure_client = lambda: True

        res = router.analyze_news("p")
        self.assertFalse(res.ok)
        self.assertEqual(fake_client.calls, 1, "第一次应真实调用 Gemini")
        self.assertTrue(state["circuit_breakers"]["gemini"]["open_until"], "熔断状态必须持久化到 state")

        # 第二轮：熔断生效，不得再请求 Gemini
        router2 = make_router(state=state, gemini_enabled=True,
                              http=FakeHTTP([Resp(402, {"error": "Insufficient Balance"})]))
        fake2 = FakeGemini(side_effect=gemini_error)
        router2.providers["gemini"]._client = fake2
        router2.providers["gemini"]._types = mock.MagicMock()
        router2.providers["gemini"]._ensure_client = lambda: True
        router2.analyze_news("p")
        self.assertEqual(fake2.calls, 0, "熔断期内不得继续请求 Gemini")

    def test_gemini_disabled_never_constructed(self):
        router = make_router(gemini_enabled=False)
        self.assertIsNone(router.providers["gemini"]._client, "禁用时不应构造 client")


# ==================== 预算与统计 ====================
class TestBudgetAndStats(unittest.TestCase):
    def test_daily_call_budget_blocks_without_http(self):
        state = {}
        http = FakeHTTP([Resp(200, ok_payload())])
        router = make_router(state=state, http=http, max_calls_per_day=2)
        router.analyze_news("p")
        router.analyze_news("p")
        res = router.analyze_news("p")
        self.assertEqual(res.status, ai.BUDGET_EXCEEDED)
        self.assertEqual(len(http.calls), 2, "预算用尽后不得再发请求")
        self.assertEqual(router._stats()["calls"], 2, "被预算拦下的请求不计入实际调用数")

    def test_stats_recorded_per_provider_and_tokens(self):
        state = {}
        router = make_router(state=state, http=FakeHTTP([Resp(200, ok_payload(prompt_tokens=100, completion_tokens=50))]))
        router.analyze_news("p")
        stats = ai.stats_snapshot(state)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["input_tokens"], 100)
        self.assertEqual(stats["output_tokens"], 50)
        self.assertEqual(stats["by_provider"]["deepseek"]["calls"], 1)

    def test_daily_stats_reset_on_new_day(self):
        state = {"ai_stats": {"date": "2000-01-01", "calls": 99, "errors": 9,
                              "input_tokens": 1, "output_tokens": 1}}
        router = make_router(state=state, http=FakeHTTP([Resp(200, ok_payload())]))
        router.analyze_news("p")
        self.assertEqual(router._stats()["calls"], 1, "跨天必须重置统计")


# ==================== 密钥脱敏 ====================
class TestSanitize(unittest.TestCase):
    def test_secrets_never_logged(self):
        text = "Authorization: Bearer sk-abcdef1234567890 failed; bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
        clean = ai.sanitize(text)
        self.assertNotIn("sk-abcdef1234567890", clean)
        self.assertNotIn("AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", clean)
        self.assertIn("REDACTED", clean)

    def test_call_record_has_no_secret(self):
        res = ai.CallResult(False, None, "deepseek", "deepseek-chat",
                            status=ai.AUTH_FAILED, error_type=ai.AUTH_FAILED,
                            error_message="key sk-secretsecret1234 rejected")
        self.assertNotIn("sk-secretsecret1234", json.dumps(res.to_record()))


# ==================== 接口完整性 ====================
class TestProviderInterface(unittest.TestCase):
    def test_unified_interface_exists(self):
        router = make_router()
        for name in ("analyze_news", "generate_summary", "generate_content", "health_check"):
            self.assertTrue(callable(getattr(router, name)), f"缺少统一接口 {name}")

    def test_call_record_fields(self):
        router = make_router(http=FakeHTTP([Resp(200, ok_payload())]))
        res = router.analyze_news("p")
        record = res.to_record()
        for field in ("provider", "model", "request_time", "response_ms",
                      "input_tokens", "output_tokens", "success", "error_type", "error_message"):
            self.assertIn(field, record)
        self.assertTrue(record["success"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
