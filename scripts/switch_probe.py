"""功能开关探针：在子进程里按当前环境变量跑一遍早报选材路径。

为什么需要子进程
----------------
`IMPORTANCE_SORT` 是模块级常量，在 import 时就定下来了。同一个进程里改环境变量
不会生效，reload 又会把整个 bot 模块重建（副作用不可控）。所以双模式验证必须
分两个进程跑，各自带着不同的环境变量。

输出一份 JSON 摘要，供 tests/test_importance_switch.py 断言。

用法：
    IMPORTANCE_SORT=0 python scripts/switch_probe.py
    IMPORTANCE_SORT=1 python scripts/switch_probe.py
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot  # noqa: E402
import content_type as ct  # noqa: E402
import evidence as ev  # noqa: E402
import importance as imp  # noqa: E402


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def item(title, domain, url, hours_ago):
    """发布时间用「距今 N 小时」，避免探针结果随真实时钟漂移而失效。"""
    ts = now_utc() - datetime.timedelta(hours=hours_ago)
    return {
        "title": title,
        "link": url,
        "desc": "",
        "publisher": domain,
        "feed_origin": "https://{}/rss".format(domain),
        "published": ts.isoformat().replace("+00:00", "Z"),
    }


def build_pool():
    """33 条混合池：C 罗退役 10 + 英超 10 + 非英超 5 + 推广 8。"""
    rows = [
        # C 罗国家队退役（同一件事 10 条）
        ("Cristiano Ronaldo's club to donate all ticket sales to tragic "
         "family", "thesun.co.uk", "donate", 5.0),
        ("A bitter end for Ronaldo - the icon who transformed Portugal",
         "theguardian.com", "bitter-end", 6.0),
        ("Cristiano Ronaldo QUITS international football over explosive "
         "fall-out with Portugal manager", "dailymail.co.uk", "quits", 7.0),
        ("Cristiano Ronaldo finished second to Messi even in retirement",
         "telegraph.co.uk", "second", 8.0),
        ("Trophies, tears, records and rows - Ronaldo's Portugal career",
         "bbc.co.uk", "trophies", 9.0),
        ("Ronaldo retires from Portugal duty", "reuters.com", "retires", 10.0),
        ("Ronaldo announces international retirement on Instagram!",
         "thesun.co.uk", "instagram", 11.0),
        ("Ronaldo's most iconic moments as he retires from international "
         "football", "skysports.com", "iconic", 12.0),
        ("Ronaldo retires from international football - Manchester United",
         "manutd.com", "manutd", 13.0),
        ("Ronaldo's Portugal statement in full", "bbc.co.uk", "statement", 14.0),
        # 英超
        ("Arsenal agree deal for striker Viktor Gyokeres as talks advance",
         "bbc.co.uk", "arsenal", 1.0),
        ("Liverpool confirm Alisson out for six weeks with calf injury",
         "skysports.com", "liverpool", 2.0),
        ("Chelsea takeover completed as consortium buys majority stake",
         "dailymail.co.uk", "chelsea", 3.0),
        ("Manchester United appoint new sporting director", "manutd.com",
         "united", 4.0),
        ("Tottenham sign defender Marc Guehi in 45m deal",
         "theguardian.com", "spurs", 5.5),
        ("Newcastle United face PSR breach hearing over stadium costs",
         "telegraph.co.uk", "newcastle", 6.5),
        ("Aston Villa beat Wolves 2-0 at Villa Park", "bbc.co.uk", "villa", 7.5),
        ("Everton points deduction appeal rejected by independent panel",
         "skysports.com", "everton", 8.5),
        ("Brighton manager criticises referee after VAR controversy",
         "theguardian.com", "brighton", 9.5),
        ("West Ham confirm Julen Lopetegui departure after poor run",
         "bbc.co.uk", "westham", 10.5),
        # 非英超
        ("Real Betis and Sevilla draw in pre-season friendly", "marca.com",
         "betis", 11.5),
        ("Juventus beat Napoli 1-0 in Serie A opener", "gazzetta.it", "juve", 12.5),
        ("Bayern Munich sign winger from Ajax", "kicker.de", "bayern", 13.5),
        ("PSG held to draw by Marseille in Ligue 1", "lequipe.fr", "psg", 14.5),
        ("Flamengo win Brazilian league title", "globo.com", "flamengo", 15.0),
        # 推广 / 八卦（最新，旧版按时间排序会把它们排在最前）
        ("Cristiano Ronaldo launches new CR7 boots collection",
         "thesun.co.uk", "boots", 0.1),
        ("Win a signed Arsenal shirt in our competition", "mirror.co.uk",
         "shirt", 0.2),
        ("Chelsea unveil new home kit for next season", "chelseafc.com",
         "kit", 0.3),
        ("Manchester United memberships on sale now with 20% off", "manutd.com",
         "membership", 0.4),
        ("Vote for your Premier League player of the month",
         "premierleague.com", "vote", 0.5),
        ("Liverpool announce Black Friday deal on club store",
         "liverpoolfc.com", "blackfriday", 0.6),
        ("Cristiano Ronaldo's girlfriend shows off new supercar",
         "thesun.co.uk", "supercar", 0.7),
        ("Tottenham launch new training range for 2026/27 season",
         "tottenhamhotspur.com", "range", 0.8),
    ]
    return [item(t, d, "https://www.{}/{}".format(d, s), h)
            for t, d, s, h in rows]


def main():
    # 计数器：证明开关关闭时新逻辑**一次都没被调用**
    calls = {"events_for_morning": 0}
    real = bot._events_for_morning

    def counting(*a, **kw):
        calls["events_for_morning"] += 1
        return real(*a, **kw)

    bot._events_for_morning = counting

    state = {"events": {}, "news_pool": []}
    raw = build_pool()
    state["news_pool"] = raw

    # ---- 第 3 步之前：旧路径选材 ----
    pool = bot._pool_for_morning(state)
    old_order = [p["title"] for p in pool]

    # ---- 第 3.5 步：事件级排序（受开关控制）----
    # 这一段与 run_morning_mode 里的代码逐字对应
    meta = {}
    event_stats = None
    if bot.IMPORTANCE_SORT:
        ordered_pool, morning_events, event_stats = bot._events_for_morning(
            state, pool)
        if ordered_pool:
            pool = ordered_pool
            meta["importance_sort"] = True
            meta["event_stats"] = {
                k: v for k, v in event_stats.items()
                if isinstance(v, (int, float, str, bool))
            }

    out = {
        "importance_sort": bot.IMPORTANCE_SORT,
        "events_for_morning_called": calls["events_for_morning"],
        "raw_count": len(raw),
        "pool_count": len(pool),
        "old_order": old_order,
        "final_order": [p.get("title") for p in pool],
        "meta": meta,
        "promo_in_final": sum(
            1 for p in pool
            if ct.is_low_value(ct.classify(
                p.get("title") or "", p.get("desc") or "",
                event_type=ev.primary_event_type(p.get("title") or "")
            )["content_type"])),
        "has_event_ids": any(p.get("_event_id") for p in pool),
        "aline_in_final": sum(
            1 for p in pool
            if imp.classify_scope(p.get("title") or "",
                                  p.get("desc") or "")[0] == imp.A_LINE),
        "ronaldo_in_final": sum(
            1 for p in pool if "ronaldo" in (p.get("title") or "").lower()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
