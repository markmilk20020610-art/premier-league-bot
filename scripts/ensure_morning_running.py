"""保证「每天北京时间 06:00 的早报」真的会被触发。

## 为什么需要它

改完 `MORNING_TARGET_HOUR_CN = 6` 之后，早报的**唯一触发源**仍然是
`scanner.yml` 里的 `cron: '0 22 * * *'`，也就是 GitHub 自带的 schedule。

而这个仓库的 GitHub schedule 实测极不可靠（`scanner.yml` 顶部注释有原始记录）：

  · 全历史只有 7 条 schedule 触发的运行记录；
  · `'0 22 * * *'`（原 06:00 早报）曾延迟 **207 分钟**才跑；
  · state.json 里 `last_summary_sent_at = 2026-11-01T00:49:24Z`，
    换算北京时间是 **08:49**，比当时的 06:00 目标晚了 169 分钟。

也就是说：**光改 cron，早报并不会真的在 06:00 到。**
真正被 cron-job.org 每分钟叫醒的只有 `scan` job（`*/5 * * * *` 只是兜底）。
所以让 scan job 顺手检查「今天 06:00 的早报发了没」，没发就补一次 dispatch——
和 `ensure_focus_running.py` 是同一套思路，用户零操作。

## 为什么不会重复推送

三道锁，任何一道生效都不会出现第二条早报：

  1. **本脚本的补发窗口**：只在 06:00 ~ 06:20（北京时间）之间动手，
     最多 5 次尝试（scan 每 5 分钟一次），窗口一过就交给 07:00 的看门狗报红。
     这不是「无限重试」——是有明确上限的有限重试。
  2. **API 去重**：已经有 `in_progress` / `queued` 的 scanner run 就不再补。
  3. **bot.py 的日期锁**：`last_morning_report_date == 今天` 直接 `MORNING_ALREADY_SENT`，
     即使前两道全失效，也不会发第二条。

## 权限

`workflow_dispatch` 用 GITHUB_TOKEN 触发会真的创建新 run（GitHub 明文规定的例外），
前提是 scan job 拿到 `permissions: actions: write`——它已经有了。

用法（CI 里）：
    GH_TOKEN=${{ secrets.GITHUB_TOKEN }} GH_REPO=owner/repo \
        python scripts/ensure_morning_running.py

本机没有 token 时**静默跳过并返回 0**，绝不因为「本地跑测试」而变红。
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
WORKFLOW_FILE = "scanner.yml"

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# ⚠️ 必须与 bot.py 的 MORNING_TARGET_HOUR_CN / MORNING_TARGET_MINUTE_CN 一致。
# 本脚本要能在「装依赖之前」跑，所以不能 import bot（bot 依赖 requirements.txt）。
# tests/test_maintenance_v12.py 会解析本文件并断言两边相等。
MORNING_TARGET_HOUR_CN = 6
MORNING_TARGET_MINUTE_CN = 0

# 补发窗口：目标时刻之后多少分钟内允许补触发。
# 5 分钟一次 scan → 最多 5 次尝试，超过就停手，避免「模型挂了就无限重试」。
RETRY_BAND_MINUTES = 20

# 这些状态算「已经在队列里」，不必再补
ACTIVE_STATUSES = ("queued", "waiting", "pending", "requested", "in_progress")

STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.json")


def _request(url, token, method="GET", payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "yuewei-morning-keeper",
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


def beijing_now(now=None):
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(CN_TZ)


def load_last_morning_date(path=None):
    """读 state.json 里的 last_morning_report_date；读不到返回空串。

    读不到**不等于**没发过——但这里选择「当作没发过」：早报漏发的代价
    远大于多触发一次（后者被 bot.py 的日期锁挡住，不会真的重复推送）。
    """
    path = path or STATE_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            return str((json.load(f) or {}).get("last_morning_report_date") or "").strip()
    except Exception:                                  # noqa: BLE001
        return ""


def target_of(now_cn):
    return now_cn.replace(hour=MORNING_TARGET_HOUR_CN, minute=MORNING_TARGET_MINUTE_CN,
                          second=0, microsecond=0)


def decide(now_cn, last_morning_date, runs=None):
    """返回 (should_dispatch, reason)。纯函数，方便测试。"""
    today = now_cn.strftime("%Y-%m-%d")
    target = target_of(now_cn)
    drift_min = (now_cn - target).total_seconds() / 60.0

    if last_morning_date == today:
        return False, f"今日（{today}）早报已发送，无需补触发"
    if drift_min < 0:
        return False, (f"未到目标 {MORNING_TARGET_HOUR_CN:02d}:{MORNING_TARGET_MINUTE_CN:02d}"
                       f"（北京 {now_cn.strftime('%H:%M')}）")
    if drift_min > RETRY_BAND_MINUTES:
        return False, (f"已过补发窗口：漂移 {drift_min:.0f} 分钟 > {RETRY_BAND_MINUTES} 分钟，"
                       f"不再重试，交给 07:00 的看门狗报红")

    active = [r for r in (runs or []) if (r.get("status") or "") in ACTIVE_STATUSES]
    if active:
        return False, f"已有 {len(active)} 条 scanner run 在跑/排队，等它们出结果"

    return True, (f"北京 {now_cn.strftime('%H:%M')}，今日（{today}）早报未发送，"
                  f"补触发 morning（漂移 {drift_min:+.0f} 分钟）")


def ensure(token, repo, transport=None, now=None, last_morning_date=None, dry_run=False):
    """主逻辑。返回 (ok, message)。任何异常都不抛出。"""
    transport = transport or _request
    now_cn = beijing_now(now)
    if last_morning_date is None:
        last_morning_date = load_last_morning_date()

    try:
        data = transport(runs_url(repo), token)
    except urllib.error.HTTPError as exc:
        return False, f"查询运行列表失败 HTTP {exc.code}（权限不足？需要 actions: write）"
    except Exception as exc:                           # noqa: BLE001
        return False, f"查询运行列表失败 {type(exc).__name__}: {exc}"

    runs = (data or {}).get("workflow_runs") or []
    should, reason = decide(now_cn, last_morning_date, runs)
    if not should:
        return True, f"跳过：{reason}"
    if dry_run:
        return True, f"[DRY] 将会触发：{reason}"
    try:
        transport(dispatch_url(repo), token, method="POST",
                  payload={"ref": "main", "inputs": {"mode": "morning"}})
    except urllib.error.HTTPError as exc:
        return False, f"触发失败 HTTP {exc.code}"
    except Exception as exc:                           # noqa: BLE001
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
    # 而且 5 分钟后还会再试（窗口内最多 5 次）。
    return 0


if __name__ == "__main__":
    sys.exit(main())
