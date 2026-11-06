"""内容类型分类（V1.0 核心修复 · 任务 1）。

解决的问题
----------
早报前 40 名里 25% 是「Spurs 新赛季球裤」「Chelsea 会员招募」「Liverpool
球衣投票」这类商品推广，把真正的重大新闻挤出了候选位。系统此前没有任何
「这条内容值不值得占名额」的判断，只看发布时间。

设计原则
--------
1. **顺序固定，不可颠倒**：
       结构性重要事件 → 必要豁免 → 商品推广 → 其他类型
   俱乐部所有权变更、财务危机、重大赞助变化里天然带商业词（deal / sponsor /
   stake / sale），如果先跑 PROMOTION 规则，这些真正重要的新闻会被当成广告删掉；
   反过来，商品页如果晚于「事件类型映射」判定，就会因为标题里的
   sack / manager 被定型成治理新闻（V1.0 P0-1 的真实事故）。
   两头都要防，所以豁免在推广之前、推广在类型映射之前。
2. **宁可漏判，不可误删**。判定失败一律回落到 OTHER；OTHER 不是「过滤」，
   而是「不因内容类型被排除」。删错一条重大新闻的代价，远大于多留一条广告。
3. **可解释**。每次判定都返回 rule_id 与命中词，便于回溯「为什么这条被降级」。
4. **确定性、零 AI 调用**。纯正则 + 词表，不增加任何模型成本。
5. **内容类型与来源可信度互相独立**。OFFICIAL_CONFIRMED 只说明「来源可信」，
   不改变「这条值不值得占名额」——官方商店的商品页依然是商品页。

对外只暴露两个函数：
    classify(title, desc="", event_type="") -> dict
    is_low_value(content_type) -> bool
"""

import re

# ==================== 内容类型枚举 ====================

BREAKING_NEWS = "BREAKING_NEWS"
COMPETITIVE_NEWS = "COMPETITIVE_NEWS"
CLUB_GOVERNANCE = "CLUB_GOVERNANCE"
FINANCIAL_NEWS = "FINANCIAL_NEWS"
DISCIPLINARY_NEWS = "DISCIPLINARY_NEWS"
TRANSFER_NEWS = "TRANSFER_NEWS"
PROMOTION = "PROMOTION"
GOSSIP = "GOSSIP"
OTHER = "OTHER"

CONTENT_TYPES = (
    BREAKING_NEWS, COMPETITIVE_NEWS, CLUB_GOVERNANCE, FINANCIAL_NEWS,
    DISCIPLINARY_NEWS, TRANSFER_NEWS, PROMOTION, GOSSIP, OTHER,
)

# 低价值内容：允许进早报，但**不得挤占** S/A 级事件的名额。
LOW_VALUE_TYPES = frozenset({PROMOTION, GOSSIP})

# 商业/交易类，但属于「重要新闻」的类型：任何过滤规则都必须放行。
HIGH_VALUE_COMMERCIAL = frozenset({CLUB_GOVERNANCE, FINANCIAL_NEWS})


# ==================== 第一层：豁免规则（必须先跑） ====================
# 每条规则 = (正则, 目标类型, 规则名)
# 命中即返回，不再往下判 PROMOTION。
_EXEMPT_RULES = (
    (
        re.compile(
            r"\b(owner|ownership|stake|stakes|takeover|take-over|acquisition|"
            r"consortium|buyout|buy-out|administration|insolvency|insolvent|"
            r"bankrupt|bankruptcy|receivership|liquidation|investor|investors|"
            r"shareholder|shareholders|majority\s+share|minority\s+share|"
            r"due\s+diligence|new\s+owner)\b",
            re.I,
        ),
        CLUB_GOVERNANCE,
        "EXEMPT_CLUB_OWNERSHIP",
    ),
    (
        re.compile(
            r"\b(sponsor|sponsors|sponsorship|shirt\s+sponsor|kit\s+sponsor|"
            r"tv\s+deal|broadcast\s+deal|media\s+deal|revenue|revenues|"
            r"financial|finances|psr|ffp|profit\s+and\s+sustainability|"
            r"accounts|annual\s+results|loss|losses|profit|profits|"
            r"wage\s+bill|wages|debt|debts|transfer\s+budget|prize\s+money)\b",
            re.I,
        ),
        FINANCIAL_NEWS,
        "EXEMPT_FINANCIAL",
    ),
    (
        re.compile(
            r"\b(ticket\s+price|ticket\s+prices|season\s+ticket\s+price|"
            r"season\s+ticket\s+prices|pricing|price\s+rise|price\s+freeze|"
            r"fan\s+protest|fan\s+group|supporter\s+group|supporters?\s+trust|"
            r"ticket\s+allocation)\b",
            re.I,
        ),
        CLUB_GOVERNANCE,
        "EXEMPT_FAN_AND_PRICING",
    ),
    # 中文豁免
    (
        re.compile(r"(收购|股权|所有权|控股|破产|托管|接管|清盘|注资|股东)"),
        CLUB_GOVERNANCE,
        "EXEMPT_CLUB_OWNERSHIP_ZH",
    ),
    (
        re.compile(r"(赞助|财报|营收|亏损|盈利|债务|工资帽|财政公平|转会预算)"),
        FINANCIAL_NEWS,
        "EXEMPT_FINANCIAL_ZH",
    ),
    (
        re.compile(r"(票价|球票价格|球迷抗议|球迷组织|票价上涨)"),
        CLUB_GOVERNANCE,
        "EXEMPT_FAN_AND_PRICING_ZH",
    ),
)


# ==================== 第二层：PROMOTION 强特征 ====================
# 只收「明确的商业推销」词。刻意不收 deal / offer 这类歧义词——
# 「Arsenal agree deal for striker」是转会，不是促销。
_PROMO_PATTERNS = (
    (re.compile(r"\b(club\s+shop|club\s+store|official\s+store|online\s+store|"
                r"megastore|club\s+website\s+shop)\b", re.I), 3),
    (re.compile(r"\b(merchandise|merch|apparel|training\s+wear|"
                r"training\s+range|training\s+kit|leisurewear)\b", re.I), 3),
    # 球衣/训练服发布本身就是商品推广，单靠这一条就够阈值（权重 = 阈值）。
    # 原为 3 分，结果「Chelsea unveil new home kit for next season」只有 3 分，
    # 差 1 分落进 OTHER，整条广告照常进早报——阈值型规则的漏洞总在临界点上。
    (re.compile(r"\b(new\s+kit|home\s+kit|away\s+kit|third\s+kit|kit\s+launch|"
                r"kit\s+revealed|shirts?\s+revealed|jersey\s+revealed)\b", re.I), 4),
    (re.compile(r"\b(jersey|jerseys|shirt|shirts|polo|shorts|socks|scarf|mug|"
                r"calendar|advent\s+calendar|poster|print|boots|boot|"
                r"collection|range|drop|collab|collaboration|capsule|"
                r"limited\s+edition|signed\s+shirt|signed\s+jersey)\b", re.I), 2),
    (re.compile(r"\b(player\s+of\s+the\s+month|goal\s+of\s+the\s+month|"
                r"moment\s+of\s+the\s+month|save\s+of\s+the\s+month|"
                r"fans?\s+vote|vote\s+now)\b", re.I), 2),
    (re.compile(r"\b(buy\s+now|shop\s+now|pre-?order|available\s+now|"
                r"on\s+sale|sale\s+now|discount|% off|\d+%\s+off|"
                r"black\s+friday|cyber\s+monday|deal\s+of\s+the\s+day)\b", re.I), 3),
    (re.compile(r"\b(membership|memberships|join\s+now|sign\s+up|"
                r"ballot|ballots|ballot\s+open|waiting\s+list)\b", re.I), 2),
    (re.compile(r"\b(vote|votes|voting|poll|polls|competition|competitions|"
                r"giveaway|give\s+away|win\s+a|enter\s+now|prize|prizes)\b", re.I), 2),
    (re.compile(r"\b(gift|gifts|christmas|festive|stocking\s+filler)\b", re.I), 2),
    (re.compile(r"(官方商店|官方商城|球衣|战袍|纪念品|周边|促销|折扣|"
                r"会员|抽奖|投票|预售|购买|限时|新品)"), 2),
)


# ---------- 商品页硬特征（V1.0 P0-1） ----------
# 为什么加权不够：商品页标题**短、无动词、商业词密度低**。
# 「Arsenal Kids Christmas Sack - Arsenal Direct」原本只命中 christmas（2 分），
# 够不到阈值 4，于是落到第 4 层按事件类型映射 —— 而 Sack 又被判成 APPOINTMENT，
# 最终成了 A 级俱乐部治理新闻。阈值型规则对这类标题天生失效：它不是"分不够"，
# 是"没有分可加"。所以这里改成**特征即结论**，不参与计分。
#
# 但不能只凭单个名词定罪（否则「Plaque unveiled to honour captain」这种
# 真新闻会被误杀），必须同时满足"商品名词 + 商品页形态"。
_MERCH_NOUN = re.compile(
    r"\b(christmas\s+sack|santa\s+hat|stocking\s+filler|advent\s+calendar|"
    r"decoration|decorations|ornament|ornaments|bauble|baubles|"
    r"plaque|plaques|mug|mugs|keyring|keyrings|varsity\s+jacket|"
    r"gift\s+set|bobble\s+hat)\b",
    re.I,
)
# 这些组合本身就是商品名，不需要再要商店语境
_INHERENT_MERCH = re.compile(
    r"\b(christmas\s+sack|santa\s+hat|stocking\s+filler|advent\s+calendar|"
    r"(christmas|festive)\s+decoration|varsity\s+jacket|gift\s+set)\b",
    re.I,
)
# 商店/购物语境
_STORE_CONTEXT = re.compile(
    r"\b(club\s+shop|club\s+store|official\s+store|online\s+store|"
    r"megastore|merchandise|merch)\b",
    re.I,
)
# 俱乐部官方商店的命名：Arsenal Direct / United Direct …
_CLUB_STORE_SUFFIX = re.compile(r"\b[A-Z][A-Za-z&\.]*\s+Direct\b")
# 商品列表页格式：「商品名 - 品牌」
_LISTING_SHAPE = re.compile(r"\s[-–—]\s*[A-Z]")


def _merch_page(title):
    """判断标题是不是商品页。返回命中的特征名，空串表示不是。

    判定顺序（从严到宽）：
      1. 俱乐部官方商店命名（Arsenal Direct）—— 出现即商品页
      2. 本身即商品名的组合（christmas sack / santa hat / varsity jacket）
      3. 商品名词 + 商店语境（plaque + official store）
      4. 商品名词 + 列表页格式（… - 品牌）
    只命中商品名词而没有上面任一形态的，**不判商品页**——
    「Plaque unveiled at stadium to honour former captain」是真新闻。
    """
    if _CLUB_STORE_SUFFIX.search(title):
        return "CLUB_STORE_SUFFIX"
    if not _MERCH_NOUN.search(title):
        return ""
    if _INHERENT_MERCH.search(title):
        return "INHERENT_MERCH"
    if _STORE_CONTEXT.search(title):
        return "STORE_CONTEXT"
    if _LISTING_SHAPE.search(title):
        return "LISTING_SHAPE"
    return ""


# 出现这些词时，即使命中推广词也不判 PROMOTION（说明是新闻而非推销）
# 刻意不含 sign/signed：英媒促销常用「win a signed shirt」，把它当新闻信号
# 会把球衣抽奖放行成 OTHER，等于没修。
_PROMO_VETO = re.compile(
    r"\b(charge|charged|ban|banned|injur|transfer|"
    r"appoint|sacked|resign|investigation|probe|arrest|dies|died|"
    r"hospital|statement|confirm|confirms|confirmed|reject|rejects|"
    r"appeal|tribunal|misconduct|owner|ownership|takeover|administration|"
    r"insolvency|bankrupt)\b",
    re.I,
)
_PROMO_VETO_ZH = re.compile(r"(转会|签约|伤|禁赛|红牌|解雇|调查|去世|住院|声明|确认|收购|破产)")

PROMOTION_THRESHOLD = 4


# ==================== 第三层：GOSSIP 特征 ====================
_GOSSIP_PATTERNS = (
    re.compile(r"\b(rumour|rumours|rumor|rumors|gossip|speculation|"
               r"transfer\s+gossip|paper\s+talk|what\s+the\s+papers\s+say)\b", re.I),
    re.compile(r"\b(wag|wags|wife|girlfriend|fiancee|holiday|vacation|"
               r"sunbathing|beach|night\s+out|party|partying|haircut|"
               r"tattoo|supercar|lifestyle|net\s+worth)\b", re.I),
    re.compile(r"\b(pundit|talkSPORT\s+host|claims?|sensational|"
               r"you\s+won'?t\s+believe|fans\s+react|twitter\s+reacts)\b", re.I),
    re.compile(r"(绯闻|八卦|花边|女友|妻子|度假|夜店|发型|纹身|豪车)"),
)


# ==================== 第四层：事件类型 → 内容类型 ====================
# 与 evidence.EVENT_KEYWORDS 的类型名对齐；未知类型不猜，回落 OTHER。
_EVENT_TYPE_TO_CONTENT = {
    "TRANSFER": TRANSFER_NEWS,
    "CONTRACT": TRANSFER_NEWS,
    "INJURY": COMPETITIVE_NEWS,
    "DISCIPLINE": DISCIPLINARY_NEWS,
    "MATCH": COMPETITIVE_NEWS,
    "APPOINTMENT": CLUB_GOVERNANCE,
    "DEATH": BREAKING_NEWS,
    "ILLNESS": BREAKING_NEWS,
    "LEGAL": BREAKING_NEWS,
    # V1.0 新增事件类型
    "RETIREMENT": BREAKING_NEWS,
    "PLAYER_CAREER": BREAKING_NEWS,
    "NATIONAL_TEAM": BREAKING_NEWS,
    "CLUB_OWNERSHIP": CLUB_GOVERNANCE,
    "GOVERNANCE": CLUB_GOVERNANCE,
    "MATCH_FIXING": BREAKING_NEWS,
    "SAFETY_WELFARE": BREAKING_NEWS,
    "SPONSORSHIP": FINANCIAL_NEWS,
}

_BREAKING_SIGNALS = re.compile(
    r"\b(breaking|official\s+statement|club\s+statement|confirmed|confirms|"
    r"announces|announced|announcement|just\s+in|developing)\b",
    re.I,
)
_BREAKING_SIGNALS_ZH = re.compile(r"(官方|宣布|声明|确认|突发|快讯)")

# 结构性类型：由事件类型直接决定内容类型，不参与广告判定。
# 这些事件在语义上不可能是「商品推广」，因此必须在 PROMOTION 之前定死。
_STRUCTURAL_TYPES = {
    "CLUB_OWNERSHIP": CLUB_GOVERNANCE,
    "GOVERNANCE": CLUB_GOVERNANCE,
    "SPONSORSHIP": FINANCIAL_NEWS,
    "DISCIPLINE": DISCIPLINARY_NEWS,
    "MATCH_FIXING": BREAKING_NEWS,
    "SAFETY_WELFARE": BREAKING_NEWS,
    "RETIREMENT": BREAKING_NEWS,
    "PLAYER_CAREER": BREAKING_NEWS,
    "NATIONAL_TEAM": BREAKING_NEWS,
    "DEATH": BREAKING_NEWS,
    "ILLNESS": BREAKING_NEWS,
    "LEGAL": BREAKING_NEWS,
}


def _first_hit(patterns, text):
    """返回 (命中总数, 命中词列表)。patterns 支持 (regex, weight) 或 regex。"""
    total = 0
    words = []
    for p in patterns:
        if isinstance(p, tuple):
            rx, weight = p
        else:
            rx, weight = p, 1
        for m in rx.finditer(text):
            total += weight
            words.append(m.group(0).lower())
    return total, words


def classify(title, desc="", event_type=""):
    """判定内容类型。

    参数
    ----
    title       : 标题（必填）
    desc        : 摘要，可为空。摘要只作辅助，权重低于标题。
    event_type  : evidence.primary_event_type(title) 的结果，可为空。
                  传进来可提高准确率，但不传也能工作（本模块不依赖 evidence）。

    返回
    ----
    dict:
        content_type : 9 类之一
        confidence   : 0.0~1.0
        rule_id      : 命中的规则名，便于回溯
        reasons      : 人类可读的判定理由（中文）
        matched      : 命中的词
        is_low_value : 是否低价值内容
    """
    title = (title or "").strip()
    desc = (desc or "").strip()
    head = title
    body = (title + " " + desc).strip()

    def _out(ctype, conf, rule_id, reasons, matched=None):
        return {
            "content_type": ctype,
            "confidence": round(conf, 2),
            "rule_id": rule_id,
            "reasons": reasons,
            "matched": matched or [],
            "is_low_value": ctype in LOW_VALUE_TYPES,
        }

    if not head:
        return _out(OTHER, 0.0, "EMPTY_INPUT", ["标题为空，回落 OTHER（不因类型被过滤）"])

    # ---- 第 0 层：结构性类型（这些事件不可能是广告，先定死）----
    # 必须排在豁免与 PROMOTION 之前。一条「俱乐部被收购」的稿子里必然出现
    # stake / deal / acquisition 这类商业词，先跑广告规则就会把真新闻杀掉。
    # 同理，纪律处罚、假球、安全事故、退役，都不可能是一条商品推广。
    et0 = (event_type or "").upper().strip()
    if et0 in _STRUCTURAL_TYPES:
        ctype = _STRUCTURAL_TYPES[et0]
        return _out(ctype, 0.85, "STRUCTURAL_{}".format(et0),
                    ["事件类型 {} 结构性映射为 {}，不参与广告判定".format(et0, ctype)])

    # ---- 第 1 层：豁免（必须先跑）----
    for rx, target, rule_id in _EXEMPT_RULES:
        m = rx.search(body)
        if m:
            return _out(
                target, 0.9, rule_id,
                [f"命中豁免规则 {rule_id}（关键词「{m.group(0)}」），"
                 f"商业词不构成广告判定，判为 {target}"],
                [m.group(0).lower()],
            )

    # ---- 第 2 层：PROMOTION ----
    # 2a. 商品页硬特征：命中即判 PROMOTION。
    #     不参与计分（商品页标题短、商业词密度低，永远凑不够阈值），
    #     也不受「新闻信号否决」约束（商品页偶尔带 confirm/available 这类词，
    #     但一条商品页不会因为出现 confirm 就变成新闻）。
    #     位置必须在第 4 层「按事件类型映射」之前——否则 Sack 一旦被判成
    #     APPOINTMENT，商品页就会被定型成 CLUB_GOVERNANCE，治理新闻的通道
    #     反而成了广告的通道。
    merch = _merch_page(head)
    if merch:
        return _out(
            PROMOTION, 0.95, "PROMOTION_MERCH_PAGE",
            [f"命中商品页特征 {merch}，判为商品推广；"
             f"商品页优先于事件类型映射，不参与治理新闻判定"],
            [merch.lower()],
        )

    # 2b. 加权计分（兜底：非标准格式的商品推广）
    score, words = _first_hit(_PROMO_PATTERNS, head)
    desc_score, desc_words = _first_hit(_PROMO_PATTERNS, desc)
    # 摘要里的推广词只按半权计入，避免描述里顺口提到「球衣」就被判广告
    score += desc_score // 2
    words += desc_words
    if score >= PROMOTION_THRESHOLD:
        if _PROMO_VETO.search(head) or _PROMO_VETO_ZH.search(head):
            return _out(
                OTHER, 0.4, "PROMO_VETOED_BY_NEWS_SIGNAL",
                [f"推广词得分 {score}，但标题含新闻信号词（转会/伤病/处罚/治理等），"
                 f"不判广告，回落 OTHER 以免误删重要新闻"],
                words[:8],
            )
        return _out(
            PROMOTION, min(0.95, 0.6 + score * 0.05), "PROMOTION_STRONG",
            [f"推广特征得分 {score} ≥ 阈值 {PROMOTION_THRESHOLD}，判为商品推广"],
            words[:8],
        )

    # ---- 第 3 层：GOSSIP ----
    g_score, g_words = _first_hit(_GOSSIP_PATTERNS, head)
    if g_score >= 1:
        return _out(
            GOSSIP, 0.7, "GOSSIP_SIGNAL",
            [f"命中八卦特征（{', '.join(g_words[:5])}），判为 GOSSIP"],
            g_words[:8],
        )

    # ---- 第 4 层：按事件类型映射 ----
    et = (event_type or "").upper().strip()
    if et in _EVENT_TYPE_TO_CONTENT:
        ctype = _EVENT_TYPE_TO_CONTENT[et]
        conf = 0.8
        reasons = [f"事件类型 {et} 映射为 {ctype}"]
        if ctype == BREAKING_NEWS and not (
            _BREAKING_SIGNALS.search(head) or _BREAKING_SIGNALS_ZH.search(head)
        ):
            # 没有「官方/宣布/确认」这类硬信号时降一档到 OTHER，
            # 但不降到 PROMOTION/GOSSIP——避免把普通新闻误判成低价值。
            conf = 0.55
            reasons.append("未见突发信号词，置信度下调，但不判低价值")
        return _out(ctype, conf, f"EVENT_TYPE_{et}", reasons)

    # ---- 第 5 层：突发信号兜底 ----
    if _BREAKING_SIGNALS.search(head) or _BREAKING_SIGNALS_ZH.search(head):
        return _out(BREAKING_NEWS, 0.5, "BREAKING_SIGNAL_ONLY",
                    ["仅命中突发信号词，无事件类型，判 BREAKING_NEWS"])

    # ---- 兜底：OTHER（保守默认，不参与任何过滤）----
    return _out(OTHER, 0.2, "FALLBACK_OTHER",
                ["未命中任何规则，保守回落 OTHER（不因内容类型被排除）"])


def is_low_value(content_type):
    """低价值内容：可进早报，但不得挤占 S/A 级名额。"""
    return content_type in LOW_VALUE_TYPES


def is_commercial_but_important(content_type):
    """带商业色彩但属于重要新闻的类型（所有权/财务），任何过滤器都必须放行。"""
    return content_type in HIGH_VALUE_COMMERCIAL


def explain(result):
    """把 classify 的结果压成一行，便于日志与报告引用。"""
    if not result:
        return "(无)"
    return "{}({:.2f}) {} | {}".format(
        result.get("content_type"), result.get("confidence", 0.0),
        result.get("rule_id"), "；".join(result.get("reasons") or []),
    )
