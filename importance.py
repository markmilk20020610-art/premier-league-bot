"""重要性分级（V1.0 核心修复 · 任务 2）。

解决的问题
----------
系统此前只有「真假」判断（evidence.py 的 E1–E4），没有「值不值得」判断。
于是排序只能看发布时间，一条 10 小时前的商品推广排在 12 小时前的球星退役
前面。本模块补上 S/A/B/C 四级。

四级定义
--------
S  全球重大足球事件      —— 跨越联赛与国界的单一事件（巨星国家队退役、
                            足球人物去世、跨国假球案、球场安全事故、
                            世界足球治理机构级决定）
A  重要英超及顶级足球事件 —— 直接影响英超竞技或治理的事实（关键伤情、
                            红牌与申诉、换帅、重大转会、俱乐部所有权变更）
B  值得关注的专业资讯     —— 常规转会、续约、赛前赛后、次级联赛动态
C  普通资讯或低价值内容   —— 商品推广、八卦、无关紧要的流水账

核心原则（写进代码，不是口号）
------------------------------
1. **球员姓名不直接决定重要性。**
   本模块**没有**任何球员姓名表。C 罗国家队退役之所以是 S，是因为
   「事件类型 = 职业生涯终止」+「国家队语境」+「多来源独立印证」三个
   信号同时成立，换任何一名同等证据强度的球员都成立；
   反过来，C 罗的新球鞋广告仍然是 C，因为内容类型是 PROMOTION。
2. **重要性 ≠ 可信度。** 一条 S 级事件可以是 E4（单一低可信来源），
   此时它进人工审核队列，而不是被降级成 A。
3. **未知默认 B，不是 C。** C 会被排序降权，把「没判出来」的新闻打成 C
   等于静默删除。判不出来就老实给 B。
4. **确定性规则，零 AI 调用。** 全部是词表与阈值，可回溯、可复现、不花钱。
"""

import re

S, A, B, C = "S", "A", "B", "C"
LEVELS = (S, A, B, C)
_RANK = {S: 4, A: 3, B: 2, C: 1}


def rank(level):
    """等级→数值，用于排序比较。未知等级按 B 处理（保守）。"""
    return _RANK.get(level, 2)


# ==================== 业务线 ====================
A_LINE = "A_LINE"
GLOBAL_EVENT_LINE = "GLOBAL_EVENT_LINE"


# ==================== 词表 ====================
# 英超 20 队 + 常见写法。刻意包含 manchester / man utd 这类写法：
# evidence.CLUB_TOKENS 为了「同一主体」比对刻意排除了 united/city，
# 但重要性判定需要**认出**这是英超俱乐部，两者的取舍方向不同。
PL_CLUBS = (
    "arsenal", "aston villa", "villa", "bournemouth", "brentford", "brighton",
    "burnley", "chelsea", "crystal palace", "palace", "everton", "fulham",
    "leeds", "liverpool", "manchester city", "man city", "manchester united",
    "man utd", "man united", "newcastle", "nottingham forest", "forest",
    "sunderland", "tottenham", "spurs", "west ham", "wolves", "wolverhampton",
    "ipswich", "leicester", "southampton", "sheffield united", "luton",
    "阿森纳", "切尔西", "利物浦", "曼联", "曼城", "热刺", "埃弗顿", "纽卡斯尔",
    "阿斯顿维拉", "西汉姆", "布莱顿", "水晶宫", "富勒姆", "狼队",
)

# 非英超的顶级俱乐部：只用于「全球事件」的体量判断，不用于重要性加分本身
GLOBAL_TOP_CLUBS = (
    "real madrid", "barcelona", "atletico madrid", "bayern", "bayern munich",
    "borussia dortmund", "dortmund", "juventus", "inter", "internazionale",
    "ac milan", "milan", "napoli", "roma", "psg", "paris saint-germain",
    "marseille", "ajax", "psv", "benfica", "porto", "sporting",
    "flamengo", "river plate", "boca juniors", "al hilal", "al nassr",
    "皇家马德里", "巴塞罗那", "拜仁", "尤文图斯", "国际米兰", "ac米兰", "巴黎圣日耳曼",
)

# 顶级赛事
TOP_COMPETITIONS = (
    "world cup", "euros", "european championship", "copa america",
    "africa cup of nations", "afcon", "asian cup", "nations league",
    "champions league", "europa league", "conference league",
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "eredivisie", "saudi pro league", "mls", "club world cup",
    "世界杯", "欧洲杯", "欧冠", "英超", "西甲", "意甲", "德甲", "法甲",
)

# 国家队语境
# 含 "duty"：英媒写国家队征召几乎只用 "international duty / national duty /
# Portugal duty"，不写 "national team"。2026-11-05 的真实标题就是
# "Ronaldo retires from Portugal duty"——只认 "national team" 会漏掉它，
# 从而把一条 S 级退役事件压成 A。足球语境下 "duty" 无歧义。
NATIONAL_TEAM_SIGNALS = (
    "international", "national team", "his country", "her country",
    "for the country", "world cup", "euros", "euro 20", "qualifier",
    "friendly", "caps", "capped", "national side", "national duty",
    "duty", "call-up", "called up", "squad call",
    "国家队", "国脚", "国家队生涯", "为国出战",
)

# 「职业生涯量级」的描述词。刻意用描述词而不是人名：
# 用描述词才能泛化到任何球员，用人名只会变成一张永远不全的名单。
CAREER_MAGNITUDE = (
    "legend", "all-time", "record", "greatest", "icon", "farewell",
    "swansong", "testimonial", "era ends", "end of an era", "tribute",
    "captain", "ballon d'or", "world cup winner", "most capped",
    "five-time", "five time", "history", "historic",
    "传奇", "纪录", "告别", "队长", "金球奖",
)

# 跨国/多俱乐部假球与腐败
MULTI_SCOPE = (
    "clubs", "dozens", "multiple", "several", "network", "syndicate",
    "league-wide", "country", "countries", "continent", "europe-wide",
    "多家", "多国", "数十",
)

# 安全事故
CASUALTY = (
    "injur", "died", "death", "dead", "crush", "stampede", "casualt",
    "hospital", "killed", "fatal", "collapse",
    "受伤", "死亡", "踩踏", "倒塌", "遇难",
)

# 世界足球治理机构
WORLD_BODY = ("fifa", "uefa", "confederation", "conmebol", "caf", "afc",
              "concacaf", "ofc", "国际足联", "欧足联")

# 事件类型分组（与 evidence.EVENT_KEYWORDS 的类型名对齐）
CAREER_END_TYPES = frozenset({"RETIREMENT", "PLAYER_CAREER", "NATIONAL_TEAM"})
GOVERNANCE_IMPACT_TYPES = frozenset({"CLUB_OWNERSHIP", "GOVERNANCE", "APPOINTMENT"})
COMPETITIVE_IMPACT_TYPES = frozenset({"INJURY", "DISCIPLINE", "MATCH", "TRANSFER", "CONTRACT"})
SAFETY_TYPES = frozenset({"SAFETY_WELFARE", "DEATH", "ILLNESS"})
INTEGRITY_TYPES = frozenset({"MATCH_FIXING", "LEGAL"})


_WORD_PATTERNS = {}


def _hit(needles, text):
    """在文本中匹配词表。拉丁词用词边界，中文用子串。

    必须用词边界：`villa`（阿斯顿维拉的常见简称）是 `sevilla` 的子串，
    纯子串匹配会把「Real Betis 与 Sevilla 战平」判成英超事件，
    进而给出 A 级重要性——这是实测中真实出现过的误判。
    """
    found = []
    for n in needles:
        if re.search(r"[\u4e00-\u9fff]", n):
            if n in text:
                found.append(n)
            continue
        pat = _WORD_PATTERNS.get(n)
        if pat is None:
            pat = re.compile(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")
            _WORD_PATTERNS[n] = pat
        if pat.search(text):
            found.append(n)
    return found


def _rx_hit(pattern, text):
    return bool(re.search(pattern, text))


def classify_scope(title, desc="", event_type="", primary_column=None):
    """判断属于哪条业务线。

    A_LINE            —— 英超相关（有英超俱乐部或英超赛事语境）
    GLOBAL_EVENT_LINE —— 非英超的全球足球事件

    注意：这是**业务线**，与 importance 的 S/A/B/C 是两个正交维度。
    命名上刻意不叫「A 线 / B 线」，就是为了避免与 A 级 / B 级混淆。
    """
    text = ((title or "") + " " + (desc or "")).lower()
    pl_hit = _hit(PL_CLUBS, text)
    if pl_hit:
        return A_LINE, [f"命中英超俱乐部：{', '.join(sorted(set(pl_hit))[:3])}"]
    if primary_column:
        return A_LINE, [f"已归属 A 线栏目 {primary_column}"]
    if "premier league" in text or "英超" in text:
        return A_LINE, ["命中英超赛事语境"]
    return GLOBAL_EVENT_LINE, ["未见英超主体，归入 GLOBAL_EVENT_LINE"]


def assess(title, desc="", event_type="", content_type="", scope="",
           independent_groups=0, has_tier1=False, has_wire=False,
           source_count=0):
    """判定重要性等级。

    参数
    ----
    title, desc        : 文本
    event_type         : evidence.primary_event_type(title) 的结果
    content_type       : content_type.classify(...) 的结果
    scope              : A_LINE / GLOBAL_EVENT_LINE（不传则内部推断）
    independent_groups : 独立来源组数量（evidence 的独立信源聚类结果）
    has_tier1          : 是否有 TIER1 域名来源
    has_wire           : 是否有通讯社原始稿
    source_count       : 报道条数

    返回
    ----
    dict: importance / rule_id / reasons / signals / confidence
    """
    text = ((title or "") + " " + (desc or "")).lower()
    et = (event_type or "").upper().strip()
    ct = (content_type or "").upper().strip()

    if not scope:
        scope, _ = classify_scope(title, desc, et)

    signals = {
        "event_type": et,
        "content_type": ct,
        "scope": scope,
        "independent_groups": int(independent_groups or 0),
        "has_tier1": bool(has_tier1),
        "has_wire": bool(has_wire),
        "source_count": int(source_count or 0),
        "pl_club": bool(_hit(PL_CLUBS, text)),
        "top_competition": bool(_hit(TOP_COMPETITIONS, text)),
        "national_team": bool(_hit(NATIONAL_TEAM_SIGNALS, text)),
        "career_magnitude": bool(_hit(CAREER_MAGNITUDE, text)),
        "world_body": bool(_hit(WORLD_BODY, text)),
        "multi_scope": bool(_hit(MULTI_SCOPE, text)),
        "casualty": bool(_hit(CASUALTY, text)),
    }

    # 「证据广度」：独立来源组是核心指标。同一家通讯社的 10 篇转载算 1 组，
    # 因此这个数字不会因为「媒体多」而虚高。
    reach = signals["independent_groups"]
    reach_strong = reach >= 3 or (signals["has_tier1"] and reach >= 2)
    reach_any = reach >= 2 or signals["has_tier1"] or signals["has_wire"]

    def out(level, rule_id, reasons, conf):
        return {
            "importance": level,
            "rule_id": rule_id,
            "reasons": reasons,
            "signals": signals,
            "confidence": round(conf, 2),
        }

    # ---------- 第 0 层：低价值内容的天花板 ----------
    # 这一条是「姓名不决定重要性」的硬保证：
    # 无论标题里出现多大牌的球星，只要它是商品推广/八卦，就只能是 C。
    if ct == "PROMOTION":
        return out(C, "CEILING_PROMOTION",
                   ["内容类型为 PROMOTION（商品推广），重要性封顶 C；"
                    "球星姓名不改变该判定"], 0.95)
    if ct == "GOSSIP":
        return out(C, "CEILING_GOSSIP",
                   ["内容类型为 GOSSIP（八卦），重要性封顶 C"], 0.9)

    # ---------- 第 1 层：S 级（全球重大） ----------
    if et in INTEGRITY_TYPES and signals["multi_scope"]:
        return out(S, "S_MATCH_FIXING_MULTI",
                   [f"事件类型 {et} + 跨国/多俱乐部语境（integrity 事件具备全球影响）"],
                   0.85)

    # 公共安全事件（踩踏/坍塌/群体伤亡）本身就够 S 级，不依赖证据广度：
    # 等三五个独立来源到齐再判 S，早报早就发完了。
    if et == "SAFETY_WELFARE" and signals["casualty"]:
        return out(S, "S_SAFETY_DISASTER",
                   [f"事件类型 {et} + 伤亡信号，属公共安全级事件（不看来源数量）"],
                   0.9)

    # 足球人物去世：顶级语境（世界机构 / 英超俱乐部 / 强证据广度）判 S
    if et == "DEATH" and (signals["world_body"] or signals["pl_club"] or reach_strong):
        return out(S, "S_FOOTBALL_DEATH",
                   [f"事件类型 DEATH + "
                    f"{'世界机构语境' if signals['world_body'] else ('英超语境' if signals['pl_club'] else '强证据广度')}"],
                   0.85)

    if et in SAFETY_TYPES and signals["casualty"] and reach_any:
        return out(S, "S_SAFETY_CASUALTY",
                   [f"事件类型 {et} + 伤亡信号 + 独立来源 {reach} 组，属公共安全级事件"],
                   0.85)

    if et in CAREER_END_TYPES and signals["national_team"] and reach_strong:
        return out(S, "S_CAREER_END_NATIONAL",
                   [f"事件类型 {et}（职业生涯终止）+ 国家队语境 + "
                    f"独立来源 {reach} 组（强证据广度）→ 全球级职业生涯事件"],
                   0.85)

    if et in GOVERNANCE_IMPACT_TYPES and signals["world_body"]:
        return out(S, "S_WORLD_GOVERNANCE",
                   [f"事件类型 {et} + 世界足球治理机构（FIFA/UEFA/洲际足联）语境"],
                   0.8)

    if et in SAFETY_TYPES and signals["world_body"] and reach_any:
        return out(S, "S_WORLD_BODY_SAFETY",
                   [f"事件类型 {et} + 世界治理机构 + 独立来源 {reach} 组"], 0.8)

    # ---------- 第 2 层：A 级（重要英超 / 顶级足球） ----------
    if scope == A_LINE and et in COMPETITIVE_IMPACT_TYPES:
        return out(A, "A_PL_COMPETITIVE",
                   [f"英超相关 + 事件类型 {et} 直接影响竞技层面"], 0.8)

    if scope == A_LINE and et in GOVERNANCE_IMPACT_TYPES:
        return out(A, "A_PL_GOVERNANCE",
                   [f"英超相关 + 事件类型 {et} 属俱乐部治理层面"], 0.8)

    if scope == A_LINE and ct in ("CLUB_GOVERNANCE", "FINANCIAL_NEWS", "DISCIPLINARY_NEWS"):
        return out(A, "A_PL_GOVERNANCE_CONTENT",
                   [f"英超相关 + 内容类型 {ct}（治理/财务/纪律）不得因商业词被降级"],
                   0.75)

    if scope == GLOBAL_EVENT_LINE and et in CAREER_END_TYPES and signals["national_team"]:
        return out(A, "A_GLOBAL_CAREER_END",
                   [f"非英超的职业生涯终止事件，但证据广度不足（独立来源 {reach} 组），"
                    f"判 A 而非 S"], 0.7)

    if scope == GLOBAL_EVENT_LINE and et in GOVERNANCE_IMPACT_TYPES and \
            (_hit(GLOBAL_TOP_CLUBS, text) or signals["top_competition"]):
        return out(A, "A_GLOBAL_TOP_CLUB_GOVERNANCE",
                   [f"顶级俱乐部/顶级赛事 + 事件类型 {et}"], 0.7)

    if scope == GLOBAL_EVENT_LINE and et in SAFETY_TYPES and reach_any:
        return out(A, "A_GLOBAL_SAFETY",
                   [f"事件类型 {et} + 独立来源 {reach} 组，但未达 S 级广度"], 0.7)

    # ---------- 第 3 层：C 级（明确的流水账） ----------
    # 必须排在 `if et: B_TYPED_NEWS` **之前**。
    # 曾经排在后面，结果 `et` 非空就一路被 B_TYPED_NEWS 吃掉，
    # 这条 C 规则成了永远走不到的死代码——「贝蒂斯与塞维利亚季前赛打平」
    # 因此被判成 B，堂而皇之进入候选池。层内顺序错误比规则缺失更难发现。
    if et == "MATCH" and scope != A_LINE and not signals["top_competition"]:
        return out(C, "C_ROUTINE_FOREIGN_MATCH",
                   ["非英超、非顶级赛事的常规赛果，判 C"], 0.6)

    # ---------- 第 4 层：B 级（值得关注的专业资讯） ----------
    if et:
        return out(B, "B_TYPED_NEWS",
                   [f"有明确事件类型 {et}，但未达 A/S 门槛（scope={scope}，"
                    f"独立来源 {reach} 组）"], 0.6)

    if ct in ("BREAKING_NEWS", "COMPETITIVE_NEWS", "TRANSFER_NEWS",
              "CLUB_GOVERNANCE", "FINANCIAL_NEWS", "DISCIPLINARY_NEWS"):
        return out(B, "B_TYPED_CONTENT",
                   [f"内容类型 {ct} 属新闻类，但事件类型未判定"], 0.5)

    # ---------- 兜底：B（保守） ----------
    # 绝不兜底成 C：C 会被排序降权，把「没判出来」的新闻打成 C 等于静默删除。
    return out(B, "FALLBACK_B",
               ["未能判定重要性，保守回落 B（不因未分类而降级为 C）"], 0.3)


def combine(levels):
    """事件级重要性 = 成员报道的最高等级。

    为什么取最高：一件事常由多条报道组成，例如「退役官宣」能判出 S/A，
    「社媒确认」只能判出 B。事件是决策单位，应按最强的那条判——
    否则一条弱报道就能把整件事拖低，等于用最差的证据决定最重要的事。
    """
    valid = [lv for lv in levels if lv in LEVELS]
    if not valid:
        return B
    return max(valid, key=rank)


# ==================== 送审准入用的「量级」 ====================
# 假定证据广度充足时的等级。只用于一个地方：判断一条 E4 事件要不要送人工审核。
_REVIEW_REACH = 3


def significance(title, desc="", event_type="", content_type="", scope=""):
    """假定证据广度充足时的等级——「这件事够不够大」。

    为什么不直接用 assess()：assess 把「独立来源数」当作 S 级的门槛之一，
    于是「刚发出、只有一家媒体报的巨星国家队退役」只能判 A。
    可**证据不足正是要送审的原因**——用证据量来决定要不要送审，逻辑是倒的：
    越没人报的事越不会送审，越容易被静默丢掉。

    严格限定用途：本函数只用于人工审核队列的准入判断。
    推送决策、排序、名额分配一律用 assess() 的真实等级，
    不得拿 significance() 冒充「这条已经是 S 级」。
    """
    r = assess(title, desc, event_type=event_type, content_type=content_type,
               scope=scope, independent_groups=_REVIEW_REACH, has_tier1=True,
               source_count=_REVIEW_REACH)
    return r["importance"]


def explain(result):
    """压成一行，便于日志引用。"""
    if not result:
        return "(无)"
    return "{}({:.2f}) {} | {}".format(
        result.get("importance"), result.get("confidence", 0.0),
        result.get("rule_id"), "；".join(result.get("reasons") or []),
    )


def is_s(level):
    return level == S


def is_high(level):
    """S 或 A：不允许被 C 级 / PROMOTION 挤占名额。"""
    return level in (S, A)
