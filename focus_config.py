"""焦点战 / 裁判 / 首发监控的全部可调配置（V2.0 §3 §4 §5）。

**为什么单独一个文件**
任务书对 §4 有一条硬要求：

> 七位重点裁判观察名单必须配置化存储，不能散落在代码或 Prompt 中。

同一条纪律适用于所有「会被人为调整的规则」：焦点战分级阈值、德比表、
传统强队名单、首发监控的时间窗、证据分级。把它们塞进业务代码里的后果是
——改一个阈值要动主流程文件，改完还得重新读一遍逻辑确认没碰坏别的。

所以本文件**只放数据，不放逻辑**。任何函数都不在这里定义。

---

## 关于「示例对阵不得写死」

任务书 §3 明确：

> 示例对阵不得在代码中永久写死，必须根据积分榜/排名/竞争关系动态识别。

本文件里确实有静态表（`DERBIES`、`BIG_CLUBS`），但它们**不是示例对阵**：

· `DERBIES` 是** rivalry 事实**（曼联-曼城是同城德比，这不会因为某赛季排名而变），
  它只提供「这场是不是德比」这一个布尔输入，不决定分级；
· `BIG_CLUBS` 是**历史地位事实**（传统强队），同样只提供一个布尔输入。

真正决定 S/A/B 的是 `match_calendar.grade_match()` 里**用当前积分榜实时算出来的**
排名、分差、剩余轮次。也就是说：

· 写死「切尔西 vs 曼联 = S 级」 → **禁止**（这是示例对阵）；
· 写死「曼联与曼城互为德比对手」 → **允许**（这是领域事实，且可配置）。

两者区别在于：前者把结论写死，后者只提供事实输入。测试里有一条
`test_grade_is_not_hardcoded` 专门守这条线——同一对球队在不同积分榜下必须得到不同分级。
"""

# =============================================================================
# 一、焦点战识别规则（§3）
# =============================================================================
# 每条规则的结构：
#   enabled  —— 是否启用（关掉某条规则不必删代码）
#   weight   —— 命中后累加的分数，最终由总分决定 S/A/B/普通
#   params   —— 该规则自己的参数
#
# ⚠️ 所有涉及「排名/分差」的参数都以**当前积分榜**为输入，不是写死的球队名。
FOCUS_RULES = {
    # 争冠直接对话：双方都在前 N，且分差不超过 gap 分
    "title_race": {
        "enabled": True,
        "weight": 3,
        "params": {"max_position": 4, "max_gap": 6},
        "label": "争冠直接对话",
    },
    # 争四关键战：排名落在 [from, to] 区间内，双方分差不超过 gap
    "top4_battle": {
        "enabled": True,
        "weight": 2,
        "params": {"position_from": 3, "position_to": 7, "max_gap": 4},
        "label": "争四关键战",
    },
    # 保级关键战：双方都在倒数 N 名内，且分差不超过 gap
    "relegation": {
        "enabled": True,
        "weight": 2,
        "params": {"from_bottom": 6, "max_gap": 4},
        "label": "保级关键战",
    },
    # 德比：对阵双方在 DERBIES 里
    "derby": {
        "enabled": True,
        "weight": 3,
        "params": {},
        "label": "德比",
    },
    # 传统强队对决：双方都在 BIG_CLUBS 里
    "big_club_clash": {
        "enabled": True,
        "weight": 2,
        "params": {},
        "label": "传统强队对决",
    },
    # 影响积分榜走势：任一方在前 N（会改变榜首/欧战区格局）
    "table_impact": {
        "enabled": True,
        "weight": 1,
        "params": {"max_position": 8},
        "label": "影响积分榜走势",
    },
    # 高度舆论关注：新闻池里近期提到这两队的条数达到阈值
    # ⚠️ 这是唯一一条依赖「外部输入」的规则：mentions 必须由调用方传入真实计数，
    #    不能由 AI 估算。拿不到计数时这条规则**不命中**（不是猜一个值）。
    "public_attention": {
        "enabled": True,
        "weight": 1,
        "params": {"min_mentions": 3},
        "label": "高度舆论关注",
    },
}

# 总分 → 分级。从高到低匹配，第一个满足的生效。
# S ≥ 6：至少两条高权重规则同时命中（例如德比 + 争冠对话）
# A ≥ 4：一条高权重 + 一条中权重
# B ≥ 2：单条中权重，或两条低权重
GRADE_THRESHOLDS = (
    ("S", 6),
    ("A", 4),
    ("B", 2),
)
NORMAL_GRADE = "普通"

# 各等级默认开哪些监控（可在 match_calendar 里被单场覆盖）
MONITOR_POLICY = {
    "S": {"referee": True, "lineup": True},
    "A": {"referee": True, "lineup": True},
    "B": {"referee": True, "lineup": False},
    NORMAL_GRADE: {"referee": False, "lineup": False},
}


# =============================================================================
# 二、德比表（领域事实，不是示例对阵）
# =============================================================================
# 只列英超可能出现的组合。键是排序后的队名元组（小写，用 canonical 名），
# 这样「曼联 vs 曼城」和「曼城 vs 曼联」不需要写两遍。
# 数据来源：英超官方对 rivalry 的公开表述 + 各俱乐部所在地。
# 维护方式：升班马带来新的同城/地区德比时在此追加一行。
DERBY_PAIRS = (
    # 同城德比
    ("man utd", "man city"),          # 曼彻斯特德比
    ("liverpool", "everton"),         # 默西塞德德比
    ("arsenal", "tottenham"),         # 北伦敦德比
    ("chelsea", "fulham"),            # 西伦敦德比
    ("newcastle", "sunderland"),      # 泰恩-威尔德比
    ("aston villa", "wolves"),        # 西米德兰兹德比
    ("crystal palace", "brighton"),   # M23 德比
    ("west ham", "chelsea"),          # 伦敦德比（东西伦敦）
    ("west ham", "tottenham"),        # 伦敦德比
    ("arsenal", "chelsea"),           # 伦敦德比
    # 传统宿敌（非同城，但舆论关注度等同德比）
    ("man utd", "liverpool"),         # 英格兰国家德比
    ("arsenal", "man utd"),           # 历史宿敌
    ("leeds", "man utd"),             # 玫瑰德比
)


# =============================================================================
# 三、传统强队（历史地位事实）
# =============================================================================
# 判定口径：英超时代拿过冠军，或长期属于「Big 6」讨论范围。
# 这只是一个输入信号（权重 2），不单独决定分级。
BIG_CLUBS = frozenset({
    "man utd", "man city", "liverpool", "arsenal", "chelsea", "tottenham",
})


# =============================================================================
# 四、七位重点裁判观察名单（§4 硬要求：必须配置化）
# =============================================================================
# ⚠️ 声明（与任务书原文一致，不得删改）：
#     这是**内容运营层面的观察优先级**，不代表系统已证明他们更易犯错或偏袒。
#     名单里的人担任主裁或 VAR 时触发八步流程，但流程产出的是「情报」，
#     不是「指控」。任何关于判罚对错的结论都必须过 EVIDENCE_GRADES 分级。
#
# 字段说明：
#   display_name  —— 中文/正式全名，用于推送文案
#   csv_names     —— football-data.co.uk 的缩写形式（"S Attwell"），用于匹配
#   aliases       —— 其他可能出现的写法（官方公告常用全名）
WATCH_REFEREES = (
    {
        "referee_id": "stuart_attwell",
        "display_name": "Stuart Attwell",
        "csv_names": ("S Attwell", "Attwell"),
        "aliases": ("Stuart Attwell", "S. Attwell"),
    },
    {
        "referee_id": "chris_kavanagh",
        "display_name": "Chris Kavanagh",
        "csv_names": ("C Kavanagh", "Kavanagh"),
        "aliases": ("Chris Kavanagh", "C. Kavanagh"),
    },
    {
        "referee_id": "darren_england",
        "display_name": "Darren England",
        "csv_names": ("D England", "England"),
        "aliases": ("Darren England", "D. England"),
    },
    {
        "referee_id": "peter_bankes",
        "display_name": "Peter Bankes",
        "csv_names": ("P Bankes", "Bankes"),
        "aliases": ("Peter Bankes", "P. Bankes"),
    },
    {
        "referee_id": "anthony_taylor",
        "display_name": "Anthony Taylor",
        "csv_names": ("A Taylor", "Taylor"),
        "aliases": ("Anthony Taylor", "A. Taylor"),
    },
    {
        "referee_id": "john_brooks",
        "display_name": "John Brooks",
        "csv_names": ("J Brooks", "Brooks"),
        "aliases": ("John Brooks", "J. Brooks"),
    },
    {
        "referee_id": "thomas_bramall",
        "display_name": "Thomas Bramall",
        "csv_names": ("T Bramall", "Bramall"),
        "aliases": ("Thomas Bramall", "T. Bramall"),
    },
)


# =============================================================================
# 五、裁判统计口径（§4：必须记录时间范围/比赛数/角色/来源/口径）
# =============================================================================
REFEREE_STATS_POLICY = {
    # 低于这个场次**不出结论**，只出原始计数。
    # 依据：单赛季 5 场样本的场均黄牌标准差极大，任何「他吹得严」的断言都站不住。
    "min_matches_for_claim": 15,
    # 统计窗口：最近几个赛季（含当前赛季）
    "seasons_window": 2,
    # 统计口径的显式声明，会原样写进输出，便于回溯
    "metric_note": (
        "场均黄牌 = 该裁判执法的比赛中双方黄牌总数 ÷ 执法场次；"
        "数据来自 football-data.co.uk 赛季结果 CSV 的 HY/AY/HR/AR 列；"
        "仅统计该裁判担任**主裁**的比赛（CSV 的 Referee 列即主裁），"
        "VAR/第四官员/助理裁判的执法不计入本统计。"
    ),
}

# 12 个判罚观察维度（§4）。顺序即推送时的展示顺序。
OBSERVATION_DIMENSIONS = (
    ("penalty", "点球判罚"),
    ("red_card", "红牌"),
    ("second_yellow", "两黄变红"),
    ("var_overturn", "VAR 改判"),
    ("goal_disallowed", "进球被吹"),
    ("offside", "越位判罚"),
    ("handball", "手球"),
    ("foul_play", "犯规尺度"),
    ("advantage", "有利原则"),
    ("added_time", "补时长度"),
    ("stoppage_management", "比赛管理/冲突控制"),
    ("consistency", "同类动作判罚一致性"),
)

# 历史争议判罚的 A–E 证据分级（§4）
# ⚠️ 纪律：不能把球迷评论升级成官方确认错误。
EVIDENCE_GRADES = {
    "A": "官方确认错误（裁判机构/联盟公开承认误判）",
    "B": "官方解释（裁判机构公开说明判罚依据，但未承认错误）",
    "C": "专业媒体分析（有署名记者/前裁判的技战术分析）",
    "D": "球迷争议（仅作线索，**不得作为事实陈述**）",
    "E": "未核实（来源不明或无法追溯）",
}
# 只有 A/B/C 可以出现在「事实陈述」里；D/E 只能出现在「争议线索」段落。
FACT_GRADES = frozenset({"A", "B", "C"})


# =============================================================================
# 六、首发阵容监控（§5）
# =============================================================================
LINEUP_POLICY = {
    # 相对开球时间的预热/提频/重点监控窗口（分钟）
    "warmup_minutes": 120,      # T-120 开始预热
    "intensify_minutes": 90,    # T-90 提频
    "critical_minutes": 75,     # T-75 至公布，重点监控
    # 轮询间隔（秒）。实际生效值还要受退避策略约束。
    "interval_seconds": {
        "idle": 300,            # 非窗口期
        "warmup": 120,
        "intensify": 60,
        "critical": 30,
    },
    # 退避：连续失败时放大间隔，避免高频打到被封
    "backoff": {
        "max_consecutive_failures": 5,
        "multiplier": 2,
        "max_interval_seconds": 300,
    },
    # 官方公布后仍要继续核对的时间窗（分钟），用于捕捉官方更正
    "post_publish_recheck_minutes": 30,
}

# 首发状态机。**只有官方确认才能到 OFFICIAL_CONFIRMED**。
LINEUP_STATUS = {
    "unknown": "未知",
    "not_published": "官方未公布",
    "predicted": "Predicted Lineup（AI/媒体预测，非事实）",
    "reported": "Reported Lineup（媒体提前披露，未获官方确认）",
    "unconfirmed": "Unconfirmed（来源不明）",
    "official_confirmed": "Official Lineup Confirmed（官方已确认）",
}
# 允许出现在推送里的状态（其余一律不得自动发布）
PUBLISHABLE_LINEUP_STATUS = frozenset({"reported", "official_confirmed"})
# 只有这些状态可以带「官方确认」字样
OFFICIAL_LINEUP_STATUS = frozenset({"official_confirmed"})

# 首发来源三级优先级（§5）。数值越小越可信。
LINEUP_SOURCE_TIERS = {
    "official_club": 1,      # 俱乐部官网/官方 App/官方社媒
    "official_league": 1,    # 英超官方
    "official_press": 2,     # 官方赛前发布会（主教练口述）
    "tier1_media": 3,        # BBC/Sky/The Athletic 等一线媒体
    "aggregator": 4,         # 聚合站/自媒体（虎扑/懂球帝/直播吧同级）
}
