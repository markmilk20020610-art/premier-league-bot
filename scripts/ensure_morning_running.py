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

四道锁，任何一道生效都不会出现第二条早报：

  1. **本脚本的补发窗口**：只在 06:00 ~ 09:00（北京时间）之间动手。
     180 分钟窗口 ÷ 5 分钟一次 scan = 最多 36 次尝试，窗口一过就停手。
     这不是「无限重试」——是有明确上限的有限重试。
  2. **自我排除**：本脚本自己就跑在某一条 scan run 里，那条 run 的状态恒为
     `in_progress`。若不排除，本脚本 100% 自我阻塞、永远不补发（这是修掉的真缺陷，
     见 `_blocking_runs`）。用 `GITHUB_RUN_ID` 精确排除自己，不靠猜。
  3. **API 去重**：已经有**目标时刻之后创建的** `in_progress` / `queued` run 就不再补。
  4. **bot.py 的日期锁**：`last_morning_report_date == 今天` 直接 `MORNING_ALREADY_SENT`，
     即使前三道全失效，也不会发第二条。

## 为什么窗口从 20 分钟放宽到 180 分钟

20 分钟的窗口只有 4 次机会，而 scan 本身要跑 1~3 分钟，再叠上
GitHub 的排队与 API 抖动，实测经常整段窗口都在「已有 run 在跑」里空转过去。
用户明确要求 06:00 正常触发、06:26 / 07:57 / 08:59 都能补上、09:01 不再补，
180 分钟正好是 06:00 + 3 小时，与 09:00 的边界一致。

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
#
# 20 → 180（B1）。理由：
#   · 20 分钟只有 4 次机会，而 scan 自己就要跑 1~3 分钟，再叠上排队与 API 抖动，
#     实测整段窗口经常都在「已有 run 在跑」里空转过去，早报照样迟到；
#   · 用户要求的行为边界很明确：06:00 正常触发，06:26 / 07:57 / 08:59 都要能补上，
#     09:01 不再补 —— 正好是 06:00 + 180 分钟；
#   · 放宽窗口不会造成重复推送：bot.py 的 last_morning_report_date 日期锁是最后一道闸，
#     窗口内每多试一次，只是多一次「查到已发就退出」的空转。
RETRY_BAND_MINUTES = 180

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


def _parse_iso(raw):
    """把 GitHub 的 ISO 时间戳解析成 aware datetime；失败返回 None。"""
    if not raw:
        return None
    txt = str(raw).strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _blocking_runs(runs, target, current_run_id=None):
    """返回真正会阻塞补触发的 run 列表。

    B2 修的就是这里。旧实现把**所有** ACTIVE_STATUSES 的 run 都当成阻塞，
    而本脚本自己就跑在一条 scan run 里——那条 run 的状态恒为 in_progress，
    于是「永远至少有一个 active run」，`decide()` 100% 返回 False，
    早报补偿功能从头到尾一次都没生效过。

    三条排除规则，全部由程序按 API 字段判定，不靠猜测：
      1. **排除自己**：run id == GITHUB_RUN_ID。这是最关键的修正。
      2. **排除目标时刻之前创建的 run**：06:00 之前创建并仍在跑的 run 只可能是
         普通扫描，不可能是今天的早报补触发，不该拦住早报。
      3. **排除已结束的 run**：只认 ACTIVE_STATUSES，completed/failure 不拦。

    注意一个诚实的局限：GitHub 的 run 列表 API **不返回 workflow_dispatch 的 inputs**，
    所以无法直接判断「这条 run 是不是 mode=morning」。因此规则 2 采取保守口径——
    06:00 之后创建的活跃 run 一律视为「可能是早报」而选择等待。
    代价是偶尔多等一轮（5 分钟），收益是绝不会重复触发早报。
    180 分钟的窗口给足了 36 轮机会，这个代价可以忽略。
    """
    target_utc = target.astimezone(datetime.timezone.utc)
    blocking = []
    for r in runs or []:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "")
        if current_run_id and rid == str(current_run_id):
            continue                       # 规则 1：不能自己拦自己
        if (r.get("status") or "") not in ACTIVE_STATUSES:
            continue                       # 规则 3：已结束的不算
        created = _parse_iso(r.get("created_at") or r.get("run_started_at"))
        if created is not None and created < target_utc:
            continue                       # 规则 2：目标时刻之前的 run 与早报无关
        blocking.append(r)
    return blocking


def decide(now_cn, last_morning_date, runs=None, current_run_id=None):
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
                       f"不再重试，交给看门狗报红")

    blocking = _blocking_runs(runs, target, current_run_id)
    if blocking:
        return False, (f"已有 {len(blocking)} 条与早报相关的 run 在跑/排队"
                       f"（已排除当前 run {current_run_id or '-'}），等它们出结果")

    return True, (f"北京 {now_cn.strftime('%H:%M')}，今日（{today}）早报未发送，"
                  f"补触发 morning（漂移 {drift_min:+.0f} 分钟）")


def ensure(token, repo, transport=None, now=None, last_morning_date=None,
           dry_run=False, current_run_id=None):
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
    should, reason = decide(now_cn, last_morning_date, runs, current_run_id)
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


def current_run_id_from_env():
    """当前 GitHub Actions run 的 id。本地运行时为空串。

    `GITHUB_RUN_ID` 是 GitHub 内置环境变量，在 job 的每一步都存在，
    所以不需要额外传参，也不存在「猜一个 id 出来」的可能。
    """
    return str(os.environ.get("GITHUB_RUN_ID") or "").strip()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repo:
        print("[INFO] 没有 GH_TOKEN / GH_REPO，跳过（本地运行属正常）")
        return 0
    run_id = current_run_id_from_env()
    ok, msg = ensure(token, repo, dry_run=dry_run, current_run_id=run_id)
    print(f"[{'OK' if ok else 'WARN'}] 当前 run={run_id or '-'} | {msg}")
    # 补触发失败不该让整个扫描 job 变红——新闻扫描比这个重要，
    # 而且 5 分钟后还会再试（窗口内最多 36 次）。
    return 0


if __name__ == "__main__":
    sys.exit(main())
