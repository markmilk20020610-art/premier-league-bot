# 越位情报局 V1.2 — 第一批（任务 1–4）实施与验证报告

> 范围声明：本报告只覆盖 V1.2 任务书的**第一批**（证据来源信任边界，P0）。
> 第二批（任务 5–7：早报证据与 Schema）及后续批次未开始，等待确认后再进入。

---

## 0. 结论摘要

| 项目 | 结果 |
|---|---|
| 全量测试 | **132 项通过 / 0 失败 / 0 错误**（V1.1 结束时的 108 项 → 132 项，新增 24 项） |
| 原有测试 | 108 项全部保留，**未删除任何用例、未放宽任何断言** |
| 端到端冒烟（正常） | 通过：4 条素材 → 1 次 AI 调用 → 2 个独立来源 → 推送 → COMMITTED |
| 端到端冒烟（`--quota`） | 通过：熔断持久化、候选保留、0 推送 |
| 线上验证 | **未完成**。本机无真实 GitHub Actions 运行记录，不声称线上验收完成 |

---

## 1. 修改文件清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `evidence.py` | 修改（881 → 1133 行） | Feed 信任边界、人物实体抽取、官方确认加严、冲突不看来源数 |
| `bot.py` | 修改（1541 → 1593 行） | 透传 `feed_origin`、非 Gemini 清空 grounding、冒充身份告警 |
| `ai_provider.py` | 修改（+7 行） | Gemini grounding 记录打 `sdk=True` 凭证 |
| `tests/test_evidence.py` | 扩充（388 → 583 行） | 新增 16 项（任务 1 / 3 / 4） |
| `tests/test_pipeline.py` | 扩充（391 → 537 行） | 新增 6 项（任务 1 / 2 主流程 + 降级回归） |
| `tests/test_ai_integration.py` | 扩充（292 → 339 行） | 新增 2 项（任务 2 端到端） |
| `scripts/smoke_run.py` | 修改 | 素材改为每条自带真实 feed，不再共用一条 BBC feed |

---

## 2. 任务 1：修复 RSS publisher 伪造

### 2.1 原缺陷
`build_records()` 直接信任 RSS item 的 `<source url>`：`domain = pub_domain or url_domain`。
任何第三方 feed（rsshub、任意自建 feed）只要在条目里写
`<source url="https://www.liverpoolfc.com">`，这条恶意文章就会被判成 **OFFICIAL**，
进而拿到 `OFFICIAL_CONFIRMED` 与自动推送资格。

### 2.2 实现
新增信任边界（`evidence.py`）：

```python
TRUSTED_PUBLISHER_FEEDS = {"news.google.com"}

def _host(url):            # 原始主机名，不做可注册域名归并
def feed_trusts_publisher(feed_origin): ...
```

归属判定改为：

| 情况 | 归属域名 | 说明 |
|---|---|---|
| feed 在白名单（Google News） | `publisher_domain` | Google 的索引元数据可信 |
| feed 不在白名单 | `url_domain`（文章真实 URL） | **publisher 一律忽略** |
| 两者都存在且不一致 | `url_domain` + `identity_conflict=True` | 留痕，且**不得充当官方确认** |

- 每条记录同时保留 `url_domain`、`publisher_domain`、`publisher_trusted`、
  `identity_conflict`、`feed_origin`、`source_id`（程序派生，模型无法伪造）。
- `candidate_official` 增加 `not r.get("identity_conflict")` 过滤。
- 主程序对冒充记录打 `::warning title=Source Identity Conflict::`，含 feed / publisher / url 三元组。

`_host()` 刻意不用 `extract_domain()`：后者会把 `news.google.com` 归并成 `google.com`，
等于让任意 `google.com` 子域都能冒充白名单 feed。

### 2.3 顺带修掉的严重回归（重要）
`_fetch_one_source()` 的降级路径原来是 `_parse_feed_items(fb_resp.content, url)`——
把**逻辑来源**（如 `thefa.com/rss`）当成了 feed 身份。加了信任边界后，
所有走 Google News 降级的源（thefa / premierleague / arsenal / 曼联曼城等官方号）
都会被判成「不可信 feed」，publisher 被丢弃，链接变成 `google.com` → 聚合器 →
**整条降级矩阵的证据能力归零**。

修正：`_parse_feed_items(content, source_url, limit, feed_origin=None)`，
降级路径传 `feed_origin=fallback_url`（真实抓取地址）。item 同时带
`source`（逻辑来源，telemetry/去重用）与 `feed_origin`（真实地址，信任判定用）。
回归用例 `test_google_news_fallback_keeps_publisher_trusted` 锁死这一点。

### 2.4 测试输入与实际输出

```
输入：
  feed_origin    = https://rsshub.app/test
  url            = https://malicious.example/fake-liverpool-news
  publisher      = https://www.liverpoolfc.com
  title          = Liverpool agree deal to sign winger Michael Olise

实际输出：
  domain            = malicious.example
  source_class      = UNKNOWN          ← 不是 OFFICIAL
  url_domain        = malicious.example
  publisher_domain  = liverpoolfc.com
  publisher_trusted = False
  identity_conflict = True
  verdict           = SINGLE_SOURCE「来路不明的单一来源（malicious.example），未经交叉印证」
  auto_push_allowed = (False, '单一来源且域名未识别（malicious.example），不允许自动推送')

对照（Google News feed，同一条 publisher）：
  feed_origin = https://news.google.com/rss/search?q=liverpool
  domain = liverpoolfc.com | source_class = OFFICIAL | publisher_trusted = True
```

---

## 3. 任务 2：DeepSeek 不得提交 Grounding 证据

### 3.1 原缺陷
`_call_model()` 里只有 `data.setdefault("grounding_records", [])`。
DeepSeek 只要在 JSON 里自己写一段 `grounding_records`，就会被 `_finalize_result()`
当成证据使用——「模型自己给自己发证据」。

### 3.2 实现
1. `provider != "gemini"` → `grounding_records = []`、`verified_grounding_urls = []`、
   `verification_status = <PROVIDER>_NO_GROUNDING`。
2. 模型自报的证据类字段全部 pop：`evidence_status`、`evidence_sources_count`、
   `evidence_sources`、`source_class`、`source_level`、`source_ratings`、`publisher_trusted`。
3. Gemini 侧新增**结构性凭证**：`GeminiProvider` 从
   `candidates[0].grounding_metadata.grounding_chunks` 解析出的记录打 `sdk=True`；
   `bot._sdk_grounding()` 只收 `sdk is True` 的记录。
   因此即使 provider 声称是 gemini，JSON 文本里自带的 `grounding_records` 也会被丢弃。

### 3.3 测试输入与实际输出

```
输入（FakeRouter，provider=deepseek）：
  grounding_records = [{liverpoolfc.com/news/olise-deal}, {thefa.com/x}]
  evidence_status   = OFFICIAL_CONFIRMED
  evidence_sources_count = 5
  source_class      = OFFICIAL

实际输出：
  grounding_records   = []
  verification_status = DEEPSEEK_NO_GROUNDING
  evidence_status     = None
  source_class        = None

端到端（真实 AIRouter + 假 HTTP，模型伪造两条官方 grounding）：
  rc = 0 | Telegram 收到 0 条 | state.events = {}
  → 伪造 grounding 无法变成 OFFICIAL_CONFIRMED

provider=gemini 且记录无 sdk 标记 → grounding=0, GEMINI_NO_GROUNDING
provider=gemini 且记录有 sdk 标记 → grounding=1, GEMINI_GROUNDED
```

---

## 4. 任务 3：加强 `same_event()`

### 4.1 原缺陷
只比对俱乐部主体、事件类型、关键数字、标题相似度。
`Liverpool 签下 Olise` 与 `Liverpool 签下 Gordon` 四项全过 → 判为同一事件 →
俱乐部官网那条会给 BBC 的新闻盖上 `OFFICIAL_CONFIRMED`。

### 4.2 实现
新增人物实体抽取 `person_names()`（无 NER，用启发式）：

1. **角色词路径**：`winger / striker / manager / coach / boss / signing / capture / ace…`
   之后的连续大写词（最多 2 个）。
2. **姓名双词路径**：相邻两个大写词，且不在机构/赛事/球场黑名单
   （`premier league`、`fa cup`、`manchester united`、`old trafford`…）。
3. **排除表** `_NAME_STOP`：停用词 + 俱乐部词 + 角色词 + 常见动词形容词
   （`suffers`、`serious`、`signs`、`joins`、月份、星期…）。

`same_event()` 增加人物维度：两边都有人名且不相交，且事件类型属于
`PERSON_CENTRIC_TYPES = {TRANSFER, CONTRACT, APPOINTMENT, DISCIPLINE, INJURY}` → 判为不同事件。

新增 `official_confirmation_ok()`：官方确认走**加严**判定，除 `same_event` 外
还要求 `shared_key_facts()` 非空（共享人物 / 数字 / 动作词 / 事件要素词）。
对应任务书「关键事实缺失时，不得仅靠高标题相似度授予官方确认」。

### 4.3 测试输入与实际输出

```
输入：
  BBC        : Liverpool agree deal to sign winger Michael Olise
  liverpoolfc: Liverpool agree deal to sign winger Anthony Gordon

实际输出：
  person_names(BBC)   = {michael, olise}
  person_names(官网)  = {anthony, gordon}
  same_event          = (False, '人物主体不同（michael/olise vs anthony/gordon）')
  official_ok         = (False, 同上)
  verdict             = UNVERIFIED「官方来源（liverpoolfc.com）指向另一事件，官方确认不成立」
  auto_push_allowed   = False
```

反例（防止过度合并）：

| 用例 | 输入 | 输出 |
|---|---|---|
| 同一球员不同事件 | `agree deal to sign winger Michael Olise` vs `confirm winger Michael Olise suffers knee injury` | `False`，`事件类型不同（TRANSFER vs INJURY）` |
| 同一球员同一事件 | `…sign winger Michael Olise` vs `…sign winger Michael Olise from Crystal Palace` | `True`，`关键事实一致` |
| 普通标题不产假人名 | `Arsenal set to sign striker in January` | `person_names = set()` |
| 普通标题不产假人名 | `Arsenal lose appeal against Dowman red card` | `person_names = set()` |

---

## 5. 任务 4：修复冲突检测

### 5.1 原缺陷
`if conflict and count >= 2:` —— 只有单一来源时，**任何矛盾都被忽略**。
一个官方来源自己前后矛盾，仍然返回 `OFFICIAL_CONFIRMED` 并推送。

### 5.2 实现
`detect_conflict()` 改为返回三元组 `(是否冲突, 原因, 冲突来源)`：

| 冲突来源 | 判定 | 处理 |
|---|---|---|
| `"claims"` | 同一 `claim_key` 同时出现 `supports` / `denies` | **直接 CONFLICTING，与来源数量无关** |
| `"model"` | 模型自报 `evidence_conflict` | 程序无法确证 → `UNVERIFIED`，保守拒绝推送 |
| `""` | 未发现 | 正常流程 |

冲突判定被**前移到官方确认之前**，因此任何未解决的关键事实冲突都不可能返回可推送的官方确认。

### 5.3 测试输入与实际输出

```
输入：
  唯一来源 = https://www.thefa.com/news/2026/oct/30/statement-2（官方）
  source_claims = [
    {claim_key: appeal_upheld, stance: supports, source: thefa.com},
    {claim_key: appeal_upheld, stance: denies,   source: thefa.com},
  ]

实际输出：
  verdict = CONFLICTING「同一事实出现相反表述：appeal_upheld」
  auto_push_allowed = False
  （V1.2 之前：OFFICIAL_CONFIRMED 且推送）

模型自报冲突（无结构化 claim）：
  verdict = UNVERIFIED「模型报告来源说法冲突，程序无法确证，保守拒绝自动推送」
  auto_push_allowed = False
```

---

## 6. 新增测试清单（24 项）

| 类 | 数量 | 覆盖 |
|---|---|---|
| `test_evidence.TestTask1FeedTrustBoundary` | 6 | 恶意 feed 冒充官方、Google News 可信、直连 feed 忽略 publisher、域名一致不算冲突、冒充记录不能确认他稿、source_id 由程序派生 |
| `test_evidence.TestTask3EntityAwareSameEvent` | 6 | 不同球员不同事件、不同球员阻断官方确认、同球员不同事件、同球员同事件、人名抽取、关键事实缺失不确认 |
| `test_evidence.TestTask4ConflictIgnoresSourceCount` | 4 | 单一官方来源内部冲突、冲突优先于多源、模型自报冲突阻断、detect_conflict 三态 |
| `test_pipeline.TestV12SourceTrustBoundary` | 6 | 恶意 feed 不确认 / 不推送、DeepSeek grounding 被清空、gemini 无 sdk 标记被拒、gemini 有标记被收、Google News 降级回归 |
| `test_ai_integration.TestV12GroundingSpoof` | 2 | 伪造 grounding 端到端不推送、证据计算前 grounding 已清空 |

---

## 7. 测试结果

```
python tests/run_all.py
模块数 7 | 用例 132 | 失败 0 | 错误 0 | 耗时 56.2s

按模块：
  test_evidence        51  （V1.1: 35 → +16）
  test_schema          12
  test_state_store     12
  test_ai_provider     16
  test_ai_integration  11  （V1.1: 9  → +2）
  test_pipeline        25  （V1.1: 19 → +6）
  test_git_commit       5

python scripts/smoke_run.py
  [INFO] 证据核验：MULTIPLE_INDEPENDENT_SOURCES | 独立来源 2 个 | 2 个独立来源交叉印证（bbc.co.uk, skysports.com）
  冒烟结果：通过

python scripts/smoke_run.py --quota
  退出码 0 | Telegram 收到 0 条 | 熔断状态 open | 冒烟结果：通过
```

---

## 8. 未解决问题 / 已知取舍

| 项 | 说明 | 影响 |
|---|---|---|
| 人物抽取是启发式 | 没有 NER。角色词路径依赖「角色词 + 首字母大写」；标题全大写或全小写时抽不到人名 | 抽不到 → 退化为 V1.1 行为（不阻断），不会误阻断；**漏检**方向 |
| 姓名双词路径可能误抽 | `Arsenal Face Chelsea` 这类全标题大小写混排理论上可能抽到假人名 | 只会多阻断合并（保守方向）。黑名单已覆盖主要机构/赛事/球场 |
| `TRUSTED_PUBLISHER_FEEDS` 只有 Google News | 其他聚合器（feedburner、flipboard）若带 `<source>` 元数据也不采信 | 这些源的条目按文章 URL 归类，行为与 V1.1 一致 |
| `identity_conflict` 记录不参与官方确认 | 即使文章 URL 本身在官方域名上 | 极端保守；只影响 publisher 与 url 不一致的异常条目 |
| 冲突 `model` 分支降级为 UNVERIFIED 而非 CONFLICTING | 程序无法确证模型所说的冲突 | 状态名从「来源冲突」变「未核验」，日志更准确，行为同样是不推送 |
| 未做线上验证 | 无真实 GitHub Actions 运行记录 | 上线后需观察 `Source Identity Conflict` 告警频率 |

---

## 9. 复现命令

```bash
python tests/run_all.py                       # 全量 132 项（含真实 git 测试）
python tests/run_all.py --fast                # 快速（跳过 git）
python -m unittest tests.test_evidence -v     # 证据引擎 51 项
python -m unittest tests.test_pipeline.TestV12SourceTrustBoundary -v
python scripts/smoke_run.py
python scripts/smoke_run.py --quota
```
