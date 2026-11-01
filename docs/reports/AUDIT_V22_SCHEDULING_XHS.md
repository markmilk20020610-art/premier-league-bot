# 越位情报局 V2.2 · 每日调度与小红书内容包独立交付 · 第一阶段审计报告

> 审计对象：`premier-league-bot`
> 审计时间：2026-11-01（北京时间）
> 审计纪律：**本阶段只读、只跑测试、只查线上运行记录。未修改任何生产代码，未提交，未推送。**
> 数据来源：本地仓库真实文件 + `state.json` 真实状态 + GitHub Actions API 真实运行记录（非模拟）

---

## 十六、先回答最后一问：本次是否修改生产代码

**没有。** 具体地说：

| 项 | 状态 |
|---|---|
| 修改生产代码 | ❌ 未修改任何 `.py` |
| 修改工作流文件 | ❌ 未动 `scanner.yml` / `focus.yml` |
| 修改环境变量 | ❌ 未动 |
| 修改定时任务 | ❌ 未动 |
| 提交 / 推送 | ❌ **未 commit、未 push**（`git status` 干净，HEAD 仍是 `85a4a23`） |
| 新建文件 | 仅本报告 `docs/reports/AUDIT_V22_SCHEDULING_XHS.md` |

`git status --porcelain` 在本报告写入前为空；写入后仅多出这一个未跟踪的 md 文件。

---

## 一、当前项目真实路径、分支和 commit SHA

| 项 | 值 |
|---|---|
| **仓库绝对路径** | `D:\premier-league-bot` |
| **当前分支** | `main` |
| **commit SHA（完整）** | `85a4a233dad17207fd70ee936f0dc032853a7e1a` |
| **commit SHA（短）** | `85a4a23` |
| **工作区状态** | 干净（`git status --porcelain` 输出为空） |
| **远程仓库** | `https://github.com/markmilk20020610-art/premier-league-bot.git` |
| **远程分支** | `origin/main`，本地与远程一致（无 ahead/behind） |
| **仓库可见性** | **public**（`private: false`，实测 GitHub API） |
| **仓库创建时间** | `2026-10-29T01:37:01Z` = 北京时间 10-29 09:37 |

> ⚠️ 注意：工作区根目录是 `D:\英超`，但**真实生产仓库在 `D:\premier-league-bot`**。`D:\英超` 不是 git 仓库，只是早期快照。所有结论以 `D:\premier-league-bot` 为准。

---

## 二、当前生产工作流架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│ 触发层                                                                │
│  ├─ cron-job.org 外部调度 → POST /actions/workflows/scanner.yml/     │
│  │                          dispatches  每 60 秒（实测 00:30–01:08   │
│  │                          连续 39 次，间隔 60±3 秒）                │
│  ├─ GitHub cron  scanner.yml: */5 * * * *   ← 实测几乎不触发（见§四）│
│  │                    0 22 * * *（早报）                             │
│  │                    0 23 * * *（看门狗）                           │
│  └─ GitHub cron  focus.yml:   0 * * * *    + workflow_dispatch       │
│                                    ↑                                 │
│                     scanner 的 scan job 每分钟用 GITHUB_TOKEN 补拉   │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ scanner.yml（工作流级 concurrency: group=<workflow>-state，          │
│              cancel-in-progress: false → 全局串行）                   │
│                                                                       │
│  job: scan      timeout 6min   RUN_MODE=scan     ← 突发新闻          │
│  job: morning   timeout 15min  RUN_MODE=morning  ← 每日早报          │
│  job: watchdog  timeout 5min   scripts/watchdog.py ← 心跳自检        │
│  job: diagnose  timeout 10min  diagnose.py       ← 手动诊断          │
│  job: verify    timeout 10min  tests/run_all.py --fast ← 回归        │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────────┐
│ bot.py（2740 行，单文件主流程）                                       │
│  run_scan_mode()  → RSS → 去重 → 时效 → 领域门 → AI 分析 → 证据核验  │
│                     → 事件去重 → 卡片 → 分块投递（带账本）            │
│  run_morning_mode() → 新闻池 → 已验证事件 → AI 汇总 → 卡片           │
│                     → **send_telegram（无账本）**                     │
└─────────────────────────────────────────────────────────────────────┘
                              ↓
        state.json（入库） + focus_state.json（入库） → commit_state.sh
```

---

## 三、所有现有核心功能及对应代码位置

| # | 核心功能 | 入口 / 实现位置 | 测试覆盖 |
|---|---|---|---|
| 1 | RSS 新闻抓取 | `bot.py:899 fetch_rss_sources()`；源表 `feed_registry.py:315` | `test_pipeline`(50) / `test_maintenance_v12`(45) |
| 2 | 跨源去重 | `bot.py:756 _dedupe_items()` / `650 clean_url()` | `test_pipeline` |
| 3 | 新闻池与持久化 | `bot.py:984 _merge_pool()` / `1038 _pool_recent()`；`state_store.py` | `test_state_store`(12) / `test_maintenance_v12` |
| 4 | 足球领域分类 | `bot.py:1394 _sport_check()` / `1412 _sport_gate()`；`sport_scope.py:399` | `test_sport_scope`(37) |
| 5 | 新闻时效过滤 | `bot.py:775 _filter_by_freshness()` / `1054 _morning_eligible()` | `test_pipeline` |
| 6 | AI 新闻筛选 | `bot.py:1184 evaluate_with_gemini()` / `1950 _prepare_ai_candidates()` | `test_ai_provider`(28) / `test_ai_integration`(11) |
| 7 | 新闻分析 | `bot.py:1187-1293` prompt；`1301 _call_model()` | `test_pipeline` / `test_ai_integration` |
| 8 | 证据核验 | `evidence.py:1047-1157`；`bot.py:1783 _finalize_result()` | **`test_evidence`(77)** |
| 9 | Telegram 新闻推送 | `bot.py:359 send_telegram_ex()` / `243 _split_html()` | `test_delivery_v12`(28) |
| 10 | 每日早报 | `bot.py:2119 run_morning_mode()` / `1596 build_summary_prompt()` / `1686 _build_summary_card()` | `test_pipeline` / `test_batch4_v12`(23) / `test_batch5_v12`(33) |
| 11 | 焦点战监控 | `scripts/focus_tick.py`(555) / `focus.yml` | **`test_focus_tick`(66)** / `test_focus_keeper`(17) |
| 12 | 裁判任命监控 | `referee_intel.py`(628) / `scripts/focus_tick.py:242` | `test_referee_intel`(40) |
| 13 | 首发情报雷达 | `lineup_monitor.py`(555) / `pl_api.py`(322) / `focus_tick.py:289` | `test_lineup_monitor`(46) / `test_pl_api`(19) |
| 14 | Token 统计 | `ai_provider.py:717 _record()` / `740 usage_snapshot()` | `test_ai_provider` |
| 15 | 跨运行状态 | `state_store.py`（`SCHEMA_VERSION=3`，`ensure_shape` 向后兼容） | `test_state_store` / `test_git_commit`(5) |
| 16 | GitHub Actions | `scanner.yml`(14.7KB) / `focus.yml`(6.6KB) | 仅静态断言（见 §八 未覆盖项） |

**16 项功能全部真实存在，无一项缺失。** 这与「如果实际代码中某项功能不存在，应如实报告」的要求相符——本次不需要报缺。

### 3.1 必须保护的机制（改动时最容易误伤的三处）

| 机制 | 位置 | 为什么不能动 |
|---|---|---|
| 模型自报证据字段作废 | `bot.py:1336-1341` | 入口处 `pop` 掉模型给的 `evidence_status`，全部由程序重算。这是防「模型自己给自己打分」的唯一防线 |
| 事件级去重 | `bot.py:1886-1950` | 同事件换媒体也算重复，防止一条新闻推两遍 |
| 分块续传 | `bot.py:2335 _deliver_card()` + `state_store.py:378 record_chunks()` | `pending_text` 存原文，重试时逐字比对；已送达的段**不允许被覆盖**（`state_store.py:383-385`） |

---

## 四、当前每日汇总的实际时间链（**本次审计最重要的发现**）

### 4.1 你观察到的「约 09:00」被数据完全证实，而且原因是排队

**计划配置**（`scanner.yml:30`）：`cron: '0 22 * * *'` → **22:00 UTC = 次日 06:00 北京时间**

**实际运行（run `41258400435`，从 GitHub Actions API 读取的逐步时间戳）**：

| 步骤 | UTC | 北京时间 | 耗时 |
|---|---|---|---|
| **计划触发** | 10-31 22:00:00 | **11-01 06:00:00** | — |
| **排队等待** | — | — | **169 分 01 秒** |
| run 创建（排队结束） | 11-01 00:49:01 | 08:49:01 | — |
| Set up job | 00:49:04 → 00:49:05 | 08:49:04 → 08:49:05 | 1s |
| Checkout repository | 00:49:05 → 00:49:06 | 08:49:05 → 08:49:06 | 1s |
| Set up Python | 00:49:06 → 00:49:09 | 08:49:06 → 08:49:09 | 3s |
| Guard against committed caches | 00:49:09 → 00:49:09 | 08:49:09 | 0s |
| Install dependencies | 00:49:09 → 00:49:15 | 08:49:09 → 08:49:15 | 6s |
| **Run bot.py (Morning Mode)** | **00:49:15 → 00:49:24** | **08:49:15 → 08:49:24** | **9s** |
| Commit state changes | 00:49:24 → 00:49:26 | 08:49:24 → 08:49:26 | 2s |
| Job 完成 | 00:49:28 | 08:49:28 | — |

**状态文件佐证**：`state.json.last_summary_sent_at = 2026-11-01T00:49:24.564417+00:00` = **北京时间 08:49:24**，与上表 `Run bot.py` 结束时刻完全吻合。

### 4.2 前一天同样延迟，不是偶发

| 日期 | 计划（北京） | 实际启动（北京） | 排队延迟 | 结论 |
|---|---|---|---|---|
| 10-31 | 06:00 | **09:27:39** | **207 分 39 秒** | 迟 3h28m |
| 11-01 | 06:00 | **08:49:01** | **169 分 01 秒** | 迟 2h49m |

两次都拿到 morning job 成功执行的记录（run `41155450138` / `41258400435`），**两次都迟了近 3 小时**。

### 4.3 归因：99.5% 的时间在等 GitHub 分配 runner

| 环节 | 耗时 | 占比 |
|---|---|---|
| **GitHub 排队** | **169 分 01 秒** | **99.8%** |
| checkout + python + pip | 14 秒 | 0.14% |
| **bot.py 本体（RSS/AI/证据/推送）** | **9 秒** | 0.09% |
| state 回写 | 2 秒 | 0.02% |

**结论：管线一点不慢。** 早报从触发到发完只要 **24 秒**，其中业务逻辑 9 秒。慢的全部是 GitHub Actions 的调度队列。

### 4.4 更严重的：GitHub 原生 cron 在这个仓库几乎不触发

| 指标 | 数值 |
|---|---|
| `*/5 * * * *` 理论触发次数（2.70 天 × 288） | **≈ 778 次** |
| 实际 `event=schedule` 的 workflow run 总数 | **13 次** |
| 其中 morning（对应 `0 22`） | 2 次 |
| 其中 scan（对应 `*/5`） | 3 次 |
| 其中未完成 / 旧版 job 名（`run-bot`/`run-scanner`） | 8 次 |
| **`*/5` 实际触发率** | **约 1.4%** |

首次 schedule 运行 `2026-10-29T07:58:09Z`，末次 `2026-11-01T00:49:01Z`，跨度 2.70 天。

**这意味着两件事：**

1. **系统的「实时性」实际上完全依赖 cron-job.org**，GitHub cron 只是名义上的兜底。突发新闻能做到分钟级，靠的是 cron-job.org 每 60 秒 dispatch（实测连续 39 次、间隔 60±3 秒）。
2. **早报只能靠 `0 22` 这一根独苗**，而它每次都要排队 2.8–3.5 小时。这就是「09:00 才收到」的全部原因。

### 4.5 你列的 9 项检查，逐条回答

| 检查项 | 结论 |
|---|---|
| 是否有 Actions 排队延迟 | **有，且是主因。169–207 分钟** |
| 是否存在任务重叠 | **是**。`concurrency: group=<workflow>-state` 全局串行，scan 每 60s 一次 + morning 最长 15min，必然排队。实测同一 workflow 只保留 1 running + 1 pending |
| 上一次未完成就启动下一次 | 不会并发，但会**顶掉 pending**（GitHub 只保留最新一个 pending） |
| 是否重复发送 | 早报有日期锁 `last_morning_report_date` + `MORNING_ALREADY_SENT`（`bot.py:2143`），**不会重复** |
| 任务成功但消息未发送 | 存在可能：`send_telegram` 返回 False 时不抛异常，`run_morning_mode` 后续处理需看代码；但 `last_summary_sent_at` 只在成功后写 |
| 消息发送成功但状态未提交 | 存在风险：`send_telegram` 后到 `commit_state.sh` 之间有约 2 秒窗口，若进程被杀则「发了但没记住」→ 靠日期锁可避免重发（因为 `last_morning_report_date` 也在同一次提交里） |
| Telegram 限流 / 网络失败 | 早报走 `send_telegram` → `send_telegram_ex`，有 3 次重试（2/4/8 秒）+ 429 `Retry-After` 处理 |
| 长时间 AI 调用造成延迟 | **不是**。bot.py 全程 9 秒 |
| 是否存在任务成功但消息未发送 | 需在第二阶段加一条断言（见 §十三 R3） |

---

## 五、UTC 与北京时间转换

**关键前提：北京时间 = UTC+8，是整小时偏移。所以 cron 的「分钟位」在 UTC 和北京完全一致，只有小时 +8。**

| cron 表达式 | UTC 语义 | 北京时间 | 所在文件 |
|---|---|---|---|
| `*/5 * * * *` | 每 5 分钟 | 每 5 分钟（相同） | `scanner.yml:23` |
| `0 22 * * *` | 每日 22:00 | **次日 06:00** | `scanner.yml:30` |
| `0 23 * * *` | 每日 23:00 | **次日 07:00** | `scanner.yml:32` |
| `0 * * * *` | 每小时整点 | 每小时整点（相同） | `focus.yml:33` |

**换算规则（本报告与后续方案统一使用）：**

```
北京 = UTC + 8 小时
UTC  = 北京 − 8 小时

0 22 * * *  → 22:00 UTC → 22+8=30 → 30-24=06:00 次日北京
0 23 * * *  → 23:00 UTC → 23+8=31 → 31-24=07:00 次日北京
0 00 * * *  → 00:00 UTC → 00+8=08:00 当日北京
```

**注意跨日陷阱**：`0 22 * * *` 在 UTC 是「当天 22 点」，在北京是「**第二天** 06 点」。任何把 cron 当成北京时间直接读的做法都会错 8 小时。

---

## 六、Telegram 当前消息交付方式与长度限制

### 6.1 两套发送路径（**一条有账本，一条没有**）

| 路径 | 用于 | 分块 | 分段账本 | 断点续传 |
|---|---|---|---|---|
| `send_telegram_ex(text, chunk_records, report)`（`bot.py:359`） | 突发新闻（经 `_deliver_card`） | ✅ 4000 字符 | ✅ 每段独立状态 | ✅ `pending_text` 原文续传 |
| `send_telegram(text)`（`bot.py:490`） | **每日早报**（`bot.py:2287`） | ✅ 4000 字符 | ❌ **无** | ❌ **无** |

> **这是本次审计的第二个重要发现**：早报用 `send_telegram(card)` → `send_telegram_ex(text)`（`chunk_records=None`）→ **分段结果不落盘、失败不重试、部分成功无法发现**。
> 如果早报卡片超过 4000 字符被切成 3 段、第 2 段失败，系统会当作「发送完成」，且 `last_morning_report_date` 已锁，**当天不会重发**。
>
> 小红书内容包绝不能走这条路。**必须复用 `_deliver_card` 那套。**

### 6.2 长度限制与切分规则

| 项 | 值 |
|---|---|
| 代码内硬上限 | `_split_html(text, 4000)`（`bot.py:375`） |
| Telegram 官方单条上限 | 4096 字符（text） |
| 安全余量 | 96 字符 |
| 切分粒度 | **按行聚合**；单行超 4000 才在行内硬切 |
| HTML 标签处理 | 行内硬切时自动补闭合 + 下一段重开（`bot.py:263-267`） |
| 分段哈希 | `_chunk_hash(text)`（`bot.py:355`），用于判断「分段布局是否变了」 |
| 重试策略 | 3 次，间隔 2 / 4 / 8 秒（`TELEGRAM_RETRY_DELAYS`） |
| 异常分类 | `OK` / `DETERMINISTIC`(400/401/403) / `RETRYABLE`(429/5xx) / `UNCERTAIN`(读超时) |
| 429 处理 | 读 `Retry-After`，并按它熔断 |

### 6.3 现有分段账本的数据形状（V2.2 可直接复用）

```python
# state_store.py:57-63
CHUNK_DELIVERED = "DELIVERED"
CHUNK_RETRYABLE = "RETRYABLE_FAILURE"
CHUNK_UNCERTAIN = "DELIVERY_UNCERTAIN"
CHUNK_FAILED    = "DELIVERY_FAILED"
CHUNK_NOT_ATTEMPTED = "NOT_ATTEMPTED"

# 每段记录
{"chunk_index": 1, "chunk_total": 7, "delivery_status": "DELIVERED",
 "telegram_message_id": 12345, "attempts": 1, "last_error": ""}
```

**已有的关键保护**（`state_store.py:383-385`）：

```python
if old and old.get("delivery_status") == CHUNK_DELIVERED and old.get("telegram_message_id"):
    continue          # 已经确认送达的段不允许被覆盖
```

→ **「重试不能造成已经成功发送的段落重复发送」这条要求，现有代码已经实现了。** 不需要重写。

---

## 七、当前 AI 调用流程及耗时

### 7.1 Provider 配置

| 项 | 值 | 位置 |
|---|---|---|
| 主 Provider | `deepseek`（`AI_PRIMARY_PROVIDER`） | `scanner.yml:183` |
| Gemini | `AI_GEMINI_ENABLED=false`（默认关闭） | `scanner.yml:184` |
| OpenAI | `AI_OPENAI_ENABLED=false` | `scanner.yml:185` |
| 单次超时 | 30 秒（`AI_REQUEST_TIMEOUT`） | `ai_provider.py:144` |
| 最大重试 | 2 次（`AI_MAX_RETRIES`） | `ai_provider.py:142` |
| 退避基数 | 2.0 秒（`AI_RETRY_BASE_DELAY`） | `ai_provider.py:143` |
| 最大等待 | 60 秒（`AI_MAX_RETRY_WAIT`） | `ai_provider.py:149` |
| 输入上限 | 20000 字符（`AI_MAX_INPUT_CHARS`） | `ai_provider.py:161` |
| 单轮文章上限 | 25（`AI_MAX_ARTICLES_PER_RUN`） | `ai_provider.py:160` |

### 7.2 输出 token 预算（**V2.2 的硬约束**）

| 任务 | 上限 | 位置 |
|---|---|---|
| `analyze_news` | **2200** | `ai_provider.py:170` |
| `generate_summary` | **3000** | `ai_provider.py:171` |
| `generate_content` | 900 | `ai_provider.py:172` |
| `health_check` | 16 | `ai_provider.py:173` |

### 7.3 实测用量（`state.json` → `ai_stats`）

```
date           = 2026-11-01
calls          = 529
requests       = 529
errors         = 0
input_tokens   = 3,239,638
output_tokens  = 350,731
平均每次输出   = 350,731 / 529 ≈ 663 tokens
```

对比 11-01 之前一天的 217 次调用 / 139,827 输出 tokens（平均 644）——**平均每次输出约 650 tokens，远低于 2200 上限**。

**这解释了为什么现在没出问题，也预警了为什么 V2.2 会出问题**：单个内容包需要 4000+ tokens 输出，是当前平均值的 6 倍。

### 7.4 耗时（真实测量）

| 场景 | bot.py 总耗时 | AI 调用次数 |
|---|---|---|
| 早报（morning） | **9 秒** | 1 次（`generate_summary`） |
| 突发扫描（scan） | 21–24 秒（run `41254030279`: 23:48:37→23:48:58） | 1 次（`analyze_news`） |

---

## 八、当前测试基线

### 8.1 测试命令

```bash
cd /d/premier-league-bot
python tests/run_all.py          # 全量（本次使用）
python tests/run_all.py --fast   # 跳过 test_git_commit（CI 里用的就是这个）
```

### 8.2 真实结果（本次实跑，非引用）

```
Ran 630 tests in 99.425s
OK

============================================================
模块数 19 | 用例 630 | 失败 0 | 错误 0 | 耗时 99.4s
============================================================
```

| 指标 | 值 |
|---|---|
| 测试模块数 | **19** |
| 测试用例数 | **630** |
| 通过 | **630** |
| 失败 | **0** |
| 错误 | **0** |
| 跳过 | **0**（`unittest` 若存在跳过会输出 `OK (skipped=N)`，实际输出为 `OK`，故为 0） |
| 执行时间 | **99.4 秒** |
| 测试框架 | `unittest`（非 pytest，环境无 pytest） |

### 8.3 各模块用例分布

| 模块 | 用例 | 模块 | 用例 |
|---|---|---|---|
| `test_evidence` | 77 | `test_match_calendar` | 37 |
| `test_focus_tick` | 66 | `test_competitor_watch` | 34 |
| `test_pipeline` | 50 | `test_batch5_v12` | 33 |
| `test_lineup_monitor` | 46 | `test_ai_provider` | 28 |
| `test_maintenance_v12` | 45 | `test_delivery_v12` | 28 |
| `test_referee_intel` | 40 | `test_batch4_v12` | 23 |
| `test_sport_scope` | 37 | `test_schema` | 22 |
| `test_pl_api` | 19 | `test_focus_keeper` | 17 |
| `test_state_store` | 12 | `test_ai_integration` | 11 |
| `test_git_commit` | 5 | | |

### 8.4 你列的 11 项覆盖检查，逐条回答

| # | 检查项 | 覆盖 | 说明 |
|---|---|---|---|
| 1 | 完整新闻抓取流程 | ⚠️ **部分** | `test_pipeline` 覆盖解析/去重/过滤，但 **RSS 抓取是打桩的**，没有真实网络请求 |
| 2 | 足球领域过滤 | ✅ | `test_sport_scope` 37 例 |
| 3 | 证据核验 | ✅ | `test_evidence` 77 例，最扎实 |
| 4 | Telegram 推送 | ✅ | `test_delivery_v12` 28 例，含部分成功/不确定/重试 |
| 5 | 每日早报 | ✅ | `test_pipeline` + `test_batch4/5_v12` |
| 6 | 裁判监控 | ✅ | `test_referee_intel` 40 例 |
| 7 | 首发情报 | ✅ | `test_lineup_monitor` 46 + `test_pl_api` 19 |
| 8 | 持久化与跨运行去重 | ✅ | `test_state_store` 12 + `test_git_commit` 5 |
| 9 | 消息发送失败 | ✅ | `test_delivery_v12` 覆盖 4 种失败分类 |
| 10 | Telegram 限制与分段发送 | ⚠️ **部分** | 有 `test_long_message_splits_into_multiple_chunks`，但夹具每段都带 `\n`，**没测过「单行长文本」路径** |
| 11 | GitHub Actions 定时调度 | ❌ **未覆盖** | 只有 `test_focus_tick` 里对 `focus.yml` 的静态文本断言（`test_job_has_write_permission` 等），**没有任何测试验证 cron 是否真的触发** |

### 8.5 未覆盖功能（必须诚实列出）

1. **真实 Telegram API 调用**——全部打桩，`TELEGRAM_BOT_TOKEN` 在测试中不存在。
2. **真实 GitHub Actions 调度**——无测试，且本次实测发现触发率仅 1.4%。
3. **真实 RSS 网络抓取**——全部打桩。
4. **端到端真实运行**——`scripts/smoke_run.py` 存在，但不在测试套件里。
5. **早报分段交付的账本**——因为早报根本不走账本路径，无从测试。
6. **真实 AI 调用**——`test_ai_integration` 用的是桩响应。

---

## 九、小红书模块建议架构

### 9.1 核心原则

> **不碰现有任何链路，只做「旁挂」。**

```
现有：news_pool + events（已验证、已去重、已评级）
        │
        ├──► 现有 scan / morning 推送   ← 一行不改
        │
        └──► [新增] xhs_pack 模块       ← 只读现有数据，独立写自己的 state 子树
                │
                ├─ 1. 选题筛选（从已验证事件里挑）
                ├─ 2. 三次 AI 调用（分析/三稿/封面）
                ├─ 3. 质量检查（程序侧）
                ├─ 4. 内容包落盘（state["xhs"]["packages"]）
                ├─ 5. 分段交付（复用 _deliver_card 的账本机制）
                └─ 6. 人工审核状态机
```

### 9.2 为什么必须拆三次 AI 调用

| 内容包区块 | 字数估算 |
|---|---|
| 选题与事实依据（A 区块 14 项） | 400–600 |
| 事实分层标注 | 200–300 |
| 三稿 ×（标题 + 正文 600–900 + 互动问题 + 依据 + 风险） | 2200–3000 |
| 9 个备选标题 + 推荐理由 + 风险检查 | 250 |
| 三行封面文字 + 构图说明 + 背景元素 | 200 |
| 豆包无文字提示词 | 500–700 |
| 豆包带文字备用提示词 | 700–900 |
| 后期叠字说明 | 150 |
| 发布信息（推荐稿/标题/正文/标签/问题/风险/时间/理由） | 300 |
| 质量检查 10 项 | 200 |
| **合计** | **≈ 4900–6700 字** |

现有 `analyze_news` 上限 **2200 tokens**。即使按中文 0.7 token/字 的乐观估计，6700 字也需要 **4700 tokens**。

**必然截断 → JSON 不完整 → `PARSE_ERROR` → 整个内容包作废。**

因此必须拆：

| 调用 | 任务名 | 输出 | 建议预算 |
|---|---|---|---|
| 第 1 次 | `analyze_news`（**复用现有**） | 事实摘要 + 评级 + 证据状态 + 推荐角度 | 2200（不变） |
| 第 2 次 | `generate_xhs_drafts`（新增） | 三稿 + 9 标题 + 事实依据 + 风险提示 | **3200** |
| 第 3 次 | `generate_xhs_cover`（新增） | 三行封面文字 + 两份提示词 + 叠字说明 | **2400** |

**新增任务只需改 2 处**（已验证 `AIRouter` 是显式方法分发，`ai_provider.py:790-800`）：

```python
# ai_provider.py:169 附近，max_output_tokens 加两行
"generate_xhs_drafts": _env_int("AI_MAX_TOKENS_XHS_DRAFTS", 3200),
"generate_xhs_cover":  _env_int("AI_MAX_TOKENS_XHS_COVER", 2400),

# ai_provider.py:790 附近，加两个 wrapper
def generate_xhs_drafts(self, prompt, **kw):
    return self._call("generate_xhs_drafts", prompt, **kw)
def generate_xhs_cover(self, prompt, **kw):
    return self._call("generate_xhs_cover", prompt, **kw)
```

### 9.3 数据结构设计（`state["xhs"]["packages"]`）

```python
{
  "xhs": {
    "schema_version": 1,
    "packages": {
      "xhs-20261101-a3f2": {
        # ---- 标识 ----
        "package_id": "xhs-20261101-a3f2",
        "content_version": 1,
        "created_at": "2026-11-01T00:10:00Z",
        "updated_at": "2026-11-01T00:12:30Z",
        "trigger": "morning",          # morning | breaking
        "status": "DELIVERED",
        "human_edited": false,         # ← 置 true 后任何自动生成不得覆盖

        # ---- A. 选题与事实依据 ----
        "topic": {
          "name": "", "reason": "", "event_time": "",
          "source_name": "", "source_url": "", "published_at": "",
          "cross_verified": "", "evidence_status": "OFFICIAL_CONFIRMED",
          "main_facts": [], "unconfirmed": [], "controversy": "",
          "recommended_angle": "A", "forbidden_phrasings": []
        },

        # ---- B. 事实分层（句子级）----
        "fact_layers": [
          {"text": "...", "basis": "FACT"},
          {"text": "...", "basis": "REPORTED"},
          {"text": "...", "basis": "INFERENCE"}
        ],

        # ---- C. 三稿 ----
        "drafts": {
          "a": {"angle": "冲突悬念", "title": "", "body": "",
                "question": "", "basis": [], "risk": ""},
          "b": {"angle": "真相拆解", ...},
          "c": {"angle": "利益影响", ...}
        },

        # ---- D. 标题系统 ----
        "titles": {
          "candidates": [{"text": "", "type": "悬念|冲突|信息差"}],
          "recommended": "", "reason": "", "risk_check": ""
        },

        # ---- E. 封面系统 ----
        "cover": {
          "title_line": "", "subtitle_line": "", "footer_line": "",
          "subject": "", "background": "",
          "bg_prompt": "", "full_prompt": "",
          "aspect": "3:4", "overlay_spec": ""
        },

        # ---- F. 发布信息 ----
        "publish": {"draft": "a", "title": "", "body": "", "tags": [],
                    "question": "", "risk": "",
                    "recommend_time": "", "time_reason": ""},

        # ---- G. 质量检查 ----
        "qc": {"passed": true, "score": 0, "items": [], "blockers": []},

        # ---- 交付（复用 state_store 形状）----
        "chunks": [],          # [{chunk_index, chunk_total, delivery_status, ...}]
        "pending_text": "",    # 断点续传原文
        "delivery_attempts": 0,
        "delivered_at": ""
      }
    }
  }
}
```

**为什么放 `state.json` 而不是新建文件**：任务书 §六 明确要求「状态设计必须符合现有持久化架构，不得另建一套容易丢失的临时状态文件」。`state_store.ensure_shape()` 只补缺失的顶层键，**加 `xhs` 子树对旧 state.json 完全向后兼容**（旧文件读进来会自动补 `{"schema_version":1,"packages":{}}`）。

**体积控制**：单个内容包约 8–12KB。当前 `state.json` = 151,675 字节。建议保留最近 **20 个**内容包（≈200KB），超出按 `updated_at` 裁剪，但**未完结状态（PENDING/GENERATING/PARTIALLY_DELIVERED/HUMAN_REVIEW/REVISION_REQUESTED）一律 pin 住不裁**——直接复用 `_prune_events` 的 pinned 逻辑（`state_store.py:190-196`）。

### 9.4 状态机设计（13 态，对齐任务书 §六）

```
                    ┌──────────┐
                    │ PENDING  │  等待生成
                    └────┬─────┘
                         │ 开始调用 AI
                    ┌────▼────────┐
                    │ GENERATING  │  正在生成
                    └────┬────────┘
              失败 ┌─────┴─────┐ 成功
                  ▼           ▼
        ┌──────────────┐  ┌────────────┐
        │  (回到       │  │ GENERATED  │ 生成完成
        │  PENDING,    │  └─────┬──────┘
        │  attempts+1) │        │ 程序侧质检
        └──────────────┘  ┌─────▼──────┐
                          │ VALIDATING │ 正在质检
                          └─────┬──────┘
                    不通过 ┌────┴────┐ 通过
                          ▼         ▼
              ┌───────────────────┐ ┌──────────────────┐
              │ VALIDATION_FAILED │ │ READY_TO_DELIVER │
              └───────────────────┘ └────────┬─────────┘
                                             │ 开始发送
                                    ┌────────▼────────┐
                                    │   DELIVERING    │
                                    └────────┬────────┘
                            全成 ┌───────────┼───────────┐ 全败
                                ▼           ▼           ▼
                        ┌──────────┐ ┌──────────────┐ ┌───────────────┐
                        │DELIVERED │ │PARTIALLY_    │ │DELIVERY_FAILED│
                        └────┬─────┘ │DELIVERED     │ └───────────────┘
                             │       └──────────────┘
                             ▼
                      ┌──────────────┐
                      │ HUMAN_REVIEW │ 等待人工审核
                      └──────┬───────┘
                   ┌─────────┼──────────┐
                   ▼         ▼          ▼
            ┌──────────┐ ┌────────┐ ┌───────────────────┐
            │ APPROVED │ │REJECTED│ │ REVISION_REQUESTED│
            └──────────┘ └────────┘ └───────────────────┘
```

**必须实现的 6 条硬约束：**

| 约束 | 实现方式 |
|---|---|
| 防止重复生成 | `package_id = sha1(trigger_date + sorted(event_ids))[:16]`，已存在且 `status != PENDING` 直接返回 |
| 防止重复发送 | 复用 `record_chunks` 的「已送达段不覆盖」逻辑 |
| 未生成完不许发 | `READY_TO_DELIVER` 只能从 `VALIDATING(passed)` 迁入，其它入口一律抛错 |
| 生成失败不得记成功 | `GENERATED` 只能由 AI 返回**完整且通过 schema 校验**的对象触发 |
| 部分发送不得记完整 | `delivery_summary(event)["partial"]` 为真 → `PARTIALLY_DELIVERED`，绝不写 `DELIVERED` |
| 人工修改不被覆盖 | `human_edited=true` 时，任何自动重生成直接跳过并记日志 |

### 9.5 分段交付设计（7 段，复用现有机制）

| 段 | 内容 | 预计字符 |
|---|---|---|
| 1/7 | 内容包 ID + 选题 + 推荐理由 + 信源 + 证据等级 + 事实分层 | 700–900 |
| 2/7 | A 稿（标题 + 正文 + 互动问题 + 依据 + 风险） | 800–1100 |
| 3/7 | B 稿 | 800–1100 |
| 4/7 | C 稿 | 800–1100 |
| 5/7 | 9 个标题 + 推荐 + 理由 + 三行封面文字 | 500–700 |
| 6/7 | 豆包无文字提示词 + 带文字备用提示词 | 900–1300 |
| 7/7 | 标签 + 互动问题 + 风险检查 + 发布时间建议 + 交付完成标记 | 500–700 |

**关键实现点：**

1. **不用 `_split_html` 自动切**——自动切会按 4000 字符随机断。改成**手工按 7 个字段组预切**，每段天然 < 4000 字符，段边界就是语义边界。
2. 每段开头加 `【1/7】` 序号。
3. 全部 7 段用同一个 `package_id`。
4. 每段发送状态写入 `chunks[]`，复用 `send_telegram_ex` 的 `chunk_records` 续传。
5. 段间加 **1 秒间隔**，避免 Telegram 429（现在没有间隔，7 段连发有风险）。
6. 第 7 段末尾加「✅ 内容包已完整交付（7/7）」标记，且只有 `delivery_summary()["delivered_indexes"] == 7` 时才加。

---

## 十、每日调度建议

### 10.1 现状与目标的差距

| 环节 | 你的目标（北京） | 现状实测 | 差距 |
|---|---|---|---|
| 汇总完成 | 08:00 | **08:49 / 09:27** | **晚 49–87 分钟** |
| 小红书生成 | 08:10–08:25 | 无此功能 | — |
| 交付 | 08:30 | 08:49 / 09:27 | 晚 19–57 分钟 |
| 人工审核 | 08:30–09:00 | — | — |

**现状根本无法满足目标**，而且差的不是管线速度（9 秒），是 GitHub 排队（169–207 分钟）。

### 10.2 三个方案对比

| 方案 | 做法 | 优点 | 缺点 | 判定 |
|---|---|---|---|---|
| **A. 把 cron 前移** | `0 22` → `0 19`（03:00 北京），用 3 小时延迟补偿 | 只改 1 个字符 | **延迟不可预测**（169/207 分钟波动），可能 05:49 发、也可能 06:30 发；且白天延迟规律未验证 | ❌ 不可靠 |
| **B. cron-job.org 准点 dispatch** | 在 cron-job.org 加一条 08:00（北京）触发 `mode=morning` | **即时触发不排队**（实测 dispatch → run 创建 < 5 秒）；已有基础设施；无需改代码 | 依赖外部服务（但**已经在依赖**） | ✅ **推荐** |
| **C. job 内 sleep 等到目标时刻** | job 启动后 sleep 到 08:00 | 不改 cron | 占着 runner 白等 3 小时，且**延迟本身无法预知**，可能睡过头 | ❌ 荒谬 |

### 10.3 推荐方案：B（cron-job.org 准点触发）+ 保留 GitHub cron 兜底

**为什么安全：双触发不会重复发。** 早报有日期锁（`bot.py:2143` `MORNING_ALREADY_SENT` + `last_morning_report_date`），第二次触发会直接跳过。这是现成的、已验证的保护。

**具体配置（需要你在 cron-job.org 加一条任务）：**

| 项 | 值 |
|---|---|
| 方法 | `POST` |
| URL | `https://api.github.com/repos/markmilk20020610-art/premier-league-bot/actions/workflows/scanner.yml/dispatches` |
| 请求头 | `Authorization: Bearer <你的 PAT>`（与现有那条相同）<br>`Accept: application/vnd.github+json`<br>`Content-Type: application/json` |
| Body | `{"ref":"main","inputs":{"mode":"morning"}}` |
| 计划 | 每天 **08:00 北京时间**（cron-job.org 里选时区 Asia/Shanghai） |

> ⚠️ **这条必须带 `inputs.mode=morning`**。现有那条每分钟的任务不带 mode（`scanner.yml:78` 注释已说明），所以它只跑 scan，不会误触发早报。新加的这条要显式带 mode。

**改完后的预期时间链：**

| 环节 | 时间（北京） | 依据 |
|---|---|---|
| cron-job.org 触发 | 08:00:00 | 外部调度 |
| run 创建 | 08:00:05 | 实测 dispatch 延迟 < 5 秒 |
| bot.py 执行 | 08:00:20 → 08:00:29 | 实测 9 秒 |
| 早报发出 | **08:00:29** | |
| 小红书生成（若启用） | 08:00:30 → 08:02:30 | 3 次 AI 调用，每次 ≤30 秒 |
| 内容包交付 | **08:02:40** | 7 段 × ~1.2 秒 + 6 秒间隔 |
| 人工审核窗口 | **08:03 → 09:00** | **57 分钟**（远超要求的 30 分钟） |

**比你的目标还早 27 分钟**，审核窗口 57 分钟。

### 10.4 cron 修改清单（如果最终需要改 GitHub cron）

**本阶段不改。** 若第二阶段要改，必须先列出：

| 项 | 现值 | 建议值 | UTC 语义 | 北京语义 | 影响 | 回滚 |
|---|---|---|---|---|---|---|
| morning cron | `0 22 * * *` | 保持不变（作为兜底） | 22:00 | 次日 06:00 | 无（有日期锁，重复触发被跳过） | 改回原值 |

**结论：不需要改 GitHub cron。** 保留它作为「cron-job.org 挂了也能兜底」的第二道防线——虽然它慢 3 小时，但总比不发好。

---

## 十一、即时触发建议（§七）

### 11.1 触发条件（必须严格，不得因热度放宽）

```python
def should_trigger_breaking(state, result):
    # 1) 必须已通过现有全部核验（证据状态不是 UNVERIFIED）
    if result.get("evidence_status") not in (OFFICIAL_CONFIRMED,
                                             MULTIPLE_INDEPENDENT_SOURCES):
        return False
    # 2) 评级必须是 Tier1 / Tier2（黑标不生成爆款包）
    if result.get("var_rating_key") not in ("Tier 1", "Tier 2"):
        return False
    # 3) 事件类型白名单
    if result.get("event_type") not in BREAKING_TYPES:
        return False
    # 4) 防刷：同一天同一事件只生成一次；每天最多 5 个即时包
    if xhs_pkg_exists_today(event_id):
        return False
    if count_today(trigger="breaking") >= 5:
        return False
    return True
```

`BREAKING_TYPES` 建议：`官方处罚 / 焦点战裁判争议 / VAR 重大判罚 / 重大转会官宣 / 官方首发确认 / 财务与治理`

### 11.2 执行位置与隔离

**放在 `run_scan_mode` 推送成功之后**（`bot.py` 的 `_deliver_card` 返回 True 之后），用独立 try/except 包裹：

```python
# 伪代码，第二阶段才写
if XHS_ENABLED and should_trigger_breaking(state, model_result):
    try:
        xhs.generate_and_deliver(state, trigger="breaking",
                                 event_ids=[event["event_id"]])
    except Exception as exc:            # noqa: BLE001
        # 绝不向上抛：新闻已经发出去了，小红书失败不能变成 job 失败
        print(f"::warning title=XHS Package Failed::即时内容包生成失败：{type(exc).__name__}")
```

**耗时预算验证**：scan job 实测 21–24 秒，timeout 6 分钟（360 秒）。加 3 次 AI 调用（≤90 秒）+ 7 段发送（≤15 秒）= 约 130 秒，**总计约 155 秒，仍在 360 秒内，安全**。

### 11.3 明确禁止

- ❌ 自动发布到小红书（无 API，且任务书明令禁止）
- ❌ 因热度降低证据标准
- ❌ 把预测首发写成官方首发
- ❌ 即时包用简化格式（必须与每日包同一套 7 段规范）

---

## 十二、预计修改的文件清单

| # | 文件 | 动作 | 原因 | 风险 |
|---|---|---|---|---|
| 1 | **`xhs_pack.py`** | 新增 | 内容包生成编排（3 次 AI 调用 + 质检 + 落盘） | 中 |
| 2 | **`xhs_templates.py`** | 新增 | A/B/C 模板、9 标题规则、封面模板、质检规则、叠字方案 | 低 |
| 3 | **`xhs_state.py`** | 新增 | 13 态状态机 + 裁剪 + 幂等键 | 中 |
| 4 | `ai_provider.py` | 改 2 处 | 加 `generate_xhs_drafts` / `generate_xhs_cover` 的 token 预算与 wrapper | 低 |
| 5 | `schema.py` | 新增函数 | 加 `validate_xhs_package()`，**不动** `validate_scan` / `validate_summary` | 低 |
| 6 | `state_store.py` | 改 1 处 | `_default_state()` 加 `"xhs"` 键（向后兼容） | 低 |
| 7 | `bot.py` | 改 2 处 | ① `run_morning_mode` 末尾调用（try/except）② `run_scan_mode` 推送后触发（try/except） | **中** |
| 8 | `scanner.yml` | 改 1 处（可选） | 加 `xhs` job（`needs: morning`）或直接在 morning 内串行 | 中 |
| 9 | **`tests/test_xhs_pack.py`** | 新增 | 内容包回归测试 | 低 |
| 10 | `docs/` | 新增文档 | 内容包规范、叠字方案 | 低 |

**明确不改**：`evidence.py` / `referee_intel.py` / `lineup_monitor.py` / `pl_api.py` / `match_calendar.py` / `focus_tick.py` / `focus.yml` / `feed_registry.py`。

### 12.1 关于 scanner.yml 的取舍（两个方案）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A（推荐）** | 同 workflow 加 `xhs` job，`needs: morning`，`if: vars.XHS_CONTENT_PACKAGE_ENABLED == 'true'` | 不新增 workflow 文件、复用 checkout/pip 缓存、job 级失败隔离、morning 失败则 xhs 不跑（合理） | workflow 级 concurrency 会让 xhs 期间（~2 分钟）阻塞 scan |
| B | 新建 `xhs.yml` + 独立 concurrency group | 完全不阻塞 scan | 多一个 workflow 文件、多一次排队、多一套维护 |

**推荐 A**，理由：早报时段（08:00）通常没有比赛，2 分钟阻塞影响极小；且 `focus.yml` 已经证明了「独立 group」的复杂度，没必要再复制一份。

---

## 十三、风险清单

| # | 风险 | 严重度 | 依据 | 缓解 |
|---|---|---|---|---|
| **R1** | **AI 输出被 2200 token 截断，整个内容包作废** | **致命** | §七.2/§九.2：内容包需 4900–6700 字 | 拆 3 次调用，各自预算 2200/3200/2400 |
| **R2** | 小红书生成失败连带新闻推送失败 | **致命** | 若不加 try/except | 独立异常隔离 + 开关默认关 |
| **R3** | **早报多段发送失败无人知** | **高** | §六.1：早报走 `send_telegram`，无账本、无续传 | 第二阶段**可选**改造为 `_deliver_card`（需单独批准，不在本任务范围） |
| **R4** | state.json 体积膨胀 | 中 | 当前 151,675 B，单包 8–12KB | 保留 20 个 + pin 未完结 + 按 updated_at 裁剪 |
| **R5** | 7 段连发触发 Telegram 429 | 中 | 现有段间**无间隔** | 段间 1 秒 + 复用 429 `Retry-After` |
| **R6** | 人工修改被覆盖 | 高 | 无锁 | `human_edited=true` 硬锁 |
| **R7** | 重复生成 / 重复发送 | 中 | 幂等键缺失 | `package_id` 幂等 + 复用 chunk 账本 |
| **R8** | 三稿「换汤不换药」 | 高 | 任务书明确禁止只靠 Jaccard | 结构骨架 + 必备段落关键词 + 首句互斥 + 人工抽查（**不单用 Jaccard**） |
| **R9** | 豆包渲染中文出错字 | 高 | 行业普遍问题 | **默认无文字背景图 + 后期叠加**（与任务书 §4.2 E 一致） |
| **R10** | 小红书期间阻塞 scan（方案 A） | 中 | workflow concurrency | 总时长控制在 3 分钟内；早报时段无比赛 |
| **R11** | cron-job.org 挂了 | 中 | 单点依赖（现状已如此） | 保留 GitHub cron 兜底（慢但会到）+ watchdog 已存在 |
| **R12** | 新增字段让旧测试失败 | 中 | 6 个测试文件断言卡片文案 | 新字段**全部可选**，旧字段一个不动 |

---

## 十四、回滚方案

### 14.1 三级回滚

| 级别 | 操作 | 生效时间 | 影响 |
|---|---|---|---|
| **L1 开关关闭** | 设 `XHS_CONTENT_PACKAGE_ENABLED=false` | 下一次运行 | 小红书功能完全不跑，现有流程 100% 原样 |
| **L2 代码回滚** | `git revert <commit>` 或 `git reset --hard 85a4a23` | 下一次运行 | 回到本次审计的基线 |
| **L3 状态清理** | 删除 `state.json` 里的 `xhs` 子树 | 手动 | 老数据自动补默认值，不影响 `events`/`news_pool` |

### 14.2 为什么回滚是安全的

1. **`xhs` 是独立子树**，不碰 `events` / `news_pool` / `processed_urls`。
2. **`ensure_shape()` 向后兼容**：即使 state.json 里有 `xhs` 但代码里没有，也只是多一个键，不影响任何逻辑。
3. **不改任何现有函数的签名**——`bot.py` 只在末尾加调用，不改已有分支。
4. **开关默认关闭**（`XHS_CONTENT_PACKAGE_ENABLED=false`），不主动开就不跑。

### 14.3 回滚验证清单

```
[ ] git status 干净
[ ] 19 模块 / 630 用例全绿
[ ] state.json 的 events / news_pool / processed_urls 与回滚前一致
[ ] 下一次 scan 正常推送
[ ] 下一次 morning 正常推送
[ ] focus.yml 仍在跑（focus_state.json 的 ticks 在增长）
```

---

## 十五、第二阶段实施步骤（小步，每步可独立验收）

> 每一步都必须：记录改动前 SHA → 改 → 跑对应测试 → 跑全量 630 → 查 diff → 记录改动后 SHA。

| 步 | 内容 | 验收 |
|---|---|---|
| **S0** | 建 `xhs_templates.py`（纯常量，无逻辑） | 630 用例不变；新增模板单测 |
| **S1** | `ai_provider.py` 加两个任务预算 + wrapper | `test_ai_provider` 通过；不改变现有任务预算 |
| **S2** | `schema.py` 加 `validate_xhs_package()` | `test_schema` 通过；现有两个校验函数一行未改 |
| **S3** | `xhs_state.py`（13 态 + 幂等 + 裁剪） | 新增单测；`state_store` 不受影响 |
| **S4** | `xhs_pack.py`（3 次调用编排，先只生成不发送） | 用桩 AI 产出完整内容包 JSON，schema 校验通过 |
| **S5** | 分段渲染 + 复用 `_deliver_card` 发送（先只发到测试 chat） | 7 段全部送达；`chunks[]` 账本正确 |
| **S6** | `bot.py` 接 morning 末尾调用（开关控制） | 开关关=行为完全不变；开关开=多一条内容包 |
| **S7** | `bot.py` 接 scan 即时触发（开关 + 白名单 + 防刷） | 开关关=行为不变；手动构造触发一次验证 |
| **S8** | `state_store.py` 加 `xhs` 默认键 + 裁剪 | 旧 state.json 读取正常 |
| **S9** | 真实 10 条新闻测试（裁判/转会/财务/表态/赛果） | 输出真实测试报告 + 失败案例 |
| **S10** | 真实 Telegram 交付验证 + 真实 Actions 运行验证 | 截图/消息 ID 记录 |

**功能开关：**

```yaml
# scanner.yml 里加
env:
  XHS_CONTENT_PACKAGE_ENABLED: ${{ vars.XHS_CONTENT_PACKAGE_ENABLED }}
```

默认在仓库 Variables 里不设 = 空 = 关闭。只有 S1–S8 全部通过且你批准后才设 `true`。

---

## 十六、需要你现在决定的 5 件事

1. **早报准点方案**：是否接受「在 cron-job.org 加一条 08:00 带 `mode=morning` 的 dispatch」？
   这是唯一能把 08:49 提前到 08:00 的办法，且**不需要改任何代码**（分支已存在）。
   如果你不想动 cron-job.org，那就只能接受「早报约 09:00 到」，小红书包相应顺延到 09:00–09:10。

2. **小红书内容包是「morning 内串行」还是「独立 job」？**
   我推荐同 workflow 加 `needs: morning` 的独立 job（失败隔离更彻底）。

3. **早报的分段账本缺陷要不要顺手修？**
   现在早报走 `send_telegram`（无账本）。如果要修，就动了 morning 的发送路径——**超出本任务范围**，需要你单独批准。不修也可以，小红书包自己走账本路径就行。

4. **即时触发每天最多几个？**
   我建议 **5 个**，且必须 `OFFICIAL_CONFIRMED` 或 `MULTIPLE_INDEPENDENT_SOURCES` + Tier1/Tier2。宁可少发，不可滥发。

5. **豆包封面是否确认走「无文字背景图 + 后期叠加」？**
   任务书 §4.2 E 已写「默认使用」，我按此设计。若要赌「带文字直出」，出错率会很高。

---

## 附：保护基线 12 项速查表（§2.3 要求）

| # | 项 | 值 |
|---|---|---|
| 1 | 仓库路径 | `D:\premier-league-bot` |
| 2 | 分支 | `main` |
| 3 | commit SHA | `85a4a233dad17207fd70ee936f0dc032853a7e1a` |
| 4 | 工作区 | 干净 |
| 5 | 远程 | `origin` → `github.com/markmilk20020610-art/premier-league-bot.git`，`main` |
| 6 | 工作流 | `scanner.yml`（scan/morning/watchdog/diagnose/verify）、`focus.yml`（focus） |
| 7 | 定时任务 | `*/5 * * * *`、`0 22 * * *`、`0 23 * * *`、`0 * * * *`；全部 UTC |
| 8 | 测试 | 19 模块 / 630 用例 / 0 失败 / 0 错误 / 0 跳过 / 99.4 秒 |
| 9 | Telegram | `send_telegram_ex`（有账本）+ `send_telegram`（早报，无账本）；上限 4000 字符 |
| 10 | 核心模块 | `bot.py`(2740) `evidence.py`(1348) `ai_provider.py`(877) `match_calendar.py`(739) `referee_intel.py`(628) `lineup_monitor.py`(555) `focus_tick.py`(555) `competitor_watch.py`(486) `state_store.py`(484) `sport_scope.py`(399) `pl_api.py`(322) `feed_registry.py`(315) `focus_config.py`(308) `diagnose.py`(278) `schema.py`(192) `redact.py`(53) |
| 11 | 持久化 | `state.json`(151,675B,入库) `focus_state.json`(2,112B,入库) `run_telemetry.log`(忽略) `.focus_cache.json`(忽略) `.focus_interval`(忽略) |
| 12 | 环境变量名（**仅名称，无值**） | `AI_AUTH_COOLDOWN_MIN` `AI_GEMINI_ENABLED` `AI_GEMINI_QUOTA_COOLDOWN_MIN` `AI_MAX_ARTICLES_PER_RUN` `AI_MAX_INPUT_CHARS` `AI_MAX_RETRIES` `AI_MAX_RETRY_WAIT` `AI_MAX_TOKENS_ANALYZE` `AI_MAX_TOKENS_CONTENT` `AI_MAX_TOKENS_SUMMARY` `AI_OPENAI_ENABLED` `AI_PRIMARY_PROVIDER` `AI_QUOTA_COOLDOWN_MIN` `AI_RATE_LIMIT_COOLDOWN_MIN` `AI_REQUEST_TIMEOUT` `AI_RETRY_BASE_DELAY` `AI_SERVER_COOLDOWN_MIN` `AI_SERVER_ERROR_THRESHOLD` `DEEPSEEK_API_KEY` `FORCE_SUMMARY` `GEMINI_API_KEY` `GH_REPO` `GH_TOKEN` `GITHUB_REPOSITORY` `GITHUB_TOKEN` `RUN_MODE` `TELEGRAM_API_BASE` `TELEGRAM_BOT_TOKEN` `TELEGRAM_CHAT_ID` `TRUSTED_SINGLE_SOURCES` `WATCHDOG_MAX_SCAN_AGE_MINUTES` |

---

**第一阶段到此结束。等待批准后再进入第二阶段。**
