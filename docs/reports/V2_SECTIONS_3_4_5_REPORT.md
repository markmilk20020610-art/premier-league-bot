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

---

## 7. 运行时接入（2026-10-31 晚，真实比赛日）

上面第 6 节列的第 1、2、6 项本轮已全部落地，并在当晚的真实比赛上跑通。

### 7.1 `focus.yml`：把调度做进 job 内部

原方案的死结是：所有调度都靠 cron-job.org 每分钟 `workflow_dispatch`，它一停全停；
而外部看门狗（Cloudflare Worker）要额外注册账号，运维成本高。

改为利用 GitHub Actions「单 job 最长 6 小时」的特性：

```
一次启动 → shell 循环里每 N 秒跑一次 focus_tick.py → 连续工作 5 小时 50 分
```

- `timeout-minutes: 355`（留 10 分钟余量，避开 6 小时硬杀）；
- **独立 workflow + 独立 `concurrency: group: focus-watch`** —— `scanner.yml` 的
  concurrency 是**工作流级**的，把这个 6 小时 job 塞进去会把新闻扫描彻底堵死；
- cron-job.org 从「每分钟必须成功」降级为「每 6 小时成功一次即可」；
- 再加 GitHub 自己的 `schedule: 0 * * * *` 兜底，已不存在单点。

`N` 不写死在 shell 里：tick 把下一拍间隔写进 `.focus_interval`，循环读它并夹在
`[30, 300]` 秒。否则 `focus_config.LINEUP_POLICY` 里的 30/60/120/300 秒配置全是摆设。

### 7.2 状态与缓存分离（防仓库膨胀）

第一版把 `calendar` + `official_fixtures` 也写进 `focus_state.json`，实测 **176KB**：

| 键 | 大小 |
|---|---|
| `official_fixtures`（380 场） | 101,332 B |
| `calendar`（6 场焦点战 + 全部赛程） | 23,694 B |
| `matches`（真正的状态） | **442 B** |

每 15 分钟提交一次 = 每天 3.5MB 的 git 对象，几周就把仓库撑坏。
现改为：

- `focus_state.json`（进仓库）：只有 `matches` / `last_tick_at` / `ticks`，实测 **1.9KB**；
- `.focus_cache.json`（`.gitignore`）：日历 + 官方赛程，只在一个 6 小时 job 内有效；
- 旧仓库第一次跑会自动剔除遗留的缓存字段（`LEGACY_STATE_KEYS`）。

### 7.3 当晚实测（2026-10-31，三场 23:00 北京开球）

| 观测项 | 实测值 |
|---|---|
| 官方裁判组首次可见 | 开球前 **59 分钟**（13:50Z 时 `officials=[]`，14:01Z 已齐全） |
| 官方首发首次可见 | 开球前 65 分钟前仍为 `teamLists=[]`，13:55Z 已齐全 |
| 对手最早发布 | 直播吧 `13:46:46Z`（T-73）、懂球帝 `13:49Z` |
| 三场焦点战 | 全部匹配到官方 `fixture_id`，0 错误 |

**这组数字的含义**：官方接口的任命与首发都是**临开球 1 小时左右**才可见的，
和「提前一周知道裁判」不是一回事——football-data 提前约 9 天只给主裁，
官方接口给全 5 个角色但极晚。两者互补，不可替代。

### 7.4 真实运行揪出的 4 个 bug

1. **`render_lineup_message` KeyError**：`pl_api.lineup_for_team` 返回的记录没有
   `match_id`，而文案标题要用它。每一次首发推送都会崩。测试抓到。
2. **官方队名 `Brighton & Hove Albion` 归不到 `Brighton`**：`canon_team` 不做 `& → and`
   归一，导致布莱顿的焦点战**静默**匹配不上官方场次，永远拿不到裁判和首发。
   已补 `&` 归一 + 20 队逐一断言。
3. **V2 把「缺阵型」判成 block**：官方刚公布首发的头几分钟只有 11 个名字、没有
   `formation`。判 block 等于「拿到官方 11 人却不许发」，用户看到「待核验」，
   比我们实际掌握的信息还差。改为：缺阵型 = warn（照发名单），阵型有值但不自洽 = block。
4. **对手采集节流用了全局时间戳**：一场刷完，其余场次全被跳过；多场同时进窗口时
   只有第一场拿得到对手时间。改为按场次节流。

另外修掉一处**数据可信度**问题：懂球帝**列表接口**给的时间会比**文章页**早 85 分钟
（实测：列表 20:24、页面「发布于 21:49」，北京）。直接拿来对比会得出
「对手比我们快一个半小时」的假结论。现在会对最早的 2 条候选额外请求文章页校正，
并在文案里注明「列表接口原报 X，已按文章页时间校正」。

### 7.5 回归

```
python tests/run_all.py
模块数 18 | 用例 611 | 失败 0 | 错误 0
```

原有 537 例一条未改、一条未删、断言未放宽；新增 74 例
（`test_focus_tick.py` 65 例 + 队名归一 2 例 + V2 分级 2 例 + 时间校正 6 例 - 去重计数）。

**仍未完成**：真实比赛日的「我方检测时间 vs 对手时间」四列速度记录还需要
`focus.yml` 连续跑满一场才能产出——本轮 dry-run 时我方尚未持续轮询，
`first_detected_at` 晚于对手，因此**不宣称领先**（这正是纪律要求的正确行为）。

### 7.6 生产环境实据（2026-10-31 14:13Z 起，连续运行）

`focus.yml` 启动后 15 分钟，仓库里的 `focus_state.json` 已经能证明整条链路在工作：

```
ticks: 31          last_tick_at: 2026-10-31T14:29:00Z     # 31 拍 / 15 分钟 ≈ 30 秒一拍
epl-20261031-bournemouth-leeds      裁判已推=True
epl-20261031-coventry-sunderland    裁判已推=True
epl-20261031-mancity-brighton       裁判已推=True  首发已推=[Brighton, Man City]
                                    首次检测=14:14:00Z   对手最早=13:46:46Z(zhibo8)
```

**今晚的速度结论：不宣称领先。** 我们 14:14:00Z 检测到，直播吧 13:46:46Z 就发了，
晚 27 分钟。原因不是机制不行，而是 **job 14:13:47 才被启动，而首发 13:45 左右就公布了**。
`first_detected_at` 是「我们第一次看到的时刻」，系统没跑的时候它只能是启动时刻。

**任命真实公布窗口**：13:50:38Z 时三场 `officials` 全为空，14:01:22Z 已齐全
→ 官方裁判组的真实可见时刻落在 **T-70 ~ T-59 分钟**之间（不是提前一周）。
football-data 提前约 9 天只给主裁，两者互补。

**今晚的覆盖缺口（诚实记录）**：20:30 北京（12:30Z）的 Chelsea vs Man Utd 是 B 级，
按 `MONITOR_POLICY` 只监控裁判、不抓首发。但它 12:30Z 就开球了，而 focus 14:13Z
才启动，早已过了「开球后 30 分钟」的 post 窗口 → 这场连裁判报告也没赶上。
这是**启动太晚**造成的覆盖缺口，不是分级规则的问题。

### 7.7 「用户零操作」：让 scanner 当保姆

原来要靠人工点 Run workflow、或让用户去 cron-job.org 加第二条任务。两条都依赖人。
而 `scanner.yml` 已经被 cron-job.org 每分钟叫一次，于是让它顺手做一件事：

```yaml
- name: Keep focus watch alive
  env:
    GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    GH_REPO: ${{ github.repository }}
  run: python3 scripts/ensure_focus_running.py
```

判断规则：**只要后面没有 pending run 就补一个**。
GitHub 的 concurrency 只保留「1 个运行中 + 1 个 pending」，所以始终留一个 pending，
当前 job 一结束它就立刻接管，**零空档**。若等到「一个都不剩」才补，
就会有最多 1 分钟的裸奔窗口——正好可能落在首发公布那一刻。

能这么做的原因：`workflow_dispatch` 是 GitHub 明文规定的例外，
**用 GITHUB_TOKEN 触发的 `workflow_dispatch` / `repository_dispatch` 会真的创建 run**
（其它事件不会，那是防递归）。前提是 job 拿到 `permissions: actions: write`。

实测：`Keep focus watch alive` 步骤 `completed/success`，并在 14:17:13Z 补出了
pending run（id 41209419419）。当前稳态就是 1 running + 1 pending。
