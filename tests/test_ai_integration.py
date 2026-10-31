"""AI Provider 与主流程联调验收（任务书测试 2 / 3 / 5 / 7）。

用真实 AIRouter + 假 HTTP，验证「不调用 / 批量调用 / 配额耗尽 / 未核验不推送」。
"""
import datetime
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
import state_store as store  # noqa: E402

BBC = "https://www.bbc.co.uk/sport/football/articles/c123"


class Resp:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        return self._payload


def analysis_payload(event_title="阿森纳申诉被驳回", evidence_url=BBC):
    body = {
        "has_valid_news": True,
        "var_rating": "✨【金哨认证·绝对真理】(Tier 1)",
        "var_reason": "官方公告",
        "event_title": event_title,
        "event_time": "2小时前",
        "fact_summary": "英足总确认驳回阿森纳关于红牌的申诉。",
        "evidence_url": evidence_url,
        "draft_a": "这次申诉被驳回，阿森纳到底冤不冤？",
        "draft_b": "英足总官方公告已确认，BBC 同步报道。",
        "draft_c": "纪律条例规定申诉窗口 48 小时，逾期不受理。",
        "cover_blueprint": "深色底 + 红色警示条",
        "image_prompt": "3d caricature style referee holding red card",
        "banner_overlay_text": "申诉被驳",
    }
    return {
        "choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 400},
    }


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append({"url": url, "json": kw.get("json")})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def get(self, url, **kw):
        self.calls.append({"url": url})
        return self.responses[0]

    def prompts(self):
        return [c["json"]["messages"][-1]["content"] for c in self.calls if "json" in c]


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self._orig_state = bot.STATE_FILE
        self._orig_router = bot.ROUTER
        bot.STATE_FILE = os.path.join(self.dir, "state.json")

        self.sent = []
        self._patches = [
            mock.patch.object(bot, "send_telegram_ex", side_effect=self._fake_send),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        bot.STATE_FILE = self._orig_state
        bot.ROUTER = self._orig_router
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _fake_send(self, text):
        self.sent.append(text)
        return True, [1001], ""

    def item(self, i, title=None):
        return {
            "title": title or f"Arsenal transfer news number {i}",
            "link": f"https://example.com/news/{i}",
            "desc": "desc",
            "source": "s",
            "published": datetime.datetime.now(datetime.timezone.utc),
        }

    def install_router(self, http, **cfg_over):
        cfg = ai.AIConfig(**cfg_over)
        providers = ai.build_providers(cfg, http=http, sleep=lambda s: None)
        providers["deepseek"].api_key = "test-key"
        router = ai.AIRouter({}, config=cfg, providers=providers)
        bot.ROUTER = router
        return router


class Test02NoNewArticles(IntegrationBase):
    def test_no_ai_call_when_nothing_new(self):
        """测试 2：没有新文章 → 不调用任何 AI API。"""
        items = [self.item(i) for i in range(3)]
        state = store.ensure_shape({"processed_urls": [it["link"] for it in items]})
        store.save_state_atomic(state, bot.STATE_FILE)

        http = FakeHTTP([Resp(200, analysis_payload())])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0)
        self.assertEqual(len(http.calls), 0, "没有新文章时不得发起任何模型请求")
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(bot.load_state().get("ai_stats", {}).get("calls", 0), 0)


class Test03BatchCall(IntegrationBase):
    def test_ten_articles_one_batch_call(self):
        """测试 3：10 篇新文章 → 去重后一次性批量送模型，不是逐篇调用。"""
        items = [self.item(i) for i in range(10)]
        http = FakeHTTP([Resp(200, analysis_payload())])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0)
        self.assertEqual(len(http.calls), 1, "10 篇应合并为 1 次调用")
        prompt = http.prompts()[0]
        for i in range(10):
            self.assertIn(f"Arsenal transfer news number {i}", prompt, "10 篇素材都应进入同一次 prompt")

        stats = bot.load_state()["ai_stats"]
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["input_tokens"], 800)

    def test_duplicate_articles_are_collapsed(self):
        items = [self.item(0), self.item(0, title="Arsenal transfer news number 0")]
        http = FakeHTTP([Resp(200, analysis_payload())])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        prompt = http.prompts()[0]
        self.assertEqual(prompt.count("Arsenal transfer news number 0"), 1, "重复文章应先去重再送模型")


class Test05QuotaExhausted(IntegrationBase):
    def test_quota_exhausted_pauses_and_keeps_candidates(self):
        """测试 5：DeepSeek 配额耗尽 → 暂停分析、保留候选、不推送未核验内容。"""
        items = [self.item(i) for i in range(3)]
        http = FakeHTTP([Resp(402, {"error": {"message": "Insufficient Balance"}})])
        self.install_router(http, max_retries=2)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0, "配额耗尽属于可恢复状态，不应让 Job 红叉")
        self.assertEqual(len(self.sent), 0, "不得推送未经核验的内容")
        self.assertEqual(len(http.calls), 1, "配额耗尽不应重试")

        state = bot.load_state()
        self.assertEqual(len(state["news_pool"]), 3, "候选新闻必须保留")
        self.assertTrue(state["circuit_breakers"]["deepseek"]["open_until"], "熔断状态必须持久化")
        self.assertEqual(state["ai_stats"]["errors"], 1)

    def test_circuit_open_skips_next_round_without_request(self):
        items = [self.item(0)]
        http = FakeHTTP([Resp(402, {"error": "Insufficient Balance"})])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(http.calls), 1)

        # 第二轮：熔断期内不得再请求
        with mock.patch.object(bot, "fetch_rss_sources", return_value=([self.item(1)], {})):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(http.calls), 1, "熔断期间不得继续请求 DeepSeek")


class Test07UnverifiedNotPushed(IntegrationBase):
    def test_has_valid_news_but_unverified_is_blocked(self):
        """测试 7：模型说 has_valid_news=true，但证据状态 UNVERIFIED → 禁止推送。"""
        items = [self.item(0)]
        # DeepSeek 没有 grounding，evidence_url 再合法也只能算 UNVERIFIED
        http = FakeHTTP([Resp(200, analysis_payload(evidence_url="https://www.bbc.co.uk/made-up"))])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 0, "UNVERIFIED 禁止自动推送")
        state = bot.load_state()
        self.assertEqual(state["events"], {}, "被拦下的内容不应产生投递记录")


class TestProgramSideEvidence(IntegrationBase):
    """DeepSeek 没有 grounding，证据必须由程序从抓到的原始报道里算出来。"""

    def test_deepseek_result_verified_by_program_evidence(self):
        items = [
            self.item(0, title="Arsenal lose appeal against Dowman red card"),
            self.item(1, title="FA rejects Arsenal appeal over Dowman red card"),
        ]
        http = FakeHTTP([Resp(200, analysis_payload(evidence_url=items[0]["link"]))])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1, "两家独立报道应判定为多源印证并允许推送")
        state = bot.load_state()
        event = list(state["events"].values())[0]
        self.assertEqual(event["evidence_status"], "MULTIPLE_INDEPENDENT_SOURCES")

    def test_reprint_is_single_source(self):
        items = [
            self.item(0, title="Arsenal lose appeal against Dowman red card"),
            self.item(1, title="Arsenal lose appeal against Dowman red card"),
        ]
        http = FakeHTTP([Resp(200, analysis_payload(evidence_url=items[0]["link"]))])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        # 两条其实是同一篇稿（标题一致），只能算单一来源
        event = list(bot.load_state()["events"].values())[0]
        self.assertEqual(event["evidence_status"], "SINGLE_SOURCE")

    def test_unrelated_pool_does_not_inflate_sources(self):
        items = [
            self.item(0, title="Arsenal lose appeal against Dowman red card"),
            self.item(1, title="Chelsea announce new shirt sponsor"),
            self.item(2, title="Liverpool confirm stadium expansion plans"),
        ]
        http = FakeHTTP([Resp(200, analysis_payload(evidence_url=items[0]["link"]))])
        self.install_router(http)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        event = list(bot.load_state()["events"].values())[0]
        self.assertEqual(event["evidence_status"], "SINGLE_SOURCE",
                         "无关新闻不得算作交叉印证")


if __name__ == "__main__":
    unittest.main(verbosity=2)
