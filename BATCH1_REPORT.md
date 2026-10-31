# V1.1 第一批（任务 1–5）实施与验证报告

> 范围声明：本报告只覆盖任务书 V1.1 的**第一批**（任务 1–5：证据引擎）。
> 第二~五批（任务 6–17 及维护项）未开始，等待确认后再进入。

---

## 0. 结论摘要

| 项目 | 结果 |
|---|---|
| 全量测试 | **108 项通过 / 0 失败 / 0 错误**（原 82 项 → 108 项，新增 26 项） |
| 原有测试 | 82 项全部保留，**未删除任何断言、未放宽任何断言** |
| 端到端冒烟（正常） | 通过：4 条素材 → 1 次 AI 调用 → 2 个独立来源 → 推送 → COMMITTED |
| 端到端冒烟（`--quota`） | 通过：熔断持久化、候选保留、0 推送 |
| 线上验证 | **未完成**。本机没有真实 GitHub Actions 运行记录，不声称线上验证已完成 |

---

## 1. 变更文件清单

| 文件 | 变更类型 | 说明 |
|---|---|---|
| `evidence.py` | 重写（421 → 881 行） | 新增事件类型词典、`same_event()`、转载聚类、单源闸门、黑标闸门 |
| `bot.py` | 局部修改（+28 行） | 接入两道新闸门、程序侧证据补 `desc`、卡片加单一来源免责 |
| `tests/test_evidence.py` | 扩充（141 → 388 行） | 新增 24 项验收用例（5 个任务类） |
| `tests/test_pipeline.py` | 修改 + 扩充 | 默认素材改为多源；单源用例拆为「默认拦截 / 白名单放行」 |
| `tests/test_ai_integration.py` | 修改 | 素材改用真实域名；转载/无关池用例改走 `_finalize_result` |
| `.github/workflows/scanner.yml` | 修改 | 两个 Job 注入 `TRUSTED_SINGLE_SOURCES` |

---

## 2. 任务 1：`OFFICIAL_CONFIRMED` 必须证明「同一件具体事件」

### 2.1 原缺陷
只要「相关记录里出现官方域名」就判 `OFFICIAL_CONFIRMED`。也就是说，arsenal.com
上任何一条关于阿森纳的新闻，都能给「阿森纳谈妥前锋」这条消息背书。

### 2.2 实现
新增 `same_event(anchor, other)`，四个维度必须同时成立：

| 维度 | 实现 | 拒绝理由示例 |
|---|---|---|
| 主体 | `event_subjects()` 取无歧义俱乐部词（`united`/`city` 这类多义后缀已排除） | `主体不同（arsenal vs chelsea）` |
| 事件类型 | `primary_event_type()`：命中次数最多者优先，同分取标题中出现更早者 | `事件类型不同（TRANSFER vs INJURY）` |
| 关键事实 | `_key_numbers()` 抽带单位数字（`60m`/`5year`/`30%`），两边都有且不相交即冲突 | `关键数字不一致（60m vs 40m）` |
| 相关度 | 标题词重叠度 ≥ `RELATED_THRESHOLD`(0.30) | `标题相关度 0.10 低于 0.3` |

**为什么需要「主事件类型」而不是「类型集合有交集」**：
`Arsenal agree deal for striker as transfer talks advance` 与
`Arsenal confirm striker injury after transfer window opens` 的**类型集合都含 TRANSFER**
（后者因为 "transfer window"），集合相交但语义相反。按命中次数+出现位置取主类型，
前者是 TRANSFER(3 次命中)、后者是 INJURY(1 次，位置更早)，才能区分开。

### 2.3 判定链（`evaluate()`）
```
evidence_url 命中证据集合？
  否 → UNVERIFIED（不在证据集合中）
  是 → 聚合器？ → UNVERIFIED
       ↓
filter_related()（same_event 粗筛）→ non_agg
       ↓
candidate_official = 官方域名 且 标题相关（0.30）的记录   ← 注意：比 filter_related 更宽松
       ↓
same_event 逐条复核 → confirmed_official / rejected_official
       ↓
剔除 rejected_official → pool → independent_sources() → count
       ↓
冲突？          → CONFLICTING（count≥2）
confirmed_official 非空 → OFFICIAL_CONFIRMED
rejected_official 且 count≤1 → UNVERIFIED（官方确认不成立）
count≥2        → MULTIPLE_INDEPENDENT_SOURCES
count==1       → SINGLE_SOURCE
```

**关键细节**：`candidate_official` 刻意用「标题相关」而不是 `filter_related` 的结果。
如果只在 `related` 里找官方记录，那条「相关的另一件事」早被 `filter_related` 滤掉了，
`rejected_official` 永远是空，也就永远发现不了「官方确认不成立」。

### 2.4 验收
```
python -m unittest tests.test_evidence.TestTask1OfficialMustProveSameEvent -v
  test_official_other_event_is_not_confirmation ... ok    → UNVERIFIED，official_rejected=['arsenal.com']
  test_official_same_event_is_confirmed          ... ok    → OFFICIAL_CONFIRMED
  test_official_anchor_self_confirms             ... ok
  test_same_event_requires_subject_and_type      ... ok
```

---

## 3. 任务 2：UNKNOWN 域名不得计入交叉印证

### 3.1 实现
`independent_sources()` 给每个组打 `trusted` 标记：组内**只要有一个可识别域名**才算可信组；
全部由 UNKNOWN 组成的组 `trusted=False`。`evaluate()` 里：

```python
trusted = [g for g in groups_all if g["trusted"]]
count = len(trusted) if trusted else (1 if groups_all else 0)
```

- 两个 UNKNOWN 站点 → 1 个组、不可信 → `count=1` → `SINGLE_SOURCE`
- BBC + UNKNOWN（标题不同）→ 1 个可信组 + 1 个不可信组 → `count=1`
- 聚合器在 `independent_sources()` 入口直接跳过

### 3.2 单一白名单
来源分级只保留 `classify_source()` 一个入口（`_domain_in()` 统一后缀匹配）。
`test_single_whitelist_source_of_truth` 断言 `arsenal.com` / `www2.arsenal.com` → OFFICIAL，
`.example` 域名 → UNKNOWN，空串 → UNKNOWN。任何模块不得另建分级表。

### 3.3 验收
```
tests.test_evidence.TestTask2UnknownDomainsDoNotCount ... 4 项 ok
```

---

## 4. 任务 3：转载 / 多源识别

### 4.1 实现（`independent_sources()`）
1. 聚合器排除
2. 通讯社（`WIRE_DOMAINS`：reuters / apnews / afp / pa.media / dpa / bloomberg）→ 归 `wire:<域名>` 一个桶
3. 其余按「同一篇报道」聚类，`_report_similarity() ≥ 0.80`
   - 标题相似度优先
   - 标题只是相关（≥0.30）但 RSS 摘要几乎一致（≥0.85）也算转载 → 覆盖「改标题洗稿」
4. UNKNOWN 归入统一的 `unknown` 桶

### 4.2 关键修正：样板摘要不能当转载证据
初版 `_report_similarity` 只要两边 `desc` 非空就比对正文，结果测试里 `desc="desc"`
这种样板句相似度 1.0，把两篇完全不同的报道合并成一个来源 —— 等于废掉多源印证。
现在要求**两边摘要各 ≥8 个不同词**才启用正文比对。

### 4.3 验收
| 用例 | 场景 | 期望 |
|---|---|---|
| `test_three_near_identical_reprints_count_once` | BBC/邮报/镜报同一篇稿 | count=1 |
| `test_two_independent_outlets_count_twice` | BBC + 天空不同措辞 | count=2 → MULTIPLE |
| `test_two_events_same_club_not_merged` | 同俱乐部转会 vs 伤情 | count=1 |
| `test_two_unknown_sites_not_independent` | 两个未知站 | ≠ MULTIPLE |
| `test_same_wire_service_counts_once` | 路透两条稿 | count=1 |
| `test_boilerplate_description_is_not_reprint_evidence` | 样板摘要 | count=2 |

---

## 5. 任务 4：SINGLE_SOURCE 默认不自动推送

### 5.1 实现
- `is_pushable(status)` **保留原语义**：状态层面是否允许推送（`SINGLE_SOURCE` 仍为 True）
- 新增 `auto_push_allowed(verdict, allowlist)`：状态门 + 单源门
- 新增 `single_source_allowed()`：白名单域名放行；**UNKNOWN 永远拒绝**（即使写进白名单）
- 新增 `single_source_note()`：返回 `{note:"未经独立交叉验证", source_name, url}`
- `bot.py`：`TRUSTED_SINGLE_SOURCES` 环境变量（逗号分隔），卡片末尾追加
  「信源提示：本条为单一来源（域名），未经独立交叉验证 / 原始来源：URL」

### 5.2 修正：单一来源必须指向「那一个来源」
新闻池是乱序的，初版用 `sources[0]` 取域名，结果判定的是 bbc.co.uk、闸门却报
skysports.com（同事件的另一篇转载）。现在 `evaluate()` 输出 `primary_source`
（域名 + 对应 URL），闸门与卡片标注都用它。

### 5.3 验收
```
TestTask4SingleSourceGate ... 5 项 ok
tests.test_pipeline.TestEvidenceGating.test_single_low_trust_source_is_blocked_by_default ... ok
tests.test_pipeline.TestEvidenceGating.test_single_source_is_pushed_but_rating_capped   ... ok
```

---

## 6. 任务 5：黑标证据闸门

### 6.1 实现
`strict_topic(text)` 识别 DEATH / ILLNESS / LEGAL / ACCIDENT / SEVERE_INJURY
（重伤需「伤情词 + 严重程度词」同时命中，避免 `injury update` 误判）。
`black_label_gate(rating_key, verdict, text)`：

- 触发条件 = 模型给了黑标 **或** 内容属于严肃议题
- 放行条件 = `OFFICIAL_CONFIRMED` 或 `MULTIPLE_INDEPENDENT_SOURCES`
- 其余一律拦截，`bot.py` 返回 `BLACK_LABEL_EVIDENCE_INSUFFICIENT` 并打 `::warning`
- **不看模型自信程度**：模型写 `var_rating=黑标` 只是触发条件，不是通行证

`bot.py` 只扫「事件标题 + 事实摘要」，不扫 A/B/C 稿：评论稿里的「警方」「调查」
多为比喻或引申，扫进去会大面积误杀。

### 6.2 与 `enforce_rating` 的分工
- `enforce_rating`：可信度分级（黑标永不降级，保持原行为）
- `black_label_gate`：证据门槛（黑标证据不足 → 整条不发）

两者互不替代，`test_10_black_label_never_downgraded` 仍原样通过。

### 6.3 验收
```
TestTask5BlackLabelGate ... 5 项 ok
test_black_label_blocked_without_strict_evidence ... ok  ← 验收原文场景
```

---

## 7. 与原测试的冲突及处理（必读）

任务 4/5 的新规格与原测试有 3 处**真实冲突**。逐条说明改了什么、为什么不是「弱化断言」：

### 冲突 1：`test_single_source_is_pushed_but_rating_capped`
- 原断言：单一小报来源（thesun.co.uk）**会被推送**，且降级到 Tier 3
- 新规格：单一来源默认不推送
- 处理：拆成两条
  1. `test_single_low_trust_source_is_blocked_by_default`：断言不推送、无投递记录
  2. `test_single_source_is_pushed_but_rating_capped`：把 thesun.co.uk 放进白名单，
     **原断言 `assertIn("Tier 3", ...)` 原样保留**，并新增「未经独立交叉验证」「原始 URL」两条断言
- 净效果：断言数量增加，覆盖面扩大

### 冲突 2：`test_reprint_is_single_source` / `test_unrelated_pool_does_not_inflate_sources`
- 原实现：跑完整流水线后读 `state["events"]`，因为 SINGLE_SOURCE 会推送
- 新规格：SINGLE_SOURCE 不再推送，`state["events"]` 为空
- 处理：改走 `bot._finalize_result()` 直接取证据判定
  - **原断言 `evidence_status == "SINGLE_SOURCE"` 原样保留**
  - 新增断言：`ok is False`、`code == "EVIDENCE_NOT_PUSHABLE"`

### 冲突 3：`test_deepseek_result_verified_by_program_evidence`
- 原素材用 `https://example.com/news/N`（UNKNOWN 域名）
- 新规格：UNKNOWN 不计入交叉印证 → 该用例必然从 MULTIPLE 掉到 SINGLE
- 处理：素材改用 bbc.co.uk / skysports.com 真实域名（这正是「两家独立报道」的原意），
  断言不变
- 同类：`test_pipeline.base_result()` 的 grounding 从 1 条改为 2 条独立报道，
  否则投递状态机的 4 个用例根本走不到推送分支，测的就不是状态机了

> 说明：以上都是**规格变更导致的用例前提调整**，不是为了让测试变绿而放宽判定。
> 每处都保留了原断言并追加了新断言；证据引擎自身的 11 项原用例**一字未改，全部通过**。

---

## 8. 已知取舍与风险

| 项 | 说明 | 影响 |
|---|---|---|
| 官方记录「相关的另一件事」导致 UNVERIFIED | 单源场景下，若官方域名有标题相关但不同事件的稿件，该条降为 UNVERIFIED（而非 SINGLE_SOURCE） | 单源本来就不推送，实际推送量不变；但**白名单内的单源**也会被这条拦住。这是刻意保守：证据存在歧义时不发 |
| 白名单默认空 | `TRUSTED_SINGLE_SOURCES` 未配置时，所有单一来源一律不发 | **上线后推送量会明显下降**。这是任务 4 的直接结果，不是 bug。需要人工确定哪些记者/机构进白名单 |
| 事件类型词典是启发式 | 英文按词、中文按子串；`talks`/`target`/`chase` 这类词有跨类型歧义 | 极端标题可能误判主类型 → 少算一个来源（保守方向），不会多算 |
| 通讯社识别只认域名与署名 | 媒体转载路透稿但未标署名的，无法识别 | 会多算来源（偏激进）。标题相似度 0.80 能拦住大部分原样转载 |
| 冒烟测试是本地 mock | `scripts/smoke_run.py` 用本地 HTTPServer 模拟 DeepSeek/Telegram | 只证明代码路径通，不证明线上行为 |

---

## 9. 未验证项（明确声明）

- **未做线上验证**：本机没有真实 GitHub Actions 运行记录，无法声称线上已验证。
- `TRUSTED_SINGLE_SOURCES` 走 `vars.TRUSTED_SINGLE_SOURCES`（仓库变量），
  尚未在仓库里创建，当前等价于空 → 单一来源全拦截。
- 真实新闻流下的误杀率未知，需要上线后统计 `EVIDENCE_NOT_PUSHABLE` /
  `BLACK_LABEL_EVIDENCE_INSUFFICIENT` / `::warning title=Official Source Rejected::` 三个信号。

---

## 10. 复现命令

```bash
python tests/run_all.py            # 全量 108 项（含真实 git 仓库测试，约 43s）
python tests/run_all.py --fast     # 快速 103 项（跳过 git）
python -m unittest tests.test_evidence -v          # 证据引擎 35 项
python scripts/smoke_run.py                        # 端到端冒烟（正常）
python scripts/smoke_run.py --quota                # 端到端冒烟（配额耗尽）
```
