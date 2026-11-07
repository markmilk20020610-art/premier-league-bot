"""PR #1 整改 P1：claim 租约过期后不得自动重复投递（Python 侧镜像）。

为什么 Python 侧也要有这一组
----------------------------
生产走 `HttpReviewStore`（Cloudflare SQLite Durable Object），本地与测试走
`LocalReviewStore`。两个后端是**两份手写实现**，SQL 必须逐句对应。历史上
已经吃过一次「两份实现漂移」的亏（review_ref 长度 32 vs 16，本地全绿、
上线 400）。

所以 P1 的修复必须在两边同时成立：
  · `deploy/cloudflare-worker/worker.test.mjs` 的 C-11a~C-11f 验证 DO 行为
  · 本文件验证 `LocalReviewStore` 行为，并要求两者语义一致

缺陷原文
--------
`SQL_CLAIM` 里有一句：

    OR (status='claimed' AND claim_expires_at IS NOT NULL AND claim_expires_at < ?)

含义是「上一任超时了，换你上」。但投递不是可回滚的动作：

    取到 claim → Telegram 发送成功 → 进程被杀 → 租约过期
                                            → 另一个 job 认领 → 重复公开推送

发送与 finish 回写不可能构成一个原子事务，所以「租约过期」只能说明
**结果未知**，不能说明**没发出去**。

修复后的语义
------------
  · 过期 claimed  → 原子转 needs_manual，绝不签发新 token
  · needs_manual  → 只有 release(review_ref, operator_id, note) 能复活
  · failed        → 确定性失败，重试机制原样保留
"""
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

EVENT_ID = "ev_lease"
TITLE = "Ronaldo retires from Portugal duty"


def payload(eid=EVENT_ID):
    return {
        "event_id": eid,
        "title": TITLE,
        "fact_summary": "据某小报报道，球员已在个人账号宣布退出国家队。",
        "articles": [{"title": TITLE, "url": "https://example.com/a",
                      "publisher": "example.com"}],
        "urls": ["https://example.com/a"],
        "publishers": ["example.com"],
        "importance": "S",
        "cred": "E4",
    }


def expire_lease(store_, ref):
    """把租约推到过去，模拟「进程在发送成功后、finish 回写前被杀」。"""
    store_._conn.execute(
        "UPDATE review SET claim_expires_at = ? WHERE review_ref = ?",
        ("2000-01-01T00:00:00+00:00", ref))
    store_._conn.commit()


class _LeaseCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._saved_tg = (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
                          bot.TELEGRAM_REVIEW_CHAT_ID)
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        self.client = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.client)
        self.ref = self.client.enqueue(payload())["review_ref"]
        self.client.decide(self.ref, "approve", "editor_01", "同意发")

    def tearDown(self):
        bot.set_review_store(None)
        self.client.close()
        (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
         bot.TELEGRAM_REVIEW_CHAT_ID) = self._saved_tg
        super().tearDown()

    def status(self, ref=None):
        return self.client.get(ref or self.ref)["status"]


class TestExpiredLeaseGoesManual(_LeaseCase):
    def test_expired_lease_raises_needs_manual(self):
        """过期认领必须抛 ReviewNeedsManual，而不是再发一个 token。"""
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)

    def test_expired_lease_state_is_needs_manual(self):
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        self.assertEqual(self.status(), review_store.NEEDS_MANUAL)

    def test_expired_lease_clears_token(self):
        """旧 token 必须被清空：即使原进程活过来也回写不了 delivered。"""
        claim = self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        row = self.client._conn.execute(
            "SELECT claim_token FROM review WHERE review_ref = ?",
            (self.ref,)).fetchone()
        self.assertFalse(row["claim_token"],
                         "转人工之后旧 claim_token 必须失效")
        with self.assertRaises(review_store.ReviewStoreError):
            self.client.finish(self.ref, claim["claim_token"],
                               review_store.DELIVERED, message_ids=["1"])

    def test_expired_lease_records_error_and_log(self):
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        state = self.client.get(self.ref)
        self.assertIn("租约", state["last_error"])
        actions = [e["action"] for e in self.client.log(self.ref)]
        self.assertIn("LEASE_EXPIRED", actions)
        # 关键：不能出现两次 CLAIMED —— 那意味着消息会被发两遍
        self.assertEqual(actions.count("CLAIMED"), 1,
                         "租约过期不得产生第二次 CLAIMED（= 第二次发送）")

    def test_repeated_claim_never_yields_token(self):
        """反复认领只会一直得到 needs_manual，永远拿不到 token。"""
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        for _ in range(3):
            with self.assertRaises(review_store.ReviewNeedsManual):
                self.client.claim(self.ref)
            self.assertEqual(self.status(), review_store.NEEDS_MANUAL)

    def test_needs_manual_is_not_claimable(self):
        """即使租约还没过期，needs_manual 也不能被认领。

        并且报的必须是 `ReviewNeedsManual` 而不是普通 `ReviewConflict`：
        调用方要靠这个区分「等人核对」与「别人正在发」，否则会另找路径重试。
        """
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        # 把租约恢复成「未过期」，状态仍是 needs_manual
        self.client._conn.execute(
            "UPDATE review SET claim_expires_at = ? WHERE review_ref = ?",
            ("2999-01-01T00:00:00+00:00", self.ref))
        self.client._conn.commit()
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)
        self.assertEqual(self.status(), review_store.NEEDS_MANUAL)


class TestExplicitRelease(_LeaseCase):
    def _to_needs_manual(self):
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        with self.assertRaises(review_store.ReviewNeedsManual):
            self.client.claim(self.ref)

    def test_release_requires_operator(self):
        """放行必须有人负责，否则 needs_manual 就是橡皮图章。"""
        self._to_needs_manual()
        with self.assertRaises(review_store.ReviewStoreError):
            self.client.release(self.ref, "", "无签名放行")
        self.assertEqual(self.status(), review_store.NEEDS_MANUAL)

    def test_release_only_from_needs_manual(self):
        """对其它状态放行要报冲突，不能当万能钥匙。"""
        with self.assertRaises(review_store.ReviewConflict):
            self.client.release(self.ref, "editor_01", "还没失败呢")

    def test_release_returns_to_decided_and_claimable(self):
        self._to_needs_manual()
        self.client.release(self.ref, "editor_01", "已核对频道无该消息")
        self.assertEqual(self.status(), review_store.DECIDED)
        again = self.client.claim(self.ref)
        self.assertTrue(again["claim_token"])
        self.assertNotEqual(again["claim_token"], "")

    def test_release_is_logged_with_operator(self):
        self._to_needs_manual()
        self.client.release(self.ref, "editor_01", "已核对")
        entries = self.client.log(self.ref)
        hit = [e for e in entries if e["action"] == "RELEASED:manual"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["actor"], "editor_01")


class TestDeterministicFailureStillRetries(_LeaseCase):
    """与「结果未知」相对的另一端：failed 是「明确没发出去」，重试是安全的。

    加固不能把这条正常路径一起堵死——否则 Telegram 一次 400 就永久卡死。
    """

    def test_failed_can_be_reclaimed(self):
        c1 = self.client.claim(self.ref)
        self.client.finish(self.ref, c1["claim_token"], review_store.FAILED,
                           error="HTTP 400 格式错误")
        self.assertEqual(self.status(), review_store.FAILED)
        c2 = self.client.claim(self.ref)
        self.assertTrue(c2["claim_token"])
        self.assertNotEqual(c2["claim_token"], c1["claim_token"])
        self.assertEqual(self.client.get(self.ref)["attempts"], 2)

    def test_failed_is_not_needs_manual(self):
        c1 = self.client.claim(self.ref)
        self.client.finish(self.ref, c1["claim_token"], review_store.FAILED,
                           error="HTTP 400")
        self.assertEqual(self.status(), review_store.FAILED)
        self.assertNotEqual(self.status(), review_store.NEEDS_MANUAL)


class TestNeedsManualBlocksPublicRetry(_LeaseCase):
    """公开 state 必须跟着说「结果未知」，否则判断错误只是换了个位置重演。"""

    def setUp(self):
        super().setUp()
        self.state = store._default_state()
        self.state["events"][EVENT_ID] = store.record_review_stub(
            self.state, EVENT_ID, self.ref, store.PENDING_DELIVERY)

    def test_execute_review_decision_reports_needs_manual(self):
        self.client.claim(self.ref)              # 上一次执行者拿到租约
        expire_lease(self.client, self.ref)      # 它在发送成功后挂了
        out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], "NEEDS_MANUAL")
        self.assertIn("release", out.get("recovery") or "")

    def test_public_state_marked_uncertain(self):
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(self.state["events"][EVENT_ID]["delivery_status"],
                         store.DELIVERY_UNCERTAIN)

    def test_public_state_blocks_rediscovery(self):
        """DELIVERY_UNCERTAIN 在 blocks_rediscovery 里返回 True —— 不会重复入队。"""
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        bot.execute_review_decision(self.state, self.ref)
        rec = self.state["events"][EVENT_ID]
        self.assertTrue(store.blocks_rediscovery(rec))

    def test_no_duplicate_send_on_second_attempt(self):
        """第二次调用不得产生第二次 Telegram 发送。

        用「发送函数被调用次数」直接证明，而不是靠状态推断。
        """
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        calls = []
        saved = bot.send_telegram_ex
        bot.send_telegram_ex = lambda *a, **k: (calls.append(1) or (True, ["1"], ""))
        try:
            out = bot.execute_review_decision(self.state, self.ref)
        finally:
            bot.send_telegram_ex = saved
        self.assertEqual(calls, [], "租约过期后绝不能再发一次 Telegram")
        self.assertEqual(out["status"], "NEEDS_MANUAL")

    def test_public_state_still_clean(self):
        """标记结果未知不得把审核私有资料带进公开 state。"""
        self.client.claim(self.ref)
        expire_lease(self.client, self.ref)
        bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(store.public_review_violations(self.state), [])


class TestHttpBackendMapsNeedsManual(unittest.TestCase):
    """生产走的是 `HttpReviewStore`，不是本地 SQLite。

    这条路径上有一个容易漏的坑：DO 用 HTTP **409** 表示「冲突」，
    而「租约过期、结果未知」也是 409。如果 `_http()` 在解析响应体之前就
    一律把 409 转成 `ReviewConflict`，那么 `needs_manual` 标记永远到不了
    调用方，P1 的修复在生产上等于没生效——本地全绿、线上照样重复推送。
    """

    def _client(self, transport=None):
        return review_store.HttpReviewStore(
            "https://vault.example", "svc_key",
            transport=transport or (lambda path, method="GET", payload=None: {}))

    def test_call_maps_needs_manual_before_conflict(self):
        body = {"ok": False, "error": "租约已过期", "conflict": True,
                "needs_manual": True, "status": "needs_manual"}
        client = self._client(lambda *a, **k: dict(body))
        with self.assertRaises(review_store.ReviewNeedsManual):
            client.claim("0123456789abcdef")

    def test_http_409_with_needs_manual_is_not_conflict(self):
        """真刀真枪走一遍 urllib 的 HTTPError 分支。"""
        import io as _io
        import urllib.error
        from unittest import mock

        body = json.dumps({"ok": False, "error": "租约已过期", "conflict": True,
                           "needs_manual": True, "status": "needs_manual"}).encode()
        err = urllib.error.HTTPError(
            "https://vault.example/internal/review/0123456789abcdef/claim",
            409, "Conflict", {}, _io.BytesIO(body))
        client = review_store.HttpReviewStore("https://vault.example", "svc_key")
        with mock.patch.object(review_store.urllib.request, "urlopen",
                               side_effect=err):
            with self.assertRaises(review_store.ReviewNeedsManual):
                client.claim("0123456789abcdef")

    def test_http_409_plain_conflict_still_conflict(self):
        """普通冲突不能被顺手改成 needs_manual。"""
        import io as _io
        import urllib.error
        from unittest import mock

        body = json.dumps({"ok": False, "error": "已被他人认领",
                           "conflict": True}).encode()
        err = urllib.error.HTTPError(
            "https://vault.example/internal/review/0123456789abcdef/claim",
            409, "Conflict", {}, _io.BytesIO(body))
        client = review_store.HttpReviewStore("https://vault.example", "svc_key")
        with mock.patch.object(review_store.urllib.request, "urlopen",
                               side_effect=err):
            with self.assertRaises(review_store.ReviewConflict):
                client.claim("0123456789abcdef")

    def test_http_500_is_store_error(self):
        import io as _io
        import urllib.error
        from unittest import mock

        err = urllib.error.HTTPError(
            "https://vault.example/internal/review/x/claim",
            500, "Server Error", {}, _io.BytesIO(b"boom"))
        client = review_store.HttpReviewStore("https://vault.example", "svc_key")
        with mock.patch.object(review_store.urllib.request, "urlopen",
                               side_effect=err):
            with self.assertRaises(review_store.ReviewStoreError) as ctx:
                client.claim("0123456789abcdef")
        self.assertNotIsInstance(ctx.exception, review_store.ReviewNeedsManual)

    def test_release_route_and_payload(self):
        """release 必须打到 DO 的 /release 路由，并带上 operator_id。"""
        seen = {}

        def transport(path, method="GET", payload=None):
            seen["path"], seen["method"], seen["payload"] = path, method, payload
            return {"ok": True, "status": "decided"}

        self._client(transport).release("0123456789abcdef", "editor_01", "已核对")
        self.assertEqual(seen["path"],
                         "/internal/review/0123456789abcdef/release")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["payload"]["operator_id"], "editor_01")
        self.assertEqual(seen["payload"]["note"], "已核对")


if __name__ == "__main__":
    unittest.main()
