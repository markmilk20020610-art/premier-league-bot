"""第 2 项验收：事件名额与证据名额分离。

核心命题（必须由测试守住）：
  · 一个事件只占**一个**事件排序名额
  · 最多 3 篇原始报道作为同一事件的证据附件
  · 模型看到的是**一个事件对象**，不是三条重复事件
  · 最终早报不重复介绍同一事件（同一 event_id 只能出现一次）
  · 来源链接完整保留，不能只留第一条

背景：名额层面原本已经只占 3 条素材，但序列化层按「素材」逐条建条目，
同一件事被编成 E01/E02/E03，模型看到三个不同标题，很自然地当成三件事
分别介绍一遍。问题不在名额，在序列化。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot

# 同一件「C 罗国家队退役」的三条不同报道（真实标题）
RETIRE = [
    ("Ronaldo retires from Portugal duty", "reuters.com",
     "https://www.reuters.com/sports/soccer/retires"),
    ("Ronaldo announces international retirement on Instagram!",
     "thesun.co.uk", "https://www.thesun.co.uk/sport/football/instagram"),
    ("Ronaldo's most iconic moments as he retires from international football",
     "skysports.com", "https://www.skysports.com/football/news/iconic"),
]
DONATION = ("Cristiano Ronaldo's club to donate all ticket sales to tragic "
            "family", "theguardian.com",
            "https://www.theguardian.com/football/donate")


def mkitem(title, domain, url, hour=18, event_id=None, event_title=None):
    it = {
        "title": title,
        "link": url,
        "desc": "",
        "publisher": domain,
        "feed_origin": "https://{}/rss".format(domain),
        "published": "2026-11-05T{:02d}:00:00Z".format(hour),
        "_age": 2.0,
    }
    if event_id:
        it["_event_id"] = event_id
        it["_event_title"] = event_title or title
        it["_importance"] = "S"
        it["_content_type"] = "BREAKING_NEWS"
        it["_scope"] = "GLOBAL_EVENT_LINE"
    return it


def grouped_pool():
    """3 条退役证据（同一事件）+ 1 条捐球票（另一事件）。"""
    pool = [mkitem(t, d, u, hour=18 + i, event_id="ev_retire",
                   event_title="Ronaldo retires from Portugal duty")
            for i, (t, d, u) in enumerate(RETIRE)]
    pool.append(mkitem(DONATION[0], DONATION[1], DONATION[2], hour=13,
                       event_id="ev_donate",
                       event_title="Ronaldo's club donates ticket sales"))
    return pool


def flat_pool():
    """旧路径：没有 _event_id，每条素材各自成事件。"""
    return [mkitem(t, d, u, hour=18 + i) for i, (t, d, u) in enumerate(RETIRE)]


class TestGrouping(unittest.TestCase):
    def test_same_event_collapses_to_one_group(self):
        groups = bot._group_morning_pool(grouped_pool())
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0][1]), 3)
        self.assertEqual(len(groups[1][1]), 1)

    def test_group_order_preserved(self):
        groups = bot._group_morning_pool(grouped_pool())
        self.assertEqual([k for k, _ in groups], ["ev_retire", "ev_donate"])

    def test_legacy_path_one_group_per_item(self):
        # 没有 _event_id 时每条各自成组，行为与旧版一致
        groups = bot._group_morning_pool(flat_pool())
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(len(items) == 1 for _, items in groups))

    def test_empty(self):
        self.assertEqual(bot._group_morning_pool([]), [])


class TestEventSlotVsEvidenceSlot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verified, cls.excluded = bot._build_morning_events(grouped_pool())

    def test_three_articles_one_event(self):
        # 4 条素材 → 2 个事件条目
        self.assertEqual(len(self.verified), 2)

    def test_evidence_attached_not_split(self):
        retire = self.verified[0]
        self.assertEqual(retire["article_count"], 3)
        self.assertEqual(len(retire["urls"]), 3)
        self.assertEqual(len(retire["sources"]), 3)

    def test_event_ids_are_per_event(self):
        ids = [e["event_id"] for e in self.verified]
        self.assertEqual(ids, ["E01", "E02"])

    def test_all_links_preserved(self):
        retire = self.verified[0]
        expected = {u for _t, _d, u in RETIRE}
        self.assertEqual(set(retire["urls"]), expected)
        # 每一条证据都能追到原始 URL 与域名
        for src in retire["sources"]:
            self.assertTrue(src["url"])
            self.assertTrue(src["domain"])
            self.assertTrue(src["title"])

    def test_event_title_used_not_article_title(self):
        # 用聚合层定下的事件标题，而不是第一条报道的标题
        retire = self.verified[0]
        self.assertEqual(retire["title"], "Ronaldo retires from Portugal duty")
        # 三条证据标题各不相同，但对外只有一个事件标题
        self.assertEqual(len({s["title"] for s in retire["sources"]}), 3)

    def test_evidence_status_computed_for_event(self):
        # 三家独立媒体 → E2（按事件算，不是按单条算）
        self.assertEqual(self.verified[0]["status"], "MULTIPLE_INDEPENDENT_SOURCES")

    def test_legacy_pool_unchanged(self):
        verified, _ = bot._build_morning_events(flat_pool())
        self.assertEqual(len(verified), 3)
        for e in verified:
            self.assertEqual(e["article_count"], 1)
            self.assertEqual(len(e["urls"]), 1)


class TestModelSeesOneEvent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verified, _ = bot._build_morning_events(grouped_pool())

    def test_event_blob_has_one_line_per_event(self):
        blob = bot._morning_event_blob(self.verified)
        lines = [l for l in blob.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_event_blob_lists_all_urls(self):
        blob = bot._morning_event_blob(self.verified)
        for _t, _d, u in RETIRE:
            self.assertIn(u, blob)

    def test_event_blob_one_id_per_event(self):
        blob = bot._morning_event_blob(self.verified)
        self.assertEqual(blob.count("[E01]"), 1)
        self.assertEqual(blob.count("[E02]"), 1)

    def test_pool_blob_has_one_line_per_event(self):
        blob = bot._build_verified_blob(self.verified)
        lines = [l for l in blob.splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        for _t, _d, u in RETIRE:
            self.assertIn(u, blob)


class TestNoDuplicateIntro(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verified, _ = bot._build_morning_events(grouped_pool())

    def test_source_urls_carry_all_evidence(self):
        result = {"top_events": [{"event_id": "E01"}]}
        ok, errors = bot._attach_summary_evidence(result, self.verified)
        self.assertTrue(ok, errors)
        item = result["top_events"][0]
        self.assertEqual(len(item["source_urls"]), 3)
        self.assertEqual(item["source_count"], 3)

    def test_duplicate_event_id_rejected(self):
        # 模型重复选同一条 → 必须报错，不能静默去重后照常发
        result = {"top_events": [{"event_id": "E01"}, {"event_id": "E01"}]}
        ok, errors = bot._attach_summary_evidence(result, self.verified)
        self.assertFalse(ok)
        self.assertTrue(any("重复" in e for e in errors))

    def test_unknown_event_id_rejected(self):
        result = {"top_events": [{"event_id": "E99"}]}
        ok, errors = bot._attach_summary_evidence(result, self.verified)
        self.assertFalse(ok)

    def test_two_distinct_events_both_kept(self):
        result = {"top_events": [{"event_id": "E01"}, {"event_id": "E02"}]}
        ok, errors = bot._attach_summary_evidence(result, self.verified)
        self.assertTrue(ok, errors)
        self.assertEqual(len(result["top_events"]), 2)


class TestEndToEndSlotAccounting(unittest.TestCase):
    """走完整链路：真实池子 → 事件级排序 → 序列化，核对名额账。"""

    @classmethod
    def setUpClass(cls):
        cls.pool = grouped_pool()
        cls.ordered, cls.events, cls.stats = bot._events_for_morning(
            {"events": {}, "news_pool": []}, pool=cls.pool)
        cls.verified, _ = bot._build_morning_events(cls.ordered)

    def test_one_slot_per_event(self):
        # 素材 4 条 → 排序名额 2 个
        self.assertEqual(len(self.verified), 2)
        self.assertEqual(len(self.events), 2)

    def test_evidence_capped_at_three(self):
        self.assertLessEqual(
            max(len(e["urls"]) for e in self.verified),
            bot.MORNING_MAX_ARTICLES_PER_EVENT)

    def test_more_than_three_articles_still_capped(self):
        # 同一事件 6 条证据 → 序列化后仍只带 3 条
        rows = [
            ("Ronaldo retires from Portugal duty", "reuters.com",
             "https://www.reuters.com/a"),
            ("Ronaldo announces international retirement on Instagram!",
             "thesun.co.uk", "https://www.thesun.co.uk/b"),
            ("Ronaldo's most iconic moments as he retires from international "
             "football", "skysports.com", "https://www.skysports.com/c"),
            ("Trophies, tears, records and rows - Ronaldo's Portugal career "
             "will divide opinion", "bbc.co.uk", "https://www.bbc.co.uk/d"),
            ("A bitter end for Ronaldo - the icon who transformed Portugal",
             "theguardian.com", "https://www.theguardian.com/e"),
            ("Ronaldo retires from international football - Manchester United",
             "manutd.com", "https://www.manutd.com/f"),
        ]
        pool = [mkitem(t, d, u, hour=13 + i, event_id="ev_big",
                       event_title="Ronaldo retires from Portugal duty")
                for i, (t, d, u) in enumerate(rows)]
        ordered, events, _ = bot._events_for_morning(
            {"events": {}, "news_pool": []}, pool=pool)
        verified, _ = bot._build_morning_events(ordered)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["article_count"],
                         bot.MORNING_MAX_ARTICLES_PER_EVENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
