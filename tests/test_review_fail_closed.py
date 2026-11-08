"""PR #1 整改 P2：`approve_review()` / `decline_review()` 的 fail-closed 加固。

审查发现的缺陷
--------------
两个函数原来的写法是：

    if client is not None and review_ref:
        try:
            client.decide(review_ref, "approve", operator_id, note)
        except review_store.ReviewConflict as e:
            raise store.StateError(...)

    store.transition(rec, store.PENDING_DELIVERY)

`client is not None and review_ref` 是「**两者都有才校验**」。任意一个不成立，
整段私有 `decide` 被静默跳过，`store.transition()` 照常执行——公开 state 于是
显示「已批准」，而私有存储里没有这条决定。更细一层：即使两者都在，也只捕获
`ReviewConflict`（业务冲突），HTTP 5xx / 连接中断 / 协议错误会直接穿透，调用方
很容易在外面包一层 `try/except` 就变成「私有失败、公开成功」。

后果不是「少记一条日志」，是**审核链条断在中间**：一条 E4 级未核实内容被放行，
事后既查不到谁批的，也查不到为什么批。所以这里必须是 fail-closed——私有决定
不成立，公开状态一步都不许动。

本文件的每个用例都从「公开 state 里有一条 PENDING_REVIEW」出发，制造一种
私有侧不成立的情形，然后同时断言两件事：
  · 抛 StateError（不是静默返回）
  · 公开 state 的序列化结果与调用前**逐字节相同**
"""
import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import evidence as ev
import review_store
import state_store as store

EVENT_ID = "ev_p2"
TITLE = "Ronaldo retires from Portugal duty"


def make_event(eid=EVENT_ID):
    """真实结构的 E4 + S 候选。"""
    return {
        "event_id": eid,
        "title": TITLE,
        "importance": "S",
        "cred": "E4",
        "content_type": "BREAKING_NEWS",
        "evidence_status": ev.UNVERIFIED,
        "evidence_refs": [
            {"url": "https://www.thesun.co.uk/sport/p2", "title": TITLE},
        ],
    }


def snapshot(state):
    """公开 state 的可比快照。用它而不是逐字段断言，是为了能一眼看出
    「refusal 之后有没有偷偷改动别的地方」。"""
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


class _BrokenStore(review_store.LocalReviewStore):
    """decide() 必定失败的后端。

    继承真实后端而不是写 mock，是为了保证 `enqueue()` / `log()` /
    `peek_payload()` 的行为与生产一致——只把 `decide()` 打断，正好模拟
    「入队成功、批准那一刻存储挂了」这种最难处理的状态。
    """

    def __init__(self, exc=None):
        super().__init__(":memory:")
        self.exc = exc if exc is not None else review_store.ReviewStoreError("store down")
        self.decide_calls = []

    def decide(self, review_ref, action, operator_id="", note=""):
        self.decide_calls.append((review_ref, action, operator_id, note))
        raise self.exc


class _FailClosedCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._saved_tg = (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
                          bot.TELEGRAM_REVIEW_CHAT_ID)
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        self.good = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.good)
        self.state = store._default_state()
        self.rec = bot._enqueue_review(self.state, make_event(), "E4 级 S 事件")
        assert self.rec is not None, "前置条件：必须能入队"
        assert self.rec["delivery_status"] == store.PENDING_REVIEW
        # P0 之后公开键是随机值，业务侧统一用 opaque 句柄定位
        self.ref = self.rec["review_ref"]
        self.stub = store.find_review_stub(self.state, self.ref)
        assert self.stub is not None, "前置条件：公开侧必须有可定位的 stub"
        # 公开键（随机值）——「review_ref 为空」的用例要靠它才能定位到记录
        self.key = next(k for k, v in self.state["events"].items() if v is self.stub)

    def tearDown(self):
        bot.set_review_store(None)
        for client in (self.good, getattr(self, "_broken", None)):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
         bot.TELEGRAM_REVIEW_CHAT_ID) = self._saved_tg
        super().tearDown()

    def use_broken_store(self, exc=None):
        """换上一个 decide() 必挂的后端，并登记以便 tearDown 关闭连接。"""
        broken = _BrokenStore(exc)
        self._broken = broken
        bot.set_review_store(broken)
        return broken

    # ---- 共用断言 ----------------------------------------------------

    def _assert_still_pending(self):
        self.assertEqual(
            self.stub["delivery_status"],
            store.PENDING_REVIEW,
            "拒绝之后公开状态必须仍停在 PENDING_REVIEW")

    def _assert_refused(self, before, action, event_id=None, operator="editor_01"):
        """调用 action，断言「抛错 + 公开状态一字未动」。"""
        fn = bot.approve_review if action == "approve" else bot.decline_review
        event_id = event_id or self.ref
        with self.assertRaises(store.StateError) as ctx:
            fn(self.state, event_id, operator)
        self._assert_still_pending()
        self.assertEqual(snapshot(self.state), before,
                         "fail-closed 拒绝时公开 state 不得发生任何变化")
        return str(ctx.exception)


class TestApproveFailsClosed(_FailClosedCase):
    def test_refuses_when_store_missing(self):
        """私有存储未配置 → 拒绝批准。

        这是最关键的一条：生产里 REVIEW_STORE_URL / REVIEW_SERVICE_KEY 漏配
        时，旧实现会把 E4 内容直接放行，而审核群里什么都没收到。
        """
        before = snapshot(self.state)
        bot.set_review_store(None)
        msg = self._assert_refused(before, "approve")
        self.assertIn("私有审核存储未配置", msg)

    def test_refuses_when_review_ref_empty(self):
        """stub 里没有 review_ref（入队时存储就没写成功）→ 拒绝批准。"""
        before = snapshot(self.state)
        self.stub["review_ref"] = ""
        before = snapshot(self.state)
        # 用公开键定位：记录找得到，但没有句柄 → 必须拒绝
        msg = self._assert_refused(before, "approve", event_id=self.key)
        self.assertIn("review_ref", msg)

    def test_refuses_on_store_error(self):
        """存储返回业务/网络错误 → 拒绝批准。"""
        self.use_broken_store()
        before = snapshot(self.state)
        msg = self._assert_refused(before, "approve")
        self.assertIn("私有审核存储", msg)

    def test_refuses_on_unexpected_exception(self):
        """非 ReviewStoreError 的意外异常同样不得放行。

        旧实现只 catch ReviewConflict，`RuntimeError` 会穿透——穿透本身还行，
        但调用方一旦在外面包 try/except 就变成静默放行。这里要求统一转成
        StateError，把「不能放行」写死在契约里。
        """
        self.use_broken_store(RuntimeError("socket closed"))
        before = snapshot(self.state)
        msg = self._assert_refused(before, "approve")
        self.assertIn("socket closed", msg)

    def test_refuses_on_conflict(self):
        """私有侧已被别人决过（ReviewConflict）→ 拒绝，且不能覆盖成批准。"""
        self.good.decide(self.ref, "decline", "editor_00", "先拒了")
        before = snapshot(self.state)
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_01")
        self._assert_still_pending()
        self.assertEqual(snapshot(self.state), before)

    def test_private_decide_is_actually_attempted(self):
        """拒绝不能变成「干脆不调私有存储」。

        失败路径必须真的打到后端：只有真的调了 `decide()`，才谈得上
        「私有存储说了不行」。否则 fail-closed 就退化成「什么都不做」，
        与「静默跳过」是同一种病的另一个方向。
        """
        broken = self.use_broken_store()
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_01")
        self.assertEqual(len(broken.decide_calls), 1)
        ref, act, op, _note = broken.decide_calls[0]
        self.assertEqual(act, "approve")
        self.assertEqual(op, "editor_01")
        self.assertTrue(ref, "必须带着 review_ref 去调私有存储")

    def test_happy_path_still_works(self):
        """加固不能把正常路径一起堵死。"""
        view = bot.approve_review(self.state, self.ref, "editor_01", "措辞确认")
        self.assertEqual(self.stub["delivery_status"],
                         store.PENDING_DELIVERY)
        self.assertEqual(view["review_status"], "APPROVED")
        self.assertEqual(view["review_cred"], "E4", "批准不得改变可信度")


class TestDeclineFailsClosed(_FailClosedCase):
    def test_refuses_when_store_missing(self):
        before = snapshot(self.state)
        bot.set_review_store(None)
        msg = self._assert_refused(before, "decline")
        self.assertIn("私有审核存储未配置", msg)

    def test_refuses_when_review_ref_empty(self):
        self.stub["review_ref"] = ""
        before = snapshot(self.state)
        msg = self._assert_refused(before, "decline", event_id=self.key)
        self.assertIn("review_ref", msg)

    def test_refuses_on_store_error(self):
        self.use_broken_store()
        before = snapshot(self.state)
        msg = self._assert_refused(before, "decline")
        self.assertIn("私有审核存储", msg)

    def test_refuses_on_unexpected_exception(self):
        self.use_broken_store(ValueError("bad json"))
        before = snapshot(self.state)
        msg = self._assert_refused(before, "decline")
        self.assertIn("bad json", msg)

    def test_happy_path_still_works(self):
        view = bot.decline_review(self.state, self.ref, "editor_01", "不采信")
        self.assertEqual(self.stub["delivery_status"],
                         store.REVIEW_DECLINED)
        self.assertEqual(view["review_status"], "DECLINED")
        with self.assertRaises(store.StateError):
            store.transition(self.stub, store.PENDING_DELIVERY)


class TestRefusalLeavesNoSideEffects(_FailClosedCase):
    """拒绝之后，这条记录必须仍然「可被正常处理」。

    一个容易忽略的坑：如果 fail-closed 实现顺手把状态改成了
    DELIVERY_FAILED / needs_manual 之类，虽然「没放行」，但这条 E4 内容
    就再也没人审了。要求是**原样不动**。
    """

    def test_record_still_actionable_after_refusal(self):
        before = snapshot(self.state)
        bot.set_review_store(None)
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_01")
        self.assertEqual(snapshot(self.state), before)

        # 恢复存储后，同一条记录必须能被正常批准
        bot.set_review_store(self.good)
        view = bot.approve_review(self.state, self.ref, "editor_01")
        self.assertEqual(view["review_status"], "APPROVED")

    def test_refusal_does_not_touch_other_events(self):
        """别的待审记录不能被牵连。"""
        other = bot._enqueue_review(
            self.state, make_event("ev_other"), "E4 级 S 事件")
        self.assertIsNotNone(other)
        other_stub = store.find_review_stub(self.state, other["review_ref"])
        before = copy.deepcopy(other_stub)
        bot.set_review_store(None)
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_01")
        self.assertEqual(other_stub, before)

    def test_refusal_keeps_public_state_serializable(self):
        """拒绝之后 state 仍然能过公开安全检查并落盘。

        防止 fail-closed 顺手写进了不该写的字段——那样 save_state 会在
        下一个环节抛错，把「拒绝一次批准」变成「整轮扫描失败」。
        """
        bot.set_review_store(None)
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_01")
        self.assertEqual(store.public_review_violations(self.state), [])


if __name__ == "__main__":
    unittest.main()
