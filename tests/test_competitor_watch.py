"""对手发布时间采集 —— 离线测试。

夹具来自 2026-10-31 的真实响应（含当晚 Chelsea vs Man Utd 的真实首发新闻），
只裁掉用不到的字段。时间戳全部按真实值保留，方便验证北京时间→UTC 的换算。
"""

import calendar
import datetime
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import competitor_watch as cw         # noqa: E402
import lineup_monitor as lm           # noqa: E402

NOW = datetime.datetime(2026, 10, 31, 13, 0)

# ---- 懂球帝：第一条是真实脏数据（published_at 写成 2030 年）----
DQD = {"articles": [
    {"title": "官方：国足11月热身赛地点改为广东肇庆",
     "published_at": "2030-10-30 15:02:51",      # 脏：未来时间
     "created_at": "2026-10-31 15:02:51",        # 真：07:02:51Z
     "share": "https://www.dongqiudi.com/article/1"},
    {"title": "切尔西vs曼联首发：帕尔默领衔",
     "published_at": "2026-10-31 19:16:12",      # → 11:16:12Z
     "created_at": "2026-10-31 19:10:00",
     "share": "https://www.dongqiudi.com/article/2"},
]}

# ---- 虎扑：真实字段顺序 title → img → link → publishTime → addTime ----
def hupu_fixture():
    epoch = calendar.timegm((2026, 10, 31, 11, 16, 12))
    return (
        '{"nid":"1","type":"IMG_TEXT","title":"剑南春首发-切尔西vs曼联：B费领衔",'
        '"img":"https://i1.hoopchina.com.cn/a.jpg","replies":3,"lights":1,'
        '"link":"https://bbs.hupu.com/643499023.html","linkType":null,'
        '"publishTime":"2026-10-31 19:16:12","addTime":%d,"oldNewsId":"1"}'
        % epoch
    )


# ---- 直播吧：列表只有日期，文章页才有秒 ----
ZB8_LIST = (
    '<li><a  target="_blank" href="//news.zhibo8.com/zuqiu/2026-10-31/abc123native.htm">'
    '切尔西vs曼联首发：B费回归！帕尔默、埃斯特旺 约罗、库尼亚先发</a></li>'
    '<li><a  target="_blank" href="//news.zhibo8.com/zuqiu/2026-10-31/def456native.htm">'
    '曼联过去5年卖人亏1.55亿镑</a></li>'
)
ZB8_ARTICLE = '<div class="article"><span>2026-10-31 19:16:56</span><p>正文</p></div>'


def transport(routes):
    def _t(url, timeout=20, headers=None):
        if url not in routes:
            raise ConnectionError(f"no route: {url}")
        return routes[url]
    return _t


# =============================================================================
# A. 时间处理
# =============================================================================
class TestTime(unittest.TestCase):
    def test_beijing_to_utc(self):
        ts = cw.beijing_to_utc("2026-10-31 19:16:12")
        self.assertEqual(ts, datetime.datetime(2026, 10, 31, 11, 16, 12))

    def test_beijing_to_utc_date_only(self):
        self.assertEqual(cw.beijing_to_utc("2026-10-31"),
                         datetime.datetime(2026, 10, 30, 16, 0))

    def test_unparsable_returns_none(self):
        self.assertIsNone(cw.beijing_to_utc("昨天"))
        self.assertIsNone(cw.beijing_to_utc(""))

    def test_future_timestamp_is_implausible(self):
        """懂球帝真实出现过 2030 年的置顶时间，必须被判为不可信。"""
        self.assertFalse(cw.is_plausible(datetime.datetime(2030, 10, 30), NOW))

    def test_too_old_is_implausible(self):
        self.assertFalse(cw.is_plausible(NOW - datetime.timedelta(hours=48), NOW))

    def test_recent_is_plausible(self):
        self.assertTrue(cw.is_plausible(NOW - datetime.timedelta(minutes=5), NOW))


# =============================================================================
# B. 懂球帝
# =============================================================================
class TestDongqiudi(unittest.TestCase):
    def test_dirty_published_at_falls_back_to_created_at(self):
        items = cw.parse_dongqiudi(DQD, NOW)
        first = items[0]
        self.assertEqual(first["published_at"], "2026-10-31T07:02:51Z",
                         "published_at 是假未来时间，必须退回 created_at")
        self.assertTrue(first["timestamp_suspect"])

    def test_normal_item_uses_published_at(self):
        items = cw.parse_dongqiudi(DQD, NOW)
        second = items[1]
        self.assertEqual(second["published_at"], "2026-10-31T11:16:12Z")
        self.assertFalse(second["timestamp_suspect"])

    def test_url_and_title_extracted(self):
        items = cw.parse_dongqiudi(DQD, NOW)
        self.assertEqual(items[1]["url"], "https://www.dongqiudi.com/article/2")
        self.assertIn("切尔西", items[1]["title"])


# =============================================================================
# C. 虎扑
# =============================================================================
class TestHupu(unittest.TestCase):
    def test_parse_real_field_order(self):
        """publishTime 在 link 之后，按顺序写死的正则必然取不到。"""
        items = cw.parse_hupu(hupu_fixture(), NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published_at"], "2026-10-31T11:16:12Z")
        self.assertEqual(items[0]["url"], "https://bbs.hupu.com/643499023.html")
        self.assertIn("首发", items[0]["title"])

    def test_epoch_mismatch_marks_suspect(self):
        """publishTime 与 addTime 自相矛盾时不可信。"""
        epoch = calendar.timegm((2026, 10, 31, 9, 0, 0))     # 差 2 小时
        html = ('{"title":"t","link":"https://bbs.hupu.com/1.html",'
                '"publishTime":"2026-10-31 19:16:12","addTime":%d}' % epoch)
        items = cw.parse_hupu(html, NOW)
        self.assertTrue(items[0]["timestamp_suspect"])
        self.assertEqual(items[0]["published_at"], "")

    def test_empty_html(self):
        self.assertEqual(cw.parse_hupu("", NOW), [])


# =============================================================================
# D. 直播吧
# =============================================================================
class TestZhibo8(unittest.TestCase):
    def test_list_has_date_only(self):
        items = cw.parse_zhibo8_list(ZB8_LIST)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["date_hint"], "2026-10-31")
        self.assertEqual(items[0]["published_at"], "")
        self.assertEqual(items[0]["precision"], "day")
        self.assertTrue(items[0]["url"].startswith("https://news.zhibo8.com/"))

    def test_article_page_gives_seconds(self):
        ts = cw.parse_zhibo8_article_time(ZB8_ARTICLE)
        self.assertEqual(ts, datetime.datetime(2026, 10, 31, 11, 16, 56))

    def test_resolve_via_extra_request(self):
        items = cw.parse_zhibo8_list(ZB8_LIST)
        hits = cw.find_lineup_mentions(
            items, "Chelsea", "Man Utd", NOW,
            transport=transport({items[0]["url"]: (200, ZB8_ARTICLE)}))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["published_at"], "2026-10-31T11:16:56Z")

    def test_resolve_requests_are_capped(self):
        """直播吧列表近千条，每条都补请求会被封——必须限制补请求数量。"""
        many = [{"source_id": cw.SOURCE_ZHIBO8,
                 "title": f"切尔西vs曼联首发{i}",
                 "url": f"https://news.zhibo8.com/zuqiu/2026-10-31/x{i}.htm",
                 "published_at": "", "date_hint": "2026-10-31",
                 "timestamp_suspect": False, "precision": "day"}
                for i in range(50)]
        calls = []

        def counting_transport(url, timeout=20, headers=None):
            calls.append(url)
            return 200, ZB8_ARTICLE

        hits = cw.find_lineup_mentions(many, "Chelsea", "Man Utd", NOW,
                                       transport=counting_transport, max_resolve=3)
        self.assertEqual(len(calls), 3, f"最多补 3 个请求，实际 {len(calls)}")
        self.assertEqual(len(hits), 3)


# =============================================================================
# E. 匹配
# =============================================================================
class TestMatching(unittest.TestCase):
    def test_lineup_keywords(self):
        self.assertTrue(cw.is_lineup_news("切尔西vs曼联首发：B费回归"))
        self.assertTrue(cw.is_lineup_news("Confirmed line-up: ..."))
        self.assertFalse(cw.is_lineup_news("曼联过去5年卖人亏1.55亿镑"))

    def test_team_hits(self):
        self.assertEqual(cw.team_hits("切尔西vs曼联首发", "Chelsea", "Man Utd"), 2)
        self.assertEqual(cw.team_hits("切尔西首发", "Chelsea", "Man Utd"), 1)
        self.assertEqual(cw.team_hits("利物浦首发", "Chelsea", "Man Utd"), 0)

    def test_requires_both_teams_when_asked(self):
        items = [{"source_id": "hupu", "title": "切尔西首发：帕尔默领衔",
                  "url": "u", "published_at": "2026-10-31T11:00:00Z",
                  "precision": "second"}]
        loose = cw.find_lineup_mentions(items, "Chelsea", "Man Utd", NOW)
        strict = cw.find_lineup_mentions(items, "Chelsea", "Man Utd", NOW,
                                        require_both_teams=True)
        self.assertEqual(len(loose), 1)
        self.assertEqual(strict, [], "严格模式只认同时提到两队的新闻")

    def test_unrelated_news_not_matched(self):
        items = [{"source_id": "hupu", "title": "曼联过去5年卖人亏1.55亿镑",
                  "url": "u", "published_at": "2026-10-31T11:00:00Z",
                  "precision": "second"}]
        self.assertEqual(cw.find_lineup_mentions(items, "Chelsea", "Man Utd", NOW), [])

    def test_earliest_wins(self):
        items = [
            {"source_id": "hupu", "title": "切尔西vs曼联首发A", "url": "a",
             "published_at": "2026-10-31T11:20:00Z", "precision": "second"},
            {"source_id": "dongqiudi", "title": "切尔西vs曼联首发B", "url": "b",
             "published_at": "2026-10-31T11:16:12Z", "precision": "second"},
        ]
        hits = cw.find_lineup_mentions(items, "Chelsea", "Man Utd", NOW)
        self.assertEqual(hits[0]["published_at"], "2026-10-31T11:16:12Z")
        self.assertEqual(cw.earliest_competitor_time(items, "Chelsea", "Man Utd", NOW),
                         "2026-10-31T11:16:12Z")


# =============================================================================
# F. 速度记录 + 与 speed_proof 对接
# =============================================================================
class TestSpeedRecord(unittest.TestCase):
    def _items(self):
        return [
            {"source_id": "hupu", "title": "剑南春首发-切尔西vs曼联：B费领衔",
             "url": "https://bbs.hupu.com/643499023.html",
             "published_at": "2026-10-31T11:16:12Z", "precision": "second"},
            {"source_id": "dongqiudi", "title": "切尔西vs曼联首发：帕尔默领衔",
             "url": "https://www.dongqiudi.com/article/2",
             "published_at": "2026-10-31T11:16:56Z", "precision": "second"},
        ]

    def test_record_uses_earliest_and_keeps_evidence(self):
        rec = cw.competitor_speed_record(self._items(), "Chelsea", "Man Utd", NOW)
        self.assertEqual(rec["competitors_seen_at"], "2026-10-31T11:16:12Z")
        self.assertEqual(rec["competitor_source"], "hupu")
        self.assertEqual(rec["evidence_count"], 2)
        self.assertIn("虎扑", rec["note"])

    def test_no_match_means_no_claim(self):
        rec = cw.competitor_speed_record([], "Chelsea", "Man Utd", NOW)
        self.assertEqual(rec["competitors_seen_at"], "")
        self.assertIn("不得宣称领先", rec["note"])

    def test_feeds_lineup_speed_proof(self):
        """端到端：把对手时间喂进 §5 的速度证明，应算出真实领先秒数。"""
        rec = cw.competitor_speed_record(self._items(), "Chelsea", "Man Utd", NOW)
        speed = {
            "match_id": "epl-20261031-chelsea-manutd",
            "team": "Chelsea",
            "official_published_at": "",
            "first_detected_at": "2026-10-31T11:15:40Z",     # 比虎扑早 32 秒
            "pushed_at": "2026-10-31T11:15:42Z",
        }
        proof = lm.speed_proof(speed, competitors_seen_at=rec["competitors_seen_at"])
        self.assertTrue(proof["proven"])
        self.assertEqual(proof["lead_seconds"], 32)

    def test_without_competitor_time_no_claim(self):
        speed = {
            "match_id": "m", "team": "Chelsea", "official_published_at": "",
            "first_detected_at": "2026-10-31T11:15:40Z",
            "pushed_at": "2026-10-31T11:15:42Z",
        }
        proof = lm.speed_proof(speed, competitors_seen_at="")
        self.assertFalse(proof["proven"])


# =============================================================================
# G. 抓取容错
# =============================================================================
class TestFetch(unittest.TestCase):
    def test_one_source_down_does_not_break_others(self):
        routes = {
            cw.URL_DONGQIUDI: (200, json.dumps(DQD, ensure_ascii=False)),
            cw.URL_HUPU: (500, ""),
            cw.URL_ZHIBO8: (200, ZB8_LIST),
        }
        out = cw.fetch_all(now=NOW, transport=transport(routes), delay=0)
        ok = {s["source_id"]: s["ok"] for s in out["source_status"]}
        self.assertTrue(ok[cw.SOURCE_DONGQIUDI])
        self.assertFalse(ok[cw.SOURCE_HUPU])
        self.assertTrue(ok[cw.SOURCE_ZHIBO8])
        self.assertTrue(out["items"], "其余源的数据仍应保留")

    def test_all_sources_down(self):
        out = cw.fetch_all(now=NOW, transport=transport({}), delay=0)
        self.assertEqual(out["items"], [])
        self.assertTrue(all(not s["ok"] for s in out["source_status"]))

    def test_item_validation(self):
        items = cw.parse_dongqiudi(DQD, NOW)
        for it in items:
            self.assertEqual(cw.validate_news_item(it), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
