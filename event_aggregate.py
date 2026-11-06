"""事件聚合与证据累积（V1.0 核心修复 · 任务 4）。

解决的问题
----------
原系统把「报道」当决策单位：同一件事被 10 家媒体报道，就在候选池里占 10 个
名额，排序时互相踩踏，最后一条都没进早报。C 罗国家队退役实测就是这种情况。

本模块把 N 条报道聚成 1 个事件，一个事件带：一个 ID、多条原始报道、
多个证据引用、一个重要性、一个可信度、一个推送状态。

为什么不只靠 same_event()
------------------------
实测 `same_event()` 的 pairwise 召回只有 52.8%（45 对里漏判 17 对）。
但它误合并 0 对——也就是说它很保守，风险方向是「漏合」而不是「错合」。
所以本模块在它之上叠加四层判断，而**不是**放松它的阈值：

  1. 事件 ID 复用（同一 canonical URL）
  2. 事件指纹（标题归一化哈希，含转载去后缀）
  3. 实体 + 事件类型族 + 时间窗（复合规则，阈值来自实测而非拍脑袋）
  4. 硬冲突否决（人物/主体/关键数字不一致，一律不许合并）

特别说明：**类型族**是本模块的关键修正。V1.0 新增了 RETIREMENT /
PLAYER_CAREER / NATIONAL_TEAM 三个细分类型后，同一件事的不同报道会被判成
「类型不同」而拒绝合并——实测这凭空制造了 16 对误拆分。因此类型比较必须
在「族」这一层做，而不是在原始类型名上做。

严禁为了合并率而错误合并
------------------------
复合规则的每一个条件都是「与」关系，任何一项不满足就不合并；
硬冲突是独立的否决项，优先级高于所有合并规则。
"""

import datetime
import hashlib
import re

import content_type as ct
import evidence as ev
import importance as imp

# ==================== 事件类型族 ====================
# 同一族内的类型视为「同一类事件」，允许进入复合合并判断。
# 跨族一律不合并——「转会」和「伤病」永远是两件事。
TYPE_FAMILY = {
    "RETIREMENT": "CAREER_END",
    "PLAYER_CAREER": "CAREER_END",
    "NATIONAL_TEAM": "CAREER_END",
    "TRANSFER": "TRANSFER",
    "CONTRACT": "TRANSFER",
    "INJURY": "INJURY",
    "DISCIPLINE": "DISCIPLINE",
    "MATCH": "MATCH",
    "APPOINTMENT": "APPOINTMENT",
    "DEATH": "DEATH",
    "ILLNESS": "ILLNESS",
    "LEGAL": "INTEGRITY",
    "MATCH_FIXING": "INTEGRITY",
    "CLUB_OWNERSHIP": "GOVERNANCE",
    "GOVERNANCE": "GOVERNANCE",
    "SPONSORSHIP": "COMMERCIAL",
    "SAFETY_WELFARE": "SAFETY",
}

# 族内代表类型的选择优先级（越靠前越具体，用作事件的展示类型）
_TYPE_PRIORITY = (
    "RETIREMENT", "MATCH_FIXING", "SAFETY_WELFARE", "CLUB_OWNERSHIP",
    "GOVERNANCE", "DEATH", "LEGAL", "ILLNESS", "DISCIPLINE", "INJURY",
    "APPOINTMENT", "TRANSFER", "CONTRACT", "NATIONAL_TEAM",
    "PLAYER_CAREER", "SPONSORSHIP", "MATCH",
)

# ==================== 复合规则阈值（实测标定） ====================
# 用 C 罗退役 10 条真实标题做阈值扫描：
#   0.30 → 救回 9/25 对，误合并 0
#   0.28 → 救回 12/25 对，误合并 0     ← 选这个
#   0.20 → 救回 13/25 对，但出现 1 对误合并（把「捐球票」并进「退役」）
# 所以 0.28 是「零误合并」前提下能取到的最低值。
COMPOSITE_JACCARD = 0.28
# 时间未知时的额外要求：没有时间约束就必须更像，才敢合并
COMPOSITE_JACCARD_NO_TIME = 0.45
# 同一事件的报道时间跨度上限。超过 48 小时的「相似标题」更可能是两轮独立事件
COMPOSITE_WINDOW_HOURS = 48

# 注意：这里刻意**没有**「硬冲突最小相关度」这类阈值。
# 早期版本有 HARD_CONFLICT_MIN_SIM = 0.10，逻辑是「相关度低于它才算冲突」，
# 方向是反的——越是措辞相似的两件不同事，相关度越高，越会被合并。
# 硬冲突现在是无条件否决，不存在任何放行阈值。


def _family(event_type):
    return TYPE_FAMILY.get((event_type or "").upper().strip(), "")


def _parse_ts(value):
    """解析时间戳。只处理 ISO 与常见 RFC822 形态，解析不了就返回 None。

    刻意不 import bot：bot 会拉起 requests / ai_provider 等重量级依赖，
    聚合层要保持可独立测试。
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo:
            return value.astimezone(datetime.timezone.utc)
        return value.replace(tzinfo=datetime.timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except ValueError:
        pass
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def _hours_between(a, b):
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 3600.0


# 俱乐部识别：用于「是不是两家不同俱乐部」的硬冲突判定。
# 为什么不复用 evidence.CLUB_TOKENS：
#   · 它存的是 "westham" 这种无空格形式，标题写「West Ham」时抽不出来；
#   · 它刻意排除了 united / city（为了「同一主体」比对不把曼联曼城混为一谈），
#     但硬冲突需要的是「能不能确定是两家不同俱乐部」，取舍方向相反。
# 所以这里单独维护一份，只服务于冲突判定，不改变 event_subjects 的既有语义。
_CLUB_PATTERNS = (
    (r"west\s+ham", "westham"),
    (r"manchester\s+united|man\s+utd|man\s+united", "manutd"),
    (r"manchester\s+city|man\s+city", "mancity"),
    (r"newcastle(\s+united)?", "newcastle"),
    (r"nottingham\s+forest", "forest"),
    (r"aston\s+villa", "astonvilla"),
    (r"crystal\s+palace", "palace"),
    (r"sheffield\s+united", "sheffieldutd"),
    (r"tottenham(\s+hotspur)?", "tottenham"),
    (r"wolverhampton|wolves", "wolves"),
    (r"brighton", "brighton"),
    (r"everton", "everton"),
    (r"chelsea", "chelsea"),
    (r"arsenal", "arsenal"),
    (r"liverpool", "liverpool"),
    (r"fulham", "fulham"),
    (r"brentford", "brentford"),
    (r"bournemouth", "bournemouth"),
    (r"burnley", "burnley"),
    (r"leeds", "leeds"),
    (r"leicester", "leicester"),
    (r"sunderland", "sunderland"),
    (r"ipswich", "ipswich"),
    (r"southampton", "southampton"),
    (r"luton", "luton"),
    (r"real\s+madrid", "realmadrid"),
    (r"barcelona", "barcelona"),
    (r"bayern(\s+munich)?", "bayern"),
    (r"juventus", "juventus"),
    (r"napoli", "napoli"),
    (r"psg|paris\s+saint-?germain", "psg"),
    (r"dortmund", "dortmund"),
    (r"ajax", "ajax"),
    (r"benfica", "benfica"),
    (r"porto", "porto"),
)
_CLUB_RX = tuple((re.compile(r"\b" + p + r"\b", re.I), k) for p, k in _CLUB_PATTERNS)


def _clubs(title):
    """抽取标题里的俱乐部键。一词多队（如 "Manchester derby"）会返回多个。"""
    text = title or ""
    out = set()
    for rx, key in _CLUB_RX:
        if rx.search(text):
            out.add(key)
    return out


# 句首/虚词的常见大写词：不算专有名词
_NON_NAME_CAPS = {
    "the", "a", "an", "in", "on", "at", "for", "and", "but", "or", "nor",
    "after", "before", "with", "from", "as", "is", "are", "was", "were",
    "it", "its", "this", "that", "these", "those", "he", "she", "they",
    "his", "her", "their", "why", "how", "what", "when", "where", "who",
    "no", "not", "new", "top", "five", "one", "two", "three", "first",
    "last", "next", "more", "most", "all", "up", "down", "out", "off",
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "breaking", "exclusive", "official", "report",
    "trophies", "tears", "records", "rows", "analysis", "opinion",
}


def _proper_nouns(title):
    """抽取标题中的专有名词（首字母大写的词），用于识别「换了主角」。

    为什么需要这一层：`person_names()` 依赖「角色词 + 姓名位」启发式，
    对 "Ronaldo retires…" 这种没有角色词的标题抽不出人名，于是
    「C罗退役」和「梅西退役」在人物维度上都是空集，硬冲突检测失效，
    两条完全不同的新闻会被合并。专有名词比对补上了这个缺口。
    """
    out = set()
    for w in re.findall(r"[A-Z][A-Za-z'\u2019\-]+", title or ""):
        lw = w.lower().replace("\u2019", "'")
        if lw.endswith("'s"):
            lw = lw[:-2]
        lw = re.sub(r"[^a-z]", "", lw)
        if len(lw) < 3 or lw in _NON_NAME_CAPS:
            continue
        out.add(lw)
    return out


def _hard_conflict(a, b, sim=None):
    """硬冲突：明确指向两件不同的事，**任何规则都不许合并**。

    五类冲突（任一成立即否决，不设「相关度够高就放行」的例外）：
      1. 人物不一致（双方都识别出人物且无交集）——「签下 A」与「签下 B」是两笔转会
      2. 专有名词不一致（双方都有专名且无交集）——「C 罗退役」与「梅西退役」
      3. 主体不一致（双方都识别出俱乐部且无交集）
      4. 关键数字冲突（比分 2-0 vs 3-1，双方都有数字且无交集）
      5. 低价值内容与新闻事件不同类——广告/八卦绝不并入新闻事件

    第 3 类在盲测里补强过一次。原实现依赖 `evidence.event_subjects`，
    而它的词表存的是 "westham" 这种无空格形式，标题写「West Ham」时抽不出主体，
    于是「West Ham 解雇 Lopetegui」和「Wolves 解雇 Lopetegui」主体维度双双为空、
    冲突检测失效，同一名教练在两支球队被解雇被判成同一件事（盲测里 3 对误合并）。
    现在改用本模块自己的 `_clubs()`，多词俱乐部名也能认出来。

    第 5 类来自另一次实测中的错误合并：`same_event` 把
    「Ronaldo retires from international football - Manchester United」
    与「Manchester United memberships on sale now with 20% off」判成同一事件，
    理由是「主体 / 事件类型 / 关键事实一致」——因为后者根本没有事件类型，
    空类型被当成了「一致」。两条标题只共享「Manchester United」两个词，
    一条是退役、一条是会员促销，靠类型比较是拦不住的，只有内容类型能拦。

    早期版本写成「相关度很低时才算冲突」，方向是反的：
    越是措辞相似的两件不同事（同队两笔转会），相关度越高，越会被合并。
    """
    reasons = []

    pa, pb = a["persons"], b["persons"]
    if pa and pb and not (pa & pb):
        reasons.append("人物不同（{} vs {}）".format(
            "/".join(sorted(pa)[:3]), "/".join(sorted(pb)[:3])))

    na_, nb_ = a["nouns"], b["nouns"]
    if na_ and nb_ and not (na_ & nb_):
        reasons.append("专有名词不同（{} vs {}）".format(
            "/".join(sorted(na_)[:3]), "/".join(sorted(nb_)[:3])))

    sa, sb = a["subjects"], b["subjects"]
    if sa and sb and not (sa & sb):
        reasons.append("主体不同（{} vs {}）".format(
            "/".join(sorted(sa)[:3]), "/".join(sorted(sb)[:3])))

    cla, clb = a["clubs"], b["clubs"]
    if cla and clb and not (cla & clb):
        reasons.append("俱乐部不同（{} vs {}）".format(
            "/".join(sorted(cla)[:3]), "/".join(sorted(clb)[:3])))

    ka, kb = a["numbers"], b["numbers"]
    if ka and kb and not (ka & kb):
        reasons.append("关键数字冲突（{} vs {}）".format(
            "/".join(sorted(ka)[:3]), "/".join(sorted(kb)[:3])))

    ca = a["content"]["content_type"]
    cb = b["content"]["content_type"]
    if ct.is_low_value(ca) != ct.is_low_value(cb):
        reasons.append("低价值内容与新闻事件不同类（{} vs {}）".format(ca, cb))

    return bool(reasons), "；".join(reasons)


def _same_protagonist(a, b):
    """同一主角的两种写法：一方抽出了人名，另一方只有这个姓氏。

    「Luka Modric announces retirement from Croatia duty」能抽出 {luka, modric}；
    「Modric retires from international football after 18 years」只有句首的 Modric，
    `person_names()` 抽不出来（没有角色词），专有名词里倒是有。
    两条标题的 jaccard 只有 0.09，复合规则进不来，于是同一件事被拆成两簇——
    这是盲测里唯一剩下的误拆分。

    安全边界（三条「与」关系，缺一不合并）：
      · 必须同事件族 ——「Modric 退役」与「Modric 出任教练」族不同，不会合并
      · 必须时间窗内 —— 跨天的同名事件更可能是两轮独立事件
      · 必须无硬冲突 —— 俱乐部不同（摩德里奇在热刺 vs 在皇马退役）照样拦

    刻意不做「同一个人就合并」的宽泛规则：那会把「某队签下 A」和
    「A 续约」并成一件事，而续约与转会本来就是两个族。
    """
    fa, fb = a["family"], b["family"]
    if not fa or fa != fb:
        return False, "类型族不同（{} vs {}）".format(fa or "-", fb or "-")

    shared = (a["persons"] & b["nouns"]) | (b["persons"] & a["nouns"])
    if not shared:
        return False, "无共同主角"

    hours = _hours_between(a["ts"], b["ts"])
    if hours is not None and hours > COMPOSITE_WINDOW_HOURS:
        return False, "时间跨度 {:.1f}h 超过 {}h".format(
            hours, COMPOSITE_WINDOW_HOURS)

    conflict, why = _hard_conflict(a, b)
    if conflict:
        return False, "硬冲突：" + why
    return True, "同一主角（{}）+ 同族（{}）".format(
        "/".join(sorted(shared)[:2]), fa)


def _composite_same(a, b, sim):
    """复合规则：类型族相同 + 相关度达标 + 时间窗内 + 无硬冲突。

    每个条件都是「与」关系。任何一项不满足就返回 False——
    这是「宁可漏合，不可错合」在代码里的体现。
    """
    fa, fb = a["family"], b["family"]
    if not fa or not fb or fa != fb:
        return False, "类型族不同或缺失（{} vs {}）".format(fa or "-", fb or "-")

    hours = _hours_between(a["ts"], b["ts"])
    if hours is None:
        if sim < COMPOSITE_JACCARD_NO_TIME:
            return False, "时间未知且相关度 {:.2f} < {:.2f}".format(sim, COMPOSITE_JACCARD_NO_TIME)
    else:
        if hours > COMPOSITE_WINDOW_HOURS:
            return False, "时间跨度 {:.1f}h 超过 {}h".format(hours, COMPOSITE_WINDOW_HOURS)
        if sim < COMPOSITE_JACCARD:
            return False, "相关度 {:.2f} < {:.2f}".format(sim, COMPOSITE_JACCARD)

    conflict, why = _hard_conflict(a, b, sim)
    if conflict:
        return False, "硬冲突：" + why
    return True, "复合规则（族={} 相关度={:.2f} 跨度={}）".format(
        fa, sim, "未知" if hours is None else "{:.1f}h".format(hours))


def _link(a, b):
    """判断两条报道是否同一事件。返回 (bool, 依据)。

    顺序是刻意设计的：
      1. URL / 指纹 —— 硬同一，直接合并
      2. 硬冲突    —— **绝对否决**，先于任何语义规则
      3. same_event —— 既有五维判定
      4. 同一主角  —— 补人名抽取不对称造成的误拆分
      5. 复合规则  —— 补 same_event 的类型族盲区

    第 2 步必须在第 3 步之前：`same_event` 本身会把「C 罗退役」和
    「梅西退役」判成同一事件（两条标题的人名都抽不出来，人物维度是空集，
    它只能靠标题相似度 0.6 放行）。硬冲突先否决，才挡得住这类错误合并。
    """
    if a["canonical"] and a["canonical"] == b["canonical"]:
        return True, "同一 URL"
    if a["fingerprint"] and a["fingerprint"] == b["fingerprint"]:
        return True, "事件指纹相同"

    conflict, why_conflict = _hard_conflict(a, b)
    if conflict:
        return False, "硬冲突否决：" + why_conflict

    ok, why = ev.same_event(a["record"], b["record"])
    if ok:
        return True, "same_event：" + why
    ok_p, why_p = _same_protagonist(a, b)
    if ok_p:
        return True, "same_event 未命中（{}）→ {}".format(why, why_p)
    sim = ev._jaccard(a["tokens"], b["tokens"])
    ok2, why2 = _composite_same(a, b, sim)
    if ok2:
        return True, "same_event 未命中（{}）→ {}".format(why, why2)
    return False, why2 or why


def _prep(item):
    """把一条候选报道规范化成聚合所需的结构。"""
    title = item.get("title") or ""
    link = item.get("link") or ""
    desc = item.get("desc") or ""
    et = ev.primary_event_type(title)
    content = ct.classify(title, desc, event_type=et)
    canonical = ev.canonical_url(link) or link
    return {
        "item": item,
        "title": title,
        "link": link,
        "desc": desc,
        "canonical": canonical,
        "fingerprint": _fingerprint(title, link),
        "event_type": et,
        "family": _family(et),
        "content": content,
        "tokens": ev._tokens(title),
        "persons": ev.person_names(title),
        "nouns": _proper_nouns(title),
        "subjects": ev.event_subjects(title),
        "clubs": _clubs(title),
        "numbers": ev._key_numbers(title),
        "ts": _parse_ts(item.get("published")),
        "record": {"canonical": canonical, "title": title},
    }


def _fingerprint(title, url=""):
    """与 state_store.event_fingerprint 保持一致的算法。

    这里独立实现而不是 import state_store，是为了让本模块可以脱离
    文件 IO 依赖单独测试；两者算法必须一致，改动时要同步。
    """
    import hashlib
    text = (title or "").strip()
    if " - " in text:
        text = text.rsplit(" - ", 1)[0]
    norm = "".join(ch for ch in text.lower() if ch.isalnum())
    if len(norm) < 12:
        norm = (url or "").strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _cluster(prepped):
    """并查集聚类，边 = _link 命中。"""
    n = len(prepped)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            ok, why = _link(prepped[i], prepped[j])
            if ok:
                union(i, j)
                edges.append({"a": i, "b": j, "reason": why})

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values()), edges


def _pick_event_type(types):
    """族内代表类型：按优先级取最具体的那个。"""
    present = {t for t in types if t}
    for t in _TYPE_PRIORITY:
        if t in present:
            return t
    return ""


def _pick_content_type(members):
    """事件内容类型：按优先级取最具体且非低价值的那个。

    不能简单取「第一个非低价值」——成员顺序是聚类产物，不稳定。
    也不能因为簇里混进一条推广就把整件事降级：低价值类型排在最后。
    """
    order = (
        ct.TRANSFER_NEWS, ct.DISCIPLINARY_NEWS, ct.CLUB_GOVERNANCE,
        ct.FINANCIAL_NEWS, ct.COMPETITIVE_NEWS, ct.BREAKING_NEWS,
        ct.OTHER, ct.PROMOTION, ct.GOSSIP,
    )
    present = {m["content"]["content_type"] for m in members}
    for t in order:
        if t in present:
            return t
    return ct.OTHER


def _pick_display_title(members, event_type):
    """事件展示标题：优先「最短且含事件类型词」的那条。

    取最长的标题会选到长篇评论稿（实测 C 罗事件被选成「finished second to
    Messi」），取最早的可能选到零散短讯。折中方案是「含类型词的最短标题」，
    通常就是最干净的事实陈述（如「Ronaldo retires from Portugal duty」）。
    """
    keywords = set()
    if event_type:
        keywords = set(ev.EVENT_KEYWORDS.get(event_type, set()))
    typed = []
    for m in members:
        toks = set(m["tokens"])
        if keywords and (toks & keywords):
            typed.append(m["title"])
    pool = typed or [m["title"] for m in members]
    if not pool:
        return ""
    if typed:
        return min(pool, key=len)
    return max(pool, key=len)


def _event_scope(members, event_type):
    """业务线：成员多数票，平票取最早的那条。"""
    votes = {}
    for m in members:
        sc, _ = imp.classify_scope(m["title"], m["desc"], m["event_type"])
        votes[sc] = votes.get(sc, 0) + 1
    if not votes:
        return imp.GLOBAL_EVENT_LINE
    best = max(votes.values())
    winners = [k for k, v in votes.items() if v == best]
    if len(winners) == 1:
        return winners[0]
    # 平票：用最早一条的判定
    first = min(members, key=lambda m: (m["ts"] or datetime.datetime.max.replace(
        tzinfo=datetime.timezone.utc)))
    return imp.classify_scope(first["title"], first["desc"], first["event_type"])[0]


def build_events(items, now=None):
    """把候选报道聚合成事件列表。

    返回 (events, stats)：
      events —— 已按「重要性优先、同级按最新进展」排好序的事件列表
      stats  —— 聚合统计，便于报告与回归对比
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    prepped = [_prep(it) for it in items if (it.get("title") or "").strip()]
    if not prepped:
        return [], {"input": 0, "events": 0, "merged": 0, "edges": 0}

    groups, edges = _cluster(prepped)

    # 事件级证据：用整簇记录算独立来源组
    events = []
    for idxs in groups:
        members = [prepped[i] for i in idxs]
        records = ev.build_records([
            {"url": m["link"], "title": m["title"],
             "publisher": m["item"].get("publisher"),
             "desc": m["desc"],
             "feed_origin": m["item"].get("feed_origin") or m["item"].get("source")}
            for m in members
        ])
        groups_src = ev.independent_sources(records)
        independent = len([g for g in groups_src if g.get("trusted", True)])
        domains = {r.get("domain") for r in records if r.get("domain")}
        has_tier1 = any(r.get("source_class") == "TIER1" for r in records)
        has_wire = any(ev._wire_service(r) for r in records)

        event_type = _pick_event_type([m["event_type"] for m in members])
        content_type = _pick_content_type(members)
        scope = _event_scope(members, event_type)

        # 事件文本：展示标题用「含类型词的最短标题」；信号提取用多条拼接
        titles_sorted = sorted((m["title"] for m in members), key=len, reverse=True)
        display_title = _pick_display_title(members, event_type)
        probe_text = " ".join(titles_sorted[:3])

        # 重要性：成员逐个判 + 事件级合并文本判，取最高
        details = []
        for m in members:
            details.append(imp.assess(
                m["title"], m["desc"], event_type=m["event_type"],
                content_type=m["content"]["content_type"], scope=scope,
                independent_groups=independent, has_tier1=has_tier1,
                has_wire=has_wire, source_count=len(members)))
        details.append(imp.assess(
            probe_text, "", event_type=event_type, content_type=content_type,
            scope=scope, independent_groups=independent, has_tier1=has_tier1,
            has_wire=has_wire, source_count=len(members)))
        level = imp.combine([d["importance"] for d in details])
        winning = next((d for d in details if d["importance"] == level), details[0])

        published = [m["ts"] for m in members if m["ts"]]
        first_pub = min(published) if published else None
        last_prog = max(published) if published else None

        anchor = min(members, key=lambda m: (m["ts"] or datetime.datetime.max.replace(
            tzinfo=datetime.timezone.utc)))
        eid = "ev_" + _fingerprint(anchor["canonical"], anchor["canonical"])[:12]

        # 可信度：复用既有证据体系，不另造一套
        verdict = ev.evaluate(anchor["link"], records)
        cred = _cred_from_status(verdict.get("status"))

        events.append({
            "event_id": eid,
            "event_fingerprint": anchor["fingerprint"],
            "title": display_title,
            "event_type": event_type,
            "type_family": _family(event_type),
            "content_type": content_type,
            "scope": scope,
            "importance": level,
            "importance_detail": winning,
            "cred": cred,
            "evidence_status": verdict.get("status"),
            "evidence_refs": [
                {
                    "url": m["link"],
                    "title": m["title"],
                    "domain": rec.get("domain") or "",
                    "publisher": m["item"].get("publisher") or "",
                    "published": m["item"].get("published") or "",
                    "source_class": rec.get("source_class") or "",
                }
                for m, rec in zip(members, records)
            ],
            "article_count": len(members),
            "independent_groups": independent,
            "domains": sorted(d for d in domains if d),
            "first_published_at": first_pub.isoformat() if first_pub else "",
            "last_progress_at": last_prog.isoformat() if last_prog else "",
            "push_state": "UNPUSHED",
            "members": [m["item"] for m in members],
        })

    stats = {
        "input": len(prepped),
        "events": len(events),
        "merged": len(prepped) - len(events),
        "edges": len(edges),
    }
    events.sort(key=lambda e: (
        -imp.rank(e["importance"]),
        _neg_ts(e["last_progress_at"]),
    ))
    return events, stats


def _neg_ts(iso):
    """按时间倒序排序的辅助键：时间越新键越小。"""
    ts = _parse_ts(iso)
    if ts is None:
        return 0.0
    return -ts.timestamp()


_CRED_MAP = {
    ev.OFFICIAL_CONFIRMED: "E1",
    ev.MULTIPLE_INDEPENDENT_SOURCES: "E2",
    ev.SINGLE_SOURCE: "E3",
    ev.UNVERIFIED: "E4",
    ev.CONFLICTING: "E4",
}


def _cred_from_status(status):
    return _CRED_MAP.get(status, "E4")
