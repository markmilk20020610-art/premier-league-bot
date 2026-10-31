# 越位情报局 V1.2 — 第二批（任务 5–7）实施与验证报告

> 范围声明：本报告只覆盖 V1.2 任务书的**第二批**（早报证据与 Schema 整改，P0）。
> 第三批（任务 8–11：Telegram 投递安全与恢复）及后续批次未开始。

---

## 0. 结论摘要

| 项目 | 结果 |
|---|---|
| 全量测试 | **155 项通过 / 0 失败 / 0 错误**（第一批结束 132 → 155，新增 23 项） |
| 原有测试 | 132 项全部保留；3 处 fixture 因 Schema 变严而补字段（详见第 6 节） |
| 端到端冒烟（正常 / `--quota`） | 均通过 |
| 早报端到端手工验证 | 通过（见第 7 节实际卡片） |
| 线上验证 | **未完成**。本机无真实 GitHub Actions 运行记录，不声称线上验收完成 |

---

## 1. 修改文件清单

| 文件 | 变更 | 说明 |
|---|---|---|
| `bot.py` | 修改（1593 → 1849 行） | 24h 硬窗口、逐条证据核验、event_id 回填、Schema 进主流程、低情报量报告 |
| `schema.py` | 修改（158 → 192 行） | `validate_summary(data, allowed_event_ids)`，top_event 七字段必填 |
| `tests/test_schema.py` | 扩充（130 → 220 行） | 新增 10 项（V1.2 Schema 反例） |
| `tests/test_pipeline.py` | 扩充（537 → 691 行） | 新增 13 项（早报证据 / 时间窗口 / 提示词隔离） |

---

## 2. 任务 5：早报逐条通过证据核验

### 2.1 原缺陷
早报完全不经过 `_finalize_result()`，只靠 Prompt 里一句「只写池子里有的内容」。
模型可以随手写一条池子里没有的新闻，或者把太阳报的爆料写成官宣，程序无从拦截。

### 2.2 实现

**（1）素材分配 source_id**
`_build_morning_events(pool)` 用 `ev.build_records()` 把池子里的每条素材规范化，
得到程序派生的 `source_id`（`sha1(canonical_url)[:12]`），模型无法伪造。

**（2）逐条独立核验**
对每条素材各跑一次 `ev.evaluate(link, records)`——早报是多事件汇总，
不能像突发扫描那样只核验一条。

**（3）分成 verified / excluded 两堆**
```python
(verified if ev.is_pushable(status) else excluded).append(entry)
```
`UNVERIFIED` / `CONFLICTING` 一律进 excluded，**根本不给模型看**。

**（4）只把 verified 喂给模型**
新增 `_build_verified_blob(events)` 取代原来的 `_build_pool_blob(pool)`。
原实现把整个新闻池塞进提示词，模型完全可以在 body 里写一条未核验的新闻；
现在清单外内容根本不出现。

**（5）模型只能选 event_id**
`_morning_event_blob()` 输出 `[E01] 证据=... | 2小时前 | bbc.co.uk（一线媒体） | 标题 | URL`，
模型返回 `top_events[].event_id`。

**（6）证据字段由程序回填**
`_attach_summary_evidence(result, events)` 把 `evidence_status`、`source_urls`、
`published_at`、`attribution` 全部覆盖为程序计算值——模型自报的一律作废。

**（7）生成后再校验**
`validate_summary(result, allowed_event_ids=...)` 二次校验，见任务 6。

**（8）卡片显示证据与来源**
每条 top_event 增加三行：`🛡 证据状态 · 📰 来源（等级）` + `🔗 原始链接`。

### 2.3 测试输入与实际输出

```
输入（新闻池 5 条）：
  2h  bbc.co.uk   Arsenal lose appeal against Dowman red card
  3h  skysports   FA rejects Arsenal appeal over Dowman dismissal
  5h  bbc.co.uk   Liverpool agree deal to sign winger Michael Olise
  26h chelseafc   Chelsea announce new shirt sponsor          ← 超窗口
  2h  google.com  Man City injury update ahead of derby        ← 聚合器链接

实际输出：
  [INFO] 早报窗口过滤：未来时间 0 条 / 时间未知 0 条 / 超过 24h 1 条 → 保留 4 条
  [INFO] 早报证据核验：4 条素材 → 通过 3 条、未通过 1 条
  [INFO]   排除 google.com | UNVERIFIED | evidence_url 是聚合器跳转链接，无法定位原始报道
  [WARN] 仅 3 条通过核验，已按「低信息日」处理，不凑数
```

---

## 3. 任务 6：`validate_summary()` 进入生产主流程

### 3.1 实现
`run_morning_mode()` 在 `send_telegram()` 之前：

```python
ev_ok, ev_errors = _attach_summary_evidence(result, verified)
schema_ok, schema_errors, result = out_schema.validate_summary(
    result, allowed_event_ids={e["event_id"] for e in verified})
if not (ev_ok and schema_ok):
    _skip("SUMMARY_SCHEMA_INVALID", ...)
    print("::error title=Morning Report Schema Invalid::...")
    return 1
```

失败 → 不推送、输出具体 Schema 错误、**返回 1 让 GitHub Actions 变红**。

### 3.2 Schema 增强
每个 `top_events[]` 必须含 7 个字段：

| 字段 | 校验 |
|---|---|
| `event_id` | 非空，且必须属于程序给出的 verified 集合 |
| `title` | ≥2 字 |
| `one_line` | ≥2 字 |
| `evidence_status` | 必须在 `ev.ALL_STATUSES` 五枚举内 |
| `source_urls` | 非空列表，每项必须是合法 http(s) |
| `published_at` | 非空 |
| `attribution` | 非空（来源名 + 等级） |

### 3.3 测试输入与实际输出

```
模型编造 event_id（E99，池子里没有）：
  [SKIP] SUMMARY_SCHEMA_INVALID | 早报未通过校验，拒绝推送 |
         top_events 第 1 项 event_id='E99' 不在已验证事件集合内
  ::error title=Morning Report Schema Invalid::...
  rc = 1 | Telegram 收到 0 条

模型自报证据字段（evidence_status=OFFICIAL_CONFIRMED，source_urls=evil.example）：
  卡片实际输出：🛡 单一来源 · 📰 example.com（来源未识别）
                🔗 https://example.com/a0
  → 模型自报的「官方确认」与 evil.example 均被程序覆盖
```

---

## 4. 任务 7：早报时间范围硬限制

### 4.1 原缺陷
```python
SUMMARY_RELAX_STEPS = (48, 72)   # 素材不足就放宽
```
提示词写「超过 24 小时的素材直接剔除」，程序却在素材不足时把 48h / 72h 的旧闻
混进池子。模型照着写，就成了「昨天的新闻」其实是前天的。

### 4.2 实现
1. **删除 `SUMMARY_RELAX_STEPS`** 及其整段放宽逻辑，`used_hours` 恒为 24。
2. 新增 `_pool_for_morning(state)`：只取 24h 内且发布时间可信的条目。
3. 新增 `_published_ts(item)`：**只读 `published`，不退化到 `first_seen`**。
   `_item_ts()` 在 published 缺失时会退到 first_seen（我们第一次抓到它的时间），
   那是「我们什么时候看到」而不是「新闻什么时候发生」——用它顶替等于把旧闻当新闻。
4. 未来时间：允许 5 分钟时钟偏差（`FUTURE_SKEW_TOLERANCE_MIN`），超出即拒绝。
5. Google News 紧急兜底**只解决「抓不到」**，抓回来的旧闻会被 `_pool_for_morning`
   重新过滤掉，不改变窗口。
6. 没有 24h 素材 → `_send_low_info_morning()`：**不调用模型**，发一份诚实的空报。

### 4.3 测试输入与实际输出

| 用例 | 输入 | 实际输出 |
|---|---|---|
| 23 小时前 | `published = now - 23h` | `_pool_for_morning` 保留 1 条 ✅ |
| 25 小时前 | `published = now - 25h` | 保留 0 条 ✅ |
| 未来时间 | `published = now + 3h` | 保留 0 条 ✅ |
| 发布时间未知 | `published = ""`（first_seen 有效） | 保留 0 条 ✅（不退化到 first_seen） |
| 素材不足 | 6 条 30h 旧闻 | 不调用模型、发低情报量报告、rc=0 ✅ |
| 池子为空 | 空池 + 抓取返回空 | 低情报量报告，含「低情报量」字样 ✅ |

低情报量报告实际内容：
```
🌅 【越位情报局 · 昨日情报汇总】
📅 2026-10-31 · 覆盖过去 24 小时
━━━━━━━━━━━━━━━
😴 低情报量的一天
24 小时内没有通过证据核验的新闻。
今天不发凑数内容——没有通过证据核验的新闻，就不写。
━━━━━━━━━━━━━━━
🔎 说明
本号只推送经过证据核验的内容：官方确认、多源独立印证，或明确标注的单一来源。
未核验与来源冲突的内容不会进入早报。
```

---

## 5. 新增测试清单（23 项）

| 类 | 数量 | 覆盖 |
|---|---|---|
| `test_schema.TestSummarySchemaV12` | 10 | 七字段齐全、缺 event_id / evidence_status / source_urls / published_at / attribution、非法状态枚举、非法 URL、event_id 不在允许集合 |
| `test_pipeline.TestV12MorningEvidence` | 13 | 23h 合格 / 25h 拒绝 / 未来拒绝 / 未知时间拒绝 / 不放宽窗口 / 未核验不进提示词 / 单一来源不标已确认 / event_blob 内容 / 编造 event_id 拒绝 / Schema 失败阻断 / 证据字段程序回填 / 卡片显示证据来源 |

---

## 6. 原有测试的调整（fixture 补字段，未放宽断言）

Schema 变严后，3 处 fixture 必须补上新增的必填字段，否则测的就不是「合法早报」：

| 位置 | 改动 | 性质 |
|---|---|---|
| `test_schema.TestSummarySchema.good_summary()` | top_events 补 `event_id / evidence_status / source_urls / published_at / attribution` | 断言不变，**前提变严** |
| `test_pipeline.TestMorningRegression._summary()` | 同上；`hook`/`cover_blueprint`/`image_prompt`/`title` 改为满足最小长度 | 断言不变 |
| `test_pipeline.TestV12MorningEvidence._summary()` | 新增类自带 fixture | 新增 |

> 这三处都是「用例输入补齐到新规格」，没有任何 `assert` 被删除或放宽。
> 原来 `validate_summary` 根本不在主流程里跑，这些 fixture 从未被真正校验过。

---

## 7. 早报端到端实际输出（手工验证）

```
[INFO] 早报模式启动 | 北京时间 2026-10-31 | 上次发送 无 | FORCE_SUMMARY=False
[INFO] 早报窗口过滤：未来时间 0 条 / 时间未知 0 条 / 超过 24h 1 条 → 保留 4 条
[INFO] 早报原料（state 新闻池 / 过去 24h）：4 条
[INFO] 早报证据核验：4 条素材 → 通过 3 条、未通过 1 条
[INFO]   排除 google.com | UNVERIFIED | evidence_url 是聚合器跳转链接，无法定位原始报道
[SUCCESS] 早报已推送 | 2026-10-31 | 原料 4 条 | 窗口 24h
```

实际 Telegram 卡片（节选）：
```
🌅 【越位情报局 · 昨日情报汇总】
📅 2026-10-31 · 覆盖过去 24 小时 · 素材 4 条
⚠️ 素材偏少： 仅 3 条通过核验，已按「低信息日」处理，不凑数
━━━━━━━━━━━━━━━
📌 今日头条： 红牌申诉被驳｜英超今日三条线

🔎 [今日必知]
1. 阿森纳红牌申诉被驳回（2小时前）
   英足总确认驳回
   🛡 多源独立印证 · 📰 bbc.co.uk（一线媒体）
   🔗 https://www.bbc.co.uk/sport/football/articles/c0
2. 利物浦谈妥奥利塞（3小时前）
   🛡 多源独立印证 · 📰 skysports.com（一线媒体）
   🔗 https://www.skysports.com/football/news/1
```

---

## 8. 测试结果

```
python tests/run_all.py
模块数 7 | 用例 155 | 失败 0 | 错误 0 | 耗时 55.8s

按模块：
  test_evidence        51
  test_schema          22   （第一批: 12 → +10）
  test_state_store     12
  test_ai_provider     16
  test_ai_integration  11
  test_pipeline        38   （第一批: 25 → +13）
  test_git_commit       5

python scripts/smoke_run.py           → 冒烟结果：通过
python scripts/smoke_run.py --quota   → 冒烟结果：通过
```

---

## 9. 未解决问题 / 已知取舍

| 项 | 说明 | 影响 |
|---|---|---|
| 同一事件多篇报道各占一个 event_id | E01=BBC、E02=天空，其实都是「红牌申诉被驳」。模型可能重复写两次 | 属第五批任务 14「事件级去重」范围，本批未做，避免与后续批次冲突 |
| 低情报量报告与「抓取全挂」无法区分 | 池子空时一律发低情报量报告并返回 0 | 抓取全挂时会显示「没有新闻」而不是报错。**建议在第五批任务 17 里加「源全挂」判定**，把这种情况变成 exit 1 |
| `SINGLE_SOURCE` 仍可进早报 | 按任务 5.4「不得写成已确认事实」处理：进清单但标注单一来源，Prompt 明令只能写「有报道称」 | 措辞是否合规只能靠 Prompt 约束，程序侧无法校验文案语义 |
| 早报不做黑标闸门 | 突发路径有 `black_label_gate`，早报没有 | 早报是汇总，黑标事件若进早报仍受证据状态约束，但缺一道独立闸门。待确认是否需要在第三批或后续补 |
| 未做线上验证 | 无真实 Actions 记录 | 上线后需核对 `SUMMARY_SCHEMA_INVALID` 与 `Low Information Day` 两个信号 |

---

## 10. 复现命令

```bash
python tests/run_all.py
python -m unittest tests.test_pipeline.TestV12MorningEvidence -v
python -m unittest tests.test_schema.TestSummarySchemaV12 -v
python scripts/smoke_run.py
```
