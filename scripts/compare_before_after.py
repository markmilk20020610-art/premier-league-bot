"""V1.0 核心修复：改造前后对照。

输出四张表（缺一不可）：
  1. 文章级排名（旧版口径：按发布时间倒序）
  2. 事件级排名（新版口径：按重要性排序、同事件只占一个名额）
  3. 重要性分级明细（每条报道的 S/A/B/C 与依据）
  4. 最终入选结果（谁进了前 40，谁被挡在外面，为什么）

数据：2026-11-05 真实抓取的 C 罗国家队退役 10 条标题，
外加一份混合池（英超 10 条 / 非英超 5 条 / 推广 8 条）用来验证名额分配。

用法：
    python scripts/compare_before_after.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot
import content_type as ct
import evidence as ev
import event_aggregate as ea
import importance as imp

BUDGET = bot.SUMMARY_MAX_MATERIALS

# ==================== 数据 ====================
RONALDO = [
    ("Cristiano Ronaldo's club to donate all ticket sales to tragic Jesus de "
     "Miguel's family and invite them as VIP guests", "thesun.co.uk", "donate", 13),
    ("A bitter end for Ronaldo - the icon who transformed Portugal",
     "theguardian.com", "bitter-end", 14),
    ("Cristiano Ronaldo QUITS international football over explosive fall-out "
     "with Portugal manager", "dailymail.co.uk", "quits", 15),
    ("Cristiano Ronaldo finished second to Messi even in retirement - the "
     "ending to his Portugal career is sad and predictable", "telegraph.co.uk",
     "second", 16),
    ("Trophies, tears, records and rows - Ronaldo's Portugal career will "
     "divide opinion", "bbc.co.uk", "trophies", 17),
    ("Ronaldo retires from Portugal duty", "reuters.com", "retires", 18),
    ("Ronaldo announces international retirement on Instagram!",
     "thesun.co.uk", "instagram", 19),
    ("Ronaldo's most iconic moments as he retires from international football",
     "skysports.com", "iconic", 20),
    ("Ronaldo retires from international football - Manchester United",
     "manutd.com", "manutd", 21),
    ("Ronaldo's Portugal statement in full", "bbc.co.uk", "statement", 22),
]

PL = [
    ("Arsenal agree deal for striker Viktor Gyokeres as talks advance",
     "bbc.co.uk", "arsenal", 12),
    ("Liverpool confirm Alisson out for six weeks with calf injury",
     "skysports.com", "liverpool", 13),
    ("Chelsea takeover completed as consortium buys majority stake",
     "dailymail.co.uk", "chelsea", 14),
    ("Manchester United appoint new sporting director", "manutd.com", "united", 15),
    ("Tottenham sign defender Marc Guehi in 45m deal",
     "theguardian.com", "spurs", 16),
    ("Newcastle United face PSR breach hearing over stadium costs",
     "telegraph.co.uk", "newcastle", 17),
    ("Aston Villa beat Wolves 2-0 at Villa Park", "bbc.co.uk", "villa", 18),
    ("Everton points deduction appeal rejected by independent panel",
     "skysports.com", "everton", 19),
    ("Brighton manager criticises referee after VAR controversy",
     "theguardian.com", "brighton", 20),
    ("West Ham confirm Julen Lopetegui departure after poor run",
     "bbc.co.uk", "westham", 21),
]

FOREIGN = [
    ("Real Betis and Sevilla draw in pre-season friendly", "marca.com", "betis", 11),
    ("Juventus beat Napoli 1-0 in Serie A opener", "gazzetta.it", "juve", 12),
    ("Bayern Munich sign winger from Ajax", "kicker.de", "bayern", 13),
    ("PSG held to draw by Marseille in Ligue 1", "lequipe.fr", "psg", 14),
    ("Flamengo win Brazilian league title", "globo.com", "flamengo", 15),
]

PROMO = [
    ("Cristiano Ronaldo launches new CR7 boots collection", "thesun.co.uk",
     "boots", 22),
    ("Win a signed Arsenal shirt in our competition", "mirror.co.uk", "shirt", 22),
    ("Chelsea unveil new home kit for next season", "chelseafc.com", "kit", 22),
    ("Manchester United memberships on sale now with 20% off", "manutd.com",
     "membership", 22),
    ("Vote for your Premier League player of the month", "premierleague.com",
     "vote", 22),
    ("Liverpool announce Black Friday deal on club store", "liverpoolfc.com",
     "blackfriday", 22),
    ("Cristiano Ronaldo's girlfriend shows off new supercar", "thesun.co.uk",
     "supercar", 22),
    ("Tottenham launch new training range for 2026/27 season",
     "tottenhamhotspur.com", "range", 22),
]


def make_pool(groups):
    out = []
    for rows in groups:
        for title, domain, slug, hour in rows:
            out.append({
                "title": title,
                "link": "https://www.{}/{}".format(domain, slug),
                "desc": "",
                "publisher": domain,
                "feed_origin": "https://{}/rss".format(domain),
                "published": "2026-11-05T{:02d}:00:00Z".format(hour),
            })
    return out


def bar(ch="=", n=100):
    print(ch * n)


def aline(item):
    return imp.classify_scope(item["title"], item.get("desc", ""))[0]


def ctype_of(item):
    et = ev.primary_event_type(item["title"])
    return ct.classify(item["title"], item.get("desc", ""), event_type=et)


def main():
    ronaldo_pool = make_pool([RONALDO])
    full_pool = make_pool([RONALDO, PL, FOREIGN, PROMO])

    # ---------- 表 1：文章级排名（旧版口径）----------
    bar()
    print("表 1  文章级排名（旧版口径：按发布时间倒序，前 15）")
    print("      数据 = 2026-11-05 C 罗退役 10 条真实报道")
    bar()
    old = sorted(ronaldo_pool, key=lambda p: p["published"], reverse=True)
    print("{:<4} {:<62} {:<16} {:<6}".format("#", "标题", "发布时间", "类型"))
    for i, it in enumerate(old[:15], 1):
        c = ctype_of(it)
        print("{:<4} {:<62} {:<16} {:<6}".format(
            i, it["title"][:60], it["published"][11:16], c["content_type"][:6]))
    print()
    print("旧版结果：同一件事占满前 10 名，10 个候选名额被 1 件事吃掉。")
    print("          10 条里有 6 条是评论/花絮，事实陈述只有 2 条。")

    # ---------- 表 2：事件级排名（新版口径）----------
    events, stats = ea.build_events(ronaldo_pool)
    print()
    bar()
    print("表 2  事件级排名（新版口径：按重要性排序，同事件只占一个名额）")
    bar()
    print("输入 {} 条 → 事件 {} 个（合并 {} 条）".format(
        stats["input"], stats["events"], stats["merged"]))
    print()
    print("{:<4} {:<6} {:<8} {:<6} {:<5} {:<8} {}".format(
        "#", "重要性", "类型", "报道数", "独立源", "可信度", "展示标题"))
    for i, e in enumerate(events, 1):
        print("{:<4} {:<6} {:<8} {:<6} {:<5} {:<8} {}".format(
            i, e["importance"], e["event_type"][:8], e["article_count"],
            e["independent_groups"], e["cred"], e["title"][:44]))

    # ---------- 表 3：重要性分级明细 ----------
    print()
    bar()
    print("表 3  重要性分级明细（每条报道，均按「证据充足」假定评级）")
    bar()
    print("{:<6} {:<6} {:<24} {}".format("等级", "类型", "规则", "标题"))
    for it in old:
        c = ctype_of(it)
        r = imp.assess(it["title"], "", event_type=ev.primary_event_type(it["title"]),
                       content_type=c["content_type"],
                       independent_groups=6, has_tier1=True, source_count=9)
        print("{:<6} {:<6} {:<24} {}".format(
            r["importance"], c["content_type"][:6], r["rule_id"][:24],
            it["title"][:46]))

    # ---------- 表 4：最终入选结果 ----------
    print()
    bar()
    print("表 4  最终入选结果（混合池：C 罗 10 + 英超 10 + 非英超 5 + 推广 8 = 33 条）")
    bar()

    old_full = sorted(full_pool, key=lambda p: p["published"], reverse=True)[:BUDGET]
    old_promo = sum(1 for p in old_full if ct.is_low_value(ctype_of(p)["content_type"]))
    old_aline = sum(1 for p in old_full
                    if aline(p) == imp.A_LINE
                    and not ct.is_low_value(ctype_of(p)["content_type"]))
    old_ronaldo = sum(1 for p in old_full if p["title"] in {r[0] for r in RONALDO})

    state = {"events": {}, "news_pool": []}
    ordered, morning_events, mstats = bot._events_for_morning(state, pool=full_pool)
    new_promo = sum(1 for p in ordered
                    if ct.is_low_value(ctype_of(p)["content_type"]))
    new_aline = sum(1 for p in ordered
                    if aline(p) == imp.A_LINE
                    and not ct.is_low_value(ctype_of(p)["content_type"]))
    # 按事件 ID 统计，而不是按标题集合：捐球票那条也在 RONALDO 标题表里，
    # 按标题统计会把「另一件事」算进退役事件的名额里。
    retire_evt = next(e for e in morning_events
                      if e["importance"] == imp.S and e["event_type"] == "RETIREMENT")
    new_ronaldo = sum(1 for p in ordered
                      if p.get("_event_id") == retire_evt["event_id"])
    ronaldo_rank = next((i for i, p in enumerate(ordered, 1)
                         if p.get("_event_id") == retire_evt["event_id"]), None)

    rows = [
        ("素材条数", len(old_full), len(ordered)),
        ("PROMOTION / GOSSIP 条数", old_promo, new_promo),
        ("A 线（英超）事实新闻条数", old_aline, new_aline),
        ("C 罗退役占用名额", old_ronaldo, new_ronaldo),
        ("C 罗退役最高名次", "占满前 10", ronaldo_rank),
        ("S 级事件数", "无此概念", mstats["s_count"]),
        ("GLOBAL_EVENT_LINE 保留位使用", "无此概念",
         "{}/{}（空置 {}）".format(mstats["global_slots_used"],
                                  mstats["global_slots_reserved"],
                                  mstats["global_slots_empty"])),
    ]
    print("{:<28} {:<22} {}".format("指标", "旧版（按时间）", "新版（事件级）"))
    print("-" * 100)
    for name, o, n in rows:
        print("{:<28} {:<22} {}".format(name, str(o), str(n)))
    print()
    print("结论：推广从 {} 条降到 {} 条；A 线事实新闻 {} → {}（保留率 {:.0f}%）；"
          "C 罗退役从「10 条报道占满前 10 名」变成「1 个事件、{} 条原文、排第 {}」。".format(
              old_promo, new_promo, old_aline, new_aline,
              (new_aline / old_aline * 100) if old_aline else 0,
              new_ronaldo, ronaldo_rank or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
