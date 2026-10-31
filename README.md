# 越位情报局

英超情报自动采集 → 证据核验 → Telegram 推送。跑在 GitHub Actions 上，零服务器成本。

## 架构

```
RSS 矩阵（19 源，失败自动降级 Google News）
  → 候选治理（URL 规范化 / 去重 / 过滤已处理 / 事件指纹）
  → AI 分析（DeepSeek 唯一启用，Gemini 接口保留但关闭）
  → 证据核验（程序算：独立来源、转载合并、冲突识别）
  → Schema 校验
  → Telegram 推送 → 投递状态机 → state.json 回写
```

模块划分：

| 文件 | 职责 |
|---|---|
| `bot.py` | 主流程：抓取、候选治理、卡片渲染、推送、早报 |
| `ai_provider.py` | 模型调用统一入口：Provider、熔断、预算、调用统计 |
| `evidence.py` | 证据核验：五种证据状态、独立来源计算、评级复核 |
| `schema.py` | 模型输出严格校验 |
| `state_store.py` | 原子持久化 + 投递状态机 + 崩溃恢复 |
| `redact.py` | 凭证脱敏：日志 / 注解 / state 里的 Token 一律打码 |
| `diagnose.py` | 只读诊断脚本 |
| `scripts/commit_state.sh` | 只提交 state.json 的 Git 回写脚本（语义合并，不整份覆盖） |
| `scripts/merge_state.py` | state.json 语义合并：按投递进度取优、URL 取并集、日期锁取更晚 |
| `scripts/watchdog.py` | 心跳看门狗：扫描/早报静默停摆时让 Actions 自己报红 |
| `scripts/check_no_cache.sh` | 仓库卫生守卫：禁止提交 `__pycache__` / `*.pyc` |
| `scripts/smoke_run.py` | 本地端到端冒烟（不消耗任何额度） |

## 部署

1. 仓库 Secrets 配置：`DEEPSEEK_API_KEY`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`。
   `GEMINI_API_KEY` 可留空（Gemini 默认关闭）。
2. 调度：
   - 突发扫描由 cron-job.org 每 10 分钟调用 `workflow_dispatch`（不传 mode 即走 scan）；
   - 每日早报走 GitHub 自带 `cron: '0 22 * * *'`（= 北京时间 06:00，中国无夏令时，全年固定 UTC+8）；
   - 心跳看门狗 `cron: '0 23 * * *'`（= 北京 07:00）：扫描停超 3 小时、或 08:00 后当天早报仍未发，Job 直接报红。
   - ⚠️ 早报时间契约：`cron` 与 `bot.py` 的 `MORNING_TARGET_HOUR_CN` 必须同时改，
     `tests/test_maintenance_v12.py` 会解析 workflow 并断言二者对应，改一半就测试失败。
3. 手动触发：Actions → Run workflow
   - `mode=scan` 跑一轮扫描
   - `mode=morning` 跑早报，可勾 `force` 绕过「今日已发」锁
   - `mode=diagnose` 只读诊断
   - `mode=watchdog` 手动跑一次心跳检查

## 模型策略

默认 `AI_PRIMARY_PROVIDER=deepseek`，`AI_GEMINI_ENABLED=false`。
关闭状态下不会构造 Gemini client，也不会产生任何 Gemini 请求。

主要环境变量见 `AI_PROVIDER_REPORT.md` 第七节。常用几个：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AI_MAX_ARTICLES_PER_RUN` | 25 | 每轮送模型的文章上限 |
| `AI_MAX_CALLS_PER_DAY` | 60 | 每日调用上限，超出直接暂停 |
| `AI_QUOTA_COOLDOWN_MIN` | 120 | 配额耗尽后的熔断时长 |
| `TRUSTED_SINGLE_SOURCES` | 空 | 可信单一来源白名单（逗号分隔域名）。空 = 单一来源一律不推送 |

## 证据状态

| 状态 | 含义 | 自动推送 |
|---|---|---|
| `OFFICIAL_CONFIRMED` | 官方渠道确认**同一件具体事件** | 是 |
| `MULTIPLE_INDEPENDENT_SOURCES` | 2 个以上**可识别域名的**独立来源 | 是 |
| `SINGLE_SOURCE` | 单一来源 | **默认否**，仅白名单域名可推，且必须标注「未经独立交叉验证」 |
| `UNVERIFIED` | 无证据 / 证据对不上 / 官方指向别的事件 | 否 |
| `CONFLICTING` | 来源说法冲突 | 否 |

证据由程序计算，模型自报的 `evidence_status`、`evidence_sources_count` 一律被覆盖。

补充规则（V1.1 第一批）：

- 官方确认要求主体 / 事件类型 / 关键数字 / 标题相关度四项同时对上；
  官方域名报道了**另一件事**时，该记录被剔除并降级为 `UNVERIFIED`。
- 无法识别身份的域名（UNKNOWN）**不计入**交叉印证，再多也最多算 1 个来源。
- 同一篇通稿被多家转载、同一家通讯社的多条稿，只算 1 个来源。
- 黑标 / 死亡 / 重伤 / 重病 / 违法 / 重大事故必须有官方确认或多源印证才发；
  模型自己写 `var_rating=黑标` 不能绕过这条。

## 测试

```bash
python tests/run_all.py          # 全部（含真实 git 仓库测试，约 50s）
python tests/run_all.py --fast   # 跳过 git 测试，约 2s
python scripts/smoke_run.py      # 本地端到端冒烟
python scripts/smoke_run.py --quota   # 模拟配额耗尽
```

## 故障排查

先跑 `diagnose`，它会把三层问题一次说清：

1. **抓取层**：每个源的 HTTP 状态、是否走了降级；
2. **去重层**：候选里多少条已被处理、新闻池过去 24/48/72h 各有多少条；
3. **AI 层**：当日调用次数、错误次数、token、是否处于熔断。

常见日志：

| 日志 | 含义 |
|---|---|
| `[SKIP] NO_NEW_ARTICLE` | 没有新文章，未调用 AI（正常） |
| `[SKIP] MODEL_FAILED` | 模型全部失败，本轮暂停，候选已保留 |
| `[SKIP] EVIDENCE_NOT_PUSHABLE` | 证据不足，拒绝推送 |
| `[SKIP] MORNING_ALREADY_SENT` | 今日早报已发，需 `force` 补发 |
| `::warning title=AI Quota Exhausted::` | 配额耗尽，已熔断 |
| `::error title=State Commit Failed::` | state.json 没推回仓库，去重与新闻池会失效 |

## 状态文件

`state.json` 由 Actions 自动提交回仓库，包含：

- `processed_urls`：已推送 URL（去重）
- `news_pool`：24~72 小时新闻池（早报原料）
- `events`：投递状态机（DISCOVERED → VERIFIED → PENDING_DELIVERY → DELIVERED → COMMITTED）
- `ai_stats`：当日调用次数、错误次数、token
- `circuit_breakers`：熔断状态

**不要手工编辑**，格式损坏会导致去重记录全部失效（程序会直接拒绝启动）。
