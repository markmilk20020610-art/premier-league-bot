"""任务 3 验收：早报排序（事件级）+ T11 保留位允许空置。

核心命题（必须由测试守住）：
  · 同一事件的 N 条报道只占 1 个名额
  · 早报前 40 名里 PROMOTION 数量为 0（不是「排在最后」，是「不进池」）
  · GLOBAL_EVENT_LINE 的 2 个保留位允许空置，不得为填满而抬级
  · S 级事件不占用保留位
  · A 线（英超）覆盖不得低于旧版按时间排序的 90%
  · 2026-11-05 的 C 罗退役必须进前 40
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import content_type as ct
import importance as imp


# ==================== 构造工具 ====================
def ev(event_id, importance, scope, content_type, articles=1):
    """构造一个最小事件，供纯函数级测试使用。"""
    return {
        "event_id": event_id,
        "importance": importance,
        "scope": scope,
        "content_type": content_type,
        "members": [{"title": "{} #{}".format(event_id, i)} for i in range(articles)],
    }


def pool_item(title, domain, url, hour, minute=0, desc=""):
    return {
        "title": title,
        "link": url,
        "desc": desc,
        "publisher": domain,
        "feed_origin": "https://{}/rss".format(domain),
        "published": "2026-11-05T{:02d}:{:02d}:00Z".format(hour, minute),
    }


RONALDO = [
    ("Cristiano Ronaldo's club to donate all ticket sales to tragic Jesus de "
     "Miguel's family and invite them as VIP guests", "thesun.co.uk", "donate", 13),
    ("A bitter end for Ronaldo - the icon who transformed Portugal",
     "theguardian.com", "bitter-end", 14),
    ("Cristiano Ronaldo QUITS international football over explosive fall-out "
     "with Portugal manager", "dailymail.co.uk", "quits", 15),
    ("Cristiano Ronaldo finished second to Messi even in retirement - the "
     "ending to his Portugal career is sad and predictable", "telegraph.co.uk",
     "second", 16),
    ("Trophies, tears, records and rows - Ronaldo's Portugal career will "
     "divide opinion", "bbc.co.uk", "trophies", 17),
    ("Ronaldo retires from Portugal duty", "reuters.com", "retires", 18),
    ("Ronaldo announces international retirement on Instagram!",
     "thesun.co.uk", "instagram", 19),
    ("Ronaldo's most iconic moments as he retires from international football",
     "skysports.com", "iconic", 20),
    ("Ronaldo retires from international football - Manchester United",
     "manutd.com", "manutd", 21),
    ("Ronaldo's Portugal statement in full", "bbc.co.uk", "statement", 22),
]

PL_ITEMS = [
    ("Arsenal agree deal for striker Viktor Gyokeres as talks advance",
     "bbc.co.uk", "arsenal", 12),
    ("Liverpool confirm Alisson out for six weeks with calf injury",
     "skysports.com", "liverpool", 13),
    ("Chelsea takeover completed as consortium buys majority stake",
     "dailymail.co.uk", "chelsea", 14),
    ("Manchester United appoint new sporting director",
     "manutd.com", "united", 15),
    ("Tottenham sign defender Marc Guehi in 45m deal",
     "theguardian.com", "spurs", 16),
    ("Newcastle United face PSR breach hearing over stadium costs",
     "telegraph.co.uk", "newcastle", 17),
    ("Aston Villa beat Wolves 2-0 at Villa Park",
     "bbc.co.uk", "villa", 18),
    ("Everton points deduction appeal rejected by independent panel",
     "skysports.com", "everton", 19),
    ("Brighton manager criticises referee after VAR controversy",
     "theguardian.com", "brighton", 20),
    ("West Ham confirm Julen Lopetegui departure after poor run",
     "bbc.co.uk", "westham", 21),
]

FOREIGN_ITEMS = [
    ("Real Betis and Sevilla draw in pre-season friendly", "marca.com",
     "betis", 11),
    ("Juventus beat Napoli 1-0 in Serie A opener", "gazzetta.it", "juve", 12),
    ("Bayern Munich sign winger from Ajax", "kicker.de", "bayern", 13),
    ("PSG held to draw by Marseille in Ligue 1", "lequipe.fr", "psg", 14),
    ("Flamengo win Brazilian league title", "globo.com", "flamengo", 15),
]

PROMO_ITEMS = [
    ("Cristiano Ronaldo launches new CR7 boots collection", "thesun.co.uk",
     "boots", 22),
    ("Win a signed Arsenal shirt in our competition", "mirror.co.uk",
     "shirt", 22),
    ("Chelsea unveil new home kit for next season", "chelseafc.com", "kit", 22),
    ("Manchester United memberships on sale now with 20% off", "manutd.com",
     "membership", 22),
    ("Vote for your Premier League player of the month", "premierleague.com",
     "vote", 22),
    ("Liverpool announce Black Friday deal on club store", "liverpoolfc.com",
     "blackfriday", 22),
    ("Cristiano Ronaldo's girlfriend shows off new supercar", "thesun.co.uk",
     "supercar", 22),
    ("Tottenham launch new training range for 2026/27 season",
     "tottenhamhotspur.com", "range", 22),
]


def build_pool():
    items = []
    for title, domain, slug, hour in RONALDO:
        items.append(pool_item(title, domain,
                               "https://www.{}/{}".format(domain, slug), hour))
    for title, domain, slug, hour in PL_ITEMS:
        items.append(pool_item(title, domain,
                               "https://www.{}/{}".format(domain, slug), hour))
    for title, domain, slug, hour in FOREIGN_ITEMS:
        items.append(pool_item(title, domain,
                               "https://www.{}/{}".format(domain, slug), hour))
    for title, domain, slug, hour in PROMO_ITEMS:
        items.append(pool_item(title, domain,
                               "https://www.{}/{}".format(domain, slug), hour))
    return items


class TestSelectMorningEvents(unittest.TestCase):
    """_select_morning_events 是纯函数，直接喂合成事件。"""

    def test_low_value_never_selected(self):
        events = [ev("p1", imp.C, imp.A_LINE, ct.PROMOTION),
                  ev("g1", imp.C, imp.A_LINE, ct.GOSSIP),
                  ev("n1", imp.B, imp.A_LINE, ct.COMPETITIVE_NEWS)]
        selected, report = bot._select_morning_events(events, budget=40)
        self.assertEqual([e["event_id"] for e in selected], ["n1"])
        self.assertEqual(report["low_value_selected"], 0)
        self.assertEqual(report["low_value_excluded"], 2)

    def test_routine_c_level_news_still_eligible(self):
        # C 级里「非英超常规赛果」不是低价值内容，仍可填剩余名额
        events = [ev("c1", imp.C, imp.GLOBAL_EVENT_LINE, ct.COMPETITIVE_NEWS)]
        selected, _ = bot._select_morning_events(events, budget=40)
        self.assertEqual(len(selected), 1)

    def test_s_events_not_limited_by_global_slots(self):
        # S 级全部入选，且不消耗保留位
        events = [ev("s1", imp.S, imp.GLOBAL_EVENT_LINE, ct.BREAKING_NEWS),
                  ev("s2", imp.S, imp.GLOBAL_EVENT_LINE, ct.BREAKING_NEWS),
                  ev("s3", imp.S, imp.A_LINE, ct.COMPETITIVE_NEWS),
                  ev("g1", imp.A, imp.GLOBAL_EVENT_LINE, ct.COMPETITIVE_NEWS)]
        selected, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["s_count"], 3)
        self.assertEqual(report["global_slots_used"], 1)
        self.assertEqual(report["global_slots_empty"], 1)

    def test_a_line_before_global_line(self):
        # A 线优先于 GLOBAL_EVENT_LINE：预算紧张时先保住英超
        events = [ev("g1", imp.A, imp.GLOBAL_EVENT_LINE, ct.COMPETITIVE_NEWS),
                  ev("a1", imp.A, imp.A_LINE, ct.COMPETITIVE_NEWS)]
        selected, _ = bot._select_morning_events(events, budget=1)
        self.assertEqual([e["event_id"] for e in selected], ["a1"])

    def test_budget_counts_articles_not_events(self):
        # 一个 9 条报道的事件只吃 3 个素材名额（MORNING_MAX_ARTICLES_PER_EVENT）
        events = [ev("big", imp.S, imp.GLOBAL_EVENT_LINE, ct.BREAKING_NEWS,
                     articles=9)]
        _, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["articles_used"],
                         bot.MORNING_MAX_ARTICLES_PER_EVENT)


class TestT11GlobalSlotsMayStayEmpty(unittest.TestCase):
    """T11：GLOBAL_EVENT_LINE 的 2 个保留位允许空置。"""

    def test_all_a_line_means_two_empty_slots(self):
        events = [ev("a{}".format(i), imp.A, imp.A_LINE, ct.COMPETITIVE_NEWS)
                  for i in range(6)]
        selected, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["global_slots_reserved"], 2)
        self.assertEqual(report["global_slots_used"], 0)
        self.assertEqual(report["global_slots_empty"], 2)
        self.assertTrue(report["global_line_note"])
        # 没有任何 A 线事件被改标成 GLOBAL_EVENT_LINE 去填位
        self.assertFalse(any(e["scope"] == imp.GLOBAL_EVENT_LINE
                             for e in selected))

    def test_no_promotion_promoted_to_fill_slot(self):
        # 唯一「非英超」的东西是推广——宁可空置，也不能抬级填位
        events = [ev("a1", imp.A, imp.A_LINE, ct.COMPETITIVE_NEWS),
                  ev("p1", imp.C, imp.GLOBAL_EVENT_LINE, ct.PROMOTION)]
        selected, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["global_slots_used"], 0)
        self.assertEqual(report["global_slots_empty"], 2)
        self.assertNotIn("p1", [e["event_id"] for e in selected])

    def test_single_global_event_uses_one_slot(self):
        events = [ev("g1", imp.A, imp.GLOBAL_EVENT_LINE, ct.BREAKING_NEWS)]
        _, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["global_slots_used"], 1)
        self.assertEqual(report["global_slots_empty"], 1)
        self.assertIn("空置 1 个", report["global_line_note"])

    def test_three_global_events_capped_at_two(self):
        events = [ev("g{}".format(i), imp.A, imp.GLOBAL_EVENT_LINE,
                     ct.BREAKING_NEWS) for i in range(3)]
        selected, report = bot._select_morning_events(events, budget=40)
        self.assertEqual(report["global_slots_used"], 2)
        self.assertEqual(report["global_slots_empty"], 0)
        self.assertEqual(len([e for e in selected
                              if e["scope"] == imp.GLOBAL_EVENT_LINE]), 2)


class TestMorningPoolEndToEnd(unittest.TestCase):
    """用合成但真实的池子跑通 _events_for_morning。"""

    @classmethod
    def setUpClass(cls):
        cls.state = {"events": {}, "news_pool": []}
        cls.raw_pool = build_pool()
        cls.ordered, cls.events, cls.stats = bot._events_for_morning(
            cls.state, pool=cls.raw_pool)

    def test_ronaldo_retirement_in_top40(self):
        # 验收第 1 项
        retirement = [it for it in self.ordered
                      if "retire" in (it["title"] or "").lower()]
        self.assertTrue(retirement, "C 罗退役未进入早报候选")
        self.assertEqual(retirement[0]["_importance"], imp.S)
        self.assertLessEqual(self.ordered.index(retirement[0]), 40)

    def test_ronaldo_event_takes_at_most_three_slots(self):
        # 退役事件有 9 条报道，但只能占 3 个素材名额
        retirement = [e for e in self.events if e["importance"] == imp.S
                      and e["event_type"] == "RETIREMENT"]
        self.assertEqual(len(retirement), 1)
        eid = retirement[0]["event_id"]
        self.assertEqual(retirement[0]["article_count"], 9)
        taken = [it for it in self.ordered if it["_event_id"] == eid]
        self.assertEqual(len(taken), bot.MORNING_MAX_ARTICLES_PER_EVENT)

    def test_donation_story_keeps_its_own_slot(self):
        # 捐球票是另一件事，单独占 1 个名额，不被并进退役
        donation = [e for e in self.events
                    if "donate" in (e["title"] or "").lower()]
        self.assertEqual(len(donation), 1)
        self.assertEqual(donation[0]["article_count"], 1)

    def test_zero_promotion_in_top40(self):
        # 验收第 2 项
        promo = [it for it in self.ordered
                 if it["_content_type"] == ct.PROMOTION]
        self.assertEqual(promo, [])
        self.assertEqual(self.stats["low_value_selected"], 0)
        # 8 条推广/八卦一条都没进池（有两条低价值内容被聚成 1 个事件，
        # 所以按事件数是 7，按条目数必须是 8）
        promo_titles = {t for t, _, _, _ in PROMO_ITEMS}
        leaked = [it["title"] for it in self.ordered
                  if it["title"] in promo_titles]
        self.assertEqual(leaked, [])
        self.assertGreaterEqual(self.stats["low_value_excluded"], 7)

    def test_pool_within_budget(self):
        self.assertLessEqual(len(self.ordered), bot.SUMMARY_MAX_MATERIALS)

    def test_a_line_coverage_not_worse_than_time_order(self):
        """验收第 3 项：A 线覆盖不得低于旧版按时间排序的 90%。

        口径必须两边一致：只统计**非低价值**内容。
        旧版前 40 名里有 6 条是「Arsenal 球衣抽奖」「Chelsea 新球衣」这类
        带英超队名的广告，把它们算作「A 线覆盖」会让旧版虚高——
        那不是覆盖，是噪音。新版把广告排除后 A 线事实新闻一条没少。
        """
        def is_aline(item):
            return imp.classify_scope(item["title"],
                                      item.get("desc", ""))[0] == imp.A_LINE

        def is_news(item):
            c = ct.classify(item["title"], item.get("desc", ""))
            return not ct.is_low_value(c["content_type"])

        # 旧版路径：按发布时间倒序，取前 40
        old = sorted(self.raw_pool,
                     key=lambda p: p["published"], reverse=True)[:40]
        old_a = sum(1 for p in old if is_aline(p) and is_news(p))
        new_a = sum(1 for p in self.ordered
                    if is_aline(p) and is_news(p))
        self.assertGreater(old_a, 0, "测试数据里没有英超条目，用例无效")
        self.assertGreaterEqual(new_a, int(old_a * 0.9),
                                "A 线覆盖 {} < 旧版 {} 的 90%".format(new_a, old_a))

    def test_no_duplicate_event_in_pool(self):
        # 验收第 4 项：同一事件不重复占名额（成员超过上限时也只取 3 条）
        counts = {}
        for it in self.ordered:
            counts[it["_event_id"]] = counts.get(it["_event_id"], 0) + 1
        for eid, n in counts.items():
            self.assertLessEqual(n, bot.MORNING_MAX_ARTICLES_PER_EVENT, eid)

    def test_events_sorted_by_importance(self):
        ranks = [imp.rank(e["importance"]) for e in self.events]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_stats_have_slot_report(self):
        for k in ("global_slots_reserved", "global_slots_used",
                  "global_slots_empty", "low_value_excluded", "s_count"):
            self.assertIn(k, self.stats)


class TestSwitchOffKeepsOldPath(unittest.TestCase):
    """开关关闭时新逻辑不执行——这是可回滚的技术保证。"""

    def test_default_is_off(self):
        self.assertFalse(bot.IMPORTANCE_SORT)

    def test_constants_match_approved_params(self):
        # 三个已批准参数
        self.assertEqual(bot.GLOBAL_EVENT_SLOTS, 2)
        self.assertEqual(bot.S_PUSH_SOFT_CAP, 3)
        self.assertEqual(bot.MORNING_MAX_ARTICLES_PER_EVENT, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
