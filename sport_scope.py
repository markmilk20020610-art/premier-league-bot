"""足球领域边界（V1.3 §F1 专项审计 A / C）。

**为什么需要这个模块**
系统原来只有「媒体权威度」这一个维度（`evidence.TIER1_DOMAINS`），
没有任何地方回答「这是不是足球」。于是 Sky Sports 的全站综合体育 feed
（`/rss/12040`，频道标题 `SkySports | News`）把 F1、高尔夫、飞镖、橄榄球、
拳击一起送进了足球推送流程，并因为 `skysports.com` 是整域白名单而直接拿到 Tier 1。

**设计原则**
1. `Sports ≠ Football`。综合体育媒体的一条稿子，默认不是足球。
2. **程序判定，不看模型**。模型自报的领域只作线索，不能作为放行依据。
3. **保守优先**：判不出来就拒绝。领域未知的新闻宁可少发一条，
   也不能把 F1 当成足球发出去——后者是不可挽回的对外错误。
4. 三级取证，逐级降权：
   - 一级：URL 路径（`/f1/`、`/sport/football/`）—— 发布方的频道归属，最可靠
   - 二级：赛事 / 组织 / 技术术语（`Grand Prix`、`Premier League`）—— 权重 2
   - 三级：运动员 / 俱乐部 / 教练名（`Verstappen`、`Arsenal`）—— 权重 1
   同一项目得分 ≥ 2 视为「检出」，两个不同项目同时检出视为「冲突」。

**为什么不直接用关键词黑名单**
黑名单只能拦住见过的项目，拦不住下一个。这里的判据是「先确定是什么」，
而不是「排除不是什么」——判不出归属的新闻同样不放行。
"""

import re

# ==================== 领域枚举 ====================
FOOTBALL = "Football"
FORMULA1 = "Formula 1"
BASKETBALL = "Basketball"
TENNIS = "Tennis"
RUGBY = "Rugby"
CRICKET = "Cricket"
GOLF = "Golf"
DARTS = "Darts"
BOXING = "Boxing"
BASEBALL = "Baseball"
AMERICAN_FOOTBALL = "American Football"
OTHER = "OtherSport"
UNKNOWN = "Unknown"
# 同一标题同时命中足球与非足球 → 不是「无法判断」，而是「自相矛盾」。
# 这类内容一律拒绝：不能因为里面出现了俱乐部名字就当成足球放行。
CONFLICT = "Conflict"

ALL_CATEGORIES = (
    FOOTBALL, FORMULA1, BASKETBALL, TENNIS, RUGBY, CRICKET, GOLF, DARTS,
    BOXING, BASEBALL, AMERICAN_FOOTBALL, OTHER, UNKNOWN, CONFLICT,
)

# 允许进入足球新闻流程的领域。**只有这一个**。
ALLOWED_CATEGORIES = frozenset({FOOTBALL})

# 领域检出阈值：一级/二级信号记 2 分，三级信号记 1 分，满 2 分才算检出。
# 单个球员名字（1 分）不足以判定领域——同名的人太多，权重必须低。
DETECT_THRESHOLD = 2


# ==================== 一级：URL 路径 ====================
# 顺序有意义：先匹配更具体的路径。
# 例：`/american-football/` 也包含 `football`，必须先判掉，否则会被当成英式足球。
_PATH_RULES = (
    # (正则, 领域)
    (r"/american[-_]football/|/nfl/", AMERICAN_FOOTBALL),
    (r"/sport/football/|/football/|/soccer/", FOOTBALL),
    (r"/f1/|/formula[-_]?1/|/motorsport/|/nascar/|/indycar/|/motogp/", FORMULA1),
    # Sky 的 /racing/ 是赛马（实测 Breeders' Cup 挂在这条路径下），
    # 归到 OTHER 而不是 Formula 1——日志里不能把赛马写成 F1。
    (r"/racing/", OTHER),
    (r"/golf/", GOLF),
    (r"/darts/", DARTS),
    (r"/boxing/|/ufc/|/mma/", BOXING),
    (r"/rugby[-_]?(union|league)?/", RUGBY),
    (r"/cricket/", CRICKET),
    (r"/tennis/", TENNIS),
    (r"/basketball/|/nba/", BASKETBALL),
    (r"/baseball/|/mlb/", BASEBALL),
)
_PATH_RES = tuple((re.compile(p, re.I), cat) for p, cat in _PATH_RULES)


def _path_category(url):
    """从 URL 路径判定频道归属。发布方自己的频道划分，是最可靠的一级信号。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    # 只看路径部分，避免查询串里的 `?ref=football` 之类误伤
    try:
        from urllib.parse import urlparse
        path = urlparse(raw).path or ""
    except Exception:
        path = raw
    for rx, cat in _PATH_RES:
        if rx.search(path):
            return cat
    return ""


# ==================== 一级半：足球专属域名 ====================
# 俱乐部官网、联赛与足协官网的 URL 路径里通常没有 "football" 字样
# （`arsenal.com/news/arteta-dowman`），只靠路径与正文会把它们判成 Unknown。
# 这些域名**本身就只发布足球内容**，域名即是领域证据。
#
# 刻意**不含**综合体育媒体域名：skysports.com、bbc.co.uk、espn.com 都是
# 综合体育媒体，它们的域名不能证明一条稿子属于足球——这正是本次事故的教训。
FOOTBALL_DOMAINS = frozenset({
    "arsenal.com", "manutd.com", "mancity.com", "liverpoolfc.com",
    "chelseafc.com", "tottenhamhotspur.com", "premierleague.com", "thefa.com",
    "efl.com", "uefa.com", "fifa.com", "bundesliga.com", "laliga.com",
    "legaseriea.it", "ligue1.com", "eredivisie.nl", "mlssoccer.com",
})


def _domain_category(domain):
    """域名本身是否只做足球。综合体育媒体一律不算。"""
    d = (domain or "").strip().lower()
    if not d:
        return ""
    for base in FOOTBALL_DOMAINS:
        if d == base or d.endswith("." + base):
            return FOOTBALL
    return ""


# ==================== 二 / 三级：文本信号 ====================
# 每条记录 (正则, 权重)。权重 2 = 赛事/组织/技术术语（强信号），1 = 人名（弱信号）。
#
# 刻意**不收录**的词（都是真实会撞车的）：
#   · "united" / "city" —— "United States Grand Prix"、"Mexico City Grand Prix"
#   · "inter" —— "Interlagos"（巴西大奖赛赛道）
#   · "qualifying" —— 足球也有 "World Cup qualifying"
#   · "stewards" —— 足球也用这个词指球场安保
#   · "red bull" —— 足球也有 Red Bull Salzburg / Leipzig
#   · "hamilton" —— 只给 1 分，足球运动员里也有 Hamilton
_TEXT_SIGNALS = {
    FOOTBALL: (
        # 赛事 / 组织
        (r"\bpremier league\b", 2), (r"\bEFL\b", 2),
        # "championship" 单独出现太通用——F1 也叫 World Championship，
        # 橄榄球、板球都有 Championship。只有限定成英冠才算强信号。
        (r"\bEFL championship\b", 2), (r"\bchampionship\b", 1),
        (r"\bfa cup\b", 2), (r"\bcarabao cup\b", 2), (r"\bleague cup\b", 2),
        (r"\bcommunity shield\b", 2), (r"\bchampions league\b", 2),
        (r"\beuropa league\b", 2), (r"\bconference league\b", 2),
        (r"\bUEFA\b", 2), (r"\bFIFA\b", 2), (r"\bworld cup\b", 2),
        (r"\bla liga\b", 2), (r"\bserie a\b", 2), (r"\bbundesliga\b", 2),
        (r"\bligue 1\b", 2), (r"\beredivisie\b", 2), (r"\bMLS\b", 2),
        # 俱乐部
        (r"\barsenal\b", 2), (r"\bchelsea\b", 2), (r"\bliverpool\b", 2),
        (r"\btottenham\b", 2), (r"\bspurs\b", 2), (r"\bman(?:chester)? utd\b", 2),
        (r"\bman(?:chester)? (?:city|united)\b", 2), (r"\bnewcastle\b", 2),
        (r"\baston villa\b", 2), (r"\beverton\b", 2), (r"\bfulham\b", 2),
        (r"\bbrighton\b", 2), (r"\bwest ham\b", 2), (r"\bcrystal palace\b", 2),
        (r"\bbrentford\b", 2), (r"\bwolves\b", 2), (r"\bnottingham forest\b", 2),
        (r"\bbournemouth\b", 2), (r"\bburnley\b", 2), (r"\bleeds\b", 2),
        (r"\bsunderland\b", 2), (r"\bstoke city\b", 2), (r"\bpreston\b", 2),
        (r"\bleicester\b", 2), (r"\bsouthampton\b", 2), (r"\bipswich\b", 2),
        (r"\bsheffield (?:united|wednesday)\b", 2), (r"\bmiddlesbrough\b", 2),
        (r"\bceltic\b", 2), (r"\brangers\b", 2), (r"\breal madrid\b", 2),
        (r"\bbarcelona\b", 2), (r"\batletico\b", 2), (r"\bjuventus\b", 2),
        (r"\bac milan\b", 2), (r"\binter milan\b", 2), (r"\bnapoli\b", 2),
        (r"\bbayern\b", 2), (r"\bdortmund\b", 2), (r"\bpsg\b", 2),
        (r"\bajax\b", 2), (r"\bbenfica\b", 2),
        # 角色 / 术语
        (r"\bstriker\b", 1), (r"\bmidfielder\b", 1), (r"\bdefender\b", 1),
        (r"\bgoalkeeper\b", 1), (r"\bkeeper\b", 1), (r"\bwinger\b", 1),
        (r"\bfull[- ]back\b", 1), (r"\bcentre[- ]back\b", 1),
        (r"\bhead coach\b", 1), (r"\bmanager\b", 1), (r"\bgaffer\b", 1),
        (r"\btransfer\b", 1), (r"\bloan\b", 1), (r"\bcontract\b", 1),
        (r"\bred card\b", 1), (r"\bpenalty\b", 1), (r"\boffside\b", 1),
        (r"\bfixture\b", 1), (r"\bmatchday\b", 1), (r"\bclean sheet\b", 1),
        (r"\bhat[- ]trick\b", 1), (r"\bderby\b", 1), (r"\bVAR\b", 1),
        (r"\bfootballer\b", 1),
        # 标题里直接出现 "football" 已经是很强的信号：实测「Football Wags」
        # 「City Football Group」这类标题除它之外没有任何俱乐部名，权重给 1 会误杀。
        (r"\bfootball\b", 2),
        # 英足总的标准缩写。体育语境里 "FA" 基本只指 Football Association。
        (r"\bFA\b", 2),
        # 英格兰第五级别联赛，UK 体育语境下专指足球
        (r"\bnational league\b", 2),
        # 顶级球员名：Messi 的指向性足够强，给 2 分不会误伤
        (r"\bmessi\b", 2),
        # "Milan" 单用给 1 分：既是 AC 米兰也是人名，靠 transfer/defender 补足阈值
        (r"\bmilan\b", 1),
    ),
    FORMULA1: (
        (r"\bf1\b", 2), (r"\bformula (?:1|one)\b", 2), (r"\bgrand prix\b", 2),
        (r"\bGP\b", 2), (r"\bnascar\b", 2), (r"\bmotogp\b", 2), (r"\bindycar\b", 2),
        (r"\bpaddock\b", 2), (r"\bpole position\b", 2), (r"\bgrid penalt(?:y|ies)\b", 2),
        (r"\bconstructors\b", 2), (r"\bchequered flag\b", 2), (r"\bsprint race\b", 2),
        (r"\bpit stop\b", 2), (r"\bDRS\b", 2), (r"\bformula e\b", 2),
        (r"\bverstappen\b", 1), (r"\bleclerc\b", 1), (r"\bhamilton\b", 1),
        (r"\bnorris\b", 1), (r"\bpiastri\b", 1), (r"\bsainz\b", 1),
        (r"\brussell\b", 1), (r"\bantonelli\b", 1), (r"\bhadjar\b", 1),
        (r"\balonso\b", 1), (r"\bperez\b", 1), (r"\btsunoda\b", 1),
        (r"\bocon\b", 1), (r"\bgasly\b", 1), (r"\bstroll\b", 1),
        (r"\bhulkenberg\b", 1), (r"\blawson\b", 1),
    ),
    GOLF: (
        (r"\bPGA\b", 2), (r"\bLPGA\b", 2), (r"\bryder cup\b", 2),
        (r"\bthe open championship\b", 2), (r"\bmasters\b", 2),
        (r"\bbirdie\b", 2), (r"\beagle\b", 2), (r"\bbogey\b", 2),
        (r"\bfairway\b", 2), (r"\btee off\b", 2), (r"\bgreen in regulation\b", 2),
        (r"\bgolfer\b", 2), (r"\bgolf\b", 2), (r"\bholes? in one\b", 2),
    ),
    DARTS: (
        (r"\bdarts\b", 2), (r"\bPDC\b", 2), (r"\bnine[- ]darter\b", 2),
        (r"\boche\b", 2), (r"\blittler\b", 1), (r"\bsherrock\b", 1),
        (r"\bvan gerwen\b", 1), (r"\bhumphries\b", 1),
    ),
    BOXING: (
        (r"\bUFC\b", 2), (r"\bMMA\b", 2), (r"\bboxing\b", 2),
        (r"\btitle fight\b", 2), (r"\bweigh[- ]in\b", 2), (r"\bheavyweight\b", 2),
        (r"\bknockout\b", 2), (r"\bwbc\b", 2), (r"\bwba\b", 2), (r"\bibf\b", 2),
        (r"\bcanelo\b", 1), (r"\bmbilli\b", 1), (r"\busyk\b", 1),
        (r"\btyson fury\b", 1), (r"\banthony joshua\b", 1),
    ),
    RUGBY: (
        (r"\brugby\b", 2), (r"\bsix nations\b", 2), (r"\bred roses\b", 2),
        (r"\ball blacks\b", 2), (r"\bscrum\b", 2), (r"\blineout\b", 2),
        (r"\bworld rugby\b", 2), (r"\bsuper rugby\b", 2),
    ),
    CRICKET: (
        (r"\bcricket\b", 2), (r"\bashes\b", 2), (r"\btest match\b", 2),
        (r"\bwicket\b", 2), (r"\binnings\b", 2), (r"\bbcci\b", 2),
        (r"\bIPL\b", 2), (r"\bT20\b", 2), (r"\bODI\b", 2),
    ),
    TENNIS: (
        (r"\btennis\b", 2), (r"\bwimbledon\b", 2), (r"\bATP\b", 2),
        (r"\bWTA\b", 2), (r"\bgrand slam\b", 2), (r"\bdjokovic\b", 1),
        (r"\balcaraz\b", 1), (r"\bsinner\b", 1), (r"\bnadal\b", 1),
    ),
    BASKETBALL: (
        (r"\bNBA\b", 2), (r"\bbasketball\b", 2), (r"\beuroleague\b", 2),
        (r"\blebron\b", 1), (r"\bstephen curry\b", 1),
    ),
    BASEBALL: (
        (r"\bMLB\b", 2), (r"\bbaseball\b", 2), (r"\bworld series\b", 2),
    ),
    AMERICAN_FOOTBALL: (
        (r"\bNFL\b", 2), (r"\bamerican football\b", 2),
        (r"\bsuper bowl\b", 2), (r"\bquarterback\b", 2),
    ),
}

_TEXT_RES = {
    cat: tuple((re.compile(p, re.I), w) for p, w in sigs)
    for cat, sigs in _TEXT_SIGNALS.items()
}


def _scores(text):
    """返回 {领域: 得分}，只保留得分 > 0 的领域。"""
    out = {}
    if not text:
        return out
    for cat, rules in _TEXT_RES.items():
        total = 0
        for rx, weight in rules:
            if rx.search(text):
                total += weight
        if total:
            out[cat] = total
    return out


class SportVerdict(dict):
    """领域判定结果。用 dict 子类是为了能直接进 state / 日志，不必额外序列化。"""

    @property
    def category(self):
        return self.get("category") or UNKNOWN

    @property
    def allowed(self):
        return self.category in ALLOWED_CATEGORIES

    @property
    def reason(self):
        return self.get("reason") or ""

    def __str__(self):
        return f"{self.category}（{self.reason}）"


def classify_sport(title, desc="", url="", domain=""):
    """判定一条新闻属于哪个体育项目。返回 SportVerdict。

    判定顺序（先强后弱，冲突即拒绝）：
      1. 频道信号：URL 路径（`/f1/`）或足球专属域名（`arsenal.com`）
         → 直接采信发布方的频道划分。
         但若正文同时命中**另一个**项目的强信号，仍判冲突——路径可以被模板写错，
         内容不会。
      2. 无频道信号时看文本：同一项目得分 ≥ 2 算检出。
      3. 足球与其他项目同时检出 → CONFLICT（不能凭俱乐部名放行）。
      4. 什么都没检出 → UNKNOWN（不是 Football）。

    `domain` 用于 Google News 这类聚合链接：链接本身没有频道信息，
    但 `<source url>` 还原出的真实发布方域名可能是足球专属域名。
    """
    text = f"{title or ''} {desc or ''}".strip()
    path_cat = _path_category(url)
    dom_cat = _domain_category(domain)
    if not dom_cat and url:
        # 没显式给 domain 就从 URL 里取，让调用方少传一个参数也不会漏判
        try:
            from urllib.parse import urlparse
            dom_cat = _domain_category(urlparse(url).netloc)
        except Exception:
            dom_cat = ""
    signal_cat = path_cat or dom_cat
    signal_kind = "URL 路径" if path_cat else ("足球专属域名" if dom_cat else "")
    scores = _scores(text)
    detected = {c: s for c, s in scores.items() if s >= DETECT_THRESHOLD}

    # 文本里各项目的排名（用于冲突时给出可读原因）
    def _fmt(d):
        return "、".join(f"{c}={s}" for c, s in sorted(d.items(), key=lambda x: -x[1]))

    football_hit = FOOTBALL in detected
    others = {c: s for c, s in detected.items() if c != FOOTBALL}

    if signal_cat and signal_cat != FOOTBALL:
        # 频道已经说明是别的项目：只有正文**强烈**指向足球时才判冲突
        if football_hit and scores.get(FOOTBALL, 0) >= 4:
            return SportVerdict(
                category=CONFLICT,
                reason=f"{signal_kind}指向 {signal_cat}，但正文强烈指向足球（{_fmt(detected)}）",
                path_category=signal_cat,
                scores=detected,
            )
        return SportVerdict(
            category=signal_cat,
            reason=f"{signal_kind} {signal_cat} 频道归属",
            path_category=signal_cat,
            scores=detected,
        )

    if signal_cat == FOOTBALL:
        # 频道说是足球，但正文命中了别的项目的强信号 → 冲突
        strong_others = {c: s for c, s in others.items() if s >= DETECT_THRESHOLD}
        if strong_others:
            return SportVerdict(
                category=CONFLICT,
                reason=f"{signal_kind}指向足球，但正文命中 {_fmt(strong_others)}",
                path_category=signal_cat,
                scores=detected,
            )
        return SportVerdict(
            category=FOOTBALL,
            reason=f"{signal_kind} {FOOTBALL} 频道归属",
            path_category=signal_cat,
            scores=detected,
        )

    # 无频道信号：纯文本判定
    if football_hit and others:
        return SportVerdict(
            category=CONFLICT,
            reason=f"同时命中足球与其他项目（{_fmt(detected)}）",
            path_category="",
            scores=detected,
        )
    if others:
        top = max(others, key=lambda c: others[c])
        return SportVerdict(
            category=top,
            reason=f"文本命中 {_fmt({top: others[top]})}",
            path_category="",
            scores=detected,
        )
    if football_hit:
        return SportVerdict(
            category=FOOTBALL,
            reason=f"文本命中 {_fmt({FOOTBALL: scores.get(FOOTBALL, 0)})}",
            path_category="",
            scores=detected,
        )
    return SportVerdict(
        category=UNKNOWN,
        reason=f"未命中任何领域信号（最高分 {max(scores.values()) if scores else 0} < {DETECT_THRESHOLD}）",
        path_category="",
        scores=scores,
    )


def is_football(verdict):
    """只有明确判定为 Football 才算通过。UNKNOWN / CONFLICT 一律不算。"""
    if isinstance(verdict, str):
        return verdict == FOOTBALL
    return bool(verdict) and verdict.get("category") == FOOTBALL


def reject_reason(verdict):
    """生成可直接写进日志与 state 的拒绝原因。"""
    if is_football(verdict):
        return ""
    cat = verdict.get("category") if isinstance(verdict, dict) else str(verdict)
    why = verdict.get("reason") if isinstance(verdict, dict) else ""
    return f"领域不是足球（{cat}）：{why}"
