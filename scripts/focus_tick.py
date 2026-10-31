"""焦点战监控单次 tick（V2.0 §5 的执行体）。

## 它为什么是一个「单次 tick」而不是长驻进程

GitHub Actions 里没法常驻一个 daemon，但**单个 job 最长可以跑 6 小时**。
所以调度由 `focus.yml` 的 shell 循环负责：

    while 未到截止时间:
        python scripts/focus_tick.py     # 干一次活，立刻退出
        sleep <下一拍间隔>

好处：
· 一次成功启动就能覆盖近 6 小时 —— **不再依赖每分钟的 dispatch**；
· 每一拍都是独立进程，某一拍崩了不会拖死整个循环；
· tick 本身是纯函数式的，可离线测试。

这就是「不用部署外部看门狗」的解法：cron-job.org 只需偶尔把它叫起来一次，
剩下的时间它自己转。

## 职责边界

只做三件事，都在 `focus_config` 的配置约束下：

1. 焦点战日历（缓存 30 分钟，避免每分钟抓 3 个 CSV）
2. 裁判组任命 → 九项报告（每场只推一次）
3. 首发名单 → 九项校验 → 推送（按阵容指纹去重）+ 对手时间对照

**不做**新闻扫描、不做早报 —— 那些仍归 `bot.py`。
"""

import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import competitor_watch as cw          # noqa: E402
import lineup_monitor as lm            # noqa: E402
import match_calendar as mc            # noqa: E402
import pl_api                          # noqa: E402
import referee_intel as ri             # noqa: E402

CALENDAR_TTL_MINUTES = 30          # 日历缓存时长
OFFICIAL_TTL_MINUTES = 360         # 官方赛程列表缓存（380 场，别每分钟抓）
COMPETITOR_MIN_INTERVAL_SECONDS = 120   # 对手资讯最短间隔，别把人家打烦
COMPETITOR_DELAY_SECONDS = 1.0          # 三个源之间的礼貌间隔（测试里置 0）
STATE_KEY = "focus"

# tick 把「下一拍睡多久」写在这个文件里，由 focus.yml 的 shell 循环读取。
# 为什么不在 shell 里写死一个数：`focus_config.LINEUP_POLICY` 里临界窗口是 30 秒、
# 预热是 120 秒，shell 写死 45 秒的话这份配置就完全不起作用了。
INTERVAL_FILE = ".focus_interval"
INTERVAL_MIN = 30
INTERVAL_MAX = 300

# 缓存与状态**必须分开存**。
#   状态（focus_state.json）：已推送标记、阵容指纹、首次检测时间 —— 400 字节量级，
#     进仓库、跨 job 重启存活，是「不重复推送」的唯一依据。
#   缓存（.focus_cache.json）：380 场官方赛程 + 焦点战日历 —— 176KB。
#     实测 official_fixtures 一项就 101KB。如果也进仓库、每 15 分钟提交一次，
#     一天就是 3.5MB 的 git 对象，几周就把仓库撑坏。
#     它只在**同一个 6 小时 job 内**有价值（避免每拍重抓），job 结束即可丢弃，
#     所以放本地文件并 .gitignore。
CACHE_FILE = ".focus_cache.json"


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _parse(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f+00:00",
                "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            return datetime.datetime.strptime(str(s), fmt)
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(str(s)).replace(tzinfo=None)
    except ValueError:
        return None


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# 状态（进仓库）与缓存（不进仓库）
# =============================================================================
def blank_state():
    """真正的状态：只放「重启后不能丢」的东西，越小越好。"""
    return {
        "matches": {},
        "last_tick_at": "",
        "ticks": 0,
    }


# 旧版本把 calendar / official_fixtures 写在状态里，176KB 每 15 分钟提交一次。
# 这里主动剔除，老仓库第一次跑就会自动瘦身。
LEGACY_STATE_KEYS = ("calendar", "calendar_at", "official_fixtures",
                     "official_at", "competitor_at")


def ensure(state):
    """取（并初始化）焦点战状态子树，并迁移掉旧版本塞进来的缓存字段。"""
    sub = state.get(STATE_KEY)
    if not isinstance(sub, dict):
        sub = blank_state()
        state[STATE_KEY] = sub
    for k in LEGACY_STATE_KEYS:
        sub.pop(k, None)
    for k, v in blank_state().items():
        sub.setdefault(k, v)
    return sub


def blank_cache():
    return {
        "calendar": None,
        "calendar_at": "",
        "official_fixtures": None,
        "official_at": "",
    }


def load_cache(path=None):
    path = path or os.path.join(ROOT, CACHE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return blank_cache()
    except (OSError, ValueError):
        return blank_cache()
    out = blank_cache()
    out.update({k: v for k, v in data.items() if k in out})
    return out


def save_cache(cache, path=None):
    """原子写：先写 .tmp 再 replace，避免 shell 读到半截 JSON。"""
    path = path or os.path.join(ROOT, CACHE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def match_state(sub, match_id):
    return sub["matches"].setdefault(match_id, {
        "referee_pushed": False,
        "appointment_seen_at": "",          # 首次观测到完整裁判组的时刻（非官方公布时刻）
        "appointment_seen_lead_minutes": None,
        "lineup_pushed": {},        # team -> 阵容指纹
        "first_seen": {},           # team -> 首次检测时间
        "official_published_at": {},
        "competitor": {},
        # 对手采集节流必须**按场次**：放在全局的话，一场刷完其余场次全被跳过，
        # 多场同时进窗口时只有第一场能拿到对手时间。
        "competitor_at": "",
    })


# =============================================================================
# 依赖注入（测试时替换成假实现）
# =============================================================================
class Deps:
    def __init__(self, transport=None, telegram=None, now=None):
        self.transport = transport            # (url) -> (status, text)
        self.telegram = telegram              # (text) -> bool
        self.now = now or _now()

    def send(self, text):
        if self.telegram is None:
            print("[DRY] 未配置推送，仅打印：\n" + text)
            return True
        return self.telegram(text)


# =============================================================================
# 日历
# =============================================================================
def get_calendar(cache, deps, force=False):
    """取焦点战日历（带 TTL 缓存）。返回 (calendar, from_cache)。"""
    cached = cache.get("calendar")
    at = _parse(cache.get("calendar_at"))
    fresh = at is not None and (deps.now - at).total_seconds() < CALENDAR_TTL_MINUTES * 60
    if cached and fresh and not force:
        return cached, True
    cal = mc.build_calendar(now=deps.now, transport=deps.transport)
    cache["calendar"] = cal
    cache["calendar_at"] = _iso(deps.now)
    return cal, False


def get_official_fixtures(cache, deps):
    """官方赛程列表（380 场，缓存 6 小时）。"""
    cached = cache.get("official_fixtures")
    at = _parse(cache.get("official_at"))
    fresh = at is not None and (deps.now - at).total_seconds() < OFFICIAL_TTL_MINUTES * 60
    if cached and fresh:
        return cached
    try:
        rows = pl_api.fetch_fixtures(transport=deps.transport)
    except Exception as exc:               # noqa: BLE001
        print(f"[WARN] 官方赛程抓取失败：{type(exc).__name__} {exc}")
        return cached or []
    cache["official_fixtures"] = [
        {**r, "kickoff_utc": r["kickoff_utc"].strftime("%Y-%m-%dT%H:%M:%SZ")}
        for r in rows
    ]
    cache["official_at"] = _iso(deps.now)
    return cache["official_fixtures"]


def _fixture_for(cache, match, deps):
    """把我们的 match 映射到官方 fixture_id。"""
    rows = get_official_fixtures(cache, deps)
    if not rows:
        return None
    kick = _parse(match["kickoff_time_utc"])
    if kick is None:
        return None
    for r in rows:
        if (mc.canon_team(r["home_team"]) == mc.canon_team(match["home_team"])
                and mc.canon_team(r["away_team"]) == mc.canon_team(match["away_team"])
                and abs((_parse(r["kickoff_utc"]) - kick).total_seconds()) <= 1200):
            return r
    return None


# =============================================================================
# 裁判
# =============================================================================
def handle_referee(sub, match, ms, deps, detail):
    """任命一旦出现就推一次九项报告（每场只推一次）。"""
    if ms.get("referee_pushed"):
        return None
    officials = (detail or {}).get("officials") or []
    if not officials:
        return None

    appts = pl_api.appointments_for_match(match, detail, deps.now)
    main = next((a for a in appts if a["role"] == ri.ROLE_REFEREE), None)
    if main is None:
        return None

    report = ri.build_nine_item_report(match, main, None, officials=officials)
    text = ri.render_nine_item_report(report)
    text += f"\n\n【焦点等级】{match['focus_level']}　开球 {match['kickoff_time_beijing']}（北京）"

    # 实测：官方接口在 T-69 分钟时仍返回 officials=[]，任命是**临开球才可见**的。
    # 这里记的是「本系统首次观测到的时刻」，不是官方公布时刻——源里根本没有这个字段，
    # 填了就是编造。措辞必须区分清楚。
    lead = _lead_minutes(match, deps.now)
    if lead is not None:
        ms["appointment_seen_at"] = _iso(deps.now)
        ms["appointment_seen_lead_minutes"] = lead
        text += (f"\n【任命观测】本系统首次看到完整裁判组：开球前 {lead} 分钟"
                 f"（官方公布时刻该接口不提供，不做推断）")

    if ri.is_watch_referee(main["referee_name"]):
        text += "\n【观察名单】该裁判在七人重点观察名单内"
    if deps.send(text):
        ms["referee_pushed"] = True
        return "referee"
    return None


def _lead_minutes(match, now):
    """距开球还有多少分钟（已开球返回 None）。"""
    kick = _parse(match.get("kickoff_time_utc"))
    if kick is None:
        return None
    mins = int(round((kick - now).total_seconds() / 60))
    return mins if mins >= 0 else None


# =============================================================================
# 首发
# =============================================================================
def handle_lineups(sub, match, ms, deps, detail):
    """检查两队首发，校验通过且是新版本才推。"""
    if not detail:
        return []
    pushed = []
    teams = detail.get("teams") or []
    if len(teams) < 2:
        return []

    # 官方队名与我们的展示名可能不同（Spurs / Tottenham）。必须按 canon 对齐，
    # 不能靠字符串相等——对不上的话，A 队的首发会被当成 B 队的推出去。
    ours = {}
    for t in teams:
        c = mc.canon_team(t)
        if c == mc.canon_team(match["home_team"]):
            ours[t] = match["home_team"]
        elif c == mc.canon_team(match["away_team"]):
            ours[t] = match["away_team"]
    if not ours:
        return []

    for off_name, team in ours.items():
        rec = pl_api.lineup_for_team(detail, off_name, deps.now)
        if not rec:
            continue                      # 官方还没公布这一队，正常
        rec["team"] = team                # 用我们的展示名，别透传源里的写法
        opp_name = next((n for n in teams if n != off_name), "")
        other = pl_api.lineup_for_team(detail, opp_name, deps.now) if opp_name else None

        h = lm.players_hash(rec["players"])
        if ms["lineup_pushed"].get(team) == h:
            continue                      # 同一版，跳过

        problems = lm.validate_lineup(
            {**rec, "match_id": match["match_id"]}, opponent=other)
        status = lm.decide_status(rec["source_tier"], True, problems)
        action = lm.decide_action(status, problems, True, rec.get("source_url"))
        if action == lm.ACT_SKIP:
            continue

        first = ms["first_seen"].get(team) or _iso(deps.now)
        ms["first_seen"].setdefault(team, first)
        speed = {
            "match_id": match["match_id"], "team": team,
            "official_published_at": ms["official_published_at"].get(team, ""),
            "first_detected_at": first,
            "pushed_at": _iso(deps.now),
        }
        comp = ms.get("competitor") or {}
        proof = lm.speed_proof(speed, competitors_seen_at=comp.get("competitors_seen_at", ""))

        # ⚠️ rec 来自 pl_api，**不带 match_id**；render_lineup_message 要用它做标题。
        # 少补这一行，每一次首发推送都会 KeyError（这个 bug 是测试抓出来的）。
        record = {**rec, "match_id": match["match_id"], "status": status}
        text = lm.render_lineup_message(record, problems, action, proof)
        text += f"\n\n【焦点等级】{match['focus_level']}　开球 {match['kickoff_time_beijing']}（北京）"
        if comp.get("competitor_title"):
            text += f"\n【对手对照】{comp['note']}"

        if deps.send(text):
            ms["lineup_pushed"][team] = h
            pushed.append(team)
    return pushed


def refresh_competitor(cache, ms, match, deps):
    """采一次对手时间（有最短间隔，别把人家站点打烦）。"""
    at = _parse(ms.get("competitor_at"))
    if at and (deps.now - at).total_seconds() < COMPETITOR_MIN_INTERVAL_SECONDS:
        return ms.get("competitor") or {}
    try:
        news = cw.fetch_all(now=deps.now, transport=deps.transport,
                            delay=COMPETITOR_DELAY_SECONDS)
    except Exception as exc:               # noqa: BLE001
        print(f"[WARN] 对手采集失败：{type(exc).__name__}")
        return ms.get("competitor") or {}
    ms["competitor_at"] = _iso(deps.now)
    rec = cw.competitor_speed_record(
        news["items"], match["home_team"], match["away_team"], deps.now,
        transport=deps.transport, max_resolve=3)
    if rec.get("competitors_seen_at"):
        ms["competitor"] = rec
    return ms.get("competitor") or {}


# =============================================================================
# 主流程
# =============================================================================
def tick(state, deps=None, max_matches=6, cache=None):
    """跑一次。返回本次动作摘要（便于日志与测试断言）。

    `cache` 是进程外的缓存字典（main 里落到 .focus_cache.json）。不传就现造一个，
    语义上等于「每次都重新抓」——测试里要显式传，才能验证 TTL 行为。
    """
    deps = deps or Deps()
    sub = ensure(state)
    cache = blank_cache() if cache is None else cache
    summary = {"at": _iso(deps.now), "phase": {}, "pushed": [], "checked": 0,
               "calendar": "cached", "errors": []}

    cal, from_cache = get_calendar(cache, deps)
    summary["calendar"] = "cached" if from_cache else "fresh"
    if not cal.get("focus_matches"):
        summary["calendar"] = "empty"
        sub["last_tick_at"] = _iso(deps.now)
        sub["ticks"] += 1
        return summary

    for match in cal["focus_matches"]:
        if not match.get("lineup_monitor_enabled") and not match.get("referee_monitor_enabled"):
            continue
        phase = lm.phase_of(match["kickoff_time_utc"], deps.now)
        summary["phase"][match["match_id"]] = phase
        # 只在监控窗口内工作（idle 时不打扰官方接口）
        if phase not in (lm.PHASE_WARMUP, lm.PHASE_INTENSIFY,
                         lm.PHASE_CRITICAL, lm.PHASE_POST):
            continue
        # max_matches 限制的是**真正打官方接口的场次数**，不是「只看最近的 6 场」。
        # 否则日历一长，窗口内的场次可能排在第 7 位之后而永远轮不到。
        if summary["checked"] >= max_matches:
            break

        ms = match_state(sub, match["match_id"])
        summary["checked"] += 1

        fx = _fixture_for(cache, match, deps)
        if not fx:
            summary["errors"].append(f"{match['match_id']}: 官方赛程里找不到对应场次")
            continue
        try:
            detail = pl_api.fetch_fixture_detail(fx["fixture_id"],
                                                 transport=deps.transport)
        except Exception as exc:           # noqa: BLE001
            summary["errors"].append(f"{match['match_id']}: 详情抓取失败 {type(exc).__name__}")
            continue

        if match.get("referee_monitor_enabled"):
            got = handle_referee(sub, match, ms, deps, detail)
            if got:
                summary["pushed"].append(f"{match['match_id']}:{got}")

        if match.get("lineup_monitor_enabled"):
            refresh_competitor(cache, ms, match, deps)
            for team in handle_lineups(sub, match, ms, deps, detail):
                summary["pushed"].append(f"{match['match_id']}:lineup:{team}")

    sub["last_tick_at"] = _iso(deps.now)
    sub["ticks"] += 1
    return summary


def next_sleep_seconds(cache, now=None):
    """下一拍睡多久（供 shell 循环使用）。读的是缓存里的日历。"""
    now = now or _now()
    cal = (cache or {}).get("calendar") or {}
    best = None
    for match in cal.get("focus_matches") or []:
        sec, _phase, _why = lm.next_interval(match["kickoff_time_utc"], now)
        best = sec if best is None else min(best, sec)
    if best is None:
        return 60
    return max(INTERVAL_MIN, min(INTERVAL_MAX, int(best)))


def write_interval_file(seconds, root=None):
    """把下一拍间隔落盘给 shell 循环读。

    写成「先写临时文件再 rename」，避免 shell 正好读到半截内容。
    """
    path = os.path.join(root or ROOT, INTERVAL_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(int(seconds)))
    os.replace(tmp, path)
    return path


# =============================================================================
# CLI
# =============================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    import bot                          # 延迟导入：bot.py 很重，测试不需要

    # ⚠️ 焦点战用自己的状态文件，**不碰 state.json**。
    # 理由：扫描 job 每 5 分钟提交一次 state.json，如果这条 6 小时的循环也写同一个文件，
    # 两边必然反复 rebase 冲突，最后谁的状态都可能被覆盖掉。
    # 分文件之后两个 job 完全解耦，各自提交各自的。
    path = os.path.join(ROOT, "focus_state.json")
    try:
        state = bot.store.load_state(path)
    except Exception as exc:            # noqa: BLE001
        print(f"[ERROR] 读 {path} 失败：{exc}")
        return 2

    deps = Deps(telegram=None if dry_run else bot.send_telegram)
    if dry_run:
        print("[DRY] 演练模式：只打印推送内容，不真发")

    # 缓存只在**同一个 job 内**有用（避免每拍重抓 380 场赛程），
    # 所以放本地文件、不进仓库；读失败一律当作没有缓存，绝不因此中断。
    cache = load_cache()
    summary = tick(state, deps, cache=cache)
    print("[FOCUS] " + json.dumps(summary, ensure_ascii=False))

    try:
        bot.store.save_state_atomic(state, path)
    except Exception as exc:            # noqa: BLE001
        print(f"[WARN] state 回写失败：{exc}")

    try:
        save_cache(cache)
    except OSError as exc:
        print(f"[WARN] 缓存写入失败（不影响本轮结果）：{exc}")

    # 无论 tick 是否成功都要写间隔：循环不能因为这一拍失败就停下来。
    sec = next_sleep_seconds(cache, deps.now)
    try:
        write_interval_file(sec)
    except OSError as exc:
        print(f"[WARN] 间隔文件写入失败：{exc}")
    print(f"[FOCUS] 下一拍间隔 {sec} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
