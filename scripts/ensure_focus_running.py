"""保证焦点战监控（focus.yml）始终有「一个在跑 + 一个在等」。

## 为什么需要它

`focus.yml` 自己会连续跑 5 小时 50 分，但**总得有人把它叫起来**。
原来指望人工点 GitHub 的 Run workflow，或者让用户去 cron-job.org 加第二条任务——
这两条都依赖人，人忘了就整晚没有首发监控。

而 `scanner.yml` 已经被 cron-job.org 每分钟叫一次（实测 14:06→14:13 每分钟一条）。
所以让它顺手检查一下 focus 是否还活着，没了就补一个——**用户零操作**。

## 为什么是「补一个 pending」而不是「没了才补」

GitHub 的 concurrency 只保留「1 个运行中 + 1 个 pending」，新的 pending 会顶掉旧的。
所以只要**始终有一个 pending 在后面排队**，当前 job 一结束它就立刻接管，中间零空档。
如果等到「一个都不剩」才补，那就会有最多 1 分钟的裸奔窗口——正好可能落在首发公布那一刻。

## 权限

`workflow_dispatch` 是 GitHub 明文规定的例外：**用 GITHUB_TOKEN 触发的
`workflow_dispatch` / `repository_dispatch` 会真的创建新 run**（其它事件不会，
这是防递归）。前提是 job 拿到 `permissions: actions: write`。

用法（CI 里）：
    GH_TOKEN=${{ secrets.GITHUB_TOKEN }} GH_REPO=owner/repo \
        python scripts/ensure_focus_running.py

本机没有 token 时**静默跳过并返回 0**，绝不因为「本地跑测试」而变红。
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
WORKFLOW_FILE = "focus.yml"
# 这些状态都算「已经在队列里」，不必再补
PENDING_STATUSES = ("queued", "waiting", "pending", "requested")


def _request(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "yuewei-focus-keeper",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read().decode("utf-8")
    return json.loads(body) if body.strip() else {}


def runs_url(repo):
    return f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/runs?per_page=20"


def dispatch_url(repo):
    return f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/dispatches"


def has_pending(runs):
    """后面有没有排队的 run。只看 pending 类状态，不看 in_progress。"""
    return any((r.get("status") or "") in PENDING_STATUSES for r in runs or [])


def has_any_run(runs):
    return any((r.get("status") or "") in ("in_progress",) + PENDING_STATUSES
               for r in runs or [])


def decide(runs):
    """返回 (should_dispatch, reason)。"""
    if has_pending(runs):
        return False, "已有 pending run 在排队，不重复触发"
    if has_any_run(runs):
        return True, "有 run 在跑但没有后继，补一个 pending 以便无缝接管"
    return True, "没有任何 run，立即触发"


def ensure(token, repo, transport=None, dry_run=False):
    """主逻辑。返回 (ok, message)。任何异常都不抛出。"""
    transport = transport or _request
    try:
        data = transport(runs_url(repo), token)
    except urllib.error.HTTPError as exc:
        return False, f"查询运行列表失败 HTTP {exc.code}（权限不足？需要 actions: write）"
    except Exception as exc:                    # noqa: BLE001
        return False, f"查询运行列表失败 {type(exc).__name__}: {exc}"

    runs = (data or {}).get("workflow_runs") or []
    should, reason = decide(runs)
    if not should:
        return True, f"跳过：{reason}（当前 {len(runs)} 条近期运行）"
    if dry_run:
        return True, f"[DRY] 将会触发：{reason}"
    try:
        transport(dispatch_url(repo), token, method="POST",
                  payload={"ref": "main"})
    except urllib.error.HTTPError as exc:
        return False, f"触发失败 HTTP {exc.code}"
    except Exception as exc:                    # noqa: BLE001
        return False, f"触发失败 {type(exc).__name__}: {exc}"
    return True, f"已触发：{reason}"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repo:
        print("[INFO] 没有 GH_TOKEN / GH_REPO，跳过（本地运行属正常）")
        return 0
    ok, msg = ensure(token, repo, dry_run=dry_run)
    print(f"[{'OK' if ok else 'WARN'}] {msg}")
    # 补触发失败不该让整个扫描 job 变红——新闻扫描比这个重要，
    # 而且下一分钟还会再试一次。
    return 0


if __name__ == "__main__":
    sys.exit(main())
