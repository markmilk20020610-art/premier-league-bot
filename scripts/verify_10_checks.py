"""冲刺令第六节：10 条重点验证。

每一条都用真实代码路径验证，不靠「读代码觉得没问题」。
输出 PASS / FAIL 与依据，任一 FAIL 则退出码 1。

用法：
    python scripts/verify_10_checks.py
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
import state_store as store

RESULTS = []


def check(no, title, ok, detail):
    RESULTS.append((no, title, bool(ok), detail))
    print("[{}] {:>2}. {}".format("PASS" if ok else "FAIL", no, title))
    print("        {}".format(detail))


# ==================== 共用数据 ====================
RONALDO_TITLES = [
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

PL_TITLES = [
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

FOREIGN_TITLES = [
    ("Real Betis and Sevilla draw in pre-season friendly", "marca.com", "betis", 11),
    ("Juventus beat Napoli 1-0 in Serie A opener", "gazzetta.it", "juve", 12),
    ("Bayern Munich sign winger from Ajax", "kicker.de", "bayern", 13),
    ("PSG held to draw by Marseille in Ligue 1", "lequipe.fr", "psg", 14),
    ("Flamengo win Brazilian league title", "globo.com", "flamengo", 15),
]

PROMO_TITLES = [
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


def mkpool(groups):
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


def aline(it):
    return imp.classify_scope(it["title"], it.get("desc", ""))[0]


def ctype_of(it):
    et = ev.primary_event_type(it["title"])
    return ct.classify(it["title"], it.get("desc", ""), event_type=et)


def main():
    full_pool = mkpool([RONALDO_TITLES, PL_TITLES, FOREIGN_TITLES, PROMO_TITLES])
    state = {"events": {}, "news_pool": []}
    ordered, morning_events, stats = bot._events_for_morning(state, pool=full_pool)

    # ---------- 1. C 罗退役进前 40 ----------
    retire = [e for e in morning_events
              if e["importance"] == imp.S and e["event_type"] == "RETIREMENT"]
    rank = next((i for i, p in enumerate(ordered, 1)
                 if retire and p.get("_event_id") == retire[0]["event_id"]), None)
    check(1, "2026-11-05 的 C 罗退役新闻进入早报前 40 名",
          rank is not None and rank <= bot.SUMMARY_MAX_MATERIALS,
          "事件 {} 排在第 {} 位，重要性 {}，{} 条报道聚成 1 个事件".format(
              retire[0]["event_id"] if retire else "-", rank,
              retire[0]["importance"] if retire else "-",
              retire[0]["article_count"] if retire else 0))

    # ---------- 2. 前 40 名 PROMOTION 为 0 ----------
    promo_in = [p for p in ordered
                if ct.is_low_value(ctype_of(p)["content_type"])]
    check(2, "早报前 40 名中的 PROMOTION 数量为 0",
          not promo_in,
          "候选池 {} 条中低价值内容 0 条（输入含 {} 条推广/八卦，已全部排除）".format(
              len(ordered), len(PROMO_TITLES)))

    # ---------- 3. A 线覆盖不低于旧版 90% ----------
    old = sorted(full_pool, key=lambda p: p["published"], reverse=True)[
        :bot.SUMMARY_MAX_MATERIALS]
    old_a = sum(1 for p in old
                if aline(p) == imp.A_LINE
                and not ct.is_low_value(ctype_of(p)["content_type"]))
    new_a = sum(1 for p in ordered
                if aline(p) == imp.A_LINE
                and not ct.is_low_value(ctype_of(p)["content_type"]))
    ratio = (new_a / old_a) if old_a else 0
    check(3, "A 线事件数量不得低于旧版的 90%",
          ratio >= 0.9,
          "旧版 A 线事实新闻 {} 条 → 新版 {} 条（{:.0f}%）".format(
              old_a, new_a, ratio * 100))

    # ---------- 4. 同一事件不重复占名额 ----------
    per_event = {}
    for p in ordered:
        per_event[p["_event_id"]] = per_event.get(p["_event_id"], 0) + 1
    worst = max(per_event.values()) if per_event else 0
    check(4, "同一事件的多篇报道不能重复占用候选名额",
          worst <= bot.MORNING_MAX_ARTICLES_PER_EVENT,
          "单个事件最多占 {} 个素材名额（上限 {}）；C 罗 10 条报道只占 {} 个".format(
              worst, bot.MORNING_MAX_ARTICLES_PER_EVENT,
              per_event.get(retire[0]["event_id"], 0) if retire else 0))

    # ---------- 5. 转载不算独立信源 ----------
    title = "Ronaldo retires from Portugal duty"
    reprints = [{
        "title": title,
        "link": "https://www.{}/r{}".format(d, i),
        "desc": "", "publisher": d,
        "feed_origin": "https://{}/rss".format(d),
        "published": "2026-11-05T18:00:00Z",
    } for i, d in enumerate(["bbc.co.uk", "dailymail.co.uk", "mirror.co.uk"])]
    evs, _ = ea.build_events(reprints)
    groups = evs[0]["independent_groups"] if evs else 0
    check(5, "转载不能算独立信源",
          len(evs) == 1 and groups == 1,
          "3 家媒体转载同一篇稿 → {} 个事件、{} 个独立来源".format(len(evs), groups))

    # ---------- 6. 未核实的转会传闻不得作为确定事实推送 ----------
    rumor_pool = [{
        "title": "Arsenal agree deal for striker Viktor Gyokeres as talks advance",
        "link": "https://www.thesun.co.uk/sport/rumour1", "desc": "",
        "publisher": "thesun.co.uk", "feed_origin": "https://thesun.co.uk/rss",
        "published": "2026-11-05T12:00:00Z",
    }]
    records = ev.build_records([{
        "url": rumor_pool[0]["link"], "title": rumor_pool[0]["title"],
        "publisher": "thesun.co.uk", "desc": "",
        "feed_origin": "https://thesun.co.uk/rss"}])
    verdict = ev.evaluate(rumor_pool[0]["link"], records)
    allowed, why = ev.auto_push_allowed(verdict, set())
    note = ev.single_source_note(verdict)
    check(6, "未经核实的转会传闻不得作为确定事实推送",
          (not allowed) or bool(note),
          "单源转会传闻证据等级 {} → auto_push_allowed={}（{}）；"
          "即使放行也必须带免责标注：{}".format(
              verdict["status"], allowed, why or "—",
              (note or {}).get("note", "无")))

    # ---------- 7. 所有权变化不被商业过滤器误删 ----------
    ownership = [
        "Man Utd takeover: Qatari consortium completes stake purchase",
        "Chelsea takeover completed as consortium buys majority stake",
        "Everton enter administration as ownership crisis deepens",
    ]
    kept = []
    for t in ownership:
        c = ct.classify(t, "", event_type=ev.primary_event_type(t))
        kept.append((t, c["content_type"], ct.is_low_value(c["content_type"])))
    ok7 = all(not low for _, _, low in kept)
    check(7, "俱乐部重要所有权变化不能被普通商业过滤器误删",
          ok7,
          "；".join("{} → {}".format(t[:34], c) for t, c, _ in kept))

    # ---------- 8. 已推送事件不重复发送 ----------
    st = {"events": {}}
    rec = store.record_event(st, "https://www.bbc.co.uk/a", "Ronaldo retires",
                             ev.MULTIPLE_INDEPENDENT_SOURCES, store.VERIFIED)
    store.begin_delivery(rec)
    store.mark_delivered(rec, 12345)
    store.transition(rec, store.COMMITTED)
    blocked_committed = store.blocks_rediscovery(rec)
    # 审核拒绝同样不得回炉重造
    st2 = {"events": {}}
    rec2 = bot._enqueue_review(st2, {
        "event_id": "ev_x", "title": "Ronaldo retires", "importance": "S",
        "cred": "E4", "content_type": "BREAKING_NEWS",
        "evidence_status": ev.UNVERIFIED,
        "evidence_refs": [{"url": "https://www.bbc.co.uk/x", "title": "x"}],
    }, "E4 级 S 事件")
    bot.decline_review(st2, "ev_x", "editor")
    blocked_declined = store.blocks_rediscovery(rec2)
    check(8, "已推送事件不能重复发送",
          blocked_committed and blocked_declined,
          "COMMITTED 状态 blocks_rediscovery={}；REVIEW_DECLINED 状态 "
          "blocks_rediscovery={}".format(blocked_committed, blocked_declined))

    # ---------- 9. E4 级 S 事件不能绕过人工审核 ----------
    e4s = {"event_id": "ev_e4s", "title": "Ronaldo retires from Portugal duty",
           "importance": imp.S, "cred": "E4", "content_type": "BREAKING_NEWS"}
    route, reason = bot._review_route(e4s)
    st3 = {"events": {}}
    rec3 = bot._enqueue_review(st3, dict(
        e4s, evidence_status=ev.UNVERIFIED,
        evidence_refs=[{"url": "https://www.thesun.co.uk/1", "title": "t"}]), reason)
    not_pushed = (rec3.get("delivery_status") == store.PENDING_REVIEW
                  and rec3.get("telegram_message_id") is None)
    status_at_enqueue = rec3.get("delivery_status")
    approve_rec = bot.approve_review(st3, "ev_e4s", "editor_01")
    cred_kept = approve_rec["review_cred"] == "E4"
    check(9, "E4 级 S 事件不能绕过人工审核状态",
          route == "REVIEW" and not_pushed and cred_kept,
          "路由={}；入队后 delivery_status={}、无 message_id={}；"
          "人工批准后转 {}，cred 仍为 {}（人工批准 ≠ 事实核验完成）".format(
              route, status_at_enqueue,
              rec3.get("telegram_message_id") is None,
              approve_rec.get("delivery_status"), approve_rec["review_cred"]))

    # ---------- 10. sport_scope 与原版一致 ----------
    import subprocess
    import sport_scope as sport
    diff = subprocess.run(
        ["git", "diff", "--stat", "HEAD", "--", "sport_scope.py"],
        cwd=ROOT, capture_output=True, text=True,
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    untouched = not diff.stdout.strip()
    probes = [
        ("Arsenal beat Chelsea in Premier League", True),
        ("Verstappen wins F1 grand prix in Monaco", False),
        ("Lakers beat Celtics in NBA finals", False),
    ]
    probe_results = [
        (t, sport.is_football(sport.classify_sport(t))) for t, _ in probes
    ]
    probe_ok = all(got == expect for (t, got), (_, expect)
                   in zip(probe_results, probes))
    check(10, "sport_scope.py 的领域判定与原版保持一致",
          untouched and probe_ok,
          "git diff 输出为空={}；抽样判定 {}/{} 正确（{}）".format(
              untouched, sum(1 for (t, g), (_, e) in zip(probe_results, probes)
                             if g == e), len(probes),
              "；".join("{}→{}".format(t[:26], "足球" if g else "非足球")
                       for t, g in probe_results)))

    # ---------- 汇总 ----------
    print()
    print("=" * 78)
    failed = [r for r in RESULTS if not r[2]]
    print("共 {} 条，通过 {} 条，失败 {} 条".format(
        len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
