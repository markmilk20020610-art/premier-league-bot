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
            "top_events": [{"rank": 1, "title": "曼联内部会议", "one_line": "高层讨论帅位"}],
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
