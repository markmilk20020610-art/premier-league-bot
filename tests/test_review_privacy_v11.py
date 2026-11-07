"""V1.1 P0 安全补充：审核数据脱离公开 state.json —— 十项安全测试。

对应用户点名的清单：

   1. E4 完整审核记录不会进入公开 state.json
   2. GitHub 公开提交不包含未公开审核资料
   3. Durable Object 重复批准只有一次成功
   4. 批准与拒绝并发只能一个生效
   5. 回调重放无法再次批准
   6. GitHub Actions 日志不泄露私有 payload
   7. Webhook 处理后私有审核状态持久化
   8. Telegram 发送成功后才允许完成投递状态迁移
   9. Worker 重启后 dispatch 冷却仍然有效
  10. Worker 重启后告警去重仍然有效

第 3—5、7—10 项的**真实** Durable Object 语义在
`deploy/cloudflare-worker/worker.test.mjs` 里用 `node:sqlite` 模拟重启验证。
本文件做两件互补的事：

  · 用同一份 SQL 的本地 SQLite 后端，在 Python 侧给出可读的失败信息
    （node 挂了也能定位到底哪条语义破了）；
  · 静态契约：Python 的 SQL / HMAC 与 Worker 的必须逐字对应，
    任何一边改了另一边没跟上，这里立刻红。

第 9、10 项额外把 node 套件整份跑一遍，让「重启后仍然有效」这条结论
在 Python 测试汇总里也有一个可见入口。
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot                      # noqa: E402
import evidence as ev           # noqa: E402
import review_store             # noqa: E402
import state_store as store     # noqa: E402

WORKER_DIR = os.path.join(ROOT, "deploy", "cloudflare-worker")

TITLE = "Ronaldo retires from Portugal duty"
SUMMARY = "Cristiano Ronaldo said he will no longer represent Portugal."
SUN_URL = "https://www.thesun.co.uk/sport/football/ronaldo-portugal-retirement"

EVENT_ID = "ev_privacy"
SECRET = "unit-test-hmac-secret"

# 任何一条都不允许出现在公开仓库、Actions 日志或 state.json 里
NEWS_MARKERS = (TITLE, SUMMARY, SUN_URL, "thesun.co.uk", "Ronaldo")


# ==================== 辅助 ====================
def make_event(eid=EVENT_ID, refs=None, importance="S", cred="E4"):
    return {
        "event_id": eid,
        "title": TITLE,
        "fact_summary": SUMMARY,
        "importance": importance,
        "cred": cred,
        "content_type": "BREAKING_NEWS",
        "event_type": "TRANSFER",
        "evidence_status": ev.UNVERIFIED,
        "evidence_url": SUN_URL,
        "evidence_refs": refs if refs is not None else [
            {"url": SUN_URL, "title": TITLE, "domain": "thesun.co.uk"},
        ],
    }


def enqueue(state, **kw):
    """把一个 E4 级 S 事件送进审核队列，返回审核视图。"""
    return bot._enqueue_review(state, make_event(**kw), "E4 级 S 事件")


def _worker_source(name):
    with open(os.path.join(WORKER_DIR, name), encoding="utf-8") as f:
        return f.read()


def _norm(text):
    """把 SQL 的多行缩进压成单空格，用于跨语言逐字比对。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _git(*args):
    return subprocess.run(
        ["git", "-C", ROOT] + list(args),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180)


class _PrivateStoreCase(unittest.TestCase):
    """每个用例一份全新的内存私有审核存储。

    bot.py **不提供**「自动兜底到内存」的路径：生产里存储没配好时必须挂起，
    不能静默变成「看起来入队成功、实际什么都没存」。所以测试要显式注入。
    """

    def setUp(self):
        super().setUp()
        self._saved_tg = (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
                          bot.TELEGRAM_REVIEW_CHAT_ID)
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"
        self.client = review_store.LocalReviewStore(":memory:")
        bot.set_review_store(self.client)

    def tearDown(self):
        bot.set_review_store(None)
        (bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID,
         bot.TELEGRAM_REVIEW_CHAT_ID) = self._saved_tg
        try:
            self.client.close()
        except Exception:                                  # noqa: BLE001
            pass
        super().tearDown()


# =============================================================================
# 第 1 项：E4 完整审核记录不会进入公开 state.json
# =============================================================================
class TestReviewRecordNeverEntersPublicState(_PrivateStoreCase):
    """入队之后，公开 state 里只剩 opaque 句柄与状态。"""

    def setUp(self):
        super().setUp()
        self.state = {"events": {}}
        self.view = enqueue(self.state)
        # P0 之后公开键是随机值，只能靠 opaque 句柄定位（这正是修复目的）
        self.stub = store.find_review_stub(self.state, self.view["review_ref"])
        self.assertIsNotNone(self.stub, "入队后公开侧必须有一条可定位的 stub")

    def test_stub_only_has_whitelisted_keys(self):
        extra = sorted(set(self.stub.keys()) - set(store.REVIEW_STUB_ALLOWED))
        self.assertEqual(extra, [], "公开 stub 出现白名单外字段：{}".format(extra))

    def test_no_news_text_in_serialized_state(self):
        raw = json.dumps(self.state, ensure_ascii=False)
        for marker in NEWS_MARKERS:
            self.assertNotIn(marker, raw,
                             "公开 state.json 里出现了新闻内容：{!r}".format(marker))

    def test_no_forbidden_field_present(self):
        leaked = [k for k in store.PUBLIC_REVIEW_FORBIDDEN if k in self.stub]
        self.assertEqual(leaked, [])

    def test_violations_empty(self):
        self.assertEqual(store.public_review_violations(self.state), [])

    def test_payload_really_stored_privately(self):
        # 「不写进 state」不等于「丢掉了」：完整快照必须在私有存储里
        payload = self.client.peek_payload(self.stub["review_ref"])
        self.assertEqual(payload["title"], TITLE)
        self.assertEqual(payload["fact_summary"], SUMMARY)
        self.assertIn(SUN_URL, payload["urls"])

    def test_review_ref_is_opaque(self):
        ref = self.stub["review_ref"]
        self.assertRegex(ref, r"^[0-9a-f]{16,32}$")
        self.assertNotIn(EVENT_ID, ref, "review_ref 不得由 event_id 推算")

    def test_dedup_still_works_after_stub(self):
        # 只留 stub 的前提是「去重还能用」，否则会反复入队、反复发卡片
        self.assertTrue(store.blocks_rediscovery(self.stub))
        again = enqueue(self.state)
        self.assertEqual(again["review_ref"], self.stub["review_ref"])

    def test_save_guard_blocks_leak(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "state.json")
        self.stub["review_payload"] = {"title": TITLE, "fact_summary": SUMMARY}
        with self.assertRaises(store.StateError) as ctx:
            store.save_state_atomic(self.state, path)
        self.assertIn("审核私有资料", str(ctx.exception))
        self.assertFalse(os.path.exists(path), "守卫触发时绝不能落盘")

    def test_ensure_shape_scrubs_legacy_leak(self):
        legacy = {"events": {EVENT_ID: {
            "event_id": EVENT_ID,
            "delivery_status": store.PENDING_REVIEW,
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "title": TITLE,
            "fact_summary": SUMMARY,
            "evidence_url": SUN_URL,
            "review_payload": {"title": TITLE, "fact_summary": SUMMARY},
            "review_status": "PENDING",
            "review_operator": "editor_01",
            "review_log": [{"note": "内部备注"}],
        }}}
        store.ensure_shape(legacy)
        rec = legacy["events"][EVENT_ID]
        self.assertLessEqual(set(rec.keys()), set(store.REVIEW_STUB_ALLOWED))
        self.assertTrue(rec.get("redacted"))
        # 去重状态必须保住：event_id / 状态 / 创建时间一个都不能丢
        self.assertEqual(rec["delivery_status"], store.PENDING_REVIEW)
        self.assertEqual(rec["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(store.public_review_violations(legacy), [])

    def test_scrub_keeps_already_public_title(self):
        # 已投递事件的标题本来就发到 Telegram 了，删掉反而破坏审计
        state = {"events": {"ev_done": {
            "event_id": "ev_done",
            "delivery_status": store.COMMITTED,
            "title": TITLE,
            "review_payload": {"title": TITLE},
            "review_operator": "editor_01",
            "created_at": "2026-01-01T00:00:00+00:00",
        }}}
        store.ensure_shape(state)
        rec = state["events"]["ev_done"]
        self.assertEqual(rec["title"], TITLE)
        self.assertNotIn("review_payload", rec)
        self.assertNotIn("review_operator", rec)
        self.assertEqual(store.public_review_violations(state), [])

    def test_scrub_is_idempotent(self):
        state = {"events": {EVENT_ID: {
            "event_id": EVENT_ID,
            "delivery_status": store.PENDING_REVIEW,
            "title": TITLE,
            "review_payload": {"title": TITLE},
        }}}
        first = store.scrub_review_records(state)
        second = store.scrub_review_records(state)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0, "第二次清理不应再改动任何记录")


# =============================================================================
# 第 2 项：GitHub 公开提交不包含未公开审核资料
# =============================================================================
@unittest.skipUnless(shutil.which("git"), "需要 git")
class TestPublicCommitHasNoReviewMaterial(unittest.TestCase):
    """直接检查 HEAD 里已经提交出去的字节——那才是「公开」的定义。"""

    def setUp(self):
        super().setUp()
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            self.skipTest("不在 git 工作区内")

    def _head(self, path):
        r = _git("show", "HEAD:" + path)
        return r.stdout if r.returncode == 0 else None

    def test_state_json_is_tracked(self):
        # 被跟踪是既定事实（scanner.yml 每轮提交它），这里只是把前提钉住
        self.assertEqual(_git("ls-files", "--", "state.json").stdout.strip(),
                         "state.json")

    def test_head_state_json_has_no_review_violations(self):
        raw = self._head("state.json")
        if raw is None:
            self.skipTest("HEAD 里没有 state.json")
        state = json.loads(raw)
        self.assertEqual(store.public_review_violations(state), [])

    def test_head_state_json_has_no_review_field(self):
        raw = self._head("state.json")
        if raw is None:
            self.skipTest("HEAD 里没有 state.json")
        leaked = [k for k in store.PUBLIC_REVIEW_FORBIDDEN
                  if '"{}"'.format(k) in raw]
        self.assertEqual(leaked, [], "已提交的 state.json 里残留审核字段：{}".format(leaked))

    def test_no_review_dump_file_tracked(self):
        files = [f for f in _git("ls-files").stdout.splitlines() if f.strip()]
        bad = [f for f in files
               if f.endswith((".sqlite", ".sqlite3", ".db", ".dump"))
               or re.search(r"(review|vault)[^/]*\.(json|txt)$", f, re.I)]
        self.assertEqual(bad, [], "审核资料不得以文件形式进入公开仓库：{}".format(bad))

    def test_tracked_json_files_carry_no_review_payload(self):
        files = [f for f in _git("ls-files", "--", "*.json").stdout.splitlines()
                 if f.strip()]
        offenders = []
        for f in files:
            raw = self._head(f)
            if raw is None:
                continue
            if '"review_payload"' in raw or '"review_operator"' in raw:
                offenders.append(f)
        self.assertEqual(offenders, [])


# =============================================================================
# 第 3 项：Durable Object 重复批准只有一次成功
# =============================================================================
class TestDuplicateApprovalOnlyOnceSucceeds(_PrivateStoreCase):
    """原子性来自「条件 UPDATE + changes() 计数」，不是「先读后写」。"""

    def setUp(self):
        super().setUp()
        self.state = {"events": {}}
        self.ref = enqueue(self.state)["review_ref"]

    def test_second_decide_conflicts(self):
        self.client.decide(self.ref, "approve", "editor_01")
        with self.assertRaises(review_store.ReviewConflict):
            self.client.decide(self.ref, "approve", "editor_02")

    def test_second_approve_raises_state_error(self):
        bot.approve_review(self.state, self.ref, "editor_01")
        with self.assertRaises(store.StateError):
            bot.approve_review(self.state, self.ref, "editor_02")

    def test_decide_after_claim_conflicts(self):
        self.client.decide(self.ref, "approve", "editor_01")
        self.client.claim(self.ref)
        with self.assertRaises(review_store.ReviewConflict):
            self.client.decide(self.ref, "decline", "editor_02")

    def test_first_operator_is_the_one_recorded(self):
        self.client.decide(self.ref, "approve", "editor_01")
        with contextlib.suppress(review_store.ReviewStoreError):
            self.client.decide(self.ref, "approve", "editor_02")
        st = self.client.get(self.ref)
        self.assertEqual(st["operator_id"], "editor_01")
        self.assertEqual(st["action"], "approve")

    def test_finish_token_is_single_use(self):
        self.client.decide(self.ref, "approve", "editor_01")
        claim = self.client.claim(self.ref)
        self.client.finish(self.ref, claim["claim_token"], review_store.DELIVERED,
                           message_ids=[1])
        with self.assertRaises(review_store.ReviewConflict):
            self.client.finish(self.ref, claim["claim_token"], review_store.DELIVERED,
                               message_ids=[2])

    def test_python_sql_is_conditional_update(self):
        self.assertIn("status='pending'", review_store.SQL_DECIDE)
        self.assertIn("WHERE review_ref=?", review_store.SQL_DECIDE)
        self.assertIn("status='claimed' AND claim_token=?",
                      review_store.SQL_FINISH_CLAIMED)

    def test_worker_sql_is_byte_identical(self):
        js = _norm(_worker_source("review_vault.js"))
        for name in ("SQL_DECIDE", "SQL_CLAIM", "SQL_FINISH_CLAIMED",
                     "SQL_EXIT", "SQL_INSERT", "SQL_FIND_ACTIVE",
                     "SQL_LOG_INSERT", "SQL_LOG_LIST",
                     "SQL_GET", "SQL_GET_PAYLOAD"):
            self.assertIn(_norm(getattr(review_store, name)), js,
                          "Python 与 Worker 的 {} 出现漂移".format(name))


# =============================================================================
# 第 4 项：批准与拒绝并发只能一个生效
# =============================================================================
class TestConcurrentApproveDeclineOnlyOneWins(_PrivateStoreCase):
    """两条独立连接打同一份存储——真实并发，不是同进程加锁。"""

    ROUNDS = 8

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "vault.db")
        self.client = review_store.LocalReviewStore(self.db)
        bot.set_review_store(self.client)

    def _race(self, ref):
        a = review_store.LocalReviewStore(self.db)
        b = review_store.LocalReviewStore(self.db)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        barrier = threading.Barrier(2)
        outcome = {}

        def run(name, conn, action):
            barrier.wait(timeout=20)
            try:
                conn.decide(ref, action, name)
                outcome[name] = "ok"
            except review_store.ReviewConflict:
                outcome[name] = "conflict"
            except Exception as exc:                       # noqa: BLE001
                outcome[name] = "{}: {}".format(type(exc).__name__, exc)

        t1 = threading.Thread(target=run, args=("editor_a", a, "approve"))
        t2 = threading.Thread(target=run, args=("editor_b", b, "decline"))
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        return outcome

    def test_exactly_one_decision_per_round(self):
        for i in range(self.ROUNDS):
            eid = "ev_race{}".format(i)
            ref = self.client.enqueue({"event_id": eid, "title": TITLE})["review_ref"]
            outcome = self._race(ref)
            self.assertEqual(sorted(outcome.values()), ["conflict", "ok"],
                             "第 {} 轮并发结果异常：{}".format(i, outcome))

            final = self.client.get(ref)
            self.assertEqual(final["status"], review_store.DECIDED)
            winner = "approve" if outcome.get("editor_a") == "ok" else "decline"
            self.assertEqual(final["action"], winner,
                             "生效的决定必须就是抢到的那一个")

    def test_public_stub_never_ends_in_two_states(self):
        state = {"events": {}}
        enqueue(state, eid="ev_race_pub")
        ref = enqueue(state, eid="ev_race_pub")["review_ref"]
        self._race(ref)
        self.assertEqual(store.find_review_stub(state, ref)["delivery_status"],
                         store.PENDING_REVIEW,
                         "私有存储的决定不会自动改写公开状态")


# =============================================================================
# 第 5 项：回调重放无法再次批准
# =============================================================================
class TestCallbackReplayCannotApproveAgain(_PrivateStoreCase):
    """重放防护在状态机里，不在签名里——签名对同一个按钮永远有效。"""

    def setUp(self):
        super().setUp()
        self.state = {"events": {}}
        self.ref = enqueue(self.state)["review_ref"]

    def _callback(self, action=bot.REVIEW_ACTION_APPROVE, **kw):
        return bot.encode_review_callback(self.ref, action, secret=SECRET, **kw)

    def test_valid_callback_verifies(self):
        ok, action, ref, reason = bot.verify_review_callback(
            self._callback(), secret=SECRET)
        self.assertTrue(ok, reason)
        self.assertEqual((action, ref), (bot.REVIEW_ACTION_APPROVE, self.ref))

    def test_callback_fits_telegram_limit(self):
        data = self._callback()
        self.assertLessEqual(len(data.encode("utf-8")), 64,
                             "Telegram callback_data 有 64 字节硬上限")

    def test_ref_length_matches_worker(self):
        # 本地后端与 Worker 必须生成同样长度的句柄。
        # 32 位本地 + 16 位线上 = 本地按钮能点、上线 400，是最难查的那种不一致。
        self.assertIn("new Uint8Array(8)", _worker_source("review_vault.js"))
        self.assertEqual(len(review_store.new_review_ref()), 16)

    def test_replay_after_decision_is_refused(self):
        data = self._callback()
        ok, _a, ref, _r = bot.verify_review_callback(data, secret=SECRET)
        self.assertTrue(ok)
        self.client.decide(ref, "approve", "editor_01")

        # 同一个按钮再点一次：签名依然有效，但状态机拒绝第二次生效
        ok2, _a2, ref2, _r2 = bot.verify_review_callback(data, secret=SECRET)
        self.assertTrue(ok2, "签名本身不该失效——重放防护在状态机里")
        with self.assertRaises(review_store.ReviewConflict):
            self.client.decide(ref2, "approve", "editor_02")

    def test_replay_after_delivery_cannot_reapprove(self):
        self.client.decide(self.ref, "approve", "editor_01")
        claim = self.client.claim(self.ref)
        self.client.finish(self.ref, claim["claim_token"],
                           review_store.DELIVERED, message_ids=[42])

        data = self._callback()
        ok, _a, ref, _r = bot.verify_review_callback(data, secret=SECRET)
        self.assertTrue(ok)
        with self.assertRaises(review_store.ReviewConflict):
            self.client.decide(ref, "approve", "editor_02")
        with self.assertRaises(review_store.ReviewConflict):
            self.client.claim(ref)

    def test_replay_after_auto_exit_is_refused(self):
        self.client.exit_review(self.ref, "第二个独立来源到达")
        with self.assertRaises(review_store.ReviewConflict):
            self.client.decide(self.ref, "approve", "editor_01")

    def test_expired_button_rejected(self):
        now = 1_800_000_000
        data = self._callback(ttl=600, now_ts=now)
        ok, _a, _r, reason = bot.verify_review_callback(
            data, now_ts=now + 601, secret=SECRET)
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_signature_is_bound_to_ref(self):
        data = self._callback()
        parts = data.split(":")
        parts[2] = "0123456789abcdef0123456789abcdef"
        ok, _a, _r, reason = bot.verify_review_callback(":".join(parts), secret=SECRET)
        self.assertFalse(ok)
        self.assertIn("签名", reason)

    def test_signature_is_bound_to_action(self):
        data = self._callback()
        parts = data.split(":")
        parts[1] = bot.REVIEW_ACTION_DECLINE
        ok, _a, _r, reason = bot.verify_review_callback(":".join(parts), secret=SECRET)
        self.assertFalse(ok)
        self.assertIn("签名", reason)

    def test_callback_contract_with_worker(self):
        js = _worker_source("worker.js")
        self.assertIn('ver + "|" + action + "|" + reviewRef + "|" + exp', _norm(js))
        self.assertIn("hex.slice(0, 16)", js)
        self.assertIn('REVIEW_CALLBACK_VERSION = "v1"', js)

        ref = "0123456789abcdef0123456789abcdef"
        exp = 1_800_000_000
        want = hmac.new(SECRET.encode("utf-8"),
                        "v1|a|{}|{}".format(ref, exp).encode("utf-8"),
                        hashlib.sha256).hexdigest()[:16]
        self.assertEqual(bot._review_callback_sig(ref, "a", exp, secret=SECRET), want)


# =============================================================================
# 契约：生产客户端走的每条路由，Durable Object 都必须真的接得住
# =============================================================================
class TestHttpClientRouteContract(unittest.TestCase):
    """按**客户端实际发出的方法**校验路由。

    这一组存在的理由很具体：node 测试用 POST 打 `/log`，而
    `HttpReviewStore.log()` 用 GET。两边各自都「测过了」，生产上却是 405，
    而 Python 侧把这个异常吞成空列表 —— 表现为「审核日志永远是空的」，
    不报错、不告警、查不出来。
    """

    REF = "0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.calls = []

        def transport(path, method="GET", payload=None):
            self.calls.append((path, method))
            return {"ok": True, "entries": [], "payload": {}, "review_ref": self.REF}

        self.client = review_store.HttpReviewStore(
            "https://worker.example", "service-key", transport=transport)

    def _exercise(self):
        ref = self.REF
        self.client.enqueue({"event_id": "ev_route"})
        self.client.get(ref)
        self.client.peek_payload(ref)
        self.client.decide(ref, "approve", "editor_01")
        self.client.claim(ref)
        self.client.finish(ref, "token", review_store.DELIVERED)
        self.client.exit_review(ref, "第二个独立来源")
        # PR #1 P1：租约过期后唯一的人工放行通道，必须也在路由契约里
        self.client.release(ref, "editor_01", "已核对频道无该消息")
        self.client.log(ref)
        return list(self.calls)

    def test_all_paths_are_under_internal(self):
        for path, _method in self._exercise():
            self.assertTrue(path.startswith("/internal/"), path)

    def test_get_routes_are_read_only_in_durable_object(self):
        js = _worker_source("review_vault.js")
        for path, method in self._exercise():
            if method != "GET":
                continue
            op = path.rstrip("/").split("/")[-1]
            needle = 'op === "{}" && request.method === "GET"'.format(op)
            self.assertIn(needle, js,
                          "客户端 GET {}，但 Durable Object 没有只读路由".format(path))

    def test_post_routes_exist_in_durable_object(self):
        js = _worker_source("review_vault.js")
        for path, method in self._exercise():
            if method != "POST":
                continue
            op = path.rstrip("/").split("/")[-1]
            if op in ("enqueue", "by-event"):
                # Worker 会把 /internal 前缀剥掉再转发给 DO，DO 只认 /review/enqueue
                do_path = path[len("/internal"):]
                self.assertIn('"{}"'.format(do_path), js,
                              "Durable Object 没有 {} 路由".format(do_path))
            else:
                self.assertIn('op === "{}"'.format(op), js,
                              "客户端 POST {}，但 Durable Object 没有对应路由".format(path))

    def test_every_get_endpoint_is_reachable_without_claim(self):
        # 读接口绝不能推进状态机，否则「看一眼」会消耗掉唯一一次认领机会
        reads = [p for p, m in self._exercise() if m == "GET"]
        self.assertTrue(any(p.endswith("/payload") for p in reads))
        self.assertTrue(any(p.endswith("/state") for p in reads))
        self.assertTrue(any(p.endswith("/log") for p in reads))


# =============================================================================
# 第 6 项：GitHub Actions 日志不泄露私有 payload
# =============================================================================
class TestActionsLogDoesNotLeakPayload(_PrivateStoreCase):
    """Actions 日志是公开的。正文、摘要、原始链接一个都不许出现。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_paths = (bot.STATE_FILE, bot.LOG_FILE)
        bot.STATE_FILE = os.path.join(self.tmp, "state.json")
        bot.LOG_FILE = os.path.join(self.tmp, "run_telemetry.log")
        self.state = {"events": {}}
        self.ref = enqueue(self.state)["review_ref"]

    def tearDown(self):
        bot.STATE_FILE, bot.LOG_FILE = self._saved_paths
        os.environ.pop("REVIEW_ID", None)
        super().tearDown()

    def _assert_clean(self, text, where):
        for marker in NEWS_MARKERS:
            self.assertNotIn(marker, text,
                             "{} 里出现了私有内容：{!r}".format(where, marker))

    def test_redact_keeps_only_shape(self):
        payload = self.client.peek_payload(self.ref)
        out = json.dumps(review_store.redact_payload_for_log(payload),
                         ensure_ascii=False)
        self._assert_clean(out, "redact_payload_for_log 输出")
        self.assertIn("title_len", out)

    def test_execute_decision_prints_only_redacted(self):
        bot.approve_review(self.state, self.ref, "editor_01")
        buf = io.StringIO()
        with mock.patch.object(bot, "_post_telegram_once",
                               return_value=(True, bot.TG_OK, "OK", 777)), \
                contextlib.redirect_stdout(buf):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], store.COMMITTED)
        log = buf.getvalue()
        self._assert_clean(log, "execute_review_decision 的 stdout")
        self.assertIn("title_len", log)

    def test_run_review_action_mode_logs_and_state_are_clean(self):
        bot.approve_review(self.state, self.ref, "editor_01")
        bot.save_state(self.state)
        os.environ["REVIEW_ID"] = self.ref
        buf = io.StringIO()
        with mock.patch.object(bot, "_post_telegram_once",
                               return_value=(True, bot.TG_OK, "OK", 900)), \
                contextlib.redirect_stdout(buf):
            code = bot.run_review_action_mode(self.state, bot._now_iso())
        self.assertEqual(code, 0)
        self._assert_clean(buf.getvalue(), "review_action 模式的 stdout")

        with open(bot.STATE_FILE, encoding="utf-8") as f:
            raw = f.read()
        self._assert_clean(raw, "落盘后的 state.json")
        self.assertEqual(store.public_review_violations(json.loads(raw)), [])

    def test_run_mode_requires_only_review_id(self):
        # workflow_dispatch 的 inputs 只允许携带 opaque 句柄。
        # 这里把「只读 REVIEW_ID」这个事实钉死：多一个环境变量都不认。
        os.environ.pop("REVIEW_ID", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = bot.run_review_action_mode(self.state, bot._now_iso())
        self.assertEqual(code, 1)
        self.assertIn("REVIEW_ID", buf.getvalue())


# =============================================================================
# 第 7 项：Webhook 处理后私有审核状态持久化
# =============================================================================
class TestPrivateStatePersistsAcrossRestart(_PrivateStoreCase):
    """决定、日志、一次性标记都必须落盘——内存变量在 Worker 回收后就不存在了。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "vault.db")
        self.client = review_store.LocalReviewStore(self.db)
        bot.set_review_store(self.client)
        self.state = {"events": {}}
        self.ref = enqueue(self.state)["review_ref"]

    def _restart(self):
        """模拟 Worker 重启：关掉旧连接，在同一份持久化存储上新建客户端。"""
        self.client.close()
        fresh = review_store.LocalReviewStore(self.db)
        bot.set_review_store(fresh)
        self.client = fresh
        return fresh

    def test_decision_survives_restart(self):
        self.client.decide(self.ref, "approve", "editor_01", "证据不足但可按归属发布")
        fresh = self._restart()
        st = fresh.get(self.ref)
        self.assertEqual(st["status"], review_store.DECIDED)
        self.assertEqual(st["action"], "approve")
        self.assertEqual(st["operator_id"], "editor_01")

    def test_payload_survives_restart(self):
        fresh = self._restart()
        payload = fresh.peek_payload(self.ref)
        self.assertEqual(payload["title"], TITLE)
        self.assertEqual(payload["event_id"], EVENT_ID)

    def test_log_survives_restart(self):
        self.client.decide(self.ref, "approve", "editor_01")
        fresh = self._restart()
        actions = [e["action"] for e in fresh.log(self.ref)]
        self.assertIn("ENQUEUED", actions)
        self.assertIn("DECIDED:approve", actions)

    def test_claim_token_survives_restart(self):
        self.client.decide(self.ref, "approve", "editor_01")
        claim = self.client.claim(self.ref)
        fresh = self._restart()
        fresh.finish(self.ref, claim["claim_token"],
                     review_store.DELIVERED, message_ids=[5])
        self.assertEqual(fresh.get(self.ref)["status"], review_store.DELIVERED)

    def test_claim_is_still_locked_after_restart(self):
        self.client.decide(self.ref, "approve", "editor_01")
        self.client.claim(self.ref)
        fresh = self._restart()
        with self.assertRaises(review_store.ReviewConflict):
            fresh.claim(self.ref)

    def test_exit_marker_survives_restart(self):
        self.client.exit_review(self.ref, "官方确认")
        fresh = self._restart()
        self.assertEqual(fresh.get(self.ref)["status"], review_store.EXITED)

    def test_view_rebuilt_from_private_store(self):
        # 公开 stub 里没有标题，但视图能靠私有存储把完整信息拼回来
        view = bot.list_pending_reviews(self.state)[0]
        self.assertEqual(view["title"], TITLE)
        self.assertEqual(view["review_status"], "PENDING")
        self.assertNotIn("title", store.find_review_stub(self.state, self.ref))


# =============================================================================
# 第 8 项：Telegram 发送成功后才允许完成投递状态迁移
# =============================================================================
class TestTelegramSuccessBeforeStateMigration(_PrivateStoreCase):
    """顺序铁律：先确认发送结果，再迁移私有审核状态。

    反过来的话，「Telegram 失败但私有状态已 delivered」会让这条永远不再重试，
    而读者一条都没收到。
    """

    def setUp(self):
        super().setUp()
        self.state = {"events": {}}
        self.ref = enqueue(self.state)["review_ref"]
        bot.approve_review(self.state, self.ref, "editor_01")

    def _post(self, ok, kind, reason, mid):
        return mock.patch.object(bot, "_post_telegram_once",
                                 return_value=(ok, kind, reason, mid))

    def test_failure_never_marks_delivered(self):
        with self._post(False, bot.TG_DETERMINISTIC, "HTTP 400: bad request", None):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], store.DELIVERY_FAILED)
        st = self.client.get(self.ref)
        self.assertEqual(st["status"], review_store.FAILED)
        self.assertNotEqual(st["status"], review_store.DELIVERED)

    def test_success_marks_delivered(self):
        with self._post(True, bot.TG_OK, "OK", 4321):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], store.COMMITTED)
        st = self.client.get(self.ref)
        self.assertEqual(st["status"], review_store.DELIVERED)
        self.assertEqual(st["message_ids"], [4321])

    def test_uncertain_maps_to_needs_manual(self):
        with self._post(False, bot.TG_UNCERTAIN, "ReadTimeout", None), \
                mock.patch("time.sleep", return_value=None):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], store.DELIVERY_UNCERTAIN)
        self.assertEqual(self.client.get(self.ref)["status"],
                         review_store.NEEDS_MANUAL,
                         "结果未知绝不能自动重发，也不能记成失败")

    def test_failed_can_retry_then_deliver(self):
        with self._post(False, bot.TG_DETERMINISTIC, "HTTP 400", None):
            bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(self.client.get(self.ref)["status"], review_store.FAILED)

        with self._post(True, bot.TG_OK, "OK", 55):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], store.COMMITTED)
        self.assertEqual(self.client.get(self.ref)["status"], review_store.DELIVERED)

    def test_delivered_cannot_be_sent_twice(self):
        with self._post(True, bot.TG_OK, "OK", 77) as post:
            bot.execute_review_decision(self.state, self.ref)
            calls = post.call_count
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["status"], "CLAIMED_BY_OTHER")
        self.assertEqual(post.call_count, calls, "已投递的事件不得再次发送")

    def test_decline_never_sends(self):
        # 换一条独立事件做拒绝，避免与上面的批准路径互相干扰
        state = {"events": {}}
        ref = enqueue(state, eid="ev_decline")["review_ref"]
        bot.decline_review(state, ref, "editor_01")
        with self._post(True, bot.TG_OK, "OK", 1) as post:
            out = bot.execute_review_decision(state, ref)
        self.assertEqual(out["status"], store.REVIEW_DECLINED)
        self.assertEqual(post.call_count, 0, "拒绝是存档动作，不得触发任何发送")
        self.assertEqual(self.client.get(ref)["status"], review_store.DECLINED)

    def test_publish_attributes_are_attributed(self):
        with self._post(True, bot.TG_OK, "OK", 8):
            out = bot.execute_review_decision(self.state, self.ref)
        self.assertEqual(out["publish_mode"], "ATTRIBUTED")
        self.assertEqual(out["publish_cred"], "E4")
        self.assertFalse(out["publish_is_confirmed"])
        self.assertIn("独立来源", out["unverified_note"])


# =============================================================================
# 第 9、10 项：Worker 重启后 dispatch 冷却与告警去重仍然有效
# =============================================================================
_NODE_CACHE = {}


def _run_node_suite():
    """跑一遍 worker.test.mjs，结果缓存（只跑一次）。"""
    if _NODE_CACHE:
        return _NODE_CACHE
    node = shutil.which("node")
    if not node:
        _NODE_CACHE.update(skip="未安装 node")
        return _NODE_CACHE
    try:
        proc = subprocess.run(
            [node, "worker.test.mjs", "--json"],
            cwd=WORKER_DIR, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
    except Exception as exc:                               # noqa: BLE001
        _NODE_CACHE.update(skip="node 执行失败：{}".format(exc))
        return _NODE_CACHE
    try:
        _NODE_CACHE["data"] = json.loads(proc.stdout)
    except Exception:                                      # noqa: BLE001
        _NODE_CACHE.update(skip="node 输出不是 JSON：{}".format(
            (proc.stdout or proc.stderr or "")[:300]))
    return _NODE_CACHE


class TestWorkerDurableStateIsPersistent(unittest.TestCase):
    """静态契约：调度锁与告警去重都必须落在 SQLite 表里。

    任何一处退回「模块级变量」，Worker 被回收后锁就没了——这正是
    旧版 dispatch 风暴与每分钟重复告警的成因。
    """

    def test_cooldown_lives_in_sqlite_table(self):
        js = _worker_source("review_vault.js")
        self.assertIn("CREATE TABLE IF NOT EXISTS ops_kv", js)
        self.assertIn('"last_dispatch_at"', js)

    def test_fault_dedup_lives_in_sqlite_table(self):
        js = _worker_source("review_vault.js")
        self.assertIn("CREATE TABLE IF NOT EXISTS ops_fault", js)
        self.assertIn("faultClaimAlert", js)
        self.assertIn("alerted=0", js, "告警名额必须能被条件 UPDATE 抢占")

    def test_worker_has_no_module_level_mutable_state(self):
        js = _worker_source("worker.js")
        self.assertNotRegex(
            js, r"(?m)^(let|var)\s+\w+",
            "Worker 模块作用域不得有可变状态（重启即丢，等于没有锁）")

    def test_dispatch_requires_durable_lock(self):
        js = _worker_source("worker.js")
        self.assertIn("skip_no_durable_lock", js,
                      "没有 Durable Object 时必须拒绝接管，而不是裸奔 dispatch")
        self.assertIn("/ops/cooldown", js)
        self.assertIn("mark: true", js, "真正 dispatch 之前必须原子抢一次锁")

    def test_alert_releases_slot_on_send_failure(self):
        js = _worker_source("worker.js")
        self.assertIn("release_alert", js,
                      "抢到名额却没发出去时必须归还，否则这次故障会被永久静音")

    def test_wrangler_uses_sqlite_backed_durable_objects(self):
        with open(os.path.join(WORKER_DIR, "wrangler.toml"), encoding="utf-8") as f:
            toml = f.read()
        self.assertIn('new_sqlite_classes = ["Vault"]', toml,
                      "Workers Free 只支持 SQLite 后端的 Durable Object")
        self.assertIn("[[durable_objects.bindings]]", toml)
        self.assertIn('class_name = "Vault"', toml)
        # 注释里提到 new_classes 是在解释「为什么不用它」，只有真正生效的配置行才算违规
        active = "\n".join(line for line in toml.splitlines()
                           if not line.strip().startswith("#"))
        self.assertNotRegex(active, r"(?m)^\s*new_classes\s*=",
                            "键值后端需要付费计划，不得混用")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑 Durable Object 重启测试")
class TestDurableObjectRestartBehaviour(unittest.TestCase):
    """真的跑一遍 node 套件：同一份持久化存储上新建 DO 实例 = 重启。"""

    def _cases(self, prefix):
        res = _run_node_suite()
        if res.get("skip"):
            self.skipTest(res["skip"])
        data = res["data"]
        failed = sorted(r["name"] for r in data["results"] if not r["ok"])
        self.assertEqual(failed, [], "node 套件存在失败用例：{}".format(failed[:5]))
        hits = [r for r in data["results"] if r["name"].startswith(prefix)]
        self.assertTrue(hits, "node 套件里找不到 {} 开头的用例".format(prefix))
        return " ".join(r["name"] for r in hits)

    def test_cooldown_survives_worker_restart(self):
        names = self._cases("C-8")
        self.assertIn("重启后冷却仍然生效", names)
        self.assertIn("冷却过期后可再次占用", names)

    def test_alert_dedup_survives_worker_restart(self):
        names = self._cases("C-9")
        self.assertIn("重启后仍然抢不到", names)
        self.assertIn("恢复后可重新告警", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
