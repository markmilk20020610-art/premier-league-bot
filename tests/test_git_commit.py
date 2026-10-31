"""阶段 2.3 验收：用真实 git 仓库验证 scripts/commit_state.sh。

覆盖：正常推送、无变化短路、远端冲突、推送失败、只提交 state.json。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "commit_state.sh")

ENV = dict(os.environ)
ENV.update({
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "echo",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
})


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, check=check,
        capture_output=True, text=True,
    )


def write_state(repo, urls):
    with open(os.path.join(repo, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"processed_urls": urls}, f)


def read_state(repo):
    with open(os.path.join(repo, "state.json"), encoding="utf-8") as f:
        return json.load(f)


def read_remote(bare_repo):
    """直接从裸仓库读 main 分支的 state.json，避免被本地工作区干扰。"""
    out = git(os.path.dirname(bare_repo), "--git-dir", bare_repo, "show", "main:state.json").stdout
    return json.loads(out)


class GitCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "--bare", "-b", "main", self.remote)

        self.seed = os.path.join(self.tmp, "seed")
        os.makedirs(self.seed)
        git(self.seed, "init", "-b", "main")
        write_state(self.seed, ["https://seed/1"])
        git(self.seed, "add", "state.json")
        git(self.seed, "commit", "-m", "seed")
        git(self.seed, "remote", "add", "origin", self.remote)
        git(self.seed, "push", "-u", "origin", "main")

        self.a = os.path.join(self.tmp, "a")
        self.b = os.path.join(self.tmp, "b")
        git(self.tmp, "clone", self.remote, self.a)
        git(self.tmp, "clone", self.remote, self.b)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, repo, attempts=2):
        return subprocess.run(
            ["bash", SCRIPT, "state.json", str(attempts)],
            cwd=repo, env=ENV, capture_output=True, text=True,
        )

    # ---------- 1. 正常推送 ----------
    def test_normal_push(self):
        write_state(self.a, ["https://a/1"])
        r = self.run_script(self.a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("已推送", r.stdout)
        self.assertEqual(read_remote(self.remote), {"processed_urls": ["https://a/1"]})

    # ---------- 2. 无变化短路 ----------
    def test_no_change(self):
        r = self.run_script(self.a)
        self.assertEqual(r.returncode, 0)
        self.assertIn("无变化", r.stdout)

    # ---------- 3. 远端冲突（两个 clone 都改 state.json）----------
    def test_remote_conflict_merges_instead_of_overwriting(self):
        """冲突时两边都是「刚跑出来的真相」，必须语义合并，谁都不许整份覆盖谁。

        V1.2 第六批之前的旧行为是 `git merge -X ours`：B 推送后 A 刚记下的
        「已处理」全部消失 → 下一轮重复推送。现在要求两边取并集。
        """
        write_state(self.a, ["https://a/conflict"])
        self.assertEqual(self.run_script(self.a).returncode, 0)

        write_state(self.b, ["https://b/conflict"])
        r = self.run_script(self.b)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("merge", r.stdout.lower())

        remote = read_remote(self.remote)["processed_urls"]
        self.assertIn("https://b/conflict", remote, "本地（B）刚记下的不能丢")
        self.assertIn("https://a/conflict", remote, "远端（A）记下的也不能丢，否则重复推送")

    # ---------- 4. 推送失败必须非 0 ----------
    def test_push_failure_returns_nonzero(self):
        git(self.a, "remote", "set-url", "origin", os.path.join(self.tmp, "missing.git"))
        write_state(self.a, ["https://a/fail"])
        r = self.run_script(self.a, attempts=2)
        self.assertNotEqual(r.returncode, 0, "推送失败必须让 Job 失败")
        self.assertIn("::error", r.stdout)

    # ---------- 5. 只提交 state.json ----------
    def test_only_state_json_committed(self):
        write_state(self.a, ["https://a/only"])
        with open(os.path.join(self.a, "bot.py"), "w", encoding="utf-8") as f:
            f.write("# dirty file should not be committed\n")
        r = self.run_script(self.a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        files = git(self.seed, "show", "--name-only", "--pretty=format:", "HEAD").stdout.split()
        self.assertIn("state.json", files)
        self.assertNotIn("bot.py", files, "脚本不得提交 state.json 之外的文件")
        self.assertIn("bot.py", git(self.a, "status", "--porcelain").stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
