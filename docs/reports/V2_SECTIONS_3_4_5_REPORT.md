# V2.0 §3 / §4 / §5 实施报告

> 时间：2026-10-31
> 范围：焦点战识别与比赛日历（§3）、裁判情报系统（§4）、首发极速监控（§5）
> 前置：§2 足球领域边界（上一轮已上线）、§5.4 调度审计（本轮完成，见 `AUDIT_SCHEDULER_5_4.md`）
> 纪律：本文所有数字均来自实测；未完成的部分在 §6 明确列出，不含「应该可以」这类表述。

---

## 0. 交付清单

| 文件 | 作用 | 行数 |
|---|---|---|
| `focus_config.py` | **纯配置**：焦点规则/德比表/传统强队/七人观察名单/统计口径/12 维度/A–E 分级/首发策略 | 308 |
| `match_calendar.py` | §3 比赛日历：抓赛程 + 自算积分榜 + 动态焦点分级 | 734 |
| `referee_intel.py` | §4 裁判情报：身份归一、统计、任命、八步流程、九项报告 | 628 |
| `lineup_monitor.py` | §5 首发监控：阶段/退避、解析、九项校验、七步推送、速度证明 | 547 |
| `pl_api.py` | 英超官方接口客户端（裁判组 5 角色 + 首发名单） | 305 |
| `tests/test_match_calendar.py` | §3 测试 | 35 例 |
| `tests/test_referee_intel.py` | §4 测试 | 40 例 |
| `tests/test_lineup_monitor.py` | §5 测试 | 44 例 |
| `tests/test_pl_api.py` | 官方接口测试 | 19 例 |
| `docs/reports/AUDIT_SCHEDULER_5_4.md` | §5.4 调度审计 | — |

**测试**：`python tests/run_all.py --fast` → **504 例全绿**（本轮新增 138 例，原有 366 例一条未改、一条未放宽）。

---

## 1. §3 比赛日历与焦点战识别

### 1.1 数据源（全部免费，逐个实测）

| source_id | 提供什么 | 实测 |
|---|---|---|
| `fixturedownload` | 全季 380 场赛程，**真 UTC**，含比分 | 78.7KB / 2.16s |
| `football_data_fixtures` | 未来约 10 天，**含裁判署名**（英超 10/10 覆盖） | 54.2KB / 3.82s |
| `football_data_results` | 已完赛结果，含裁判 + 红黄牌（80/80 有裁判） | 38KB / 3.62s |
| `premierleague_official` | 裁判组 5 角色 + 首发名单（见 §4.3） | 见 §5.2 |

### 1.2 发现并修掉一个会造成「提前一小时」的坑

**两个源的时间基准不同**：

- `fixturedownload` 的 `DateUtc` 是**真 UTC**；
- `football-data` 的 `Date`+`Time` 是**英国本地时间**。

2026-10-31 这天两者看起来完全一致（英国已于 10-25 回到 GMT，本地=UTC），所以「两源时间一致」这个观察在当时是**假象**。实测证据（2026-08-21，BST 期间）：

```
fixturedownload: 2026-08-21 19:00:00Z
football-data  : 21/08/2026 20:00      ← 本地 20:00 = UTC 19:00
```

若直接把 football-data 的时间当 UTC 用，整季前半段所有开球时间都会**晚一小时**，首发监控就会**提前一小时**开盯——白烧额度，且真公布时可能已经在退避状态。

**修复**：`match_calendar.uk_local_to_utc()` 按英国夏令时规则（3 月最后一个周日 01:00 起 BST，10 月最后一个周日 02:00 起 GMT）换算，不依赖 `tzdata`（本机 `zoneinfo` 实测不可用，CI 可用，两边行为不一致比手写更危险）。

**验证**：把全季 380 场结果（本地时间）与 fixturedownload（UTC）逐场对齐，**0 条时间不一致告警**。这不是抽样，是全季。

### 1.3 焦点战分级：不写死对阵

`focus_config.DERBIES` / `BIG_CLUBS` 只提供两个布尔输入；等级由 `grade_match()` 用**当前积分榜**（自己从 80 场结果算出来的）的排名与分差累加分数得出。

守门测试 `test_grade_is_not_hardcoded`：同一对球队（Chelsea vs Man Utd）在两份不同积分榜下必须得到**不同等级**（实测 S → B）。若哪天有人把「某对阵 = 某等级」写进代码，这条测试立刻红。

### 1.4 实测输出（2026-10-31 12:50 UTC，未来 21 天）

积分榜由 80 场结果算出，前 6：Man City 21 分 / Arsenal 21 / Brighton 16 / Liverpool 15 / Chelsea 13 / Brentford 11。

日历 21 场，焦点战 6 场：

| 等级 | 对阵 | 北京时间 | 裁判 | 理由 |
|---|---|---|---|---|
| **S** | Liverpool vs Arsenal | 11-02 00:30 | A Madley | 争冠对话（4 vs 2，差 6）+ 榜面 + 传统强队 |
| **A** | Man City vs Brighton | 10-31 23:00 | C Pawson | 争冠对话（1 vs 3，差 5）+ 榜面 |
| B | Chelsea vs Man Utd | 10-31 20:30 | P Bankes | 榜面 + 传统强队（曼联第 16，故未进争冠） |
| B | Bournemouth vs Leeds | 10-31 23:00 | M Salisbury | 争四区间 + 榜面 |
| B | Coventry vs Sunderland | 10-31 23:00 | S Hooper | 保级关键战（19 vs 20，差 1） |
| B | Leeds vs Tottenham | 11-07 20:30 | **未公布** | 争四区间 + 榜面 |

注意最后一场：**裁判未公布**，这正是 §4 任命监控要盯的目标。Chelsea vs Man Utd 只有 B 级也说明规则在正常工作——曼联第 16 名，它不该因为「历史名气」被抬成 S。

---

## 2. §4 裁判情报系统

### 2.1 七人观察名单：配置化 + 实测 7/7 命中

名单在 `focus_config.WATCH_REFEREES`（不在代码逻辑里，也不在 Prompt 里），并原样保留任务书的声明：**这是内容运营层面的观察优先级，不代表系统已证明他们更易犯错或偏袒**。

用两季结果 CSV（2025/26 的 380 场 + 2026/27 的 80 场，共 460 场）实测，**7/7 全部命中**：

| 裁判 | 场次 | 场均黄牌 | 统计窗口 | 可下结论 |
|---|---|---|---|---|
| Chris Kavanagh | 36 | 4.19 | 2025-08-18 ~ 2026-10-24 | 是 |
| Anthony Taylor | 31 | 3.94 | 2025-08-15 ~ 2026-05-24 | 是 |
| Stuart Attwell | 30 | 4.73 | 2025-08-24 ~ 2026-10-25 | 是 |
| Darren England | 28 | 4.18 | 2025-08-17 ~ 2026-10-18 | 是 |
| Thomas Bramall | 27 | 3.70 | 2025-08-23 ~ 2026-10-23 | 是 |
| Peter Bankes | 27 | 3.96 | 2025-08-17 ~ 2026-10-24 | 是 |
| John Brooks | 17 | 4.24 | 2025-10-04 ~ 2026-10-17 | 是 |

联赛均值 3.79。**注意口径**：这是「两季合并」的样本，单赛季只有 5–6 场（远低于 15 场门槛），所以模块里 `can_conclude` 是按合并样本判定的，且每条统计都强制携带 `interpretation_warning`（「黄牌多不等于裁判有问题」）。

### 2.2 任命监控：两种源的公布时点差异（本轮最意外的实测结果）

| 源 | 最早可拿到任命的时点 | 覆盖 |
|---|---|---|
| `football-data fixtures.csv` | **提前约 9 天**（11-07 那场当时无署名，其余 10 场全有） | 只给主裁 |
| `premierleague_official` | **T-130 分钟仍是空数组** | 给全 5 种角色（主裁/助理×2/第四官员/VAR/AVAR） |

实测（2026-10-31 12:50 UTC，5 场 T-130 分钟的比赛）：`matchOfficials` 全部为 `[]`。

**结论**：这两条路径是互补的，不是替代关系——
- **早**知道「谁吹」→ 用 football-data（只能拿到主裁）；
- **全**知道「整个裁判组」→ 用官方 API（但要等到临场）。

因此模块的 `build_appointment()` 保留 `published_at=""`（源里没有公告时间列，填了就是编造），只记 `first_seen_at`（我方首次观测）。`audit_lead_time()` 把「距开球几天首次看到署名」累积成分布，且明确写「单次快照不构成规律」。

### 2.3 五类角色独立统计

`role` 区分 Referee / Assistant Referee / Fourth Official / VAR / AVAR，`appointments_for_match()` 实测能从官方接口拿到全部 6 条记录（2 名助理裁判靠「无 role 字段」归入，见 `pl_api.ROLE_MAP` 的注释）。

样例（Arsenal vs Coventry，fixture 128923）：

```
Referee          Thomas Bramall   ← 七人名单成员
Assistant Referee Gary Beswick
Assistant Referee Matt Wilkes
Fourth Official  Adam Herczeg
VAR              Jarred Gillett
AVAR             Neil Davies
```

**VAR 判罚不计入主裁个人统计**：`build_referee_stats()` 只聚合 CSV 的 `Referee` 列（即主裁），`metric_note` 里写死了这条口径。

### 2.4 九项报告（A–I）与「不凑内容」

每场生成 A–I 九项，每项带 `status`：`ok` / `insufficient_data` / `source_unavailable`。

实测样例（Chelsea vs Man Utd，主裁 Peter Bankes）：

```
✓ A. 主裁与执法团队：主裁 Peter Bankes（观察名单）
✓ B. 执法数据：窗口 2025-08-17~2026-10-24，主裁 27 场，场均黄 3.96（联赛 3.79）
— C. 与两队历史交手：免费源不含「裁判×球队」索引
— D. 争议判罚：本次未检索到可核实的争议记录
× E. VAR 团队：本次未获取到官方 VAR/AVAR 任命
— F. 判罚尺度（12 维度）：仅牌数类可量化，其余需逐场事件数据
— G. 舆论关注点：未采集到热度数据
✓ H. 风险提示：黄牌数不能反推裁判水平
✓ I. 信息缺口：4 条
```

六项打「—」或「×」不是缺陷，是**纪律**：任务书要求「不能为凑内容编造争议」，所以没数据的项只写「没有什么」。当官方裁判组可用时（如 §2.3 那场），E 项会升级为 `ok` 并列出真实 VAR/AVAR——模块不会为了好看而假装有数据。

### 2.5 证据分级与「球迷评论不得升级」

`grade_evidence()` 把来源映射成 A–E，`split_by_grade()` 强制把 D/E 级放进 `leads`（线索）而不是 `facts`（事实）。测试 `test_fan_comment_never_becomes_fact` 守着这条线。

### 2.6 八步流程

命中七人名单时触发八步，实测输出里 `team_history` / `disputes` / `dimensions` 三步常态是 `insufficient_data` 或 `partial`，`human_review` 永远是 `pending`（未经人工复核不得对外定性）。

---

## 3. §5 首发阵容极速监控

### 3.1 为什么「30–60 秒轮询」有依据

实测（2026-10-31 12:50 UTC，5 场 T-130 分钟的比赛）：官方接口的 `teamLists` **全部为空**。

结合「首发通常在 T-60 左右公布」，说明：

- T-120 预热、T-90 提频、**T-75 起 30 秒** 的三段式设计是合理的；
- 在 T-130 就开始 30 秒轮询纯属浪费额度，还可能触发风控。

`next_interval()` 实测：idle 300s → warmup 120s → intensify 60s → critical 30s；连续失败 3 次退避到 300s 上限（×8），**不会无限加速**。

### 3.2 解析与九项校验

解析器支持官网/媒体的常见写法（`Starting XI: A; B, C, D; ...`）与中文（`首发阵容：…；…、…`），自动从分组人数推出阵型。

九项校验：V1 人数=11 / V2 阵型与分组自洽 / V3 姓名格式 / V4 无重复 / V5 归属该队 / V6 有门将 / V7 与对手不重叠 / V8 来源层级支撑状态 / V9 与已确认版本不冲突。

**用真实官方名单验证**（Arsenal vs Coventry，fixture 128923，11 人 + 阵型 4-2-3-1 + 分组 [1,4,2,3,1]）：九项校验**只剩 V5 一条提示**（没有该队阵容名单），零阻断。这是真实数据跑出来的结果，不是构造的夹具。

### 3.3 「只有官方确认才能标 Official」

`decide_status()` 的判定表：

| 来源层级 | 解析 | 阻断问题 | 状态 |
|---|---|---|---|
| tier 1（官方） | 通过 | 无 | **official_confirmed** |
| tier 2（官方发布会） | 通过 | 无 | reported |
| tier 3（一线媒体） | 通过 | 无 | reported |
| tier 4（聚合/自媒体） | 通过 | 无 | unconfirmed |
| 任意 | 失败 | — | unconfirmed |

解析失败或有阻断问题时，`decide_action()` 只会返回 `publish_link_only`（发官方链接 + 待核验）或 `skip`，**绝不自动发名单**。

### 3.4 速度证明：机制已就绪，数据尚未采集

`SPEED_RECORD_FIELDS` 强制记录 `official_published_at` / `first_detected_at` / `pushed_at`。`speed_proof()` 只有在**三个时间戳齐全且对手时间也拿到**时才返回 `proven=True`，否则文案里一个字都不提速度。

**当前状态**：`competitors_seen_at`（虎扑/懂球帝的出现时间）**没有任何采集手段**，所以现在所有推送都会走「不宣称领先」分支。

这是本轮唯一没有真正达成的 §5 目标，且我不打算用「我们每 30 秒轮询一次所以肯定更快」来冒充。要证明，必须真的去抓对手的发布时间——需要你决定是否允许采集这三家的页面（涉及对方 ToS）。

---

## 4. 回归与验证

```
python tests/run_all.py --fast
模块数 15 | 用例 504 | 失败 0 | 错误 0 | 耗时 8.4s
```

- 原有 366 例**一条未改、一条未删、断言未放宽**；
- 新增 138 例：日历 35 / 裁判 40 / 首发 44 / 官方接口 19；
- 真实数据验证（非测试，手动执行）：全季 380 场时间对齐 0 告警、7/7 名单命中、官方首发过九项校验。

**测试期间修掉的真 bug**：官方接口在未开赛时返回 `teamLists: [null, null]`（不是空对象），原解析器直接 `AttributeError` 会把整轮监控打挂。已加 `isinstance(dict)` 守卫。

---

## 5. 与调度决策的耦合关系

本轮的模块**不依赖**轮询频率：日历、分级、裁判统计都是「拿到数据就能算」。

但 §5 的「30–60 秒」目标取决于 `AUDIT_SCHEDULER_5_4.md` 里的三个待裁决项：

1. cron-job.org 节拍 5 分钟 → 1 分钟（或加第二条任务错开 30 秒）；
2. 早报从 GitHub schedule 搬到 cron-job.org（现在实测**迟到 207 分钟**）；
3. 是否允许新增 Cloudflare Workers（免费档）跑首发热路径。

---

## 6. 未完成 / 需要你决策

| # | 事项 | 现状 | 卡在哪 |
|---|---|---|---|
| 1 | 首发监控接入实际运行 | 模块完成、真实数据验证通过 | 需要独立的 `focus.yml` + 独立 concurrency group（我可以直接写），以及第 1 项调度决策 |
| 2 | 「早于虎扑/懂球帝」的实证 | 机制就绪，**数据为空** | 需你允许采集对手页面时间戳 |
| 3 | 裁判×球队历史交手 | 标 `insufficient_data` | 免费源没有，需人工或付费源 |
| 4 | 逐场判罚事件（点球/改判/越位） | 标 `insufficient_data` | 同上 |
| 5 | 早报 207 分钟延迟 | 已定位 | 需你在 cron-job.org 加 `mode=morning` 任务 |
| 6 | `focus.yml` 工作流 | 未创建 | 等第 1 项决策；创建后仍需真实比赛日验证 |

**下一步建议顺序**：先定调度（1 分钟 vs 30 秒）→ 我写 `focus.yml` → 在 11-02 Liverpool vs Arsenal（S 级）那天做首次真实演练，用真实时间戳产出第一份速度记录。
