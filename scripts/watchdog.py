"""心跳看门狗（V1.2 第六批）。

只做一件事：确认「该发的东西真的发了」。

为什么需要它
------------
前五批修的都是「发出去的内容对不对」，但还有一类故障是**彻底静默**：
cron-job.org 停了、GitHub schedule 被跳过、Secrets 过期、仓库被归档……
此时每一轮都正常退出，Actions 上什么都看不到，只有用户那边「好几天没消息」。

看门狗独立于 bot.py 运行，只读 state.json 里两个心跳字段：
  · last_scan_at             最近一次扫描跑完的时间
  · last_morning_report_date 最近一次早报的北京日期
任一超期就 exit 1，让 Actions 自己把问题喊出来。

用法：
    python scripts/watchdog.py            # 检查
    python scripts/watchdog.py --json     # 输出 JSON 报告
"""

import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 扫描由 cron-job.org 每 10 分钟触发；超过这个时长没有任何扫描 = 调度已停
MAX_SCAN_AGE_HOURS = 3
# 早报每天 06:00 北京时间；到 08:00 仍没发就算漏报
MORNING_DEADLINE_HOUR_CN = 8

# state.json 体积警戒线（V1.3 慢性问题）。
# 实测：news_pool 占全文 81%（84 条 × 598 字节），正常体积 60~90KB。
# 因为 state.json 每轮都会被提交，体积失控会直接放大 git 历史的增长
# （实测每版 delta 只有 24B~1.5KB，年增约 15MB；一旦文件翻倍，年增也翻倍）。
# 超过这条线基本只有两种原因：池子上限被改大，或事件表没被裁剪。
MAX_STATE_KB = 250


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_iso(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _looks_fresh_clone(state):
    """没有任何历史痕迹 = 刚 clone 的空仓库，给一次宽限，别一上来就报红。"""
    return not (state.get("processed_urls") or state.get("events")
                or state.get("news_pool") or state.get("last_scan_at"))


def check(state, now=None, state_bytes=None):
    """返回 (是否健康, [问题描述...], 诊断信息)。

    `state_bytes` 是 state.json 的磁盘体积（可选）：给了就顺带检查体积警戒线。
    """
    now = now or _now()
    now_cn = now.astimezone(CN_TZ)
    today_cn = now_cn.strftime("%Y-%m-%d")
    fresh_clone = _looks_fresh_clone(state)
    problems = []

    last_scan = _parse_iso(state.get("last_scan_at"))
    scan_age_h = None
    if last_scan is None:
        if not fresh_clone:
            problems.append("state.json 里没有 last_scan_at：从未成功跑过一次扫描")
    else:
        scan_age_h = (now - last_scan).total_seconds() / 3600.0
        if scan_age_h > MAX_SCAN_AGE_HOURS:
            problems.append(f"最近一次扫描在 {scan_age_h:.1f} 小时前"
                            f"（上限 {MAX_SCAN_AGE_HOURS}h），调度可能已停")

    last_morning = (state.get("last_morning_report_date") or "").strip()
    if now_cn.hour >= MORNING_DEADLINE_HOUR_CN and last_morning != today_cn:
        if not (fresh_clone and not last_morning):
            problems.append(f"今日（北京 {today_cn}）早报尚未发送，"
                            f"最后发送日期为 {last_morning or '无'}")

    # 体积警戒：state.json 每轮都会被提交进 git，体积失控会放大历史增长
    if state_bytes and state_bytes > MAX_STATE_KB * 1024:
        problems.append(f"state.json 已 {state_bytes / 1024:.0f}KB"
                        f"（警戒线 {MAX_STATE_KB}KB）：新闻池或事件表可能失控，"
                        f"会连带放大 git 历史增长")

    info = {
        "checked_at": now.isoformat(),
        "beijing_time": now_cn.strftime("%Y-%m-%d %H:%M"),
        "last_scan_at": state.get("last_scan_at"),
        "scan_age_hours": None if scan_age_h is None else round(scan_age_h, 2),
        "last_morning_report_date": last_morning,
        "today_cn": today_cn,
        "max_scan_age_hours": MAX_SCAN_AGE_HOURS,
        "morning_deadline_hour_cn": MORNING_DEADLINE_HOUR_CN,
        "fresh_clone": fresh_clone,
        "state_kb": None if not state_bytes else round(state_bytes / 1024, 1),
        "max_state_kb": MAX_STATE_KB,
    }
    return (not problems), problems, info


def load_state(path=None):
    path = path or os.path.join(ROOT, "state.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"::error title=Watchdog Cannot Read State::state.json 解析失败：{e}")
        return None


def _state_bytes(path=None):
    path = path or os.path.join(ROOT, "state.json")
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def main():
    as_json = "--json" in sys.argv
    state = load_state()
    if state is None:
        return 1
    ok, problems, info = check(state, state_bytes=_state_bytes())

    if as_json:
        print(json.dumps({"ok": ok, "problems": problems, **info}, ensure_ascii=False, indent=2))
    else:
        print(f"[INFO] 看门狗检查 | 北京时间 {info['beijing_time']}")
        print(f"[INFO] last_scan_at={info['last_scan_at']} "
              f"last_morning_report_date={info['last_morning_report_date']}")
        if info.get("state_kb") is not None:
            print(f"[INFO] state.json 体积 {info['state_kb']}KB"
                  f"（警戒线 {info['max_state_kb']}KB）")
        for p in problems:
            print(f"[PROBLEM] {p}")

    if ok:
        print("::notice title=Watchdog OK::心跳正常，扫描与早报都在预期时间内")
        return 0
    for p in problems:
        print(f"::error title=Watchdog Alert::{p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
