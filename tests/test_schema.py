"""阶段 1.3 验收：模型输出 Schema 校验。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schema


def good_scan():
    return {
        "has_valid_news": True,
        "var_rating": "✨【金哨认证·绝对真理】(Tier 1)",
        "event_title": "英足总驳回阿森纳申诉",
        "fact_summary": "英足总官方公告确认驳回阿森纳关于道曼红牌的申诉。",
        "evidence_url": "https://www.thefa.com/news/2026/oct/30/statement",
        "evidence_sources": [{"domain": "thefa.com", "source_class": "OFFICIAL", "url": "https://www.thefa.com/x"}],
        "evidence_status": "OFFICIAL_CONFIRMED",
        "evidence_sources_count": 1,
        "draft_a": "这次申诉被驳回，阿森纳冤不冤？你站哪一边？",
        "draft_b": "英足总官方公告已确认驳回，BBC 同步报道。",
        "draft_c": "纪律条例规定申诉窗口 48 小时，逾期不受理。",
        "cover_blueprint": "深色底 + 红色警示条",
        "image_prompt": "3d caricature style referee holding red card",
        "banner_overlay_text": "申诉被驳",
    }


class TestScanSchema(unittest.TestCase):
    def test_valid(self):
        ok, errors, data = schema.validate_scan(good_scan())
        self.assertTrue(ok, errors)
        self.assertEqual(data["var_rating_key"], "Tier 1")

    def test_has_valid_news_must_be_bool(self):
        d = good_scan()
        d["has_valid_news"] = "true"
        ok, errors, _ = schema.validate_scan(d)
        self.assertFalse(ok)
        self.assertIn("布尔值", errors[0])

    def test_missing_required_field(self):
        for field in ("event_title", "fact_summary", "draft_a", "draft_b", "draft_c",
                      "cover_blueprint", "image_prompt", "banner_overlay_text"):
            d = good_scan()
            d.pop(field)
            ok, errors, _ = schema.validate_scan(d)
            self.assertFalse(ok, f"{field} 缺失应当被拦下")

    def test_bad_evidence_url(self):
        d = good_scan()
        d["evidence_url"] = "javascript:alert(1)"
        ok, errors, _ = schema.validate_scan(d)
        self.assertFalse(ok)
        self.assertTrue(any("evidence_url" in e for e in errors))

    def test_illegal_rating(self):
        d = good_scan()
        d["var_rating"] = "S 级神哨"
        ok, errors, _ = schema.validate_scan(d)
        self.assertFalse(ok)
        self.assertTrue(any("var_rating" in e for e in errors))

    def test_illegal_evidence_status(self):
        d = good_scan()
        d["evidence_status"] = "VERIFIED_VIA_DEEPSEEK"
        ok, errors, _ = schema.validate_scan(d)
        self.assertFalse(ok)

    def test_empty_evidence_sources(self):
        d = good_scan()
        d["evidence_sources"] = []
        ok, errors, _ = schema.validate_scan(d)
        self.assertFalse(ok)

    def test_rating_normalization(self):
        for raw, expect in [
            ("Tier1", "Tier 1"), ("tier 2", "Tier 2"),
            ("💛【吃瓜狂欢·黄牌警告】(Tier 3)", "Tier 3"),
            ("🐦‍⬛【官方严谨·黑标警示】", "黑标"),
            ("完全看不懂", None),
        ]:
            self.assertEqual(schema.normalize_rating(raw), expect, raw)

    def test_has_valid_news_false_short_circuits(self):
        ok, errors, _ = schema.validate_scan({"has_valid_news": False})
        self.assertTrue(ok, errors)


class TestSummarySchema(unittest.TestCase):
    def good_summary(self):
        return {
            "has_valid_news": True,
            "note_title": "曼联换帅｜48小时内的三条线索",
            "hook": "阿莫林的位置，比你想的更稳，也更危险。",
            "body": "【一】先说结论\n说人话就是：帅位暂时安全，但压力在累积。" * 3,
            "tags": "#英超 #曼联 #阿莫林",
            # V1.2 任务 6：每个 top_event 必须能追溯到程序侧的已验证事件
            "top_events": [{
                "rank": 1,
                "event_id": "E01",
                "title": "曼联内部会议",
                "one_line": "高层讨论帅位",
                "evidence_status": "MULTIPLE_INDEPENDENT_SOURCES",
                "source_urls": ["https://www.bbc.co.uk/sport/football/articles/c1"],
                "published_at": "2026-10-30T22:00:00+00:00",
                "attribution": "bbc.co.uk（一线媒体）",
            }],
            "fact_check_note": "信息截至 10月31日 06:00",
            "cover_blueprint": "深色底 + 红色标题",
            "image_prompt": "3d caricature style manager under pressure",
            "banner_overlay_text": "帅位悬念",
        }

    def test_valid(self):
        ok, errors, _ = schema.validate_summary(self.good_summary())
        self.assertTrue(ok, errors)

    def test_missing_top_events(self):
        d = self.good_summary()
        d["top_events"] = []
        ok, errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_body_too_short(self):
        d = self.good_summary()
        d["body"] = "太短"
        ok, errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)


class TestSummarySchemaV12(unittest.TestCase):
    """V1.2 任务 6：top_event 必须可追溯到程序侧已验证事件。"""

    def base(self):
        return {
            "has_valid_news": True,
            "note_title": "标题",
            "hook": "钩子一句话",
            "body": "正文" * 40,
            "tags": "#英超",
            "top_events": [{
                "rank": 1,
                "event_id": "E01",
                "title": "事件",
                "one_line": "一句话",
                "evidence_status": "OFFICIAL_CONFIRMED",
                "source_urls": ["https://www.arsenal.com/news/x"],
                "published_at": "2026-10-30T22:00:00+00:00",
                "attribution": "arsenal.com（官方）",
            }],
            "fact_check_note": "截至 06:00",
            "cover_blueprint": "深色底",
            "image_prompt": "3d caricature style",
            "banner_overlay_text": "标题",
        }

    def test_all_required_fields_present(self):
        ok, errors, _ = schema.validate_summary(self.base(), allowed_event_ids={"E01"})
        self.assertTrue(ok, errors)

    def test_missing_event_id(self):
        d = self.base()
        d["top_events"][0].pop("event_id")
        ok, errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)
        self.assertIn("event_id", schema.describe_errors(errors))

    def test_missing_evidence_status(self):
        d = self.base()
        d["top_events"][0].pop("evidence_status")
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_illegal_evidence_status(self):
        d = self.base()
        d["top_events"][0]["evidence_status"] = "TOTALLY_CONFIRMED"
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_missing_source_urls(self):
        d = self.base()
        d["top_events"][0]["source_urls"] = []
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_illegal_source_url(self):
        d = self.base()
        d["top_events"][0]["source_urls"] = ["not-a-url"]
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_missing_published_at(self):
        d = self.base()
        d["top_events"][0].pop("published_at")
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_missing_attribution(self):
        d = self.base()
        d["top_events"][0].pop("attribution")
        ok, _errors, _ = schema.validate_summary(d)
        self.assertFalse(ok)

    def test_event_id_outside_allowed_set(self):
        """模型自己造了一条新闻池里不存在的事件 → 拒绝整篇早报。"""
        d = self.base()
        d["top_events"][0]["event_id"] = "E99"
        ok, errors, _ = schema.validate_summary(d, allowed_event_ids={"E01", "E02"})
        self.assertFalse(ok)
        self.assertIn("不在已验证事件集合内", schema.describe_errors(errors))

    def test_has_valid_news_false_short_circuits(self):
        ok, errors, _ = schema.validate_summary({"has_valid_news": False})
        self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
