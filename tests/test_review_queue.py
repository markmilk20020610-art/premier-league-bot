"""T13 验收：E4 级 S 事件必须进入内部人工审核队列。

核心命题（必须由测试守住）：
  · E4 + S 不得公开推送为确定事实，必须转 PENDING_REVIEW
  · 待审记录不进公开通道：不发 Telegram、不写推送队列、push_state 仍为 UNPUSHED
  · 人工批准 ≠ 事实核验完成：cred 不因批准而改变
  · 拒绝进终态 REVIEW_DECLINED，不得复用 DELIVERY_FAILED（会让失败率失真、watchdog 误重试）
  · 第二独立来源到达 → 自动退出审核，回到正常投递流程
  · 状态机严格分工：delivery_status 管「能不能进管道」，push_state 管「是否已露给用户」
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import evidence as ev
import state_store as store


def make_event(eid="ev_test", importance="S", cred="E4",
               content_type="BREAKING_NEWS", title="Ronaldo retires from Portugal duty"):
    return {
        "event_id": eid,
        "title": title,
        "importance": importance,
        "cred": cred,
        "content_type": content_type,
        "evidence_status": ev.UNVERIFIED if cred == "E4" else ev.SINGLE_SOURCE,
        "evidence_refs": [
            {"url": "https://www.thesun.co.uk/sport/1", "title": title},
            {"url": "https://www.dailymail.co.uk/sport/2", "title": title},
        ],
    }


class TestReviewRoute(unittest.TestCase):
    def test_e4_s_goes_to_review(self):
        route, reason = bot._review_route(make_event())
        self.assertEqual(route, "REVIEW")
        self.assertTrue(reason)

    def test_e2_s_pushes(self):
        route, _ = bot._review_route(make_event(cred="E2"))
        self.assertEqual(route, "PUSH")

    def test_e4_a_pushes(self):
        # 只有 S 级才拦；A 级 E4 走既有的单一来源规则，不在这里处理
        route, _ = bot._review_route(make_event(importance="A"))
        self.assertEqual(route, "PUSH")

    def test_low_value_archived(self):
        route, _ = bot._review_route(
            make_event(cred="E2", content_type="PROMOTION"))
        self.assertEqual(route, "ARCHIVE")

    def test_low_value_s_still_archived_not_review(self):
        # 推广就是推广，哪怕是 S 级 E4 也不该占用人工审核的时间
        route, _ = bot._review_route(
            make_event(content_type="GOSSIP"))
        self.assertEqual(route, "ARCHIVE")


class TestEnqueue(unittest.TestCase):
    def setUp(self):
        self.state = {"events": {}}
        self.evt = make_event()

    def test_enqueued_as_pending_review(self):
        rec = bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)

    def test_not_in_public_channel(self):
        rec = bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        # 没有 message_id = 没发出去
        self.assertIsNone(rec.get("telegram_message_id"))
        # 不在投递管道里
        self.assertNotIn(rec["delivery_status"],
                         (store.PENDING_DELIVERY, store.DELIVERED,
                          store.COMMITTED))
        # push_state 属于另一个状态机，此时仍是 UNPUSHED
        self.assertNotEqual(rec.get("push_state"), "PUSHED")

    def test_review_metadata_recorded(self):
        rec = bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        self.assertEqual(rec["review_status"], "PENDING")
        self.assertEqual(rec["review_importance"], "S")
        self.assertEqual(rec["review_cred"], "E4")
        self.assertTrue(rec["review_enqueued_at"])
        self.assertEqual(rec["review_log"][-1]["action"], "ENQUEUED")

    def test_all_evidence_refs_registered(self):
        rec = bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        urls = {r.get("url") for r in rec.get("evidence_refs") or []}
        for ref in self.evt["evidence_refs"]:
            self.assertIn(ref["url"], urls)

    def test_listed_in_pending_queue(self):
        bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        pending = bot.list_pending_reviews(self.state)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event_id"], "ev_test")

    def test_event_without_id_rejected(self):
        evt = make_event(eid="")
        self.assertIsNone(bot._enqueue_review(self.state, evt, "x"))

    def test_blocks_rediscovery(self):
        rec = bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")
        self.assertTrue(store.blocks_rediscovery(rec))


class TestApprove(unittest.TestCase):
    def setUp(self):
        self.state = {"events": {}}
        self.evt = make_event()
        bot._enqueue_review(self.state, self.evt, "E4 级 S 事件")

    def test_approve_moves_to_delivery(self):
        rec = bot.approve_review(self.state, "ev_test", "editor_01", "确认措辞")
        self.assertEqual(rec["delivery_status"], store.PENDING_DELIVERY)
        self.assertEqual(rec["review_status"], "APPROVED")

    def test_approve_does_not_change_cred(self):
        rec = bot.approve_review(self.state, "ev_test", "editor_01")
        # 人工批准 ≠ 事实核验完成
        self.assertEqual(rec["review_cred"], "E4")
        self.assertTrue(rec["cred_unchanged_by_approval"])

    def test_approve_forces_attributed_wording(self):
        rec = bot.approve_review(self.state, "ev_test", "editor_01")
        self.assertEqual(rec["publish_mode"], "ATTRIBUTED")

    def test_approve_logged(self):
        rec = bot.approve_review(self.state, "ev_test", "editor_01", "ok")
        last = rec["review_log"][-1]
        self.assertEqual(last["action"], "APPROVED")
        self.assertEqual(last["operator_id"], "editor_01")
        self.assertEqual(last["cred_after"], last["prev_cred"])

    def test_approve_twice_rejected(self):
        bot.approve_review(self.state, "ev_test", "editor_01")
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, "ev_test", "editor_01")

    def test_approve_unknown_rejected(self):
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, "nope", "editor_01")

    def test_no_longer_pending(self):
        bot.approve_review(self.state, "ev_test", "editor_01")
        self.assertEqual(bot.list_pending_reviews(self.state), [])


class TestDecline(unittest.TestCase):
    def setUp(self):
        self.state = {"events": {}}
        bot._enqueue_review(self.state, make_event(), "E4 级 S 事件")

    def test_decline_terminal(self):
        rec = bot.decline_review(self.state, "ev_test", "editor_01", "不采信")
        self.assertEqual(rec["delivery_status"], store.REVIEW_DECLINED)
        self.assertEqual(rec["review_status"], "DECLINED")

    def test_decline_not_marked_as_failure(self):
        rec = bot.decline_review(self.state, "ev_test", "editor_01")
        # 复用 DELIVERY_FAILED 会让失败率指标失真、watchdog 误重试
        self.assertNotEqual(rec["delivery_status"], store.DELIVERY_FAILED)

    def test_declined_is_terminal(self):
        bot.decline_review(self.state, "ev_test", "editor_01")
        rec = self.state["events"]["ev_test"]
        with self.assertRaises(store.StateError):
            store.transition(rec, store.PENDING_DELIVERY)

    def test_declined_blocks_requeue(self):
        rec = bot.decline_review(self.state, "ev_test", "editor_01")
        self.assertTrue(store.blocks_rediscovery(rec))

    def test_cannot_approve_after_decline(self):
        bot.decline_review(self.state, "ev_test", "editor_01")
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, "ev_test", "editor_01")


class TestAutoExitOnSecondSource(unittest.TestCase):
    """第二**独立**来源到达 → 自动退出审核。

    注意签名：`recheck_review` 不接受「证据等级」参数。
    调用方只能追加证据行，结论由程序按现有规则重算——
    否则「抓到第二条的人」可以自己声明 E2 把 E4 送出去。
    """

    def setUp(self):
        self.state = {"events": {}}
        # 初始只有一条低可信来源，判 E4 入队
        bot._enqueue_review(self.state, make_event(), "E4 级 S 事件")

    def _rows(self, pairs):
        return [{"url": u, "title": t} for u, t in pairs]

    def test_stays_when_still_e4(self):
        # 只有原记录里那一条，证据没变 → 继续待审
        self.assertIsNone(bot.recheck_review(self.state, "ev_test"))
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)

    def test_reprint_does_not_exit(self):
        # 3 家媒体转载同一篇稿 → 1 个独立发布方 → 不得退出
        rows = self._rows([
            ("https://www.bbc.co.uk/sport/r1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.mirror.co.uk/sport/r2",
             "Ronaldo retires from Portugal duty"),
        ])
        self.assertIsNone(bot.recheck_review(self.state, "ev_test", rows))
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)

    def test_same_publisher_two_pieces_does_not_exit(self):
        # 同一家媒体的两篇稿只算 1 个独立发布方。
        # 必须用一个只有单一域名的记录来测，否则初始记录里的
        # thesun + dailymail 会凑成第二个「发布方」，把结论带偏。
        state = {"events": {}}
        bot._enqueue_review(state, {
            "event_id": "ev_sky", "title": "Ronaldo retires from Portugal duty",
            "importance": "S", "cred": "E4", "content_type": "BREAKING_NEWS",
            "evidence_status": ev.UNVERIFIED,
            "evidence_refs": [{"url": "https://www.skysports.com/news/s0",
                               "title": "Ronaldo retires from Portugal duty"}],
        }, "E4 级 S 事件")
        rows = self._rows([
            ("https://www.skysports.com/news/s1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.skysports.com/live-blog/s2",
             "Ronaldo calls time on Portugal career"),
        ])
        self.assertIsNone(bot.recheck_review(state, "ev_sky", rows))
        self.assertEqual(len(bot.list_pending_reviews(state)), 1)

    def test_two_independent_publishers_exits(self):
        rows = self._rows([
            ("https://www.bbc.co.uk/sport/i1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.skysports.com/football/i2",
             "Ronaldo announces international retirement on Instagram"),
        ])
        rec = bot.recheck_review(self.state, "ev_test", rows)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["delivery_status"], store.PENDING_DELIVERY)
        self.assertEqual(rec["review_status"], "AUTO_EXITED")
        self.assertGreaterEqual(rec["review_exit_groups"], 2)
        self.assertIn("独立来源", rec["review_exit_reason"])

    def test_official_confirmation_exits_alone(self):
        rows = self._rows([
            ("https://www.manutd.com/news/official",
             "Ronaldo retires from international football"),
        ])
        rec = bot.recheck_review(self.state, "ev_test", rows)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["review_exit_evidence"], ev.OFFICIAL_CONFIRMED)

    def test_caller_cannot_forge_status(self):
        # 早期签名是 recheck_review(state, id, evidence_status)。
        # 传一个状态字符串必须**无效**——不能凭声明退出。
        self.assertIsNone(
            bot.recheck_review(self.state, "ev_test",
                               ev.MULTIPLE_INDEPENDENT_SOURCES))
        self.assertEqual(len(bot.list_pending_reviews(self.state)), 1)

    def test_exit_leaves_queue(self):
        rows = self._rows([
            ("https://www.bbc.co.uk/sport/i1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.skysports.com/football/i2",
             "Ronaldo announces international retirement on Instagram"),
        ])
        bot.recheck_review(self.state, "ev_test", rows)
        self.assertEqual(bot.list_pending_reviews(self.state), [])

    def test_exit_logged(self):
        rows = self._rows([
            ("https://www.bbc.co.uk/sport/i1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.skysports.com/football/i2",
             "Ronaldo announces international retirement on Instagram"),
        ])
        rec = bot.recheck_review(self.state, "ev_test", rows)
        self.assertEqual(rec["review_log"][-1]["action"], "AUTO_EXITED")

    def test_unknown_event_ignored(self):
        self.assertIsNone(bot.recheck_review(self.state, "nope"))

    def test_not_pending_ignored(self):
        bot.approve_review(self.state, "ev_test", "editor_01")
        rows = self._rows([
            ("https://www.bbc.co.uk/sport/i1",
             "Ronaldo retires from Portugal duty"),
            ("https://www.skysports.com/football/i2",
             "Ronaldo announces international retirement on Instagram"),
        ])
        self.assertIsNone(bot.recheck_review(self.state, "ev_test", rows))


class TestStateMachineSeparation(unittest.TestCase):
    """delivery_status 与 push_state 是两个状态机，不得混用。"""

    def test_pending_review_is_not_a_push_state(self):
        state = {"events": {}}
        rec = bot._enqueue_review(state, make_event(), "E4 级 S 事件")
        # PENDING_REVIEW 只出现在 delivery_status
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)
        self.assertNotIn("PENDING_REVIEW", str(rec.get("push_state") or ""))

    def test_transitions_declared(self):
        self.assertIn(store.PENDING_REVIEW, store._TRANSITIONS[store.VERIFIED])
        self.assertEqual(store._TRANSITIONS[store.PENDING_REVIEW],
                         {store.PENDING_DELIVERY, store.REVIEW_DECLINED})
        self.assertEqual(store._TRANSITIONS[store.REVIEW_DECLINED], set())

    def test_schema_version_bumped(self):
        self.assertGreaterEqual(store.SCHEMA_VERSION, 4)


class TestSignificanceAdmission(unittest.TestCase):
    """准入看「够不够大」，不看「现在有几家媒体」。"""

    def test_single_source_mega_event_still_admitted(self):
        import importance as imp
        title = "Ronaldo retires from Portugal duty"
        et = ev.primary_event_type(title)
        # 真实等级（单源）只是 A
        self.assertEqual(
            imp.assess(title, event_type=et, independent_groups=1)["importance"],
            imp.A)
        # 但准入量级是 S —— 否则这条永远进不了审核队列
        self.assertEqual(imp.significance(title, event_type=et), imp.S)
        route, _ = bot._review_route(make_event(importance=imp.S))
        self.assertEqual(route, "REVIEW")

    def test_promotion_ceiling_still_applies(self):
        import importance as imp
        title = "Cristiano Ronaldo launches new CR7 boots collection"
        et = ev.primary_event_type(title)
        self.assertEqual(
            imp.significance(title, event_type=et, content_type="PROMOTION"),
            imp.C)


if __name__ == "__main__":
    unittest.main(verbosity=2)
