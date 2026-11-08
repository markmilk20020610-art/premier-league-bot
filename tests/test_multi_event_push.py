"""T12 验收：S 级每日即时推送软上限（3 条）。

核心命题（必须由测试守住）：
  · 软上限 ≠ 硬闸门：第 4 条及以上的**独立** S 事件照常推送
  · 不得降级、不得推迟、不得丢弃——函数不返回任何被拦下的事件
  · 同一事件的多次进展只算 1 条（聚合层已合并，这里按 event_id 双保险）
  · 溢出必须落盘留痕（state["s_overflow"]），不能静默
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import importance as imp


TODAY = "2026-11-05"


def s_event(eid, title=None):
    return {
        "event_id": eid,
        "title": title or ("S 级事件 " + eid),
        "importance": imp.S,
        "content_type": "BREAKING_NEWS",
        "scope": imp.GLOBAL_EVENT_LINE,
        "cred": "E2",
        "evidence_refs": [],
    }


class TestSoftCap(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def test_first_three_no_note(self):
        events = [s_event("e{}".format(i)) for i in range(3)]
        out, bucket = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertEqual(len(out), 3)
        self.assertEqual(bucket["count"], 3)
        self.assertTrue(all(e["s_push_note"] == "" for e in out))

    def test_fourth_still_pushed_with_note(self):
        events = [s_event("e{}".format(i)) for i in range(4)]
        out, bucket = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        # 第 4 条没有被拦下——软上限不是闸门
        self.assertEqual(len(out), 4)
        self.assertEqual(bucket["count"], 4)
        self.assertEqual(out[3]["s_push_note"], "今日第 4 条 S 级事件")

    def test_fourth_not_downgraded(self):
        events = [s_event("e{}".format(i)) for i in range(4)]
        out, _ = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        for e in out:
            self.assertEqual(e["importance"], imp.S)

    def test_ten_events_all_pushed(self):
        events = [s_event("e{}".format(i)) for i in range(10)]
        out, bucket = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertEqual(len(out), 10)
        self.assertEqual(bucket["count"], 10)
        self.assertEqual(sum(1 for e in out if e["s_push_note"]), 7)

    def test_same_event_progress_counts_once(self):
        # 同一事件的 4 次进展 → 只算 1 条，且都不带溢出标注
        events = [s_event("same") for _ in range(4)]
        out, bucket = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertEqual(len(out), 4)          # 照常推送
        self.assertEqual(bucket["count"], 1)   # 只计 1 条
        self.assertTrue(all(e["s_push_note"] == "" for e in out))

    def test_same_event_keeps_push_index(self):
        events = [s_event("same") for _ in range(2)]
        out, _ = bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertEqual([e["s_push_index"] for e in out], [1, 1])

    def test_overflow_persisted(self):
        events = [s_event("e{}".format(i)) for i in range(5)]
        bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertIn(TODAY, self.state["s_overflow"])
        rec = self.state["s_overflow"][TODAY]
        self.assertEqual(rec["count"], 5)
        self.assertEqual(len(rec["events"]), 5)

    def test_counter_accumulates_across_calls(self):
        # 同一天分两批推送，计数必须累加，不能每批重新从 1 开始
        bot._apply_s_soft_cap(self.state, [s_event("a"), s_event("b")],
                              today=TODAY)
        out, bucket = bot._apply_s_soft_cap(
            self.state, [s_event("c"), s_event("d")], today=TODAY)
        self.assertEqual(bucket["count"], 4)
        self.assertEqual(out[0]["s_push_note"], "")
        self.assertEqual(out[1]["s_push_note"], "今日第 4 条 S 级事件")

    def test_new_day_resets_counter(self):
        bot._apply_s_soft_cap(self.state, [s_event("a")], today="2026-11-05")
        _, bucket = bot._apply_s_soft_cap(self.state, [s_event("b")],
                                          today="2026-11-06")
        self.assertEqual(bucket["count"], 1)

    def test_retention_seven_days(self):
        for d in range(1, 12):
            bot._apply_s_soft_cap(self.state, [s_event("e{}".format(d))],
                                  today="2026-11-{:02d}".format(d))
        self.assertLessEqual(len(self.state["s_overflow"]), 7)
        self.assertIn("2026-11-11", self.state["s_overflow"])

    def test_empty_input(self):
        out, bucket = bot._apply_s_soft_cap(self.state, [], today=TODAY)
        self.assertEqual(out, [])
        self.assertEqual(bucket["count"], 0)

    def test_input_not_mutated(self):
        events = [s_event("a")]
        bot._apply_s_soft_cap(self.state, events, today=TODAY)
        self.assertNotIn("s_push_note", events[0])

    def test_bad_state_shape_recovered(self):
        self.state["s_overflow"] = "not-a-dict"
        out, bucket = bot._apply_s_soft_cap(self.state, [s_event("a")],
                                            today=TODAY)
        self.assertEqual(bucket["count"], 1)
        self.assertIsInstance(self.state["s_overflow"], dict)


class TestCapConstant(unittest.TestCase):
    def test_cap_is_three(self):
        self.assertEqual(bot.S_PUSH_SOFT_CAP, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
