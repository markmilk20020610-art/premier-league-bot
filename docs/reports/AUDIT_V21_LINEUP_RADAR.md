# V2.1 首发情报雷达 · 第一阶段审计报告

> 审计对象：`premier-league-bot` @ `f05ba29`
> 审计范围：《越位情报局 V2.1 新增模块：首发情报雷达 Lineup Intelligence Radar》§一 – §十三
> 审计纪律：**本阶段未修改任何生产代码**。所有结论来自本机实测请求，未实测的一律标注 `UNVERIFIED`。
> 审计时间：2026-10-31（UTC）
> 基线：19 个测试模块 / 630 个用例 / 全绿 / 96.7s

---

## 零、结论先行（六条，按重要性排序）

1. **V2.1 的前提被实测部分推翻。** 任务书假设「懂球帝 21:50 首发、我们 22:14 拿到、落后 24 分钟」的原因是**情报源不够快**。实测 Esteemed Kompany 那篇文章的 `article:published_time` = `2026-10-31T13:48:39+00:00`，正文写的是 "have **confirmed** their starting lineups"，并内嵌 `@BHAFC` 官方 TEAM NEWS 推文——**它是转述官方，不是提前泄露**。真正的瓶颈是**我方覆盖窗口有断档**（14:13:47 才开始跑），不是缺跟队记者。

2. **V5 归属校验的失败是「我们自己丢字段」造成的，不是数据源不给。** 官方 `teamLists[i].lineup[j].id` 与我方 `/players?compSeasons=841&teams={id}` 返回的 `content[k].id` **实测 11/11 完全对齐**。V5 现在只能 `warn`，是因为 `parse_team_lists()` 只取了 `name.display`，把 `id` 扔了。

3. **但「在名单里」≠「现在属于这队」。** 曼城 62 人名单里，Akanji 的 `currentTeam.id = 241`、Aké 的 `currentTeam.id = 366`——都是外租/已转会球员。归属判定必须用 `currentTeam.id == teamId`，不能用「名字在不在列表里」。

4. **零成本源覆盖率 16/20，缺口 3 队（Brighton / Chelsea / Hull City）**，其中 Brighton 与 Hull 的官网从本机 **TLS 证书校验失败**（不是 404，是连不上）。这三队只能靠 Google News RSS 兜底。

5. **X 平台在本机完全不可用，且不是配置问题。** 三条免 key 通道全废（syndication 429 / nitter 410 / fxtwitter 无时间线）。任务书要求的「监控跟队记者 X 账号」在当前技术条件下**做不到**，必须降级为「从文章里抽作者 + Google News 按人名检索」。**我不会假装能做到。**

6. **官方 PL API 存在可观测的滞后，且俱乐部自宣可能早于 API。** 13:48:39 一个球迷站已经在引述官方首发，而我方 13:50:38Z 拉到的 `teamLists` 仍是空数组。这条只证明「API 不是最快通道」，**不能**据此推算滞后的具体分钟数（样本不足，且我方 13:52–14:13 没有在跑）。

---

## 一、审计方法与证据分级

| 级别 | 含义 | 本报告中的用法 |
|---|---|---|
| **M** | 本机实测（真发了 HTTP，记录了状态码/字节数/字段） | 全部 A/B/C/D/E/F 结论 |
| **C** | 代码阅读（可指到文件行号） | A 的流程描述、H 的状态迁移 |
| **U** | 未验证 / 无法验证 | 明确标 `UNVERIFIED`，不写成结论 |

**没有使用任何记忆或推测填充 E（源清单）。** 每一条 RSS 都是真请求、真解析、真计数；解析不出来的（返回 200 但内容为 HTML 的软 404）一律记为不可用。

---

## 二、A. 当前首发数据采集流程（代码级）

### A.1 调用链

```
focus.yml (GitHub Actions, 自循环 5.8h, 单 job)
  └─ while 循环，每拍:
       python scripts/focus_tick.py
         ├─ get_calendar()        TTL 30min，读 match_calendar → focus_matches
         ├─ get_official_fixtures() TTL 360min，pl_api.fetch_fixtures()（380 场）
         ├─ _fixture_for()        按 canon_team + kickoff ±20min 找官方 fixture_id
         └─ for match in focus_matches:
              ├─ handle_referee()   ← 裁判组，T-70 ~ T-59 实测窗口
              ├─ handle_lineups()   ← **本次审计主体**
              └─ refresh_competitor() 节流 120s，3 源 + ≤2 次页面校正
```

### A.2 `handle_lineups()` 的实际行为（`scripts/focus_tick.py:289`）

1. 拿 `detail`（官方 fixture detail）→ 若为空直接返回。
2. 用 `mc.canon_team()` 把官方队名映射到我方展示名（`Brighton & Hove Albion` → `Brighton`）。
3. 逐队调 `pl_api.lineup_for_team()`，返回 `None` 视为「官方还没公布」，**静默跳过**。
4. `lm.players_hash(players)` 与 `ms["lineup_pushed"][team]` 比对，同版跳过。
5. `lm.validate_lineup()` 跑九项校验 → `decide_status()` → `decide_action()`。
6. 通过则渲染文案 + 推送，成功后写 `lineup_pushed[team] = hash`。

### A.3 频率策略（`focus_config.py:265`）

| 阶段 | 触发条件 | 间隔 |
|---|---|---|
| idle | > T-120 或 < T-post | 300s |
| warmup | T-120 → T-90 | 120s |
| intensify | T-90 → T-75 | 60s |
| critical | T-75 → 开球 | 30s |
| post | 开球后 30min 内 | 30s |

连续失败 5 次触发指数退避（防封）。**这一层设计是对的，V2.1 不该动它。**

### A.4 A 阶段审计发现（3 个问题）

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| A-1 | **覆盖窗口有断档**：job 结束到下一个 job 接管之间可能空几分钟；cron-job.org 5min 时更糟 | 10-31 当晚 job 14:13:47Z 才启动，而首发 13:45 左右已公布 | 首发推送晚 27 分钟，与情报源无关 |
| A-2 | **官方 API 是唯一入口**，没有任何「提前情报源」通道 | 代码里 `source_tier` 恒为 1，`ST_REPORTED` 从未被赋值 | 拿到官方之前完全静默 |
| A-3 | **`lineup_for_team` 用队名字符串匹配**（`pl_api.py:282`），没有用 `teamId` | 官方 `teams[i].team.id` 就在 payload 里 | 队名一旦改写（`Spurs`/`Tottenham`）就静默失效 |

---

## 三、B. 官方 Premier League API 原始数据结构（实测）

**端点**：`GET https://footballapi.pulselive.com/football/fixtures/129010?altIds=true`
**实测**：HTTP 200，耗时 **968ms**，`kickoff.millis` → `2026-10-31 15:00:00Z`

### B.1 顶层键（22 个）

```
_source, altIds, behindClosedDoors, clock, events, extraTime, fixtureType,
gameweek, ground, halfTimeScore, id, kickoff, matchOfficials, metadata,
neutralGround, penaltyShootouts, phase, provisionalKickoff, replay,
shootout, status, teamLists, teams
```

### B.2 `teamLists[i]`（每队一个对象）

```
['formation', 'lineup', 'substitutes', 'teamId']
```

实测值：`teamId` = `11`（Man City）/ `131`（Brighton）；两队 `lineup` 各 11 人；
`formation.label` = `4-2-3-1`；`formation.players` = `[[22857],[76204,137371,16431,67178],[51593,141425],[31936,63741,54156],[65970]]`
—— **按 ID 分好组的二维数组，不是按人数猜的切片**。

### B.3 `lineup[j]`（球员对象）

```
['matchPosition', 'matchShirtNumber', 'captain', 'playerId', 'info',
 'nationalTeam', 'birth', 'age', 'name', 'id', 'altIds']
```

| 字段 | 实测值 | 说明 |
|---|---|---|
| `id` | `16431` | **真实球员 ID，可与 `/players` 对齐** |
| `playerId` | `0`（11 人全为 0） | **不可用**，别拿它做连接键 |
| `altIds.opta` | `p171314` | Opta 侧 ID |
| `name.display` | `"Erling Haaland"` 等 | 我方当前唯一取用的字段 |
| `matchPosition` | `"F"` 等 | 位置码 |
| `captain` | `true/false` | 队长标记 |

### B.4 未开赛时的形态（重要）

`teamLists` 为 `[null, null]`——**不是空对象**。`parse_team_lists()` 已有 `if not isinstance(tl, dict): continue` 兜底（`pl_api.py:173`），这一处是对的。

---

## 四、C. V5 球员归属验证失败原因（根因已定位，解法已实测）

### C.1 根因

`pl_api.py:177`：

```python
players = [((p.get("name") or {}).get("display") or "").strip() for p in lineup]
```

**只取了名字，`id` 被丢掉。** 于是下游 `validate_lineup(record, squad=None)` 的 `squad` 永远是 `None`，走到 `lineup_monitor.py:299`：

```python
add("V5", "warn", "无该队阵容名单，球员归属未核验")
```

生产日志「V5 无该队阵容名单，球员归属未核验」就是这么来的。**数据源给了 ID，是我们没接。**

### C.2 解法实测（零成本，官方自有端点）

**端点**：`GET /football/players?compSeasons=841&teams={teamId}&pageSize=100`

| 队伍 | teamId | 返回人数 | 耗时 |
|---|---|---|---|
| Manchester City | 11 | **62** | 1293ms |
| Brighton & Hove Albion | 131 | **62** | ~1300ms |

返回结构：`{pageInfo, content[]}`，`content[k]` 含 `id` / `playerId` / `name.display` / `currentTeam.id`。

> ⚠️ **坑 1**：不带 `compSeasons=841` 会返回历史全部球员（实测 100 条，第一条是 2013 年的 Djamel Abdoun）。**必须带赛季参数。**
> ⚠️ **坑 2**：`currentTeam.id` 与 `id` 是 **float**（`11.0` / `71432.0`），必须 `int()` 后再比较。

### C.3 连接验证（决定性证据）

| 检验项 | 结果 |
|---|---|
| `lineup[j].id` ∈ `content[k].id` 集合 | **11 / 11** ✅ |
| `lineup[j].id` ∈ `content[k].playerId` 集合 | **0 / 11** ❌ |

**结论：`lineup[j].id` ↔ `content[k].id` 是正确的连接键。**

### C.4 但「在名单里」不等于「属于这队」（新发现）

曼城 62 人名单里：

| 球员 | `id` | `currentTeam.id` | 实际归属 |
|---|---|---|---|
| Rayan Aït-Nouri | 71432 | 11 | 曼城 ✅ |
| Manuel Akanji | 15656 | **241** | 已不在曼城 ⚠️ |
| Nathan Aké | 4499 | **366** | 已不在曼城 ⚠️ |

`teams=11` 返回的是「本赛季与曼城有关联的球员」，**包含外租与已转会者**。

**归属判定规则（必须这样写）：**

```
currentTeam.id == teamId        → 确认属于该队
currentTeam.id != teamId        → 属于其他俱乐部 → V5 应报 block（名单里出现了别队球员）
名单里查不到 + 需进一步确认      → 回退 /players/{id}?altIds=true 看 currentTeam
```

**绝不能**用「名字在 62 人列表里」当通过条件——Akanji 会通过，而他已经不是曼城球员。

### C.5 阵型校验可以不再靠「按人数切片」

`_groups_from_formation()`（`pl_api.py:307`）现在用 `formation.groups`（`[1,4,2,3,1]`）切名字数组，前提是「lineup 数组顺序 == formation.players 顺序」。这个前提**本次实测成立**，但没有被断言保证。

V2.1 应改为：直接用 `formation.players` 的 **ID 分组** + `lineup[].id` 反查名字，得到「谁在哪一组」的确定答案。这样 V2/V6 从「推断」升级为「读取」。

### C.6 被排除的方案（都实测过）

| 方案 | 排除原因 |
|---|---|
| FPL `bootstrap-static` | `code` ≠ 官方 `playerId`（Raya: FPL `154561` vs 官方 `16431`）；名单里**查不到 Rúben Dias** |
| `/teams/11/compseasons/841/players` | 404 |
| `/clubs/11/teams` | 404 |
| `/teams/11/players` | 404 |
| `/compseasons/841/teams?pageSize=50` | 返回 20 条 list，无球员 |

---

## 五、D. Esteemed Kompany 这条「提前首发」的核验（结论与任务书相反）

### D.1 时间核验（三处元数据一致，带时区）

| 来源 | 值 |
|---|---|
| RSS `pubDate` | `Sat, 31 Oct 2026 13:48:39 +0000` |
| HTML `article:published_time` | `2026-10-31T13:48:39+00:00` |
| JSON-LD `datePublished` | `2026-10-31T13:48:39+00:00` |
| JSON-LD `dateModified` | 同值（未修改过） |

**时区明确为 UTC（`+0000`），不是模糊的「页面显示时间」。** 换算北京时间 = **21:48:39**。

- 标题：`The starting lineups are in as Manchester City take on Brighton yet again`
- 作者：`Adam Marrett`（RSS `dc:creator` 与页面 `meta[name=author]` 一致）
- URL：`https://esteemedkompany.com/2026/10/31/the-starting-lineups-are-in-as-manchester-city-take-on-brighton-yet-again/`

### D.2 内容核验（决定性）

正文写的是：

> "Manchester City and Brighton **have confirmed** their starting lineups"

并**内嵌 `@BHAFC` 官方 TEAM NEWS 推文**。

**这不是泄露，是转述。** 官方在 13:48:39 之前就已经公布了。这篇文的性质是**二手复述**，`source_tier` 应判 3（媒体），**不得**作为「提前情报源」计入领先时间。

### D.3 与任务书 24 分钟差距的真实归因

| 时间（UTC） | 事件 | 来源 |
|---|---|---|
| ~13:45（推定） | 俱乐部自宣首发 | 由 D.2 反推，**UNVERIFIED 具体时刻** |
| **13:48:39** | Esteemed Kompany 转述官方 | 实测 M |
| 13:50:38 | 我方拉 PL API，`teamLists` **仍为空** | 生产日志 M |
| 14:01:22 | 我方拉 PL API，裁判组齐全（首发未记录） | 生产日志 M |
| 13:52 – 14:13 | **我方无任何进程在跑**（job 空档） | workflow 日志 M |
| **14:13:47** | focus job 启动 | M |
| **14:14:00** | 首次读到官方首发 | `focus_state.json` M |

**归因结论（三条并列，不合并）：**

1. **主因是覆盖断档**：13:52–14:13 的 21 分钟里我们根本没在看。这 21 分钟比任何「情报源」都快不起来。
2. **次因是 PL API 滞后**：13:48 俱乐部已自宣，13:50:38 API 仍空。滞后长度**无法从现有日志推算**（我方 13:52 就停了）。
3. **与「没有跟队记者源」无关**：唯一一个球迷站是转述官方的，它也没比官方快。

> 这条推翻了任务书的核心假设，但不推翻 V2.1 的价值——**提前情报源的价值在「官方公布前的 T-6h ~ T-90m」**，而这一整段我们现在是空白的。D 只证明「这一条不是提前情报」，不证明「所有提前情报都不存在」。

---

## 六、E. 可监控的球队专属媒体及记者来源清单（全部实测）

### E.1 官方俱乐部站点 RSS（20 队中 10 队可用）

| 队伍 | URL | 状态 | 条目 | 最新条目时间 |
|---|---|---|---|---|
| Aston Villa | `https://www.avfc.co.uk/rss.xml` | 200 | 20 | 2026-10-31 12:00 |
| Bournemouth | `https://www.afcb.co.uk/rss.xml` | 200 | 20 | 2026-10-31 13:48:46 |
| Brentford | `https://www.brentfordfc.com/rss.xml` | 200 | 100 | 2026-10-31 12:45 |
| Coventry City | `https://www.ccfc.co.uk/rss.xml` | 200 | 20 | 2026-10-31 10:59:49 |
| Crystal Palace | `https://www.cpfc.co.uk/rss.xml` | 200 | 200 | 2026-10-31 11:35:32 |
| Everton | `https://www.evertonfc.com/rss.xml` | 200 | 20 | 2026-10-31 12:30 |
| Fulham | `https://www.fulhamfc.com/rss.xml` | 200 | 20 | 2026-10-30 18:23 |
| Ipswich Town | `https://www.itfc.co.uk/rss.xml` | 200 | 20 | 2026-10-31 13:47:19 |
| Nottingham Forest | `https://www.nottinghamforest.co.uk/rss.xml` | 200 | 20 | 2026-10-31 13:45 |
| Sunderland | `https://www.safc.com/rss.xml` | 200 | 20 | 2026-10-31 13:40 |

**规律**：能用的 10 家都是 `application/xml` 直出。**剩下 10 家全部是 SPA 站点**，任何 `/feed` `/rss` 路径都返回 `200 + text/html`（软 404），**不是 404**——用状态码判断会全部误判为可用。

### E.2 球队专属媒体 / 球迷站（11 条可用）

| 队伍 | 站点 | URL | 状态 | 条目 |
|---|---|---|---|---|
| Man City | Esteemed Kompany | `https://esteemedkompany.com/feed/` | 200 | 10 |
| Liverpool | This Is Anfield | `https://www.thisisanfield.com/feed/` | 200 | 16 |
| Liverpool | Empire of the Kop | `https://www.empireofthekop.com/feed/` | 200 | 15 |
| Arsenal | Arseblog | `https://arseblog.com/feed/` | 200 | 20 |
| Arsenal | Arsenal Station | `https://www.arsenalstation.com/feed/` | 200 | 10 |
| Man Utd | Stretty News | `https://www.strettynews.com/feed/` | 200 | 18 |
| Man Utd | The Peoples Person | `https://thepeoplesperson.com/feed/` | 200 | 10 |
| Tottenham | Spurs Web | `https://www.spurs-web.com/feed/` | 200 | 20 |
| Newcastle | The Mag | `https://www.themag.co.uk/feed/` | 200 | 10 |
| Leeds | Leeds All Over | `https://www.leedsallover.com/feed/` | 200 | 10 |
| Brentford | Beesotted | `https://beesotted.com/feed/` | 200 | 6 |

**实测失败（不要写进配置）**：`bluemoon-mcfc.co.uk/feed` 404、`liverpool.com/rss` 404、
`weaintgotnohistory.sbnation.com/rss` 404、`royalbluemersey.sbnation.com/rss` 404、
`rokerreport.sbnation.com/rss` 404、`7500toholte.sbnation.com/rss` 404（**SB Nation 整条线已下线**）、
`nufcblog.com/feed` 404、`wearebrighton.com/feed` 404、`holmesdale.net/feed` 404、
`hammyend.com/feed` 404、`twtd.co.uk/rss` 404、`cartilagofree.net/feed` URLError、
`thechelseachronicle.com/feed` URLError。

### E.3 覆盖率结论

| 状态 | 队伍数 | 队伍 |
|---|---|---|
| 有官方 RSS | 10 | Villa / Bournemouth / Brentford / Coventry / Palace / Everton / Fulham / Ipswich / Forest / Sunderland |
| 仅有球迷站 | 6 | Arsenal / Liverpool / Man City / Man Utd / Newcastle / Leeds / Tottenham（7 队，Arsenal 与 Spurs 各 1，Liverpool 与 Man Utd 各 2） |
| **完全无专属源** | **3** | **Brighton / Chelsea / Hull City** |

**16/20 = 80% 覆盖。缺口 3 队的具体原因（实测）：**

| 队伍 | 实测情况 |
|---|---|
| Brighton | `brightonandhovealbion.com` **TLS 证书校验失败**（`CERTIFICATE_VERIFY_FAILED`），所有路径都连不上，不是 404。`theseagulls.co.uk/feed` 返回 200 但内容是 HTML。 |
| Chelsea | 官网首页 **HTTP 500**；`/rss.xml` `/en/rss` 均 404；`news?format=feed` 返回 200 但是 HTML；`thechelseachronicle.com` URLError。 |
| Hull City | `hullcitytigers.com` TLS 校验失败；`hullcity-mad.co.uk/rss` 404；`hullcityafc.net` URLError。 |

### E.4 缺口兜底：Google News RSS（实测可用，20 队通用）

```
https://news.google.com/rss/search?q={query}&hl=en-GB&gl=GB&ceid=GB:en
```

| 查询 | 结果 | 首条标题（实测） |
|---|---|---|
| `"Brighton" "starting XI" OR "team news"` | 200 / 100 条 | `Man City XI vs Brighton: Confirmed team news and predicted l…` |
| `"Chelsea" "starting XI" OR "team news"` | 200 / 100 条 | `Chelsea XI vs Man Utd: Predicted lineup and confirmed team n…` |
| `"Hull City" "team news"` | 200 / 92 条 | `Hull City vs Ipswich Town: Premier League preview, team news…` |
| `site:mancity.com` | 200 / 100 条 | `Xabi Alonso confirms Chelsea team news for Manchester United` |
| `site:chelseafc.com` | 200 / 100 条 | `Xabi Alonso confirms Chelsea team news for Manchester United` |

> ⚠️ **限制（必须写进文档）**：Google News 的 `pubDate` 是**索引时间**，不是原文发布时间；
> `link` 是 `news.google.com` 跳转，**不是原文 URL**；`item` **没有作者字段**。
> 所以它只能做**发现通道**，绝不能用于速度证明。

### E.5 交叉验证层（实测）

| 源 | URL | 状态 | 条目 |
|---|---|---|---|
| BBC Sport Football | `https://www.bbc.co.uk/sport/football/rss.xml` | 200 | 75 |
| Sky Sports PL | `https://www.skysports.com/rss/12040` | 200 | 20 |
| The Guardian PL | `https://www.theguardian.com/football/premierleague/rss` | 200 | 20 |
| ESPN Soccer | `https://www.espn.com/espn/rss/soccer/news` | **202** | 0（拒绝） |

### E.6 robots.txt 合规（全部实测）

| 站点 | Disallow 条数 | 是否封禁 feed |
|---|---|---|
| avfc / afcb / brentfordfc / ccfc / cpfc / evertonfc / fulhamfc / itfc / safc / nottinghamforest | 0 | 否 |
| esteemedkompany | 6 | 否 |
| thisisanfield | 3 | 否 |
| empireofthekop | 6 | 否 |
| arseblog | 32 | 否 |
| arsenalstation | 2 | 否 |
| strettynews | 6 | 否 |
| thepeoplesperson | 1 | 否 |
| spurs-web | 7 | 否 |
| themag | 1 | 否 |
| leedsallover | 1 | 否 |
| beesotted | 0 | 否 |
| news.google.com | 3 | 否 |
| bbc.co.uk | 96 | 否 |
| skysports.com | 62 | 否（仅封 `/ical-feeds/`） |

**结论：全部 15 个源的 feed 路径未被 robots.txt 禁止。** 但 robots 允许 ≠ 允许高频抓取，仍需自律节流（见 I.4）。

### E.7 记者层：能做到什么，做不到什么

**做不到（实测）**：X 平台三条免 key 通道全废，详见 F.1。**没有任何合法免费方式按账号读 X 时间线。**

**能做到（实测）**：从文章页抽作者。

| 站点 | RSS `dc:creator` | 页面 `meta[author]` | 页面 `article:published_time` |
|---|---|---|---|
| Esteemed Kompany | `Adam Marrett` | `Adam Marrett` | `2026-10-31T14:19:10+00:00` |
| This Is Anfield | `Sam Millne` | `Sam Millne` | `2026-10-31T14:56:01+00:00` |
| Arseblog | `arseblog` | `arseblog` | `2026-10-31T07:58:50+00:00` |
| Spurs Web | `Bharad Thirumalai` | `Bharad Thirumalai` | `2026-10-31T13:59:01+00:00` |
| Stretty News | `Stretty News Editor` | `Stretty News Editor` | `2026-10-31T10:48:34+00:00` |
| The Mag | `Joe Dixon` | （无） | **（无）** |

**6/6 能拿到作者；5/6 能拿到带时区的发布时间。** 所以「跟队记者」这一层应实现为：

> **作者级订阅** = 从已确认源的文章中提取 `author`，建立 `author_id`，按作者聚合其历史命中率。
> 而不是「去 X 上关注某个账号」——后者当前技术上不可行。

---

## 七、F. 公开 X、网站与其他平台的可用监控方式及限制（实测）

### F.1 X / Twitter：**全部不可用**

| 通道 | 实测结果 | 判定 |
|---|---|---|
| `syndication.twitter.com/srv/timeline-profile/screen-name/{user}` | **429 Too Many Requests** | 不可用 |
| `nitter.net/{user}/rss` | **410 Gone** | 已下线 |
| `api.fxtwitter.com/{user}` | 200，但**只返回用户资料，无时间线** | 不可用 |
| 官方 API v2 | 需 Bearer Token，免费档 **无法读时间线**（仅发推） | **需付费/申请，明确报告** |

**结论：任务书 §三「监控跟队记者 X 账号」在当前条件下不可实现。** 我不会写一个「假装能监控 X」的模块。

### F.2 Telegram：本机不可达

`t.me/s/{channel}` 从本机发起 **URLError（网络层不可达）**。不是 404，是连不上。
→ 部署在 GitHub Actions（美国 IP）上**可能**可达，但本机无法验证，标 **UNVERIFIED**。不建议作为主通道。

### F.3 网站 RSS：可行，是唯一稳定层

见 E.1 / E.2 / E.5。**共 25 个可用 feed（10 官方 + 11 专属 + 4 交叉，ESPN 排除）。**

### F.4 网页直抓：可行但有代价

- 已实现并验证：懂球帝文章页 `发布于 YYYY-MM-DD HH:MM` 时间校正（`competitor_watch.py`）。
- 通用文章页可稳定取到：`article:published_time`（5/6 站点）、`meta[author]`（6/6）。
- 代价：每次 1 个请求 + ~200KB 流量；**不能对每个 feed 条目都抓**，只对「候选首发条目」抓前 2 条。

### F.5 结论表：V2.1 三层体系的技术可行性

| 层 | 任务书要求 | 实测可行性 | 落地方式 |
|---|---|---|---|
| 提前情报源 | 跟队记者 / 球队专属媒体 | **部分可行**：11 个球迷站 feed + 作者抽取；X 不可行 | RSS + 作者聚合，**放弃 X** |
| 交叉验证源 | BBC / Sky / Guardian | **完全可行** | 4 个 RSS |
| 官方确认源 | PL API + 俱乐部官网 | **可行**：PL API 稳定；官网 10/20 有 RSS | 保留，**不降低其核验地位** |

---

## 八、G. 记者消息来源独立性识别方案

### G.1 要防的是什么（真实案例）

同一篇稿件可能以 5 种形态出现：

1. 记者本人在 X 发一条 → 本站发一篇 → 通讯社转发 → 3 家聚合站转载 → 懂球帝/直播吧翻译。
2. 球迷站**转述官方**（如 D 节的 Esteemed Kompany），看起来像独立情报，实际是官方回声。
3. 同一记者的两个账号（个人号 + 站点号）各发一次。

**如果把上面任意两条当成「两个独立源」，就会误判 MULTI_SOURCE。**

### G.2 判定模型：三重指纹 + 引用关系

每条情报生成三个指纹：

| 指纹 | 计算方式 | 用途 |
|---|---|---|
| `url_key` | 去 scheme / `www.` / 尾部 `/` / `utm_*` / `amp` / 追踪参数后的 host+path | 完全相同 URL = 同一条 |
| `author_key` | `sha1(canon_author(publisher_domain, author_name))` | 同站点同作者 = 同一人 |
| `body_key` | 正文去标签 → 归一化空白/大小写 → 5-gram shingle → **SimHash 64bit** | 跨站转载识别 |

**两个条目是「同一条消息」当且仅当满足任一：**

```
url_key 相等
OR simhash(body_a, body_b) <= 3        # 汉明距离，实测转载稿通常 ≤2
OR a 的正文里出现 b 的 url_key（引用）
OR author_key 相同 且 |pub_a - pub_b| <= 30min
```

**两个源「独立」当且仅当：**

```
publisher_domain 不同
AND author_key 不同
AND body_key 不相似
AND 互不引用
```

### G.3 关键规则：**引用官方的，不算独立源**

Esteemed Kompany 那篇内嵌 `@BHAFC` 推文 → 正文含 `twitter.com/BHAFC` 或引用块 →
**必须标 `derived_from=official`，`source_tier=3`，且不计入 MULTI_SOURCE 的独立计数。**

实现方式（无需 NLP）：

```python
OFFICIAL_HINTS = ("confirmed their starting lineups", "have confirmed",
                  "official team news", "as announced by the club")
OFFICIAL_LINK_RE = re.compile(r"(twitter\.com|x\.com)/([A-Za-z0-9_]+)/status/")
# 命中任一 → derived_from = official
```

再加一条硬规则：**任何 `derived_from=official` 的条目，状态最高只能到 `REPORTED`，永远不能自己升级为 `MULTI_SOURCE`。**

### G.4 记者身份表（V2.1 落地形态）

```
author_id       sha1(publisher + name)        稳定主键
display_name    原始写法
publisher       所属站点
first_seen_at
articles        已见文章 url_key 列表
hit_count       该作者历史上「首发相关」文章数
correct_count   事后被官方证实的次数
lead_time_avg   相对官方公布的平均提前分钟数（只有 >=3 个样本才计算）
enabled         人工开关
```

**明确不使用的信号（任务书写明禁止）：粉丝数、媒体知名度。** 本方案只用「事后是否被官方证实」这一个可计量指标。

---

## 九、H. 提前首发六态状态机设计

### H.1 与现有状态的关系

现有 `focus_config.LINEUP_STATUS` 有 6 个值（`unknown / not_published / predicted / reported / unconfirmed / official_confirmed`），但 `decide_status()` **从不返回 `predicted`**，也**没有** `multi_source / corrected / rejected`。

V2.1 需要**新增 4 个状态**并**保留旧值映射**（避免破坏 630 个已有测试）：

| V2.1 状态 | 映射到旧值 | 可发布 | 可写「官方确认」 |
|---|---|---|---|
| `PREDICTED` | `predicted` | 仅标注预测 | ❌ |
| `REPORTED` | `reported` | ✅ 带「单源未确认」 | ❌ |
| `MULTI_SOURCE` | `reported`（+ `multi_source=True`） | ✅ 带「N 源一致」 | ❌ |
| `OFFICIAL` | `official_confirmed` | ✅ | ✅ |
| `CORRECTED` | `official_confirmed`（+ `correction_of`） | ✅ 必发更正 | ✅（新版本） |
| `REJECTED` | `unconfirmed` | ❌ 只发链接 | ❌ |

### H.2 迁移图（含守卫条件）

```
                    ┌──────────────┐
   [无情报] ───────► │  (初始空态)   │
                    └──────┬───────┘
                           │ 解析出 11 人
                           ▼
        ┌──────────────────────────────────────┐
        │  证据强度单调上升，只允许：            │
        │  PREDICTED → REPORTED → MULTI_SOURCE  │
        │           → OFFICIAL                  │
        └──────────────────────────────────────┘

PREDICTED ──(来源声明为预测)──────────────► 停留
    │
    │ 来源是「宣称掌握名单」的报道，且非预测措辞
    ▼
REPORTED ──(≥2 个独立源、11 人集合完全一致)──► MULTI_SOURCE
    │                                              │
    │ 官方源出现且九项校验无 block                    │
    ▼                                              ▼
OFFICIAL ◄─────────────────────────────────────────┘
    │
    ├─(官方名单与已发的 REPORTED/MULTI_SOURCE 不同)──► CORRECTED（必发更正）
    │
    └─(校验出现 block：人数≠11 / 重复 / 他队球员 / 两队重叠)──► REJECTED（不发名单）
```

### H.3 守卫条件（每一条都要有测试）

| 迁移 | 守卫 |
|---|---|
| → `PREDICTED` | 标题/正文含 `predicted / possible XI / could line up / expected XI` |
| → `REPORTED` | 单一源；非预测措辞；解析出 11 人；V1/V3/V4 无 block |
| `REPORTED` → `MULTI_SOURCE` | **独立源数 ≥ 2**（按 G.2 判定）；11 人 `players_hash` 完全一致；无 `derived_from=official` |
| → `OFFICIAL` | `source_tier == 1`；有 `source_url`；九项校验无 block；**且 `derived_from != official` 的反面**——即必须是官方本体 |
| `REPORTED/MULTI_SOURCE` → `CORRECTED` | 官方 11 人 hash ≠ 已发 hash |
| 任意 → `REJECTED` | 任一 block 级问题 |
| **禁止** | `PREDICTED` 或 `REPORTED` 直接跳 `OFFICIAL`（必须经过独立校验，不允许「媒体说是官方」就升级） |
| **禁止** | `MULTI_SOURCE` 用同一稿件的转载凑数 |
| **禁止** | 降级（`OFFICIAL` → `REPORTED`）；官方更正只能走 `CORRECTED` |

### H.4 每条情报的 13 个字段

```python
{
  "intel_id":            sha1(url_key + players_hash)[:16],
  "match_id":            "epl-20261031-mancity-brighton",
  "team":                "Man City",
  "status":              "REPORTED",
  "players":             [...11 个名字...],
  "players_hash":        "b19c1dc4ba2992ea",
  "formation":           "4-2-3-1",
  "source_id":           "esteemedkompany",
  "source_url":          "https://esteemedkompany.com/2026/10/31/...",
  "author_key":          "sha1:...",
  "published_at":        "2026-10-31T13:48:39Z",   # 原文时间，带时区
  "first_seen_at":       "2026-10-31T13:49:02Z",   # 我方首次读到
  "derived_from":        "official",                # official / independent
  "independent_sources": ["thisisanfield"],         # 只放真正独立的
}
```

> **`published_at` 与 `first_seen_at` 必须是两个字段。** 任务书 §十一 明确禁止把「网页最后更新时间」当「首次发布时间」，同理也不能把「我方读到的时间」当「发布时间」。

---

## 十、I. 零成本部署方案（复用现有 GitHub Actions，不新增外部服务）

### I.1 架构（不引入任何新依赖）

```
focus.yml（已有，5.8h 自循环）
  └─ focus_tick.py（已有）
       └─ 新增 lineup_radar.py
            ├─ source_registry.py     ← 25 个已实测 feed 的静态配置
            ├─ feed_poll()            ← 条件 GET（ETag/Last-Modified）
            ├─ intel_parse()          ← 标题/正文 → 11 人 + 状态
            ├─ independence.py        ← G 节的三重指纹
            └─ state_machine.py       ← H 节的六态
```

**不新增**：数据库、消息队列、外部看门狗、付费 API、X 抓取。

### I.2 请求预算（按实测耗时推算）

| 项 | 现状 | V2.1 后 |
|---|---|---|
| 官方 fixture detail | 1 req/拍/场，968ms | 不变 |
| 官方 squad（新增） | 0 | **2 req/场/6h**（TTL 360min），1293ms/次 |
| 专属源 feed | 0 | 4 req/场/拍（2 队 × 2 源） |
| 交叉源 | 0 | 4 req/60s（全局共享，不按场次翻倍） |
| 对手采集 | ≤5 req/120s | 不变 |

**最坏情况（3 场同时 critical，60s 一拍）**：

```
3 场 × 4 feed = 12 req/min + 4 交叉源/min = 16 req/min ≈ 960 req/hour
```

加上条件 GET，稳态下 90% 返回 `304 Not Modified`（响应体 ~200 字节）：
**带宽约 300KB/小时，Actions 运行时间不变。**

### I.3 必须实现的节流（否则会被封）

| 约束 | 值 | 理由 |
|---|---|---|
| 单 feed 最小间隔 | **60s** | 实测这些站都是分钟级更新，更快无收益 |
| 单 feed 每小时上限 | **60 次** | 保守值，远低于任何风控阈值 |
| 遵守 `Retry-After` | 有则必从 | 429 时退避，不退避会被永久封 |
| 连续失败 3 次 | 该源本场次禁用 30min | 与现有 `next_interval` 退避逻辑一致 |
| 条件 GET | `If-None-Match` / `If-Modified-Since` | 省 90% 流量，且是礼貌抓取 |
| User-Agent | 带项目标识，如 `YueweiBot/2.1 (+https://github.com/...)` | 可被站长识别，避免被当恶意爬虫 |

### I.4 成本

| 项 | 成本 |
|---|---|
| GitHub Actions（公开仓库） | **¥0**（公开仓库不计分钟数） |
| 25 个 RSS | **¥0**，无 key |
| PL API | **¥0**，无 key（实测无需鉴权） |
| Google News RSS | **¥0**，无 key |
| X API | **不做**（付费，且免费档读不了时间线） |
| **合计** | **¥0/月** |

### I.5 与「早于对手」的关系（诚实结论）

零成本方案能做到的：

- ✅ **覆盖不断档**：job 5.8h 自循环 + scanner 每分钟补 pending（已上线实测）
- ✅ **官方通道更快**：T-120 起每 30s 轮询，官方一公布就抓
- ✅ **提前情报可见**：官方公布前的媒体名单能读到并标 `REPORTED`
- ❌ **无法保证早于懂球帝**：如果他们转的是同一篇官方公告，双方都在等官方

**唯一能真正「早」的场景**：某记者在官方公布前拿到名单并发布 → 我们靠 RSS 在 60s 内读到 → 标 `REPORTED` 先发。**这个场景本次样本中没有出现，不能宣称已具备。**

---

## 十一、J. 修改文件、测试计划与潜在风险

### J.1 拟修改文件（第二阶段，需确认后才动）

| 文件 | 改动 | 风险 |
|---|---|---|
| `pl_api.py` | `parse_team_lists` 保留 `id`；新增 `fetch_squad()`；`lineup_for_team` 改用 `teamId` 匹配 | **中**：改解析会影响 630 个测试中的 pl_api 用例 |
| `lineup_monitor.py` | 新增 `MULTI_SOURCE/CORRECTED/REJECTED`；`decide_status` 接受独立源数 | **高**：状态机是核心，改错会误发 |
| `scripts/focus_tick.py` | 接入 `lineup_radar`，新增 `intel` 子树到 `ms` | **中**：状态结构变化需同步 `merge_state.py` |
| `scripts/merge_state.py` | `_merge_focus` 增加 `intel` 语义合并（取更早 `first_seen`、并集 `independent_sources`） | **中**：合并错会丢情报 |
| `focus_config.py` | 新增 `LINEUP_SOURCES`、`RADAR_POLICY` | 低 |
| **新增** `lineup_radar.py` | 源注册 + 轮询 + 解析 + 独立性 | 中 |
| **新增** `source_registry.py` | 25 个已实测源（含 `enabled` 开关） | 低 |
| **新增** `tests/test_lineup_radar.py` | 15 条回归 | 低 |
| `.focus_cache.json` 结构 | 新增 `feeds` 子树（ETag/Last-Modified） | 低（已在 `.gitignore`） |

**明确不改**：`focus.yml` 的循环骨架、`next_interval` 退避逻辑、官方源的 `source_tier=1` 地位。

### J.2 建议的 15 条回归测试（每条对应一个实测失败模式）

> 若与任务书 §十二 原文条目不一致，以任务书为准，请指出差异我再对齐。

| # | 测试 | 对应实测事实 |
|---|---|---|
| T1 | `test_parse_team_lists_keeps_player_id` | C.1：`id` 被丢弃导致 V5 永久 warn |
| T2 | `test_lineup_id_joins_squad_id` | C.3：11/11 连接成立 |
| T3 | `test_squad_membership_is_not_belonging` | C.4：Akanji `currentTeam=241` 仍在名单 |
| T4 | `test_squad_requires_comp_season` | C.2 坑1：不带赛季参数返回历史球员 |
| T5 | `test_current_team_id_is_float` | C.2 坑2：`11.0` 必须 `int()` |
| T6 | `test_official_api_lag_does_not_block_radar` | D.3：API 空时媒体源仍能出 REPORTED |
| T7 | `test_repost_is_not_independent_source` | G.2：同 body SimHash ≤3 视为同一条 |
| T8 | `test_same_author_two_accounts_is_one_source` | G.2：`author_key` 相同 → 不计数 |
| T9 | `test_official_derived_article_never_upgrades` | G.3：Esteemed Kompany 类文章最高 REPORTED |
| T10 | `test_reported_cannot_jump_to_official` | H.3：禁止 PREDICTED/REPORTED 直跳 |
| T11 | `test_official_diff_triggers_correction` | H.2：官方与已发不一致 → CORRECTED |
| T12 | `test_rejected_never_publishes_players` | H.3：block 时不发名单只发链接 |
| T13 | `test_soft_404_html_is_not_a_feed` | E.1：`200 + text/html` 必须判不可用 |
| T14 | `test_feed_poll_respects_min_interval` | I.3：单 feed ≥60s |
| T15 | `test_missing_roster_shows_pending_not_pass` | §九：缺失名单必须显示 `ROSTER_VALIDATION_PENDING` |

### J.3 风险清单（按严重度）

| # | 风险 | 严重度 | 缓解 |
|---|---|---|---|
| R1 | **提前情报源价值未被证实**：本次唯一样本是「转述官方」，不是提前 | **高** | 先在 11-02 Liverpool vs Arsenal 实测，拿到真实 lead_time 再决定是否扩源 |
| R2 | 转载冒充独立源 → 误判 MULTI_SOURCE | **高** | G 节三重指纹 + `derived_from` 硬约束 |
| R3 | 用名字判归属 → 别队球员通过 V5 | **高** | 全部改 ID；`currentTeam.id` 校验 |
| R4 | 官方更正后重复推送 | 中 | `lineup_pushed` 存 hash；`CORRECTED` 单独状态 |
| R5 | feed 抓取被风控 | 中 | 60s 间隔 + 条件 GET + `Retry-After` |
| R6 | 状态结构变化破坏 `merge_state` | 中 | `_merge_focus` 加 `intel` 子树，取「宁可多推」语义 |
| R7 | Google News 索引时间被当成发布时间 | 中 | 代码层禁止 `google_news` 源的 `published_at` 进入速度证明 |
| R8 | Actions 运行时长增加触发 6h 上限 | 低 | 单拍耗时需 < 间隔的 50%（现约 2.5s + 新增 ~3s） |
| R9 | Brighton / Hull 的 TLS 失败无法绕过 | 低 | 接受缺口，用 Google News 兜底，**不关证书校验** |
| R10 | 把「网页更新时间」当「发布时间」 | 中 | 只用 `article:published_time` / RSS `pubDate`；取不到就留空标 `UNKNOWN` |

---

## 十二、§九 专项：球员归属核验（`ROSTER_VALIDATION_PENDING`）

### 12.1 现状（7 项检查逐条回答）

| # | 任务书问题 | 审计结论 |
|---|---|---|
| 1 | 是否用了官方名单做归属？ | **否**。`squad` 参数恒为 `None` |
| 2 | 是否只凭姓名判断？ | **是**（也是唯一手段，因为 ID 被丢了） |
| 3 | 名单缺失时是否静默放行？ | **是**。只 `warn`，`decide_action` 仍可能发名单 |
| 4 | 官方接口返回名单了吗？ | **返回了**，`lineup[].id` 齐全 |
| 5 | 球员归属核验与阵型来源是否分别记录？ | **否**，混在 `problems` 一个列表里 |
| 6 | 是否有球员映射表？ | **无** |
| 7 | 缺名单时用户看到什么？ | 文案带「球员归属未核验」，但**状态仍是 `official_confirmed`** |

**第 7 条是最危险的**：明明没核验归属，状态却已经是「官方确认」。

### 12.2 应改为

```
名单可用 + 全部 currentTeam.id == teamId     → V5 pass
名单可用 + 有球员 currentTeam.id != teamId   → V5 block（名单里混入他队球员）
名单不可用（接口失败/超时）                   → 状态显示 ROSTER_VALIDATION_PENDING
                                              不得显示「官方确认」
```

**三个验证维度必须分别记录**（任务书 §九 明确要求）：

```python
"verification": {
    "list_returned":      True,      # 官方接口是否返回名单
    "roster_verified":    True,      # 归属是否核验通过
    "formation_source":   "official", # 阵型来源：official / derived / missing
}
```

---

## 十三、待用户确认的 5 个决策点

1. **是否接受「X 平台放弃」？** 三条免 key 通道实测全废，官方 API 免费档读不了时间线。若不接受，需要你提供付费 API key 或第三方服务账号。
2. **是否接受 16/20 的源覆盖？** Brighton / Chelsea / Hull City 无专属源（TLS 失败 / 官网 500），只能 Google News 兜底。
3. **`derived_from=official` 的文章是否计入「领先时间」？** 我建议**不计入**，否则会把「转述官方」当成自己的成绩。
4. **`ROSTER_VALIDATION_PENDING` 时是否允许发名单？** 我建议**允许发但状态降级为 `REPORTED`**（不发等于把已掌握信息藏起来，但标成官方确认是撒谎）。
5. **11-02 Liverpool vs Arsenal 是否作为首个实测样本？** 这是唯一能拿到真实 `lead_time` 的机会，建议在此之前不动生产代码。

---

## 十四、本阶段交付物清单

| 项 | 状态 |
|---|---|
| A 采集流程 | ✅ 代码级 + 3 个问题 |
| B 官方 API 结构 | ✅ 22 个顶层键 + `teamLists`/`lineup` 全字段 |
| C V5 失败原因 | ✅ 根因 + 实测解法 + 4 个被排除方案 |
| D Esteemed Kompany 核验 | ✅ 三处元数据一致 + **结论反转** |
| E 源清单 | ✅ 25 个 feed 实测 + 3 队缺口 + robots 合规 |
| F 平台可用性 | ✅ X 全废 / Telegram 不可达 / RSS 可用 |
| G 独立性方案 | ✅ 三重指纹 + 引用关系 + 硬规则 |
| H 状态机 | ✅ 六态 + 迁移守卫 + 13 字段 |
| I 部署方案 | ✅ 零成本 + 请求预算 + 节流约束 |
| J 改动与风险 | ✅ 9 个文件 + 15 条测试 + 10 项风险 |
| **生产代码修改** | **❌ 未修改任何一行** |

**等待确认后再进入第二阶段。**
