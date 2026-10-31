# 越位情报局专项审计：F1 非足球新闻混入系统

**审计日期**：2026-10-31
**审计阶段**：第一阶段（只审计，未修改任何代码）
**结论**：确认污染成立，已定位到**唯一物理入口**与**四个结构性缺口**。F1 新闻不仅被采集，还被判定为「多源独立印证」并真实投递（Telegram `message_id = 26`）。

---

## 一、生产事实（非推测）

### 1.1 该新闻确实已被推送

来源：`state.json`（`origin/main`，提交 `a250299`）`events.b03cd11506e7`

```json
{
  "event_id": "b03cd11506e7",
  "title": "维斯塔潘与劳森练习赛碰撞双双被罚退后一位发车",
  "article_title": "Verstappen, Lawson receive grid penalties after 'unacceptable' practice collision",
  "evidence_status": "MULTIPLE_INDEPENDENT_SOURCES",
  "delivery_status": "COMMITTED",
  "telegram_message_id": 26,
  "created_at": "2026-10-31T08:25:36.868982+00:00",
  "chunks": [{"chunk_index": 1, "delivery_status": "DELIVERED", "telegram_message_id": 26}]
}
```

- 证据链接：`https://www.skysports.com/f1/news/12040/13605836/max-verstappen-liam-lawson-receive-grid-penalties-for-unacceptable-practice-collision-at-mexico-city-grand-prix`
- 中文标题是模型生成的，说明 **AI 完整处理了这条稿子并认定其为有效新闻**。
- `evidence_status` 不是 `SINGLE_SOURCE`，而是 `MULTIPLE_INDEPENDENT_SOURCES` —— 它连单源闸门都不需要触发。

### 1.2 对应的生产运行

| 项 | 值 |
|---|---|
| run id | `41184532805` |
| 触发 | `workflow_dispatch`，2026-10-31T08:25:11Z |
| job | `scan`（id `124488612712`），success，耗时 26 秒 |
| 关键步骤 | `Run bot.py (Scan Mode)` success → `Commit state changes (if any)` success |

> 说明：Actions 原始 stdout 需鉴权下载（`/logs` 接口匿名访问返回 **HTTP 403**）。
> 下面 §1.3 的日志行是用**生产同一份 state 快照**在本地跑真实代码复现的，输出与生产逻辑逐字同构，但**不冒充生产 stdout**。

### 1.3 复现日志（真实代码、真实数据）

```
[1] 证据核验: MULTIPLE_INDEPENDENT_SOURCES | 独立来源 2 个 | 2 个独立来源交叉印证（skysports.com, skysports.com）
[2] 单源门 auto_push_allowed: (True, '')
[3] 红线门 _red_line_gate:   (True, '', '')
[4] 完整闸门 _evidence_gates:(True, '', '')
[5] 评级复核 enforce_rating(Tier1): ('TIER1', False, '')   ← 未降级
```

三道闸门全部放行，与生产事件记录完全一致。

---

## 二、F1 新闻完整流转路径

| 阶段 | 代码位置 | 实际行为 |
|---|---|---|
| ① 采集 | `bot.py:885 fetch_rss_sources()` → `bot.py:102` | 从 `RSS_FEEDS` 里取 `https://www.skysports.com/rss/12040`，并发抓取 |
| ② 解析 | `bot.py:583 _parse_feed_items()` | 只取 title/link/desc/pubDate/publisher，**不判断领域** |
| ③ 去重 | `bot.py:906 _dedupe_items()` | 链接指纹 + 标题指纹，**不做领域过滤** |
| ④ 时效 | `bot.py:914 _filter_by_freshness()` | 8h→12h，**不做领域过滤** |
| ⑤ 入池 | `_merge_pool()` | 写入 `news_pool`，`source`/`feed_origin` 记为 skysports 12040 |
| ⑥ 候选 | `run_scan_mode()` `bot.py:2275` | 池内条目进候选，**无领域门槛** |
| ⑦ 分类 | `evidence.py:318 primary_event_type()` | F1 标题判不出类型 → `primary_type = ""`（空值不构成否决） |
| ⑧ 来源分级 | `evidence.py:162 classify_source()` | `skysports.com` 命中 `TIER1_DOMAINS` → `TIER1` |
| ⑨ 证据 | `evidence.py:981 evaluate()` | news 稿 + live-blog 两篇 → `count = 2` → 多源印证 |
| ⑩ 闸门 | `bot.py:1672 _finalize_result()` | 单源门(过) + 红线门(过) + 12h时效(过) + schema(过) |
| ⑪ 评级 | `evidence.py:1293 enforce_rating()` | Tier1 未被降级 |
| ⑫ 投递 | `bot.py:2206 _deliver_card()` → `bot.py:381 send_telegram_ex()` | `message_id = 26`，DELIVERED → COMMITTED |

**这条链上没有任何一环问过「这是足球吗」。**

---

## 三、逐条回答九个问题

### Q1. 哪个 RSS 源抓到了这条 F1 新闻？

`RSS_FEEDS` 中的 **`https://www.skysports.com/rss/12040`**（`bot.py:102`）。

### Q2. 该 feed 的完整 URL？

`https://www.skysports.com/rss/12040`

池内 `source` / `feed_origin` 字段对该条记录即为此值。

### Q3. 该 RSS 源是否属于综合体育源，而不是足球专属源？

**是综合体育源。实测确认（2026-10-31 真实请求）：**

```
HTTP 200 | 15384 bytes | application/xml
<channel>
  <title>SkySports | News</title>          ← 全站新闻，不是 Football 频道
  <description>Sports News</description>
  <link>http://www.skysports.com</link>
</channel>
条目 20 条，实际构成：
  · Stoke City vs Preston LIVE!                                   足球
  · Fernandes restored to Man Utd line-up ... LIVE!               足球
  · Sherrock 'happy to overcome hate' after lucrative Littler win  飞镖
  · Yamashita in command as Hull and Woad fall away in Kuala Lumpur 高尔夫
  · Novak pulls three shots clear in Mexico ...                    高尔夫
  · Verstappen, Lawson receive grid penalties ...                   F1  ← 本案
  · Moloney-MacDonald stars as Red Roses ...                       橄榄球
  · Mexico City Grand Prix - Practice highlights                   F1
  · Canelo and Mbilli make weight for world title fight in Riyadh  拳击
  · Leclerc seals Mexico City practice double ...                  F1
  · Hadjar summoned to stewards ...                                F1
```

**约一半条目不是足球。** 而它在 `bot.py:100` 被注释为「2. 顶级权威主流媒体 (Tier 1 权威主流)」，与 `feeds.bbci.co.uk/sport/football/rss.xml` 并列，被当成足球源使用。

对照实测：Sky Sports 真正的足球专属 feed 是 **`/rss/11095`** 与 **`/rss/11661`**，各 20 条**全部**为足球内容（Man Utd / PL / 转会 / 各队跟队），无一例外。

### Q4. 新闻进入哪个候选池？

`state.news_pool`（`_merge_pool()` 写入）。生产快照池内 **137 条，其中 10 条非足球**，全部来自该 feed：

| 项目 | 条数 | 示例 |
|---|---|---|
| F1 / 赛车 | 6 | Verstappen 撞车被罚、Leclerc 练习赛最快、Mexico City GP |
| 高尔夫 | 2 | Yamashita 领先、Novak 三杆优势 |
| 橄榄球 | 2 | Red Roses 击败美国、Jones 谈北美巡回 |
| 飞镖 | 2 | Sherrock 击败 Littler 拿 £501,000 |
| 拳击 | 1 | Canelo 与 Mbilli 称重 |

该 feed 在池内总产出 20 条，**一半是噪音**。

### Q5. 新闻分类在哪个函数执行？

**没有任何「体育项目」分类函数存在。** 全项目搜索 `sport_category` / `SPORT_` / `is_football` / `domain_check` 结果为空。

最接近的两个函数都不是领域分类：

- `evidence.py:318 primary_event_type(title)` —— 只认足球**事件类型**（TRANSFER / INJURY / MATCH / CONTRACT / APPOINTMENT / DISCIPLINE）。F1 标题实测返回：

| 标题 | primary_event_type |
|---|---|
| Verstappen, Lawson receive grid penalties… | `""`（空） |
| Leclerc seals Mexico City practice double… | `""`（空） |
| Mexico City Grand Prix \| Practice highlights | `MATCH` ← **误判成比赛** |
| 'Every weekend!' - Lawson fumes at Verstappen… | `MATCH` ← **误判成比赛** |
| Mexico City GP: Verstappen, Lawson summoned by stewards… | `""`（空） |

- `evidence.py:162 classify_source(domain)` —— 判的是**媒体权威度**，不是领域。

### Q6. 分类结果是否明确标记为 Football？

**没有。** 记录里根本没有这个字段。`build_records()` 产出的字段为：

```
url, canonical, source_id, domain, url_domain, publisher_domain,
publisher_trusted, identity_conflict, feed_origin, source_class,
title, title_key, desc, tokens, desc_tokens, primary_type, persons
```

无 `sport_category`，无 `is_football`。`source_class` 的取值是 `OFFICIAL / TIER1 / LOW_TRUST / AGGREGATOR / UNKNOWN`，**与体育项目正交**。

### Q7. AI 筛选是否把 F1 当成足球新闻？

**是，而且系统没有给它任何机会区分。**

- `bot.py:1166` 提示词开篇：**「你是一位严谨的体育新闻事实核验员」** —— 全篇没有 football-only 约束。
- `bot.py:1238-1258` 输出 schema 里**没有 sport 字段**，模型连「这是 F1」都无处申报。
- 评级标准 `bot.py:1224` 明写：`✨【金哨认证·绝对真理】(Tier 1)：官方公告、BBC、天空体育、英超官方` —— 直接把「天空体育」等同于 Tier 1，与内容领域无关。
- 结果：模型生成中文标题「维斯塔潘与劳森练习赛碰撞双双被罚退后一位发车」，按足球流程走完全程。

### Q8. 为什么非足球新闻能够获得 Tier 1？

三层叠加：

1. `evidence.py:60-64 TIER1_DOMAINS` 收录的是 **`skysports.com` 整域**，不是「Sky Sports 足球频道」。F1 稿子挂在 `/f1/` 路径下，域名仍是 skysports.com → `TIER1`。
2. 提示词把「天空体育」写进 Tier 1 定义，模型据此自评 Tier 1。
3. `evidence.py:1293 enforce_rating()` 只按「证据状态 + 来源等级」降级，**不检查领域**，因此 Tier 1 未被降级（实测 `('TIER1', False, '')`）。

这正是用户指出的问题：**权威媒体报道 ≠ 官方原始证据**，但当前实现把两者混为一谈。

### Q9. 最终推送前是否存在领域校验？为什么没拦截？

**不存在。**

推送前的完整闸门链（`bot.py:1672 _finalize_result()`）：

```python
allowed, why, gate_code = _evidence_gates(verdict, rating_key, topic_text, TRUSTED_SINGLE_SOURCES)
    #   ├─ ev.auto_push_allowed()  状态门 + 单源门
    #   └─ ev.black_label_gate()   红线门（死亡/重伤/重病/违法/重大事故）
fresh_ok, fresh_why = _breaking_freshness_gate(...)   # 12h 时效
ok, errors, data = out_schema.validate_scan(data)     # 字段完整性
```

- `_evidence_gates`（`bot.py:1380`）= 单源门 + 红线门。红线关键词表（`STRICT_TOPIC_KEYWORDS`）只覆盖死亡 / 重病 / 违法 / 事故，**不覆盖体育项目**。
- 它拿到的是 `MULTIPLE_INDEPENDENT_SOURCES`，连单源门都不触发 → 直接 `(True, '')`。
- `validate_scan`（`schema.py:63`）只校验字段非空与枚举合法，无领域字段可校验。

早报路径（`bot.py:1392 _build_morning_events()`）同样只走 `_red_line_gate`。实测：24h 窗口 137 条 → 通过 40 条 → 其中**飞镖 Sherrock 已进入早报候选**（`SINGLE_SOURCE`）。**早报是第二条污染通道。**

---

## 四、根本原因与次要原因

### 根本原因

**R1｜来源登记错误（唯一物理入口）**
`bot.py:102` 把 Sky Sports **全站综合体育** feed 登记为 Tier 1 足球源。这是 F1 进入系统的唯一通道。其余 18 个源实测均无此问题（BBC 用的是 `/sport/football/rss.xml`，太阳报用的是 `/sport/football/feed/`）。

**R2｜系统缺少「体育项目」这一维度**
全项目没有任何 `sport_category` 概念。现有 `classify_source` 判的是媒体权威度，`primary_event_type` 判的是足球事件类型，两者都无法回答「这是不是足球」。**没有这个维度，任何下游校验都无从下手。**

**R3｜领域与权威度耦合**
`TIER1_DOMAINS` 是整域白名单，`skysports.com` 下的任何频道（足球 / F1 / 高尔夫 / 飞镖）都自动获得最高权威等级。权威度被当成了领域凭证。

### 次要原因

**R4｜同域「假多源」（影响面超出 F1，严重）**
`evidence.py:805 independent_sources()` 按**文章相似度**（`REPRINT_THRESHOLD`）聚类，而不是按**发布方**聚类。同一家媒体的两篇不同稿子（news + live-blog）相似度低于阈值 → 被算成 2 个「独立来源」：

```
2 个独立来源交叉印证（skysports.com, skysports.com）
```

实测全池 137 条中，判为多源印证的 38 条，**其中 8 条（21%）的所有来源其实是同一家媒体**：

| 标题 | 假多源 |
|---|---|
| 'We fell behind' - what does new San Siro deal mean for AC Milan and Inter? | bbc.co.uk × 2 |
| Chelsea tickets and hospitality packages … Stamford Bridge | thesun.co.uk × 3 |
| England's haunted stadiums revealed … Stamford Bridge | thesun.co.uk × 2 |
| AC Milan v Inter and the tale of an iconic image | bbc.co.uk × 2 |
| Verstappen, Lawson receive grid penalties … | skysports.com × 2 |
| Leclerc seals Mexico City practice double … | skysports.com × 2 |

**这条与 F1 无关，是通用缺陷**：它让「同一家媒体写了两篇」等价于「两家独立媒体交叉验证」，直接影响所有新闻的可信度判定。这属于 V1.3 P0-3「多源印证虚高」的同类问题，但发生在**发布方维度**而非**事件维度**。

**R5｜AI 提示词无领域边界**
`bot.py:1166` 写的是「体育新闻事实核验员」，schema 无 sport 字段。即使模型察觉异常也无处申报。

**R6｜空值即放行**
`primary_type = ""` 被当作「无类型冲突」而非「无法确认领域」。F1 标题恰好 5 条里有 3 条判不出类型，全部因此通过。

---

## 五、影响范围

1. **已确认污染**：1 条 F1 新闻真实投递（`message_id = 26`），标题为「维斯塔潘与劳森练习赛碰撞双双被罚退后一位发车」。
2. **池内待发污染**：137 条中有 10 条非足球（F1 6 / 高尔夫 2 / 橄榄球 2 / 飞镖 2 / 拳击 1，部分重叠），随时可能被下一轮选中。
3. **早报通道同样失守**：飞镖 Sherrock 已进入早报候选列表。
4. **假多源污染全局**：21% 的多源判定是同一家媒体自证，与体育项目无关，**足球新闻同样受影响**。
5. **潜在范围**：只要该 feed 继续在线，每天约 10 条非足球内容会持续注入；F1 赛历密集期（练习赛 / 排位 / 正赛三天连发）污染量会显著上升。

---

## 六、最小修复方案（待确认后实施）

按用户 A–E 五点设计，遵循「不删源、不放宽、不加硬性人工介入」原则。

### A. 足球领域白名单（新增模块 `sport_scope.py`）

```
SPORT_FOOTBALL / SPORT_FORMULA1 / SPORT_BASKETBALL / SPORT_TENNIS /
SPORT_RUGBY / SPORT_CRICKET / SPORT_GOLF / SPORT_DARTS / SPORT_BOXING /
SPORT_OTHER / SPORT_UNKNOWN
```

放行规则：**只有 `SPORT_FOOTBALL` 可进入足球流程**。`SPORT_UNKNOWN` 与任何冲突判定一律拒绝自动推送。
明确纪律：`Sports ≠ Football`。

### B. 来源级过滤（`FEED_REGISTRY`）

每个 feed 必须登记：`source_id / feed_url / publisher / domain / sport_category / source_scope / enabled`。

- `source_scope ∈ {football_only, mixed_sport, unknown}`。
- **只有 `football_only` 允许直接采集**；`mixed_sport` 必须逐条过 C 项新闻级分类；`unknown` 不得进入生产采集。
- 把 `bot.py:102` 的 `12040` 替换为实测纯足球的 `11095`（或保留 `12040` 但标 `mixed_sport` 并强制逐条分类）。
- `RSS_FEEDS` 改为由 `FEED_REGISTRY` 派生，避免「配置一处、注释一处」再次漂移。

### C. 新闻级过滤（`classify_sport(title, desc, url)`）

程序侧判定，**不看模型**。三维取证：

1. **URL 路径**：`/f1/`、`/golf/`、`/darts/`、`/boxing/`、`/rugby-union/` → 直接判定对应项目。
2. **赛事 / 组织名**：Grand Prix、F1、PGA、PDC、WTA、Six Nations、Test match…
3. **运动员 / 队伍名**：Verstappen、Leclerc、Littler、Canelo…

判定规则：
- 命中非足球信号且**未命中足球信号** → 判该项目，拒绝。
- **足球信号与非足球信号同时命中 → `SPORT_CONFLICT`，拒绝**（对应用户测试 7：标题同时出现俱乐部与 F1 关键词，不能凭俱乐部名放行）。
- 全部未命中 → `SPORT_UNKNOWN`，拒绝自动推送。

### D. 最终推送硬校验（`sport_gate()`）

在 `_evidence_gates()`（突发）与 `_build_morning_events()`（早报）**两处**调用，拒绝时打印 `::warning title=OutOfScopeSport::` 并写入拒绝原因。

判定输入是**程序算出的 `sport_category`**，模型自报的 `sport` 字段只作线索、不作依据（对应用户测试 8）。
同时要求事件有足球事实支撑（联赛 / 俱乐部 / 球员 / 转会 / 裁判等至少一项）。

### E. 评级独立性

- `classify_source()` 拆成两个正交维度：`publisher_authority`（媒体权威度）与 `sport_category`（领域）。
- `enforce_rating()` 增加前置约束：**非 Football 领域不得授予 Tier 1**。
- 权威度不再自动等于事实等级：`TIER1` 只表示「这是一线媒体」，`evidence_status` 仍由 grounding 独立计算。

### 附带修复 R4（同域假多源）

`independent_sources()` 增加按**发布方域名**去重：同一 domain 的多篇文章合并为 1 个来源组，`count` 只统计不同 domain。这是 F1 事故的直接放大器，且影响全局，建议与 A–E 同批修。

---

## 七、回归测试方案（10 条，全部使用模拟数据，不发真实 Telegram）

| # | 用例 | 期望 |
|---|---|---|
| 1 | Sky Sports F1 新闻 | 拒绝，`sport_category = FORMULA1` |
| 2 | Sky Sports Football 新闻 | 允许进入后续审核 |
| 3 | BBC F1 新闻 | 拒绝 |
| 4 | BBC Football 新闻 | 允许进入后续审核 |
| 5 | FIFA 官方足球新闻 | 允许进入后续审核 |
| 6 | FIA F1 官方新闻 | 拒绝（官方域名 ≠ 足球） |
| 7 | 标题同时含俱乐部名与 F1 关键词 | 拒绝（`SPORT_CONFLICT`） |
| 8 | AI 把 F1 标成 Football | 推送仍被拒绝（以程序判定为准） |
| 9 | 来源为综合体育 RSS（12040） | 不自动获得足球白名单资格 |
| 10 | 领域未知 / 分类冲突 | 拒绝自动推送 |

补充用例（覆盖 R4）：
- 同一 domain 两篇不同文章 → `count = 1`，不得判 `MULTIPLE_INDEPENDENT_SOURCES`。
- 同一 domain + 另一 domain 各一篇 → `count = 2`，正常多源。

**纪律**：不删除任何新闻源；不放宽任何既有断言；不伪造测试结果；新增用例只能收紧判定。

---

## 八、待确认事项

1. **`12040` 的处理方式**：直接替换为 `11095`（干净但丢失 Sky Sports 的部分独家视频条目），还是保留但标 `mixed_sport` 强制逐条分类（保留覆盖面，但依赖 C 项准确率）？**建议后者**，因为 C 项无论如何都要做，且 `11095` 与 `12040` 的足球部分高度重叠。
2. **R4 是否同批修**：建议同批。它是本次事故的直接放大器，且与领域无关、影响全局。
3. **历史 `message_id = 26` 是否撤回**：不自动撤回，交由人工决定。

---

**本报告为第一阶段产物，未修改任何代码。**
