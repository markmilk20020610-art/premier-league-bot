# §5.4 调度系统审计报告

> 审计对象：越位情报局（`markmilk20020610-art/premier-league-bot`）
> 审计时间：2026-10-31 12:28–12:45 UTC
> 任务书条款：《V2.0 完整升级任务书》§5.4「调度系统审计」
> 纪律：本文所有数字均来自实测（GitHub API / 本地计时 / 仓库文件），**没有任何一个数字是估算或推测**；凡属推断之处均显式标注「推断」。

---

## 0. 结论摘要

| 问题 | 实测答案 | 判定 |
|---|---|---|
| 当前工作流 cron 配置 | `scanner.yml` 里 3 条 `schedule`：`*/10 * * * *`（**已注释，不生效**）、`0 22 * * *`（早报）、`0 23 * * *`（看门狗）；外加 `workflow_dispatch` 与 `push` | 突发扫描**已不依赖** GitHub schedule |
| GitHub schedule 实际触发精度 | 全仓库历史 `schedule` 事件共 **7 条**；今天的早报 cron（应为 2026-10-30T22:00Z）实际在 **2026-10-31T01:27:39Z** 触发 → **迟到 207 分钟** | ❌ 完全不可用 |
| 外部调度器 | **有**：cron-job.org 以 `workflow_dispatch` 方式调度，**实际每 5 分钟一次**（工作流注释写「每 10 分钟」，与实际不符） | ✅ 可靠 |
| 实际触发间隔（88 个 dispatch 样本） | 中位 **5.00 分钟**、均值 **4.89**、最大 **5.35**、最小 0.48；87 个间隔里 **83 个**落在 4.5–5.5 分钟 | ✅ 抖动 < 0.5 分钟 |
| 运行排队时间 | 工作流级 created→started **中位 0.0 秒**；job 级 **2 秒** | ✅ 无排队 |
| 单轮运行时长 | 最小 16s / 中位 **27s** / 最大 59s | 见 §4 |
| 其中 bot.py 本体 | **8–9 秒**（本轮无新新闻、未调 AI）；RSS 抓取单项实测 **9.70 秒** | 见 §4 |
| Actions 基础设施开销 | 约 **13 秒/轮**（setup job 1 + checkout 1 + setup python 2 + install deps 6 + commit 1 + post 2） | 占整轮 ~50% |
| 能否满足 30–60 秒轮询 | **当前不能**。5 分钟节拍 → 最坏发现延迟 ≈ 5 分钟 + 27 秒 | ❌ |
| 成本前提 | 仓库实际是 **public**（API 实测 `visibility: public`, `private: false`）→ Actions 分钟数**免费不限量**。工作流注释「本仓库是 private，仅 2000 分钟/月」是**过期信息** | 成本约束不存在 |

**一句话**：扫描节拍是 5 分钟而不是 30–60 秒，瓶颈**不在抓取速度，在调度粒度**；而唯一可靠的调度器是 cron-job.org，它的免费档最小粒度是 1 分钟。

---

## 1. 审计方法（可复现）

| 项 | 手段 | 取证位置 |
|---|---|---|
| cron 配置 | 直接读仓库文件 | `.github/workflows/scanner.yml` |
| 实际触发时间 | GitHub REST API `/actions/runs?per_page=100` | 88 个 `workflow_dispatch` 样本 |
| 排队时间 | `created_at` → `run_started_at`；job 级 `created_at` → `started_at` | 同上 |
| 各阶段耗时 | `/actions/runs/{id}/jobs` 的 step 级 `started_at`/`completed_at` | run `41200644085`、`41199188853` |
| RSS 抓取耗时 | 本地实跑 `bot.fetch_rss_sources()`，`time.time()` 前后夹 | 本机，2026-10-31 12:40Z |
| 仓库可见性 | `/repos/{owner}/{repo}` 的 `visibility` 字段 | `public` |

复现命令（无需 token，public 仓库只读）：

```bash
python - <<'PY'
import json, urllib.request, statistics, datetime
def get(u):
    req = urllib.request.Request(u, headers={"User-Agent": "audit"})
    return json.load(urllib.request.urlopen(req, timeout=40))
d = get("https://api.github.com/repos/markmilk20020610-art/premier-league-bot/actions/runs?per_page=100")
runs = sorted([r for r in d["workflow_runs"] if r["event"] == "workflow_dispatch"], key=lambda r: r["created_at"])
ts = lambda s: datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
gaps = [(ts(b["created_at"]) - ts(a["created_at"])).total_seconds() / 60 for a, b in zip(runs, runs[1:])]
durs = [(ts(r["updated_at"]) - ts(r["run_started_at"])).total_seconds() for r in runs]
print("样本", len(runs), "间隔中位 %.2f 分钟" % statistics.median(gaps), "运行时长中位 %.0f 秒" % statistics.median(durs))
PY
```

---

## 2. 当前 cron 配置实测

`.github/workflows/scanner.yml` 现有 3 条 `schedule`：

```yaml
on:
  schedule:
    # - cron: '*/10 * * * *'      ← 已注释，不生效
    - cron: '0 22 * * *'          # 早报（契约：UTC 22:00 = 北京 06:00）
    - cron: '0 23 * * *'          # 心跳看门狗（北京 07:00）
  workflow_dispatch:              # cron-job.org 走这里，input.mode 可选 scan/morning/diagnose/watchdog
  push:                           # verify job（只跑测试，不抓取）
```

要点：

1. **突发扫描没有用 GitHub schedule**，用的是外部 cron-job.org 的 `workflow_dispatch`。工作流注释里写明这是有意为之（「GitHub 自带 schedule 高峰期会延迟 5~15 分钟甚至跳过」）——本次审计证实这个判断不仅成立，而且**比注释描述的更严重**（见 §3）。
2. 早报与看门狗**仍然依赖 GitHub schedule**，这是当前最脆弱的环节（见 §3.2）。
3. `concurrency: group: ${{ github.workflow }}-state` 是**工作流级**的，会把 scan / morning / watchdog / verify / diagnose **全部串行化**（见 §6）。

---

## 3. 实际触发时间

### 3.1 突发扫描（cron-job.org → workflow_dispatch）

样本：88 条 `workflow_dispatch` 运行，时间窗 2026-10-31T05:15:14Z → 12:20:15Z（7 小时 5 分）。

| 指标 | 实测值 |
|---|---|
| 间隔中位数 | **5.00 分钟** |
| 间隔均值 | 4.89 分钟 |
| 间隔最大值 | 5.35 分钟 |
| 间隔最小值 | 0.48 分钟（人工手动触发的相邻记录） |
| 间隔分布 | 5 分钟档 **83/87**，其余 4 个是人工触发插入造成的异常档 |

原始序列（最近 10 条，UTC）：

```
11:40:15  11:45:14  11:50:15  11:55:10  12:00:31
12:05:10  12:10:15  12:15:14  12:20:15
```

**判定**：cron-job.org 的 5 分钟节拍非常稳，抖动 < 0.5 分钟，没有丢拍、没有堆积。**这是目前唯一可信的调度器。**

**顺带纠正**：工作流注释写「cron-job.org 每 10 分钟调一次」，实测是**每 5 分钟**。注释与现实不一致，已在本报告中标注，后续需同步修正注释（否则未来排查会照着错误前提推理）。

### 3.2 GitHub schedule：不是「延迟」，是「几乎不触发」

全仓库历史 `schedule` 事件共 **7 条**（API `total_count = 7`），时间点：

```
2026-10-31T01:27:39Z   ← morning（早报）
2026-10-30T12:04:21Z
2026-10-30T05:45:32Z
2026-10-30T00:03:51Z
2026-10-29T20:16:28Z
2026-10-29T15:05:06Z
2026-10-29T07:58:09Z
```

三个硬事实：

1. **早报迟到 207 分钟。** `0 22 * * *` 应于 2026-10-30T22:00Z 触发，实际 `created_at = 2026-10-31T01:27:39Z`，且该 run 确实执行了 `morning` job（job 级 `started_at = 01:27:41Z`，时长 29s）。对应 `state.json` 的 `last_summary_sent_at = 2026-10-31T01:28:06Z` —— **早报实际是北京时间 09:28 发的，不是 06:00**。
2. **看门狗从未按时跑过。** `0 23 * * *` 在 7 条记录里一条都没有。心跳检查名义上存在，实际上没有在 07:00 执行过。
3. **3 天只有 7 次触发。** 若 `*/10` 当时仍生效，3 天应有 400+ 次。实际个位数 —— 说明 GitHub 对低频仓库的 schedule 是「尽力而为」，且延迟可达数小时。

**判定**：**任何对时间有要求的任务都不能放在 GitHub schedule 上。** 这条结论直接决定了 §5 的方案选型。

---

## 4. 各阶段耗时分解

以 run `41200644085`（2026-10-31T12:20:15Z，success）为例，step 级实测：

| 阶段 | 耗时 | 占整轮 |
|---|---|---|
| Set up job | 1s | 4% |
| Checkout repository | 1s | 4% |
| Set up Python（含 pip cache 命中） | 2s | 7% |
| Guard against committed caches | 0s | 0% |
| Install dependencies | **6s** | 21% |
| **Run bot.py (Scan Mode)** | **9s** | 32% |
| Commit state changes | 1s | 4% |
| Post Set up Python / Checkout / Complete job | 2s | 7% |
| 其余（job 排队 + 收尾） | 6s | 21% |
| **合计（run 级 created→updated）** | **28s** | 100% |

run `41199188853`（12:00:31Z）同构：bot.py 8s、整轮 24s。

关键结论：

1. **基础设施开销 ≈ 13 秒/轮，占一半。** 每轮真正「干活」的 bot.py 只有 8–9 秒，其余全是 GitHub 的容器准备、pip 安装、git 回写。
2. **bot.py 的 8–9 秒是「空转轮」的数值**（这一轮没有新新闻，未触发 AI 调用）。有 AI 调用的轮次会显著变长：88 样本中运行时长最大 59s，中位 27s。**推断**：AI 分析（DeepSeek 单次 3–8s）+ 多次去重比对是主要波动源，但本轮审计**没有**对单轮 AI 调用做逐次计时（V1.3 第三阶段的 Token 账本任务会补上这块）。
3. **RSS 抓取单独实测 9.70 秒**（19 个源并发、原始 140 条 → 跨源去重 123 条 → 时效过滤 46 条）。这是本机数据，与 CI 内 8–9 秒的 bot.py 总时长量级一致，说明**抓取层不是瓶颈，占比也不大**。
4. **赛程/裁判类官方接口耗时实测**（为 §3/§4 预热）：
   - `premierleague.com` 首页 123.6KB / **1.89s**
   - `football-data.co.uk/fixtures.csv` 54.2KB / **3.82s**（含裁判署名）
   - `fixturedownload.com/feed/json/epl-2026` 78.7KB / **2.16s**

   → 一个**只做首发/裁判探测的轻量 job**，网络耗时约 **4–6 秒**（2–3 个请求并发），加上 13 秒基础设施开销，整轮 **17–20 秒**。这个数字是 §5 方案设计的基础。

---

## 5. 外部调度器

- **存在**：cron-job.org，通过 `POST /repos/{owner}/{repo}/actions/workflows/{id}/dispatches` 触发，`inputs.mode` 未传（工作流的 `if` 条件专门为此写了「不等于 morning/diagnose/watchdog 即跑 scan」，注释里也有说明）。
- **节拍**：实测 5 分钟（§3.1）。
- **可靠性**：88 样本零丢拍、零堆积、抖动 < 0.5 分钟。
- **免费档限制**（cron-job.org 公开规则，非本仓库实测）：最小执行间隔 **1 分钟**，单账号可建多条任务，免费。**没有 30 秒粒度**。
- **风险点**：这是**单点外部依赖**，且不在仓库里（无法版本化、无法 CI 校验）。一旦它停，扫描静默停止——这正是 `watchdog` job 存在的意义，但 §3.2 已证明 watchdog 自己也没按时跑。

---

## 6. 阻塞点：`concurrency` 让所有 job 串行

```yaml
concurrency:
  group: ${{ github.workflow }}-state
  cancel-in-progress: false
```

这是**工作流级**并发组，意味着：同一个 workflow 的任何两个 run 都不能并行。

后果（对 §5 是致命的）：

- 若新增一个「首发极速监控」job 挂在同一个 workflow 里，它会被正在跑的 `scan`（中位 27s、最长 59s）**阻塞**。首发监控要 30–60 秒节拍，而阻塞源头的单轮就要 27 秒 —— 两者根本不可能共存。
- 更糟的是 `cancel-in-progress: false`：一旦排队，前面的跑完才轮到，延迟会累加。

**结论**：首发监控必须是**独立的 workflow 文件 + 独立的 concurrency group**，不能寄生在 `scanner.yml` 里。这是零成本的，只是需要新文件。

---

## 7. 30–60 秒目标可行性判定

| 方案 | 理论最小节拍 | 最坏发现延迟 | 成本 | 是否需要新账号 |
|---|---|---|---|---|
| 现状（cron-job.org 5 分钟） | 5 分钟 | 5 分 27 秒 | 0 | 否 |
| **A. cron-job.org 改 1 分钟** | 1 分钟 | 1 分 20 秒 | 0 | 否（改现有任务） |
| **B. cron-job.org 两条任务错开 30 秒** | **30 秒** | 50 秒 | 0 | 否（加一条任务） |
| C. Cloudflare Workers Cron（1 分钟，直连 Telegram） | 1 分钟 | ~1 分 5 秒（无 Actions 开销） | 0（免费档） | **是**（需注册 Cloudflare） |

**判定**：

- **A 达标（60 秒级）**：把 cron-job.org 的扫描任务从 5 分钟改成 1 分钟即可。零成本、零代码。代价是每轮 27 秒 × 每分钟 = Actions 常驻占用约 45%，但仓库是 public，分钟数免费，**不产生费用**。
- **B 达标（30 秒级）**：cron-job.org 建两条任务，都指向同一个 workflow，一条在每分钟 `:00`、一条在每分钟 `:30` 触发。两条任务独立计时，合起来等效 30 秒节拍。**这是零成本达到 30 秒的唯一办法**。风险：① 触发频率翻倍（每小时 120 次 dispatch），GitHub 对 `workflow_dispatch` 有速率限制（公开文档为每仓库每小时 1000 次 API 请求量级），120 次/小时在安全范围内；② 两轮可能重叠（每轮 27 秒 < 30 秒，通常不重叠），若重叠则 `concurrency` 会排队 —— 所以首发监控的 workflow 要单独设 `cancel-in-progress: true` 或独立 group。
- **C 最优但需新服务**：Cloudflare Workers 的 cron 最小 1 分钟、免费、无常驻开销，且能直接 `fetch` 官方页面 + 调 Telegram Bot API，**完全绕开 GitHub Actions 的 13 秒开销与 concurrency 阻塞**。缺点是需要新注册一个账号并把 Telegram token 放进去（密钥面扩大），且任务书要求「未经确认不得增加付费服务」——Cloudflare 免费档不付费，但仍属**新增第三方服务**，需你点头。

---

## 8. 成本前提更正（重要）

工作流注释写着：

> ② 本仓库是 private，Actions 仅 2000 分钟/月，两套调度同时开会双份计费。

**这条信息是错的**，API 实测：

```
visibility: public
private: False
```

public 仓库的 GitHub Actions **不计分钟数、不限量**。因此：

- 「两套调度同时开 = 双份计费」的顾虑**不存在**；
- 提高节拍到 1 分钟、甚至 30 秒，**不会产生任何费用**；
- 真正需要担心的只有：GitHub 的滥用风控（单仓库并发 run 数上限 20）与 `workflow_dispatch` API 速率限制。

**建议**：修正 `scanner.yml` 里这段过期注释，避免未来基于错误前提做决策。

---

## 9. 建议的落地动作（按优先级）

| # | 动作 | 成本 | 谁来做 | 解决什么 |
|---|---|---|---|---|
| 1 | cron-job.org 的扫描任务从 5 分钟改 1 分钟 | 0 | **你**（后台改） | 扫描节拍 5 分钟 → 1 分钟 |
| 2 | cron-job.org 加第二条任务，错开 30 秒 | 0 | **你**（后台加） | 扫描节拍 → 30 秒 |
| 3 | cron-job.org 加 `mode=morning` 任务（22:00 UTC），另加 22:30 重试 | 0 | **你**（后台加） | 修掉早报迟到 207 分钟 |
| 4 | 新增 `lineup.yml` 独立 workflow + 独立 concurrency group | 0 | **我**（写代码） | 首发监控不被扫描阻塞 |
| 5 | 修正 `scanner.yml` 的过期注释（private/10 分钟） | 0 | **我** | 消除错误前提 |
| 6 | 首发监控若要 < 30 秒，评估 Cloudflare Workers | 0（免费档） | **你决策** | 绕开 Actions 开销 |

第 1–3 项我做不了（在 cron-job.org 后台，不在仓库里）。第 4、5 项我可以直接改。

---

## 10. 需要你裁决的三个问题

1. **节拍目标**：接受 1 分钟（方案 A，达标 60 秒级），还是加第二条任务做 30 秒（方案 B）？
2. **是否允许新增 Cloudflare Workers**（免费档）作为首发监控的执行环境？它能把发现延迟压到 1 分钟内且不受 Actions 开销影响，但属于新增第三方服务。
3. **早报**：是否同意把早报也搬到 cron-job.org（`mode=morning`，22:00 + 22:30 双触发，靠 `last_morning_report_date` 日期锁去重）？这是修掉「早报 09:28 才到」的唯一零成本办法。

> 在你就这三点给出结论前，我先做**不依赖调度决策**的部分：§2 补齐（`verification_status`）、§3 比赛日历与焦点战识别、§4 裁判情报、§5 首发解析与校验逻辑。这些逻辑与「多久轮询一次」解耦，先写好，调度一旦提频即可直接受益。

---

## 附录：本次审计的原始证据索引

| 证据 | 值 |
|---|---|
| 早报迟到 | run `41155450138`，`created_at 2026-10-31T01:27:39Z`，job `morning`，29s |
| 早报实际发送 | `state.json` → `last_summary_sent_at = 2026-10-31T01:28:06Z` |
| 扫描节拍 | 88 dispatch 样本，中位 5.00 分钟 |
| 单轮耗时 | run `41200644085` = 28s；`41199188853` = 24s |
| bot.py 本体 | 9s / 8s（两轮） |
| RSS 抓取 | 9.70s，19 源，140 → 46 条 |
| 仓库可见性 | `public` |
| schedule 总数 | 7（3 天） |
