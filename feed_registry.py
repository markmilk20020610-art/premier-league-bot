"""RSS 来源登记表（V2.0 §2.4 来源白名单）。

**为什么需要这张表**
F1 新闻之所以能进入足球推送流程，唯一入口是 `bot.py` 的 `RSS_FEEDS` 列表里
那一行 `https://www.skysports.com/rss/12040`。它被注释成「顶级权威主流媒体」，
与 BBC 的 `/sport/football/rss.xml` 并列，但它其实是 Sky Sports 的**全站新闻** feed
（频道标题 `SkySports | News`），同一批 20 条里混着足球、F1、高尔夫、飞镖、
橄榄球、拳击、赛马。

一张 URL 列表表达不了「这个源覆盖哪些体育项目」。所以改成登记表：每个源必须显式
声明 `sport_category` 与 `source_scope`，采集层据此决定要不要逐条分类。

**source_scope 的三种取值**
  · `football_only` —— 已核实只出足球内容的源。条目可直接入池，仍会在推送前复核。
  · `mixed_sport`   —— 综合体育源。**每一条都必须过 `sport_scope.classify_sport()`**，
                        判不出是足球的一律丢弃。综合体育源永远不获得足球白名单资格。
  · `unknown`       —— 无法确认覆盖范围。**不得进入生产采集**。

**verification_status（V2.0 §2.4 新增）的五个取值**
  · `verified`           —— 原生 feed 直连实测可用：HTTP 200 且能解析出条目。
  · `degraded_fallback`  —— 原生 feed 不可用/不是 RSS，实际靠站点检索降级取到条目。
                            数据可用，但来源形态与登记不符，属已知缺陷。
  · `degraded_empty`     —— 降级通道可访问，但解析出 0 条。**不能算可用**。
  · `blocked`            —— 实测 403 / 风控，且无可用降级。**不得 enabled**。
  · `unverified`         —— 从未实测。**默认值，且不得 enabled**。

这个字段存在的意义：以前「加了源就等于源能用」，没有任何人记录过「你最后一次
确认它能用是什么时候、用什么方式确认的」。现在每个源必须带 `verified_at`
与 `verification_evidence`，`validate_registry()` 会拒绝「开着但没实测过」的源。

新增来源时必须填全十个字段，否则 `validate_registry()` 会报错，
而不是像以前那样「加一行 URL 就上线」。
"""

import re

import sport_scope as ss

FOOTBALL_ONLY = "football_only"
MIXED_SPORT = "mixed_sport"
UNKNOWN_SCOPE = "unknown"

VALID_SCOPES = frozenset({FOOTBALL_ONLY, MIXED_SPORT, UNKNOWN_SCOPE})

# ---- verification_status ----
VERIFIED = "verified"
DEGRADED_FALLBACK = "degraded_fallback"
DEGRADED_EMPTY = "degraded_empty"
BLOCKED = "blocked"
UNVERIFIED = "unverified"

VALID_VERIFICATION = frozenset({
    VERIFIED, DEGRADED_FALLBACK, DEGRADED_EMPTY, BLOCKED, UNVERIFIED,
})

# 「可用」的判定：只有这两种状态允许进入生产采集。
# degraded_empty 明确不算——它每次都能连上，但每次都拿不到东西，
# 把它算成可用等于让一个哑源长期占据采集配额。
USABLE_VERIFICATION = frozenset({VERIFIED, DEGRADED_FALLBACK})

REQUIRED_FIELDS = (
    "source_id", "feed_url", "publisher", "domain",
    "sport_category", "source_scope", "enabled",
    "verification_status", "verified_at", "verification_evidence",
)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 本次全量实测的时间（UTC）。所有 verified_at 都指向它，不是拍脑袋写的日期。
MEASURED_AT = "2026-10-31"


def _feed(source_id, feed_url, publisher, domain, sport_category,
          source_scope, verification_status=UNVERIFIED, verified_at="",
          evidence="", enabled=True, note=""):
    return {
        "source_id": source_id,
        "feed_url": feed_url,
        "publisher": publisher,
        "domain": domain,
        "sport_category": sport_category,
        "source_scope": source_scope,
        "enabled": enabled,
        "verification_status": verification_status,
        "verified_at": verified_at,
        "verification_evidence": evidence,
        "note": note,
    }


# ==================== 登记表 ====================
# 顺序与 bot.py 原来的 RSS_FEEDS 保持一致，telemetry 里的源顺序不变。
# 每一条的 verification_* 都来自 2026-10-31T12:45Z 的全量实跑，
# 状态串格式与 bot.fetch_rss_sources() 的 source_status 一一对应：
#   直连成功 → "200"
#   降级成功 → "FALLBACK_OK(200, N items)"
#   降级为空 → "FALLBACK_EMPTY(0 items)"
#   被拒绝   → "403"
FEEDS = (
    # ---- 1. 官方与裁判仲裁层 ----
    _feed("thefa", "https://www.thefa.com/rss", "The FA", "thefa.com",
          ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="无原生 RSS，走站点检索降级"),
    _feed("premierleague", "https://www.premierleague.com/rss/news",
          "Premier League", "premierleague.com",
          ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="端点已下线，走站点检索降级"),

    # ---- 2. 顶级权威主流媒体 ----
    _feed("bbc_football", "https://feeds.bbci.co.uk/sport/football/rss.xml",
          "BBC Sport", "bbc.co.uk",
          ss.FOOTBALL, FOOTBALL_ONLY,
          VERIFIED, MEASURED_AT, "200",
          note="BBC 足球频道专属 feed"),
    # ⚠️ 这一条是 F1 事故的入口。实测 channel title = "SkySports | News"，
    # 20 条里约一半不是足球（F1 / 高尔夫 / 飞镖 / 橄榄球 / 拳击 / 赛马）。
    # 保留它是因为 Sky 的独家视频与实时博客有价值，但必须标 mixed_sport，
    # 由采集层逐条分类，绝不因为它挂在 skysports.com 下就当足球。
    _feed("skysports_news", "https://www.skysports.com/rss/12040",
          "Sky Sports", "skysports.com",
          ss.OTHER, MIXED_SPORT,
          VERIFIED, MEASURED_AT, "200",
          note="全站综合体育 feed（channel: SkySports | News），必须逐条分类"),
    # The Athletic 的 /feed/ 同样是全站 feed，且 403 Cloudflare 无降级方案。
    # 2026-10-31 实测 403，按 blocked 处理并关闭采集：每轮都白扔一个请求，
    # 还会把 source_health 拖成 failed，掩盖真正的问题源。
    # 若哪天它恢复了，先重测、改回 VERIFIED 再 enabled。
    _feed("theathletic", "https://theathletic.com/feed/", "The Athletic",
          "theathletic.com", ss.OTHER, MIXED_SPORT,
          BLOCKED, MEASURED_AT, "403",
          enabled=False,
          note="全站 feed，403 Cloudflare 风控，无可用降级（已关闭采集）"),

    # ---- 3. 爆料、八卦与财务舆论场 ----
    _feed("dailymail_football",
          "https://www.dailymail.com/sport/football/index.rss",
          "Daily Mail", "dailymail.com", ss.FOOTBALL, FOOTBALL_ONLY,
          VERIFIED, MEASURED_AT, "200",
          note="足球频道专属；dailymail.co.uk 已 301 到 .com"),
    _feed("thesun_football", "https://www.thesun.co.uk/sport/football/feed/",
          "The Sun", "thesun.co.uk", ss.FOOTBALL, FOOTBALL_ONLY,
          VERIFIED, MEASURED_AT, "200",
          note="注意是 .co.uk；thesun.com SSL 证书错误"),

    # ---- 4. 顶级名记与转会/跟队信源 ----
    _feed("twitter_ornstein", "https://rsshub.app/twitter/user/David_Ornstein",
          "David Ornstein", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 9 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_romano", "https://rsshub.app/twitter/user/FabrizioRomano",
          "Fabrizio Romano", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_mattlaw", "https://rsshub.app/twitter/user/Matt_Law_DT",
          "Matt Law", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_EMPTY, MEASURED_AT, "FALLBACK_EMPTY(0 items)",
          enabled=False,
          note="rsshub 降级可连但 0 条目，属哑源，已关闭（需换源或改采集方式）"),
    _feed("twitter_samlee", "https://rsshub.app/twitter/user/SamLee",
          "Sam Lee", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 2 items)",
          note="rsshub 公共实例，走站点检索降级"),

    # ---- 5. Big 6 官方及核心跟队直连 ----
    _feed("arsenal_site", "https://www.arsenal.com/rss/news", "Arsenal",
          "arsenal.com", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 9 items)",
          note="原生 503，走 site: 降级"),
    _feed("twitter_charleswatts", "https://rsshub.app/twitter/user/CharlesWatts",
          "Charles Watts", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 2 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_manutd", "https://rsshub.app/twitter/user/ManUtd", "Man Utd",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_mancity", "https://rsshub.app/twitter/user/ManCity", "Man City",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_arsenal", "https://rsshub.app/twitter/user/Arsenal", "Arsenal",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 9 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_lfc", "https://rsshub.app/twitter/user/LFC", "Liverpool",
          "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_chelsea", "https://rsshub.app/twitter/user/ChelseaFC",
          "Chelsea", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 10 items)",
          note="rsshub 公共实例，走站点检索降级"),
    _feed("twitter_spurs", "https://rsshub.app/twitter/user/SpursOfficial",
          "Tottenham", "rsshub.app", ss.FOOTBALL, FOOTBALL_ONLY,
          DEGRADED_FALLBACK, MEASURED_AT, "FALLBACK_OK(200, 6 items)",
          note="rsshub 公共实例，走站点检索降级"),
)


# ==================== 派生索引 ====================
_BY_URL = {f["feed_url"]: f for f in FEEDS}

# 采集层真正使用的列表。
#
# 不得进入生产采集的三类：
#   ① enabled=False（人工关闭）
#   ② source_scope=unknown（覆盖范围不明）
#   ③ verification_status 不属于 USABLE（未实测 / 空源 / 被封）
#
# 第三条是 V2.0 §2.4 的核心：**没有实测证据的源不许采集**。
# 这里刻意用「白名单」而不是「黑名单」——只有明确可用才放行，
# 以后若新增枚举值却忘了登记进 USABLE，默认结果是被拦下而不是被采信。
RSS_FEEDS = [
    f["feed_url"] for f in FEEDS
    if f["enabled"]
    and f["source_scope"] != UNKNOWN_SCOPE
    and f["verification_status"] in USABLE_VERIFICATION
]


def entry_for(feed_url):
    """按 feed 地址取登记项；未登记返回 None。"""
    return _BY_URL.get((feed_url or "").strip())


def scope_of(feed_url):
    """取采集范围。**未登记的源一律视为 unknown**（= 不得采集）。

    这是刻意的默认值：新增一个源却忘了登记，结果应该是「不采集」，
    而不是「当成足球源全量采集」。
    """
    e = entry_for(feed_url)
    return e["source_scope"] if e else UNKNOWN_SCOPE


def category_of(feed_url):
    e = entry_for(feed_url)
    return e["sport_category"] if e else ss.UNKNOWN


def verification_of(feed_url):
    """取实测状态。未登记 = unverified（从未测过）。"""
    e = entry_for(feed_url)
    return e["verification_status"] if e else UNVERIFIED


def is_usable(feed_url):
    """该源是否达到「可用」标准（有实测证据且不是空源/被封）。"""
    return verification_of(feed_url) in USABLE_VERIFICATION


def requires_item_check(feed_url):
    """该源是否必须逐条做领域分类。

    `football_only` 不强制（否则每条都要跑一遍正则，且官方源偶尔发错内容
    由推送前复核兜底）；`mixed_sport` 必须逐条分类；`unknown` 不采集。
    """
    return scope_of(feed_url) == MIXED_SPORT


def validate_registry():
    """自检登记表，返回问题列表（空 = 通过）。测试会直接调它。"""
    problems = []
    seen_ids, seen_urls = set(), set()
    for f in FEEDS:
        missing = [k for k in REQUIRED_FIELDS if k not in f]
        if missing:
            problems.append(f"{f.get('feed_url', '?')} 缺少字段 {missing}")
            continue
        if f["source_id"] in seen_ids:
            problems.append(f"source_id 重复：{f['source_id']}")
        if f["feed_url"] in seen_urls:
            problems.append(f"feed_url 重复：{f['feed_url']}")
        seen_ids.add(f["source_id"])
        seen_urls.add(f["feed_url"])
        if f["source_scope"] not in VALID_SCOPES:
            problems.append(f"{f['source_id']} 的 source_scope 非法：{f['source_scope']}")
        if f["sport_category"] not in ss.ALL_CATEGORIES:
            problems.append(f"{f['source_id']} 的 sport_category 非法：{f['sport_category']}")
        if f["source_scope"] == MIXED_SPORT and f["sport_category"] == ss.FOOTBALL:
            problems.append(
                f"{f['source_id']} 标为 mixed_sport 却声明 sport_category=Football——"
                f"综合体育源不得声称自己是足球源")
        if not isinstance(f["enabled"], bool):
            problems.append(f"{f['source_id']} 的 enabled 必须是布尔值")

        # ---- V2.0 §2.4：来源必须带可追溯的实测证据 ----
        vs = f["verification_status"]
        if vs not in VALID_VERIFICATION:
            problems.append(f"{f['source_id']} 的 verification_status 非法：{vs}")
            continue
        if vs != UNVERIFIED:
            if not _DATE_RE.match(str(f["verified_at"])):
                problems.append(
                    f"{f['source_id']} 声称 {vs} 但 verified_at 不是 YYYY-MM-DD："
                    f"{f['verified_at']!r}")
            if not str(f["verification_evidence"]).strip():
                problems.append(
                    f"{f['source_id']} 声称 {vs} 但没写 verification_evidence"
                    f"（必须留下当时的实测状态串）")
        if f["enabled"] and vs == UNVERIFIED:
            problems.append(
                f"{f['source_id']} 从未实测却 enabled=True——"
                f"未验证的源不得进入生产采集")
        if f["enabled"] and vs == BLOCKED:
            problems.append(
                f"{f['source_id']} 实测被封（blocked）却仍 enabled=True——"
                f"先重测确认恢复，再改回 verified 并开启")
        if f["enabled"] and vs == DEGRADED_EMPTY:
            problems.append(
                f"{f['source_id']} 实测 0 条目（degraded_empty）却仍 enabled=True——"
                f"哑源不得占用采集配额，先换源或改采集方式")
    return problems
