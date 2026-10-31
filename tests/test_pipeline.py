"""阶段 1.2 / 1.3 + 阶段 2 联调验收：模型输出 → 证据核验 → Schema → 投递状态机。

用桩替换外部依赖，但走的是 bot.py 里真实的 _finalize_result / run_scan_mode 逻辑。
"""
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

import bot  # noqa: E402
import ai_provider as ai  # noqa: E402
import state_store as store  # noqa: E402

BBC = "https://www.bbc.co.uk/sport/football/articles/c123"
SKY = "https://www.skysports.com/football/news/11095/arsenal"
SUN = "https://www.thesun.co.uk/sport/football/12345/arsenal"

# 默认素材故意给「两家独立报道同一件事」：
# V1.1 任务 4 之后 SINGLE_SOURCE 默认不再自动推送，若仍用单条 grounding，
# 下面这些投递状态机测试就全都走不到推送分支，测的就不是状态机了。
BBC_TITLE = "Arsenal lose appeal against Dowman red card"
SKY_TITLE = "FA rejects Arsenal appeal over Dowman dismissal"


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


class BotTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self._orig_state_file = bot.STATE_FILE
        bot.STATE_FILE = self.state_path

        self.sent = []
        self._patches = [
            # 默认给一条「新文章」，否则新逻辑会走 NO_NEW_ARTICLE 分支、根本不调 AI
            mock.patch.object(bot, "fetch_rss_sources", return_value=([self._item(0)], {})),
            mock.patch.object(bot, "send_telegram_ex", side_effect=self._fake_send),
        ]
        for p in self._patches:
            p.start()

    def _item(self, i, title=None):
        import datetime
        return {
            "title": title or f"Arsenal transfer update number {i}",
            "link": f"https://example.com/news/{i}",
            "desc": "desc",
            "source": "s",
            "published": datetime.datetime.now(datetime.timezone.utc),
        }

    def tearDown(self):
        for p in self._patches:
            p.stop()
        bot.STATE_FILE = self._orig_state_file
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _fake_send(self, text):
        self.sent.append(text)
        return True, [4242], ""

    def write_state(self, state):
        store.save_state_atomic(store.ensure_shape(state), self.state_path)


class TestEvidenceGating(BotTestBase):
    def test_unverified_blocks_push(self):
        """有 grounding 但 evidence_url 对不上 → UNVERIFIED → 不推送。"""
        data = base_result(
            evidence_url="https://www.bbc.co.uk/sport/football/articles/does-not-exist",
            grounding_records=[{"url": BBC, "title": "Arsenal appeal red card"}],
        )
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 0, "UNVERIFIED 不得自动推送")
        state = bot.load_state()
        self.assertEqual(state["events"], {})

    def test_conflicting_blocks_push(self):
        data = base_result(
            grounding_records=[
                {"url": BBC, "title": "Arsenal set to sign striker"},
                {"url": SKY, "title": "Arsenal rule out striker signing"},
            ],
            source_claims=[
                {"claim_key": "sign_striker", "stance": "supports", "source": "bbc.co.uk"},
                {"claim_key": "sign_striker", "stance": "denies", "source": "skysports.com"},
            ],
        )
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0, "CONFLICTING 不得自动推送")

    def test_single_low_trust_source_is_blocked_by_default(self):
        """V1.1 任务 4：单一低可信来源默认不得自动推送。"""
        data = base_result(
            var_rating="✨【金哨认证·绝对真理】(Tier 1)",
            evidence_url=SUN,
            grounding_records=[{"url": SUN, "title": "Arsenal ready to bid 60m"}],
        )
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 0, "单一来源默认不得自动推送")
        self.assertEqual(bot.load_state()["events"], {}, "被拦下的内容不应产生投递记录")

    def test_single_source_is_pushed_but_rating_capped(self):
        """白名单内的可信单一来源可以推，但必须降级到 Tier 3 并显式标注未经交叉验证。

        原用例（V1.0）假设「单一来源一律推送」，V1.1 任务 4 改为「默认不推、白名单例外」。
        原断言（Tier 3 降级）在此完整保留，另加免责标注断言。
        """
        data = base_result(
            var_rating="✨【金哨认证·绝对真理】(Tier 1)",
            evidence_url=SUN,
            grounding_records=[{"url": SUN, "title": "Arsenal ready to bid 60m"}],
        )
        with mock.patch.object(bot, "TRUSTED_SINGLE_SOURCES", {"thesun.co.uk"}):
            with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
                rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Tier 3", self.sent[0], "单一小报来源必须被降级到 Tier 3")
        self.assertIn("未经独立交叉验证", self.sent[0], "单一来源必须显式标注未经交叉验证")
        self.assertIn(SUN, self.sent[0], "单一来源必须给出原始来源 URL")

    def test_black_label_blocked_without_strict_evidence(self):
        """V1.1 任务 5：单一低可信站点报道球员重伤 → 不得自动生成黑标快讯。"""
        data = base_result(
            var_rating="🐦‍⬛【严肃通报·黑标警示】(黑标)",
            event_title="阿森纳球员训练中受重伤",
            fact_summary="有消息称阿森纳一名球员在训练中膝盖重伤，可能赛季报销。",
            evidence_url=SUN,
            grounding_records=[
                {"url": SUN, "title": "Arsenal star suffers serious knee injury in training"},
            ],
        )
        with mock.patch.object(bot, "TRUSTED_SINGLE_SOURCES", {"thesun.co.uk"}):
            with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
                bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0, "黑标议题证据不足时一律不得自动推送")

    def test_schema_invalid_blocks_push(self):
        data = base_result()
        data.pop("draft_c")
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0, "Schema 不通过不得推送")

    def test_missing_has_valid_news_type_blocks_push(self):
        data = base_result(has_valid_news="true")   # 字符串而非布尔
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0)

    def test_illegal_rating_blocks_push(self):
        data = base_result(var_rating="S 级神哨")
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0)


class TestModelOutputNormalization(BotTestBase):
    """模型调用下沉到 AIRouter 后，主程序仍需保证输出被规范化。"""

    def test_provider_result_is_normalized(self):
        class FakeRouter:
            def analyze_news(self, prompt, **kw):
                return ai.CallResult(
                    True,
                    {
                        "has_valid_news": True, "event_title": "t", "evidence_url": BBC,
                        "evidence_status": "OFFICIAL_CONFIRMED", "evidence_sources_count": 99,
                        "var_rating": "Tier 1",
                    },
                    "deepseek", "deepseek-chat",
                )

        with mock.patch.object(bot, "get_router", return_value=FakeRouter()):
            data, err = bot.evaluate_with_gemini("blob", state={})

        self.assertIsNone(err)
        self.assertNotIn("evidence_status", data)
        self.assertNotIn("evidence_sources_count", data)
        self.assertEqual(data["verification_status"], "DEEPSEEK_NO_GROUNDING")
        self.assertEqual(data["ai_provider"], "deepseek")

    def test_provider_failure_returns_status(self):
        class FakeRouter:
            def analyze_news(self, prompt, **kw):
                return ai.CallResult(False, None, "deepseek", "deepseek-chat",
                                     status=ai.RATE_LIMITED, error_type=ai.RATE_LIMITED,
                                     error_message="429 too many requests")

        with mock.patch.object(bot, "get_router", return_value=FakeRouter()):
            data, err = bot.evaluate_with_gemini("blob", state={})
        self.assertIsNone(data)
        self.assertIn("RATE_LIMITED", err)

    def test_deepseek_result_never_pushed_as_verified(self):
        data = base_result(grounding_records=[], verification_status="DEEPSEEK_NO_GROUNDING")
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(len(self.sent), 0, "无 grounding 的结果不得推送")


class TestDeliveryPersistence(BotTestBase):
    def test_success_writes_committed_state(self):
        data = base_result()
        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)

        state = bot.load_state()
        events = list(state["events"].values())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["delivery_status"], store.COMMITTED)
        self.assertEqual(event["telegram_message_id"], 4242)
        self.assertIn(BBC, state["processed_urls"])

    def test_telegram_ok_but_state_write_fails(self):
        """消息已发出去、状态没存下来：必须 exit 1，且不能假装 COMMITTED。"""
        data = base_result()
        real_save = bot.save_state

        def fail_after_delivery(state):
            # 只在事件已标记 DELIVERED 的那次写盘上失败，精准复现「发成功、没记住」
            for e in (state.get("events") or {}).values():
                if e.get("delivery_status") == store.DELIVERED:
                    raise store.StateError("模拟写盘失败")
            return real_save(state)

        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)), \
             mock.patch.object(bot, "save_state", side_effect=fail_after_delivery):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(rc, 1, "状态写失败必须让 Job 失败")
        self.assertEqual(len(self.sent), 1)
        state = bot.load_state()
        statuses = [e["delivery_status"] for e in state["events"].values()]
        self.assertNotIn(store.COMMITTED, statuses)

    def test_state_write_fails_before_send_aborts(self):
        """发送前就写不进去 → 直接中止，绝不发送（避免发了记不住）。"""
        data = base_result()
        real_save = bot.save_state

        def fail_on_pending(state):
            for e in (state.get("events") or {}).values():
                if e.get("delivery_status") == store.PENDING_DELIVERY:
                    raise store.StateError("模拟写盘失败")
            return real_save(state)

        with mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)), \
             mock.patch.object(bot, "save_state", side_effect=fail_on_pending):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.sent), 0, "状态写不进去就不该发消息")


class TestRecoveryOnStartup(BotTestBase):
    def test_delivered_but_not_committed_is_recovered_without_resend(self):
        state = store.ensure_shape({})
        event = store.record_event(state, BBC, "Arsenal news", "SINGLE_SOURCE")
        store.transition(event, store.VERIFIED)
        store.begin_delivery(event)
        store.mark_delivered(event, 777)
        self.write_state(state)

        with mock.patch.object(bot, "run_scan_mode", return_value=0) as m:
            bot.main()
        self.assertTrue(m.called)
        recovered = bot.load_state()["events"][event["event_id"]]
        self.assertEqual(recovered["delivery_status"], store.COMMITTED)
        self.assertEqual(recovered["telegram_message_id"], 777)


class TestMorningRegression(BotTestBase):
    """阶段 2 改动了 save_state，早报链路必须有回归保护（阶段 5 会再扩）。"""

    def _pool(self, n=6):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        return [{
            "title": f"Arsenal transfer update number {i}",
            "link": f"https://example.com/a{i}",
            "desc": "d", "source": "s",
            "published": (now - datetime.timedelta(hours=2)).isoformat(),
            "first_seen": now.isoformat(),
        } for i in range(n)]

    def _summary(self):
        return {
            "has_valid_news": True, "note_title": "标题", "hook": "钩子",
            "body": "正文" * 40, "tags": "#英超",
            "top_events": [{"rank": 1, "title": "T", "one_line": "o",
                            "var_rating": "Tier1", "time": "2小时前", "url": "https://e.com/0"}],
            "fact_check_note": "截至 06:00", "cover_blueprint": "c",
            "image_prompt": "p", "banner_overlay_text": "标题",
        }

    def test_morning_sets_date_lock_after_success(self):
        self.write_state({"processed_urls": [], "last_morning_report_date": "", "news_pool": self._pool()})
        with mock.patch.object(bot, "evaluate_summary", return_value=(self._summary(), None)):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(bot.load_state()["last_morning_report_date"], bot._cn_date_str())

    def test_morning_skips_when_already_sent(self):
        self.write_state({"processed_urls": [], "last_morning_report_date": bot._cn_date_str(),
                          "news_pool": self._pool()})
        with mock.patch.object(bot, "evaluate_summary", return_value=(self._summary(), None)):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 0, "同一天正常早报不能重复发送")

    def test_morning_send_ok_but_state_fail_returns_1(self):
        self.write_state({"processed_urls": [], "last_morning_report_date": "", "news_pool": self._pool()})

        def fail(state):
            raise store.StateError("模拟写盘失败")

        with mock.patch.object(bot, "evaluate_summary", return_value=(self._summary(), None)), \
             mock.patch.object(bot, "save_state", side_effect=fail):
            rc = bot.run_morning_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 1, "早报已发但状态没存下来必须报错")


class TestPoolPersistence(BotTestBase):
    def test_pool_saved_even_when_nothing_pushed(self):
        """本轮没推送，新闻池也必须落盘，否则早报第二天仍然无米下锅。"""
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        items = [{
            "title": f"Liverpool injury update number {i}",
            "link": f"https://example.com/l{i}", "desc": "d", "source": "s",
            "published": now, "_age": 0.1,
        } for i in range(3)]
        data = base_result(has_valid_news=False)

        with mock.patch.object(bot, "fetch_rss_sources", return_value=(items, {})), \
             mock.patch.object(bot, "evaluate_with_gemini", return_value=(data, None)):
            bot.run_scan_mode(bot.load_state(), bot._now_iso())

        self.assertEqual(len(self.sent), 0)
        pool = bot.load_state()["news_pool"]
        self.assertEqual(len(pool), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
