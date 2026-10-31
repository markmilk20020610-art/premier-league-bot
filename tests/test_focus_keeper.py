"""焦点战监控的「保姆」—— 离线测试。

`scripts/ensure_focus_running.py` 的作用是让 `focus.yml` 永远有
「一个在跑 + 一个在等」。它一旦判断错，后果是**整晚没有首发监控**
（漏触发）或者**每分钟灌一个 run**（重复触发，把仓库跑爆）。

所以这里把 GitHub API 注入成假的，断言的是「该不该触发」这个决策本身。
"""

import os
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ensure_focus_running as ef     # noqa: E402


def run(status, conclusion=None):
    return {"status": status, "conclusion": conclusion}


class FakeAPI:
    """记录调用；按 url 返回预置数据。"""

    def __init__(self, runs, dispatch_error=None):
        self.runs = runs
        self.dispatch_error = dispatch_error
        self.calls = []

    def __call__(self, url, token, method="GET", payload=None):
        self.calls.append({"url": url, "method": method, "payload": payload})
        if method == "POST":
            if self.dispatch_error:
                raise self.dispatch_error
            return {}
        return {"workflow_runs": self.runs}

    @property
    def dispatched(self):
        return [c for c in self.calls if c["method"] == "POST"]


class TestDecide(unittest.TestCase):
    def test_no_runs_means_dispatch(self):
        should, reason = ef.decide([])
        self.assertTrue(should)
        self.assertIn("没有任何 run", reason)

    def test_in_progress_without_successor_dispatches(self):
        """有 run 在跑但后面没人排队 → 必须补一个，否则它一结束就有空档。"""
        should, reason = ef.decide([run("in_progress")])
        self.assertTrue(should)
        self.assertIn("补一个 pending", reason)

    def test_pending_is_enough(self):
        """已有 pending 就不要再触发——否则每分钟灌一个 run。"""
        should, reason = ef.decide([run("in_progress"), run("pending")])
        self.assertFalse(should)
        self.assertIn("已有 pending", reason)

    def test_queued_counts_as_pending(self):
        should, _ = ef.decide([run("queued")])
        self.assertFalse(should)

    def test_completed_only_means_dispatch(self):
        should, reason = ef.decide([run("completed", "success")])
        self.assertTrue(should)

    def test_failed_last_run_still_dispatches(self):
        """上一轮失败正是最该补的时候。"""
        should, _ = ef.decide([run("completed", "failure")])
        self.assertTrue(should)


class TestEnsure(unittest.TestCase):
    def test_dispatch_posts_to_right_endpoint(self):
        api = FakeAPI([run("completed", "success")])
        ok, msg = ef.ensure("tok", "o/r", transport=api)
        self.assertTrue(ok)
        self.assertIn("已触发", msg)
        post = api.dispatched[0]
        self.assertEqual(
            post["url"],
            "https://api.github.com/repos/o/r/actions/workflows/focus.yml/dispatches")
        self.assertEqual(post["payload"], {"ref": "main"})

    def test_skip_does_not_post(self):
        api = FakeAPI([run("in_progress"), run("pending")])
        ok, msg = ef.ensure("tok", "o/r", transport=api)
        self.assertTrue(ok)
        self.assertIn("跳过", msg)
        self.assertEqual(api.dispatched, [])

    def test_dry_run_does_not_post(self):
        api = FakeAPI([])
        ok, msg = ef.ensure("tok", "o/r", transport=api, dry_run=True)
        self.assertTrue(ok)
        self.assertIn("[DRY]", msg)
        self.assertEqual(api.dispatched, [])

    def test_permission_error_is_reported_not_raised(self):
        def boom(url, token, method="GET", payload=None):
            raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)
        ok, msg = ef.ensure("tok", "o/r", transport=boom)
        self.assertFalse(ok)
        self.assertIn("403", msg)
        self.assertIn("actions: write", msg)

    def test_network_error_is_not_fatal(self):
        def boom(url, token, method="GET", payload=None):
            raise ConnectionError("dns fail")
        ok, msg = ef.ensure("tok", "o/r", transport=boom)
        self.assertFalse(ok)
        self.assertIn("ConnectionError", msg)

    def test_dispatch_failure_is_reported(self):
        api = FakeAPI([], dispatch_error=urllib.error.HTTPError(
            "u", 422, "Unprocessable", {}, None))
        ok, msg = ef.ensure("tok", "o/r", transport=api)
        self.assertFalse(ok)
        self.assertIn("触发失败", msg)

    def test_main_without_token_is_silent_success(self):
        """本地跑测试不该因为没 token 就红。"""
        old = {k: os.environ.pop(k, None) for k in ("GH_TOKEN", "GITHUB_TOKEN",
                                                    "GH_REPO", "GITHUB_REPOSITORY")}
        try:
            self.assertEqual(ef.main([]), 0)
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v


class TestWorkflowWiring(unittest.TestCase):
    """workflow 接线错了，脚本再对也不会被调用。"""

    def setUp(self):
        self.path = os.path.join(ROOT, ".github", "workflows", "scanner.yml")
        with open(self.path, encoding="utf-8") as f:
            self.src = f.read()

    def test_scan_job_calls_the_keeper(self):
        self.assertIn("scripts/ensure_focus_running.py", self.src)

    def test_scan_job_has_actions_write(self):
        """没有 actions: write，workflow_dispatch 会被 403。"""
        self.assertIn("actions: write", self.src)

    def test_keeper_runs_before_pip_install(self):
        """纯标准库、零依赖，放前面才能在 pip 装崩时仍然把 focus 拉起来。"""
        self.assertLess(self.src.index("ensure_focus_running.py"),
                        self.src.index("pip install --timeout 60"))

    def test_uses_python3(self):
        """ubuntu-latest 不保证有 `python` 这个命令。"""
        self.assertIn("python3 scripts/ensure_focus_running.py", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
