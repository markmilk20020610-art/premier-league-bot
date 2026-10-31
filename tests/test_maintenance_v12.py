"""V1.2 第六批验收：项目维护与时间契约（GitHub / 调度 / 依赖 / 合并 / 看门狗）。

这一批没有「新功能」，全是**防止未来的自己（或未来的 AI）把系统改坏**的护栏。
因此测试形态和前五批不同：大量用例是「解析仓库里的文件并断言契约」，
而不是调用函数。这也是本批的价值所在 —— 契约只写在注释里等于没写。
"""
import contextlib
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import _stubs  # noqa: E402

_stubs.install()

import bot  # noqa: E402
import state_store as store  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import merge_state  # noqa: E402
import watchdog  # noqa: E402

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "scanner.yml")
REQUIREMENTS = os.path.join(ROOT, "requirements.txt")
COMMIT_SH = os.path.join(ROOT, "scripts", "commit_state.sh")
NO_CACHE_SH = os.path.join(ROOT, "scripts", "check_no_cache.sh")
GITIGNORE = os.path.join(ROOT, ".gitignore")

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def workflow_text():
    return read(WORKFLOW)


def cron_entries(text):
    """取出 workflow 里所有生效的 cron 表达式（被 # 注释掉的不算）。"""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^-\s*cron:\s*['\"]([^'\"]+)['\"]", stripped)
        if m:
            out.append(m.group(1))
    return out


def utc_hour_to_cn(hour, minute):
    """UTC 时:分 → 北京时间 时:分（中国无夏令时，固定 +8）。"""
    total = (hour * 60 + minute + 8 * 60) % (24 * 60)
    return total // 60, total % 60


class TempStateBase(unittest.TestCase):
    """把 STATE_FILE / LOG_FILE 指到临时目录，避免测试往仓库根目录写 state.json。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.dir)
        self._saved = (bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID)
        bot.STATE_FILE = os.path.join(self.dir, "state.json")
        bot.LOG_FILE = os.path.join(self.dir, "run_telemetry.log")
        bot.TELEGRAM_BOT_TOKEN = "TEST_TOKEN"
        bot.TELEGRAM_CHAT_ID = "123456"

    def tearDown(self):
        bot.STATE_FILE, bot.LOG_FILE, bot.TELEGRAM_BOT_TOKEN, bot.TELEGRAM_CHAT_ID = self._saved
        os.chdir(self.cwd)
        shutil.rmtree(self.dir, ignore_errors=True)


# =============================================================================
# 一、早报时间契约：北京时间 06:00（用户明确要求）
# =============================================================================
class TestMorningScheduleContract(TempStateBase):
    def test_target_constant_is_0600_beijing(self):
        self.assertEqual(bot.MORNING_TARGET_HOUR_CN, 6)
        self.assertEqual(bot.MORNING_TARGET_MINUTE_CN, 0)

    def test_cron_maps_to_six_am_beijing(self):
        """cron 是 UTC，必须是 22:00 才对得上北京 06:00 —— 这是本批的核心契约。"""
        entries = cron_entries(workflow_text())
        self.assertIn("0 22 * * *", entries,
                      f"早报 cron 必须存在且为 '0 22 * * *'，实际 {entries}")

        m = re.fullmatch(r"(\d+)\s+(\d+)\s+\*\s+\*\s+\*", "0 22 * * *")
        self.assertIsNotNone(m)
        h, mi = utc_hour_to_cn(int(m.group(2)), int(m.group(1)))
        self.assertEqual((h, mi), (bot.MORNING_TARGET_HOUR_CN, bot.MORNING_TARGET_MINUTE_CN),
                         "cron 与 MORNING_TARGET_* 必须严格对应，改一边忘另一边就会在这里炸")

    def test_morning_job_bound_to_same_cron(self):
        """morning job 的 if 条件必须绑定 '0 22 * * *'，否则 cron 改了 job 也不会跑。"""
        text = workflow_text()
        block = text.split("morning:", 1)[1].split("watchdog:", 1)[0]
        self.assertIn("'0 22 * * *'", block)

    def test_scan_job_does_not_steal_morning_slot(self):
        """扫描 job 必须显式排除早报/看门狗那两个 cron，避免整点双重触发。"""
        text = workflow_text()
        block = text.split("scan:", 1)[1].split("morning:", 1)[0]
        self.assertIn("github.event.schedule != '0 22 * * *'", block)
        self.assertIn("github.event.schedule != '0 23 * * *'", block)

    def test_offset_minutes(self):
        base = datetime.datetime(2026, 3, 1, 6, 0, tzinfo=CN_TZ)
        self.assertAlmostEqual(bot._morning_offset_minutes(base), 0.0, places=3)
        self.assertAlmostEqual(bot._morning_offset_minutes(base.replace(hour=7, minute=40)), 100.0)
        self.assertAlmostEqual(bot._morning_offset_minutes(base.replace(hour=5, minute=30)), -30.0)

    def test_off_schedule_warning_is_visible(self):
        """07:40 才触发早报 → 必须在 Actions 上打出 warning，不能悄悄晚发。"""
        state = {"last_morning_report_date": "2026-03-01"}
        late = datetime.datetime(2026, 3, 1, 7, 40, tzinfo=CN_TZ)
        buf = io.StringIO()
        with mock.patch.object(bot, "_cn_now", return_value=late), \
                mock.patch.object(bot, "_cn_date_str", return_value="2026-03-01"), \
                mock.patch.object(bot, "FORCE_SUMMARY", False), \
                contextlib.redirect_stdout(buf):
            rc = bot.run_morning_mode(state, bot._now_iso())
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Morning Off-Schedule", out)
        self.assertIn("MORNING_ALREADY_SENT", out, "日期锁仍要生效，不能因为晚到就重发")

    def test_on_time_run_has_no_warning(self):
        state = {"last_morning_report_date": "2026-03-01"}
        on_time = datetime.datetime(2026, 3, 1, 6, 3, tzinfo=CN_TZ)
        buf = io.StringIO()
        with mock.patch.object(bot, "_cn_now", return_value=on_time), \
                mock.patch.object(bot, "_cn_date_str", return_value="2026-03-01"), \
                mock.patch.object(bot, "FORCE_SUMMARY", False), \
                contextlib.redirect_stdout(buf):
            bot.run_morning_mode(state, bot._now_iso())
        self.assertNotIn("Morning Off-Schedule", buf.getvalue())

    def test_force_summary_suppresses_off_schedule_warning(self):
        """手动补发本来就不该卡在 06:00，别对补发也报「偏离目标」。"""
        state = {"last_morning_report_date": "2026-03-01"}
        late = datetime.datetime(2026, 3, 1, 11, 0, tzinfo=CN_TZ)
        buf = io.StringIO()
        with mock.patch.object(bot, "_cn_now", return_value=late), \
                mock.patch.object(bot, "_cn_date_str", return_value="2026-03-01"), \
                mock.patch.object(bot, "FORCE_SUMMARY", True), \
                mock.patch.object(bot, "_pool_for_morning", return_value=[]), \
                mock.patch.object(bot, "fetch_rss_sources", return_value=([], {})), \
                mock.patch.object(bot, "_emergency_google_news_fetch", return_value=([], {})), \
                mock.patch.object(bot, "_send_low_info_morning", return_value=0), \
                contextlib.redirect_stdout(buf):
            rc = bot.run_morning_mode(state, bot._now_iso())
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn("Morning Off-Schedule", out)
        self.assertIn("FORCE_SUMMARY", out)


# =============================================================================
# 二、GitHub Actions 供应链固定
# =============================================================================
class TestActionPinning(unittest.TestCase):
    def _uses(self, text):
        return re.findall(r"uses:\s*([^\s#]+)", text)

    def test_all_actions_pinned_to_commit_sha(self):
        """tag（@v4）可以被上游悄悄改指向，必须钉到 40 位 commit SHA。"""
        bad = []
        for ref in self._uses(workflow_text()):
            if ref.startswith(("docker://", "./")):
                continue
            name, _, version = ref.partition("@")
            if not re.fullmatch(r"[0-9a-f]{40}", version or ""):
                bad.append(ref)
        self.assertEqual(bad, [], f"这些 Action 没钉到 commit SHA：{bad}")

    def test_no_floating_branch_refs(self):
        for ref in self._uses(workflow_text()):
            self.assertNotRegex(ref, r"@(main|master|latest)$", f"不得跟随浮动分支：{ref}")

    def test_sha_has_version_comment(self):
        """钉 SHA 之后人会看不懂是哪个版本，注释必须写清楚。"""
        lines = [l for l in workflow_text().splitlines() if "uses:" in l]
        for line in lines:
            if "./" in line or "docker://" in line:
                continue
            self.assertIn("#", line, f"钉 SHA 的 Action 必须带版本注释：{line.strip()}")


# =============================================================================
# 三、state.json 语义合并（禁止 merge -X ours）
# =============================================================================
class TestSemanticMergeWiring(unittest.TestCase):
    # 只匹配「真的会执行」的命令行：注释和文档里解释历史是允许的，
    # 否则想留一条「为什么不再这么做」的说明反而会让测试变红。
    LIVE_MERGE = re.compile(r"(?m)^\s*git\s+merge\b[^\n]*(?:-X\s*ours|--strategy-option=ours)")

    def test_no_strategy_override_merge(self):
        """`git merge -X ours` 会整份丢弃远端 state，等于数据丢失。"""
        for name in os.listdir(os.path.join(ROOT, "scripts")):
            path = os.path.join(ROOT, "scripts", name)
            if not os.path.isfile(path):
                continue
            self.assertIsNone(self.LIVE_MERGE.search(read(path)),
                              f"{name} 里仍在执行 -X ours 合并")

    def test_commit_script_calls_semantic_merge(self):
        text = read(COMMIT_SH)
        self.assertIn("merge_state.py", text)
        self.assertIsNone(self.LIVE_MERGE.search(text))

    def test_commit_script_does_not_hide_git_failures(self):
        """关键 Git 操作失败必须让 Job 失败，不能靠 || true 吞掉。"""
        text = read(COMMIT_SH)
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#") or not s:
                continue
            if re.search(r"git\s+(fetch|push|commit|reset)", s):
                self.assertNotIn("|| true", s, f"Git 失败被吞掉：{s}")


class TestMergeStateSemantics(unittest.TestCase):
    def _event(self, status, **over):
        ev = {"event_id": "e1", "delivery_status": status, "updated_at": "2026-03-01T00:00:00+00:00",
              "evidence_url": "https://a/1", "chunks": []}
        ev.update(over)
        return ev

    def test_delivered_beats_pending(self):
        local = {"events": {"e1": self._event(store.DELIVERED, telegram_message_id=99)}}
        remote = {"events": {"e1": self._event(store.PENDING_DELIVERY)}}
        merged = merge_state.merge(local, remote)
        self.assertEqual(merged["events"]["e1"]["delivery_status"], store.DELIVERED,
                         "远端可能刚记下已送达，不能被本地这次运行覆盖回待投递")
        self.assertEqual(merged["events"]["e1"]["telegram_message_id"], 99)

    def test_committed_never_downgraded(self):
        local = {"events": {"e1": self._event(store.PENDING_DELIVERY)}}
        remote = {"events": {"e1": self._event(store.COMMITTED)}}
        merged = merge_state.merge(local, remote)
        self.assertEqual(merged["events"]["e1"]["delivery_status"], store.COMMITTED)

    def test_chunk_delivered_not_downgraded(self):
        local = {"events": {"e1": self._event(store.PENDING_DELIVERY, chunks=[
            {"chunk_index": 0, "delivery_status": "PENDING_DELIVERY"}] )}}
        remote = {"events": {"e1": self._event(store.PENDING_DELIVERY, chunks=[
            {"chunk_index": 0, "delivery_status": "DELIVERED", "telegram_message_id": 7}] )}}
        merged = merge_state.merge(local, remote)
        chunk = merged["events"]["e1"]["chunks"][0]
        self.assertEqual(chunk["delivery_status"], "DELIVERED")
        self.assertEqual(chunk["telegram_message_id"], 7, "已送达段必须带着回执一起保留")

    def test_morning_lock_takes_later_date(self):
        local = {"last_morning_report_date": "2026-02-28"}
        remote = {"last_morning_report_date": "2026-03-01"}
        self.assertEqual(merge_state.merge(local, remote)["last_morning_report_date"], "2026-03-01")
        self.assertEqual(merge_state.merge(remote, local)["last_morning_report_date"], "2026-03-01")

    def test_ai_stats_not_summed(self):
        """两边统计的是同一批调用，累加会凭空翻倍。"""
        local = {"ai_stats": {"date": "2026-03-01", "calls": 3}}
        remote = {"ai_stats": {"date": "2026-03-01", "calls": 5}}
        self.assertEqual(merge_state.merge(local, remote)["ai_stats"]["calls"], 5)

    def test_breaker_takes_later_open_until(self):
        local = {"circuit_breakers": {"deepseek": {"open_until": "2026-03-01T10:00:00+00:00"}}}
        remote = {"circuit_breakers": {"deepseek": {"open_until": "2026-03-01T12:00:00+00:00"}}}
        merged = merge_state.merge(local, remote)
        self.assertEqual(merged["circuit_breakers"]["deepseek"]["open_until"],
                         "2026-03-01T12:00:00+00:00")

    def test_processed_urls_union(self):
        local = {"processed_urls": ["https://a/1", "https://a/2"]}
        remote = {"processed_urls": ["https://a/2", "https://a/3"]}
        merged = merge_state.merge(local, remote)
        self.assertEqual(sorted(merged["processed_urls"]),
                         ["https://a/1", "https://a/2", "https://a/3"])

    def test_pool_union_dedup_sorted_desc(self):
        local = {"news_pool": [{"link": "https://a/1", "published": "2026-03-01T01:00:00"}]}
        remote = {"news_pool": [
            {"link": "https://a/1", "published": "2026-03-01T05:00:00"},
            {"link": "https://a/2", "published": "2026-03-01T03:00:00"},
        ]}
        pool = merge_state.merge(local, remote)["news_pool"]
        self.assertEqual(len(pool), 2, "同一链接必须去重")
        self.assertEqual(pool[0]["link"], "https://a/1", "应保留发布时间更晚的那份并排在前面")

    def test_cli_writes_atomically(self):
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "state.json")
            remote = os.path.join(d, "remote.json")
            with open(local, "w", encoding="utf-8") as f:
                json.dump({"events": {}, "processed_urls": ["https://a/1"]}, f)
            with open(remote, "w", encoding="utf-8") as f:
                json.dump({"events": {}, "processed_urls": ["https://a/2"]}, f)
            with mock.patch.object(sys, "argv", ["merge_state.py", local, remote]), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = merge_state.main()
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(local + ".merge.tmp"), "临时文件必须被 os.replace 吃掉")
            data = json.loads(read(local))
            self.assertEqual(len(data["processed_urls"]), 2)


# =============================================================================
# 四、心跳看门狗
# =============================================================================
class TestWatchdog(unittest.TestCase):
    NOW = datetime.datetime(2026, 3, 1, 7, 0, tzinfo=datetime.timezone.utc)  # 北京 15:00

    def test_healthy_state(self):
        state = {
            "last_scan_at": (self.NOW - datetime.timedelta(minutes=20)).isoformat(),
            "last_morning_report_date": "2026-03-01",
        }
        ok, problems, info = watchdog.check(state, now=self.NOW)
        self.assertTrue(ok, problems)
        self.assertEqual(problems, [])
        self.assertEqual(info["today_cn"], "2026-03-01")

    def test_stale_scan_is_alerted(self):
        state = {
            "last_scan_at": (self.NOW - datetime.timedelta(hours=6)).isoformat(),
            "last_morning_report_date": "2026-03-01",
        }
        ok, problems, _ = watchdog.check(state, now=self.NOW)
        self.assertFalse(ok, "调度停了 6 小时必须报警，否则用户只会觉得「今天没新闻」")
        self.assertTrue(any("扫描" in p for p in problems))

    def test_missing_morning_report_is_alerted(self):
        state = {
            "last_scan_at": (self.NOW - datetime.timedelta(minutes=10)).isoformat(),
            "last_morning_report_date": "2026-02-28",
        }
        ok, problems, _ = watchdog.check(state, now=self.NOW)
        self.assertFalse(ok)
        self.assertTrue(any("早报" in p for p in problems))

    def test_morning_not_yet_due_is_fine(self):
        """北京 07:00 时早报本来就还没到截止时间，不能误报。"""
        early = datetime.datetime(2026, 2, 28, 23, 0, tzinfo=datetime.timezone.utc)  # 北京 07:00
        state = {
            "last_scan_at": (early - datetime.timedelta(minutes=5)).isoformat(),
            "last_morning_report_date": "2026-02-28",
        }
        ok, problems, _ = watchdog.check(state, now=early)
        self.assertTrue(ok, problems)

    def test_fresh_clone_gets_grace(self):
        """刚 clone 的空仓库什么都没跑过，一上来就红叉只会教人忽略告警。"""
        ok, problems, info = watchdog.check({}, now=self.NOW)
        self.assertTrue(ok, problems)
        self.assertTrue(info["fresh_clone"])

    def test_missing_heartbeat_on_used_state_is_alerted(self):
        """有历史痕迹却没有心跳 = 升级后从没成功跑过，必须报。"""
        state = {"processed_urls": ["https://a/1"], "last_morning_report_date": "2026-03-01"}
        ok, problems, _ = watchdog.check(state, now=self.NOW)
        self.assertFalse(ok)
        self.assertTrue(any("last_scan_at" in p for p in problems))

    # ---- V1.3：state.json 体积警戒（慢性膨胀问题）----
    def _healthy(self):
        return {
            "last_scan_at": (self.NOW - datetime.timedelta(minutes=20)).isoformat(),
            "last_morning_report_date": "2026-03-01",
        }

    def test_oversized_state_is_alerted(self):
        """state.json 每轮都会被提交进 git，体积失控必须变成可见告警而不是慢性病。"""
        ok, problems, info = watchdog.check(self._healthy(), now=self.NOW,
                                            state_bytes=300 * 1024)
        self.assertFalse(ok)
        self.assertTrue(any("KB" in p for p in problems), problems)
        self.assertEqual(info["state_kb"], 300.0)
        self.assertEqual(info["max_state_kb"], watchdog.MAX_STATE_KB)

    def test_normal_state_size_is_fine(self):
        ok, problems, info = watchdog.check(self._healthy(), now=self.NOW,
                                            state_bytes=62 * 1024)
        self.assertTrue(ok, problems)
        self.assertEqual(info["state_kb"], 62.0)

    def test_no_size_info_when_not_provided(self):
        """没传体积时不能凭空报错，也不能伪造 state_kb。"""
        ok, problems, info = watchdog.check(self._healthy(), now=self.NOW)
        self.assertTrue(ok, problems)
        self.assertIsNone(info["state_kb"])

    def test_main_exit_codes(self):
        with mock.patch.object(sys, "argv", ["watchdog.py"]), \
                mock.patch.object(watchdog, "load_state", return_value={
                    "last_scan_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "last_morning_report_date": datetime.datetime.now(CN_TZ).strftime("%Y-%m-%d"),
                }), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watchdog.main(), 0)

        with mock.patch.object(sys, "argv", ["watchdog.py"]), \
                mock.patch.object(watchdog, "load_state", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watchdog.main(), 0, "空 state 属于宽限情形")

        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["watchdog.py"]), \
                mock.patch.object(watchdog, "load_state", return_value={
                    "processed_urls": ["https://a/1"],
                    "last_scan_at": "2020-01-01T00:00:00+00:00",
                    "last_morning_report_date": "2020-01-01",
                }), contextlib.redirect_stdout(buf):
            self.assertEqual(watchdog.main(), 1)
        self.assertIn("Watchdog Alert", buf.getvalue())


# =============================================================================
# 五、仓库卫生：字节码缓存 / 依赖锁定
# =============================================================================
class TestRepoHygiene(unittest.TestCase):
    def test_gitignore_covers_bytecode(self):
        text = read(GITIGNORE)
        self.assertRegex(text, r"(?m)^\s*\*?/?__pycache__/?\s*$|__pycache__")
        self.assertRegex(text, r"\*\.py(\[[a-z]+\]|c)")

    def test_cache_guard_script_exists(self):
        self.assertTrue(os.path.exists(NO_CACHE_SH), "workflow 引用了 check_no_cache.sh，文件必须存在")

    def test_cache_guard_wired_into_build_jobs(self):
        text = workflow_text()
        self.assertGreaterEqual(text.count("scripts/check_no_cache.sh"), 3,
                                "scan / morning / diagnose 三个 job 都要装这道守卫")

    def test_cache_guard_detects_tracked_pyc(self):
        bash, git = shutil.which("bash"), shutil.which("git")
        if not (bash and git):
            self.skipTest("本机没有 bash/git，跳过（CI 上会真实执行）")
        with tempfile.TemporaryDirectory() as d:
            self._git(git, d, "init", "-q")
            with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("__pycache__/\n*.py[cod]\n")
            os.makedirs(os.path.join(d, "__pycache__"))
            with open(os.path.join(d, "__pycache__", "bot.cpython-310.pyc"), "wb") as f:
                f.write(b"\x00\x01")
            # -f：模拟「.gitignore 是后来才补上的，字节码早就被提交进去了」的真实场景
            self._git(git, d, "add", "-f", "-A")

            out = self._run(bash, NO_CACHE_SH, d)
            self.assertEqual(out.returncode, 1, "跟踪了 .pyc 必须让 Job 失败")
            self.assertIn("Committed Bytecode", out.stdout + out.stderr)

            self._git(git, d, "rm", "-r", "--cached", "-q", "__pycache__")
            out = self._run(bash, NO_CACHE_SH, d)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_cache_guard_requires_gitignore_rule(self):
        bash, git = shutil.which("bash"), shutil.which("git")
        if not (bash and git):
            self.skipTest("本机没有 bash/git，跳过")
        with tempfile.TemporaryDirectory() as d:
            self._git(git, d, "init", "-q")
            with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
                f.write("state.json\n")
            self._git(git, d, "add", "-A")
            out = self._run(bash, NO_CACHE_SH, d)
            self.assertEqual(out.returncode, 1, ".gitignore 漏了字节码规则就等于没守卫")

    def _git(self, git, cwd, *args):
        return subprocess.run([git] + list(args), cwd=cwd, capture_output=True,
                              text=True, errors="replace", timeout=60)

    def _run(self, bash, script, cwd):
        return subprocess.run([bash, script], cwd=cwd, capture_output=True,
                              text=True, errors="replace", timeout=60)

    def test_requirements_are_pinned(self):
        for line in read(REQUIREMENTS).splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            self.assertRegex(s, r"^[A-Za-z0-9_.\-]+==\d+(\.\d+)*$",
                             f"依赖必须锁死版本，不能留浮动版本：{s}")

    def test_requirements_covers_used_packages(self):
        text = read(REQUIREMENTS)
        self.assertIn("requests==", text)
        self.assertIn("google-genai==", text)


# =============================================================================
# 六、移除写死的主帅映射（换帅是英超最高频变动）
# =============================================================================
class TestNoHardcodedStaff(unittest.TestCase):
    FORBIDDEN = ("Guardiola", "Maresca", "Amorim", "Postecoglou", "瓜迪奥拉", "斯洛特")

    def test_bot_source_has_no_staff_table(self):
        src = read(os.path.join(ROOT, "bot.py"))
        for name in self.FORBIDDEN:
            self.assertNotIn(name, src,
                             f"bot.py 里仍有写死的人事信息（{name}）：几周后就会变成错误事实")

    def test_prompts_ask_model_to_use_material_only(self):
        prompt = bot.build_summary_prompt("池子", "统计", "")
        self.assertIn("人事信息铁律", prompt)
        self.assertIn("只能来自", prompt)

    def test_scan_prompt_has_same_rule(self):
        import inspect
        src = inspect.getsource(bot.evaluate_with_gemini)
        self.assertIn("人事信息铁律", src)
        for name in self.FORBIDDEN:
            self.assertNotIn(name, src)


# =============================================================================
# 七、事件表裁剪保护 / 扫描心跳
# =============================================================================
class TestPruneAndHeartbeat(TempStateBase):
    def test_prune_keeps_unfinished_events(self):
        """未完结事件被裁掉 = 「等人工确认」的那条静默消失。"""
        events = {}
        for i in range(210):
            events[f"old{i}"] = {
                "event_id": f"old{i}", "delivery_status": store.COMMITTED,
                "updated_at": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00",
            }
        events["pinned_pending"] = {"event_id": "pinned_pending",
                                    "delivery_status": store.PENDING_DELIVERY,
                                    "updated_at": "2025-01-01T00:00:00+00:00"}
        events["pinned_retry"] = {"event_id": "pinned_retry",
                                  "delivery_status": store.RETRYABLE_FAILURE,
                                  "updated_at": "2025-01-01T00:00:00+00:00"}
        events["pinned_uncertain"] = {"event_id": "pinned_uncertain",
                                      "delivery_status": store.DELIVERY_UNCERTAIN,
                                      "updated_at": "2025-01-01T00:00:00+00:00"}
        state = {"events": events}

        with contextlib.redirect_stdout(io.StringIO()):
            store._prune_events(state, keep=200)

        for eid in ("pinned_pending", "pinned_retry", "pinned_uncertain"):
            self.assertIn(eid, state["events"], f"{eid} 未完结，不得被裁掉")
        self.assertLessEqual(len(state["events"]), 203)
        self.assertGreater(len(state["events"]), 200, "普通事件应被裁到 keep 上限附近")

    def test_scan_sets_heartbeat(self):
        """每轮扫描都要写 last_scan_at，否则看门狗分不清「没新闻」和「没在跑」。"""
        with mock.patch.object(bot, "fetch_rss_sources", return_value=([], {"https://a/rss": 200})), \
                mock.patch("time.sleep", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = bot.run_scan_mode(bot.load_state(), bot._now_iso())
        self.assertEqual(rc, 0)
        state = bot.load_state()
        self.assertTrue(state.get("last_scan_at"), "心跳没落盘，看门狗会误报")


if __name__ == "__main__":
    unittest.main(verbosity=2)
