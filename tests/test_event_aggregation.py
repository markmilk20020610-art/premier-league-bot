"""任务 4 验收：事件聚合与证据累积。

核心命题（必须由测试守住）：
  · 同一事件的 N 条报道只占 1 个候选名额
  · 不同事件绝不被合并——宁可漏合，不可错合
  · 转载与同源多稿都不算独立信源
  · 类型族相同才可复合合并（V1.0 新增类型的回归防线）
  · 硬冲突是绝对否决，优先于任何语义相似规则

本文件用的 C 罗数据集是 2026-11-05 的真实抓取标题，不是编造样本。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import event_aggregate as ea
import importance as imp


def item(title, domain, url, published="2026-11-05T10:00:00Z", desc="",
         publisher=None):
    """构造一条候选报道。feed_origin 用域名区分 RSS 源。"""
    return {
        "title": title,
        "link": url,
        "desc": desc,
        "publisher": publisher or domain,
        "feed_origin": "https://{}/rss".format(domain),
        "published": published,
    }


# ==================== 真实数据集 ====================
# 索引 0 是「俱乐部捐球票」——一条独立事件，曾被误并进退役事件
# 索引 1-9 是同一件「C 罗国家队退役」的 9 条不同报道
RONALDO_TITLES = [
    ("Cristiano Ronaldo's club to donate all ticket sales to tragic Jesus de "
     "Miguel's family and invite them as VIP guests", "thesun.co.uk",
     "https://www.thesun.co.uk/sport/football/donate"),
    ("A bitter end for Ronaldo - the icon who transformed Portugal",
     "theguardian.com", "https://www.theguardian.com/football/bitter-end"),
    ("Cristiano Ronaldo QUITS international football over explosive fall-out "
     "with Portugal manager", "dailymail.co.uk",
     "https://www.dailymail.co.uk/sport/football/quits"),
    ("Cristiano Ronaldo finished second to Messi even in retirement - the "
     "ending to his Portugal career is sad and predictable", "telegraph.co.uk",
     "https://www.telegraph.co.uk/football/second"),
    ("Trophies, tears, records and rows - Ronaldo's Portugal career will "
     "divide opinion", "bbc.co.uk",
     "https://www.bbc.co.uk/sport/football/trophies"),
    ("Ronaldo retires from Portugal duty", "reuters.com",
     "https://www.reuters.com/sports/soccer/retires"),
    ("Ronaldo announces international retirement on Instagram!",
     "thesun.co.uk", "https://www.thesun.co.uk/sport/football/instagram"),
    ("Ronaldo's most iconic moments as he retires from international football",
     "skysports.com", "https://www.skysports.com/football/news/iconic"),
    ("Ronaldo retires from international football - Manchester United",
     "manutd.com", "https://www.manutd.com/en/news/detail/retires"),
    ("Ronaldo's Portugal statement in full", "bbc.co.uk",
     "https://www.bbc.co.uk/sport/football/statement"),
]


def ronaldo_items():
    out = []
    for i, (title, domain, url) in enumerate(RONALDO_TITLES):
        out.append(item(title, domain, url,
                        published="2026-11-05T{:02d}:00:00Z".format(13 + i)))
    return out


class TestCristianoRetirement(unittest.TestCase):
    """2026-11-05 真实数据集：10 条报道 → 2 个事件。"""

    @classmethod
    def setUpClass(cls):
        cls.items = ronaldo_items()
        cls.events, cls.stats = ea.build_events(cls.items)

    def test_two_events_not_ten(self):
        # 关键：10 条报道只占 2 个名额，不是 10 个
        self.assertEqual(self.stats["input"], 10)
        self.assertEqual(self.stats["events"], 2)
        self.assertEqual(self.stats["merged"], 8)

    def test_retirement_event_holds_nine_articles(self):
        big = max(self.events, key=lambda e: e["article_count"])
        self.assertEqual(big["article_count"], 9)
        self.assertEqual(big["event_type"], "RETIREMENT")

    def test_retirement_is_s_level(self):
        big = max(self.events, key=lambda e: e["article_count"])
        self.assertEqual(big["importance"], imp.S)
        self.assertEqual(big["importance_detail"]["rule_id"],
                         "S_CAREER_END_NATIONAL")

    def test_donation_story_not_merged_into_retirement(self):
        # 捐球票是另一件事，不能被并进退役
        donation = [e for e in self.events
                    if "donate" in (e["title"] or "").lower()]
        self.assertEqual(len(donation), 1)
        self.assertEqual(donation[0]["article_count"], 1)
        self.assertNotEqual(donation[0]["event_id"],
                            max(self.events,
                                key=lambda e: e["article_count"])["event_id"])

    def test_pairwise_against_ground_truth(self):
        """pairwise 精确率/召回率——这是 62.2% 那个口径的同一算法。"""
        gt = [1] + [0] * 9
        # 用对象身份反查每条 item 落在哪个事件
        ids = []
        for it in self.items:
            hit = None
            for e in self.events:
                for m in e["members"]:
                    if m is it:
                        hit = e["event_id"]
            ids.append(hit)
        self.assertTrue(all(ids), "存在未归入任何事件的报道")

        tp = fp = fn = 0
        for i in range(10):
            for j in range(i + 1, 10):
                same_pred = ids[i] == ids[j]
                same_gt = gt[i] == gt[j]
                if same_pred and same_gt:
                    tp += 1
                elif same_pred and not same_gt:
                    fp += 1
                elif same_gt and not same_pred:
                    fn += 1
        self.assertEqual(fp, 0, "出现误合并")
        self.assertEqual(fn, 0, "出现误拆分")
        self.assertEqual(tp, 36)

    def test_events_sorted_by_importance(self):
        ranks = [imp.rank(e["importance"]) for e in self.events]
        self.assertEqual(ranks, sorted(ranks, reverse=True))


class TestEvidenceAccumulation(unittest.TestCase):
    """证据累积：事件带多条原始报道与时间戳，不是只留一条。"""

    @classmethod
    def setUpClass(cls):
        cls.events, _ = ea.build_events(ronaldo_items())
        cls.big = max(cls.events, key=lambda e: e["article_count"])

    def test_evidence_refs_match_articles(self):
        self.assertEqual(len(self.big["evidence_refs"]),
                         self.big["article_count"])
        for ref in self.big["evidence_refs"]:
            self.assertTrue(ref["url"])
            self.assertTrue(ref["title"])

    def test_first_and_last_progress(self):
        self.assertTrue(self.big["first_published_at"])
        self.assertTrue(self.big["last_progress_at"])
        self.assertLess(self.big["first_published_at"],
                        self.big["last_progress_at"])

    def test_domains_deduplicated(self):
        self.assertEqual(len(self.big["domains"]),
                         len(set(self.big["domains"])))
        self.assertIn("bbc.co.uk", self.big["domains"])

    def test_independent_groups_less_than_articles(self):
        # 同一家媒体的多篇稿只算 1 个来源，所以组数必须小于报道数
        self.assertLess(self.big["independent_groups"],
                        self.big["article_count"])

    def test_push_state_starts_unpushed(self):
        for e in self.events:
            self.assertEqual(e["push_state"], "UNPUSHED")


class TestReprintNotIndependent(unittest.TestCase):
    """转载不算独立信源——这是「多源印证」的根基。"""

    def test_identical_reprints_count_as_one(self):
        title = "Ronaldo retires from Portugal duty"
        items = [
            item(title, "bbc.co.uk", "https://www.bbc.co.uk/a1"),
            item(title, "dailymail.co.uk", "https://www.dailymail.co.uk/b1"),
            item(title, "mirror.co.uk", "https://www.mirror.co.uk/c1"),
        ]
        events, stats = ea.build_events(items)
        self.assertEqual(stats["events"], 1)
        self.assertEqual(events[0]["article_count"], 3)
        # 三家转载同一篇稿 → 只有 1 个独立来源，不是 3 个
        self.assertEqual(events[0]["independent_groups"], 1)

    def test_same_publisher_multiple_pieces_count_as_one(self):
        items = [
            item("Ronaldo retires from Portugal duty", "skysports.com",
                 "https://www.skysports.com/news/1"),
            item("Ronaldo calls time on Portugal career", "skysports.com",
                 "https://www.skysports.com/live-blog/2"),
        ]
        events, _ = ea.build_events(items)
        for e in events:
            self.assertEqual(e["independent_groups"], 1)


class TestNoSlotDuplication(unittest.TestCase):
    """同一事件的多篇报道不能重复占用候选名额。"""

    def test_one_event_one_slot(self):
        items = ronaldo_items()
        events, _ = ea.build_events(items)
        ids = [e["event_id"] for e in events]
        self.assertEqual(len(ids), len(set(ids)))
        # 事件数 ≤ 输入条数，且远小于输入
        self.assertLessEqual(len(events), len(items))

    def test_duplicate_url_collapses(self):
        items = [
            item("Arsenal agree deal for striker", "bbc.co.uk",
                 "https://www.bbc.co.uk/a?utm_source=rss"),
            item("Arsenal agree deal for striker", "bbc.co.uk",
                 "https://www.bbc.co.uk/a"),
        ]
        events, stats = ea.build_events(items)
        self.assertEqual(stats["events"], 1)


class TestTypeFamily(unittest.TestCase):
    """V1.0 新增类型后，必须在「族」层面比较，否则凭空制造误拆分。"""

    def test_family_mapping(self):
        self.assertEqual(ea._family("RETIREMENT"), "CAREER_END")
        self.assertEqual(ea._family("PLAYER_CAREER"), "CAREER_END")
        self.assertEqual(ea._family("NATIONAL_TEAM"), "CAREER_END")
        self.assertEqual(ea._family("CONTRACT"), "TRANSFER")
        self.assertEqual(ea._family("MATCH_FIXING"), "INTEGRITY")
        self.assertEqual(ea._family("GOVERNANCE"), "GOVERNANCE")

    def test_cross_family_never_merges(self):
        # 转会与伤病永远是两件事
        a = item("Arsenal sign winger Michael Olise in 60m deal", "bbc.co.uk",
                 "https://www.bbc.co.uk/a2")
        b = item("Arsenal winger Michael Olise out for eight weeks with injury",
                 "skysports.com", "https://www.skysports.com/b2")
        events, stats = ea.build_events([a, b])
        self.assertEqual(stats["events"], 2)

    def test_same_family_different_types_can_merge(self):
        # 「退役」与「国家队生涯」是同一件事的两种写法，同族 → 可合并
        a = item("Ronaldo retires from Portugal duty", "bbc.co.uk",
                 "https://www.bbc.co.uk/a3")
        b = item("Ronaldo's Portugal career ends after 21 years", "reuters.com",
                 "https://www.reuters.com/b3")
        events, stats = ea.build_events([a, b])
        self.assertEqual(stats["events"], 1)


class TestHardConflict(unittest.TestCase):
    """硬冲突绝对否决：不设「相关度够高就放行」的例外。"""

    def _conflict(self, ta, tb):
        a = ea._prep(item(ta, "bbc.co.uk", "https://www.bbc.co.uk/x1"))
        b = ea._prep(item(tb, "skysports.com", "https://www.skysports.com/x2"))
        return ea._hard_conflict(a, b)

    def test_different_person_vetoes(self):
        ok, why = self._conflict(
            "Cristiano Ronaldo announces retirement from international football",
            "Lionel Messi announces retirement from international football")
        self.assertTrue(ok)
        self.assertIn("不同", why)

    def test_different_club_vetoes(self):
        ok, _ = self._conflict(
            "Arsenal sign winger Michael Olise in 60m deal",
            "Chelsea sign defender Marc Guehi in 45m deal")
        self.assertTrue(ok)

    def test_different_score_vetoes(self):
        ok, why = self._conflict("Arsenal beat Chelsea 2-0 at the Emirates",
                                 "Arsenal beat Chelsea 3-1 at Stamford Bridge")
        self.assertTrue(ok)
        self.assertIn("数字", why)

    def test_same_subject_no_conflict(self):
        ok, _ = self._conflict(
            "Ronaldo retires from Portugal duty",
            "Ronaldo announces international retirement on Instagram")
        self.assertFalse(ok)


class TestAdversarialNoWrongMerge(unittest.TestCase):
    """对抗用例：看起来很像、实际是两件事。"""

    CASES = [
        ("两名球员同日退役",
         ["Cristiano Ronaldo announces retirement from international football",
          "Lionel Messi announces retirement from international football"], 2),
        ("同俱乐部两笔不同转会",
         ["Arsenal sign winger Michael Olise in 60m deal",
          "Arsenal sign defender Marc Guehi in 45m deal"], 2),
        ("两场不同比分的比赛",
         ["Arsenal beat Chelsea 2-0 at the Emirates",
          "Arsenal beat Chelsea 3-1 at Stamford Bridge"], 2),
        ("同俱乐部两名球员受伤",
         ["Newcastle confirm Sven Botman out for eight weeks with knee injury",
          "Newcastle confirm Anthony Gordon out for six weeks with hamstring "
          "injury"], 2),
        ("所有权变更 vs 赞助签约",
         ["Chelsea takeover completed as consortium buys majority stake",
          "Chelsea announce new shirt sponsor in record deal"], 2),
        ("同一事件的不同措辞",
         ["Ronaldo retires from Portugal duty",
          "Ronaldo announces international retirement on Instagram",
          "Ronaldo's most iconic moments as he retires from international "
          "football"], 1),
    ]

    def test_cases(self):
        domains = ["bbc.co.uk", "skysports.com", "thesun.co.uk"]
        for name, titles, expected in self.CASES:
            with self.subTest(case=name):
                items = [item(t, domains[i % 3],
                              "https://www.{}/a{}".format(domains[i % 3], i),
                              published="2026-11-05T{:02d}:00:00Z".format(10 + i))
                         for i, t in enumerate(titles)]
                _, stats = ea.build_events(items)
                self.assertEqual(stats["events"], expected, name)

    def test_identical_title_outside_window_still_merges(self):
        # 标题完全一致 = 同一件事的后续报道，指纹规则优先于时间窗
        a = item("Everton points deduction appeal rejected by FA", "bbc.co.uk",
                 "https://www.bbc.co.uk/a7", published="2026-11-01T10:00:00Z")
        b = item("Everton points deduction appeal rejected by FA",
                 "skysports.com", "https://www.skysports.com/b7",
                 published="2026-11-06T10:00:00Z")
        _, stats = ea.build_events([a, b])
        self.assertEqual(stats["events"], 1)


class TestEmpty(unittest.TestCase):
    def test_empty_input(self):
        events, stats = ea.build_events([])
        self.assertEqual(events, [])
        self.assertEqual(stats["input"], 0)

    def test_blank_titles_ignored(self):
        events, stats = ea.build_events([{"title": "   ", "link": "x"}])
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
