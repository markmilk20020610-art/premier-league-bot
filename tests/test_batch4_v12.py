"""V1.2 第四批验收：突发新闻 12h 硬时效 + URL 清洗只清跟踪参数（任务 12 / 13）。

任务 12 的历史问题：8h 不够放宽到 24h，24h 还不够就**退回全量**，
把三天前的稿子一起喂给模型，指望 Prompt 里一句「只要突发」兜底。
安静时段里最容易被推出去的恰恰是陈年旧闻。

任务 13 的历史问题：clean_url 把整个查询串抹掉，
`?id=1001` 和 `?id=1002` 撞成同一条 → 第二条新闻被判为「已处理」静默丢弃。
"""
import datetime
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import _stubs  # noqa: E402

_stubs.install()

import bot  # noqa: E402

BBC = "https://www.bbc.co.uk/sport/football/articles/c123"
SKY = "https://www.skysports.com/football/news/11095/arsenal"
BBC_TITLE = "Arsenal lose appeal against Dowman red card"
SKY_TITLE = "FA rejects Arsenal appeal over Dowman dismissal"


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def item(i, age_hours=1.0, link=None, title=None, published=True):
    """构造一条候选。age_hours=None 表示不带发布时间。"""
    it = {
        "title": title or f"Arsenal transfer update number {i}",
        "link": link or f"https://example.com/news/{i}",
        "desc": "desc",
        "source": "s",
    }
    if published:
        if age_hours is None:
            it["published"] = None
        else:
            it["published"] = now() - datetime.timedelta(hours=age_hours)
    return it


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


# ==================== 任务 12：突发新闻 12h 硬时效 ====================
class TestTask12BreakingFreshness(unittest.TestCase):
    def _fetch(self, items, feeds=None):
        """跑真实 fetch_rss_sources，只把单源抓取换成桩。

        V1.3 §F1 审计 B 之后，未在 feed_registry 登记的源会被整源丢弃
        （未登记 = 不得采集）。所以这里必须用一个**真实登记过**的足球源地址，
        否则测的就不是时效过滤，而是「源被领域登记表拦掉」。
        """
        feeds = feeds or ["https://feeds.bbci.co.uk/sport/football/rss.xml"]
        with mock.patch.object(bot, "RSS_FEEDS", feeds), \
                mock.patch.object(bot, "_fetch_one_source",
                                  side_effect=lambda u, h: (u, list(items), 200)):
            return bot.fetch_rss_sources()

    def test_old_news_is_never_fed_to_model(self):
        """核心反例：只有 30 小时前的旧闻时，绝不能「退回全量」。"""
        items = [item(i, age_hours=30) for i in range(20)]
        fresh, _status = self._fetch(items)
        self.assertEqual(fresh, [], "12h 之外的旧闻一条都不许进候选池")

    def test_unknown_publish_time_is_excluded(self):
        """没有发布时间 = 无法证明是突发，不能放行（旧逻辑是保留）。"""
        items = [item(i, published=False) for i in range(5)]
        fresh, _status = self._fetch(items)
        self.assertEqual(fresh, [], "发布时间未知的条目不得进入突发候选")

    def test_future_publish_time_is_excluded(self):
        items = [item(i, age_hours=-3) for i in range(5)]      # 3 小时后才「发布」
        fresh, _status = self._fetch(items)
        self.assertEqual(fresh, [])

    def test_widens_from_8h_to_12h_but_no_further(self):
        items = ([item(i, age_hours=2) for i in range(3)]      # 8h 内 3 条
                 + [item(10 + i, age_hours=10) for i in range(3)]   # 8~12h 3 条
                 + [item(20 + i, age_hours=13) for i in range(3)])  # 12h 外 3 条
        fresh, _status = self._fetch(items)
        ages = sorted((now() - it["published"]).total_seconds() / 3600 for it in fresh)
        self.assertEqual(len(fresh), 6, "应放宽到 12h 恰好收进 6 条")
        self.assertLessEqual(max(ages), bot.FRESHNESS_HARD_CAP_HOURS + 0.01)
        self.assertGreater(max(ages), bot.FRESHNESS_HOURS, "确实用到了放宽窗口")

    def test_no_relaxation_when_8h_is_enough(self):
        items = [item(i, age_hours=1) for i in range(20)]
        fresh, _status = self._fetch(items)
        self.assertEqual(len(fresh), 20)
        self.assertLessEqual(
            max((now() - it["published"]).total_seconds() / 3600 for it in fresh),
            bot.FRESHNESS_HOURS + 0.01)

    def test_hard_cap_is_twelve_hours(self):
        self.assertEqual(bot.FRESHNESS_HARD_CAP_HOURS, 12)
        self.assertFalse(hasattr(bot, "FRESHNESS_FALLBACK_HOURS"),
                         "「放宽到 24h」这条路径必须被删除，而不是留着不用")
        self.assertFalse(bot.FRESHNESS_ALLOW_UNKNOWN,
                         "默认不得放行发布时间未知的条目")

    # ---- 推送前的二次闸门 ----
    def _index(self, url, age_hours):
        return {bot._normalize_link(url): now() - datetime.timedelta(hours=age_hours)}

    def test_stale_evidence_blocks_push(self):
        """模型把一条 30 小时前的稿子当突发 → 必须拦下。"""
        data = base_result()
        ok, _data, reason, code = bot._finalize_result(
            data, [], published_index=self._index(BBC, 30))
        self.assertFalse(ok)
        self.assertEqual(code, "STALE_BREAKING_NEWS")
        self.assertIn("30", reason)

    def test_fresh_evidence_passes_gate(self):
        data = base_result()
        ok, _data, _reason, code = bot._finalize_result(
            data, [], published_index=self._index(BBC, 2))
        self.assertTrue(ok, f"2 小时前的新闻不该被时效闸门拦下：{code}")

    def test_boundary_just_under_cap_passes(self):
        data = base_result()
        ok, _data, _reason, _code = bot._finalize_result(
            data, [], published_index=self._index(BBC, 11.5))
        self.assertTrue(ok)

    def test_unknown_evidence_age_does_not_block(self):
        """证据文章不在本轮集合里 → 无法判定，不能凭空拦下。"""
        data = base_result()
        ok, _data, _reason, _code = bot._finalize_result(
            data, [], published_index={})
        self.assertTrue(ok)

    def test_future_evidence_time_blocked(self):
        data = base_result()
        ok, _data, reason, code = bot._finalize_result(
            data, [], published_index=self._index(BBC, -6))
        self.assertFalse(ok)
        self.assertEqual(code, "STALE_BREAKING_NEWS")
        self.assertIn("未来", reason)

    def test_gate_is_skipped_without_index(self):
        """老调用方（两个参数）不受影响。"""
        ok, _data, _reason, _code = bot._finalize_result(base_result(), [])
        self.assertTrue(ok)


# ==================== 任务 13：URL 清洗只清跟踪参数 ====================
class TestTask13UrlCleaning(unittest.TestCase):
    def test_tracking_params_removed(self):
        cases = [
            "https://x.com/a?utm_source=twitter&utm_medium=social",
            "https://x.com/a?oc=5",
            "https://x.com/a?fbclid=abc123",
            "https://x.com/a?ito=social",
            "https://x.com/a?CMP=share_btn_tw",
            "https://x.com/a?ref_src=twsrc%5Etfw",
            "https://x.com/a?_ga=2.123",
        ]
        for raw in cases:
            self.assertEqual(bot.clean_url(raw), "https://x.com/a", f"{raw} 未清洗干净")

    def test_identity_params_kept(self):
        """核心反例：文章身份在查询串里时，两条不同新闻不能被合并。"""
        a = bot.clean_url("https://x.com/article?id=1001")
        b = bot.clean_url("https://x.com/article?id=1002")
        self.assertNotEqual(a, b, "不同 id 的新闻撞成同一条 → 会被误判为已处理")
        self.assertIn("id=1001", a)
        self.assertIn("id=1002", b)

    def test_tracking_removed_but_identity_kept(self):
        raw = "https://x.com/article?id=1001&utm_source=rss&oc=5"
        self.assertEqual(bot.clean_url(raw), "https://x.com/article?id=1001")

    def test_param_order_normalized(self):
        a = bot.clean_url("https://x.com/a?b=2&a=1")
        b = bot.clean_url("https://x.com/a?a=1&b=2")
        self.assertEqual(a, b, "参数顺序不同应视为同一页面")

    def test_fragment_removed(self):
        self.assertEqual(bot.clean_url("https://x.com/a#comments"), "https://x.com/a")

    def test_trailing_slash_normalized(self):
        self.assertEqual(bot.clean_url("https://x.com/a/"), "https://x.com/a")
        self.assertEqual(bot.clean_url("https://x.com/"), "https://x.com/")

    def test_host_case_and_scheme_normalized(self):
        self.assertEqual(bot.clean_url("HTTPS://X.COM/A"), "https://x.com/A")

    def test_idempotent(self):
        for raw in ("https://x.com/a?id=1&utm_source=rss",
                    "https://x.com/a?b=2&a=1#x",
                    "https://x.com/"):
            once = bot.clean_url(raw)
            self.assertEqual(bot.clean_url(once), once, f"{raw} 清洗不幂等")

    def test_empty_and_invalid_input(self):
        self.assertEqual(bot.clean_url(""), "")
        self.assertEqual(bot.clean_url(None), "")
        self.assertEqual(bot.clean_url("not a url"), "not a url")

    def test_legacy_key_still_matches_processed_urls(self):
        """升级后必须还能认出旧规则写下的 processed_urls，否则会把已推新闻重推一遍。"""
        legacy_stored = bot._legacy_clean_url("https://x.com/article?id=1001&utm_source=rss")
        self.assertEqual(legacy_stored, "https://x.com/article")
        sets = bot._processed_sets([legacy_stored])
        # 旧记录丢掉了 id，只能靠「入参也退化成旧形态」来命中
        self.assertTrue(bot._matches_processed("https://x.com/article?id=1001", sets))

    def test_new_form_stored_key_matches(self):
        sets = bot._processed_sets(["https://x.com/article?id=1001&utm_source=rss"])
        self.assertTrue(bot._matches_processed("https://x.com/article?id=1001", sets))
        self.assertFalse(bot._matches_processed("https://x.com/article?id=1002", sets),
                         "不同 id 的新闻不得被判为已处理")


if __name__ == "__main__":
    unittest.main(verbosity=2)
