# V1.2 第六批执行报告（项目维护 + 早报时间修正）

范围：早报时间契约（北京时间 06:00）、GitHub 仓库维护、state.json 语义合并、
事件裁剪保护、移除写死的主帅映射、心跳看门狗。
原则：增量修改；不删测试、不放宽断言；不引入付费服务；不输出真实密钥。

测试结果（真实执行，非声称）：

- `python tests/run_all.py` → **281 用例，失败 0，错误 0，耗时 58.6s**
- 本批新增 `tests/test_maintenance_v12.py` → **42 用例**
- 基线 239 → 281（+42），原有 239 项全部保留并通过
- `python scripts/smoke_run.py` → 通过（`exit_code: 0`、`requests: 1`、投递 `COMMITTED`）
- `python scripts/smoke_run.py --quota` → 通过（退出码 0，自愈型状态，熔断 120 分钟）
- `python scripts/watchdog.py` → 退出码 0（空 state 走宽限）
- `bash scripts/check_no_cache.sh` → 退出码 0（非 git 环境提示跳过）

> 线上验证：本项目仍**没有**真实 GitHub Actions 运行记录，因此本报告不声明
> 「已在线验证」。以下全部是本地可复现的执行结果。

---

## 一、早报时间修正：准确北京时间 06:00（用户明确要求）

### 契约
```
GitHub cron（UTC）        北京时间（UTC+8，中国无夏令时）
'0 22 * * *'        →     每天 06:00
```

### 代码侧（`bot.py`）
```python
MORNING_TARGET_HOUR_CN = 6
MORNING_TARGET_MINUTE_CN = 0
MORNING_SCHEDULE_TOLERANCE_MIN = 25     # GitHub 高峰期延迟 5~15 分钟，超 25 分钟必须可见

def _cn_now(): ...
def _morning_offset_minutes(now_cn=None):
    """当前北京时间距离 06:00 差多少分钟（正数=晚于目标）。"""
```
`run_morning_mode` 启动第一件事就打偏差：

```
[INFO] 早报模式启动 | 北京时间 2026-03-01 06:03 | 目标 06:00 | 偏差 +3 分钟 | ...
```

偏差超过容忍值且不是手动补发时：

```
::warning title=Morning Off-Schedule::早报在 07:40（北京）触发，偏离目标 06:00 达 +100 分钟（容忍 25 分钟）
```

为什么只告警不硬退：`FORCE_SUMMARY` 手动补发本来就该允许任意时间，
而 GitHub 自带的 schedule 延迟是平台行为，硬退等于当天不发早报——
那是比「晚 10 分钟」严重得多的故障。所以选择**让它可见**，而不是让它更糟。

### workflow 侧
```yaml
- cron: '0 22 * * *'    # 早报：UTC 22:00 = 北京次日 06:00
- cron: '0 23 * * *'    # 看门狗：UTC 23:00 = 北京 07:00
```
`scan` job 的 `if` 显式排除这两个 schedule，避免整点被 cron-job.org 与
GitHub schedule 双重触发。

### 契约由测试守护（关键）
注释写「必须对应」等于没写。`tests/test_maintenance_v12.py` 里：

- `test_cron_maps_to_six_am_beijing`：正则解析 workflow 的 `cron`（跳过被注释的行），
  按 UTC+8 换算后断言等于 `MORNING_TARGET_HOUR_CN/MINUTE_CN`；
- `test_morning_job_bound_to_same_cron`：`morning` job 的 `if` 必须绑同一个表达式；
- `test_scan_job_does_not_steal_morning_slot`：`scan` 必须排除这两个 cron；
- `test_off_schedule_warning_is_visible` / `test_on_time_run_has_no_warning`
  / `test_force_summary_suppresses_off_schedule_warning`：告警只在「非补发且偏离」时出现。

以后谁改 cron 忘了改常量（或反过来），提交前就会测试失败。

---

## 二、GitHub 与项目维护

### 1. 字节码缓存守卫（`scripts/check_no_cache.sh`）
`__pycache__` / `*.pyc` 进仓库有三个后果：历史只增不减；不同 Python 小版本的
pyc 魔数不兼容；最要命的是**掩盖「代码没同步」**——本机旧字节码带上 CI，
看到的行为和源码对不上，是最难查的一类幽灵 bug。

脚本只读检查三件事，任何一条不满足直接让 Job 失败：
1. `git ls-files` 里有没有被跟踪的 `__pycache__` / `*.py[cod]`；
2. `.gitignore` 有没有对应规则（否则下次还会误提交）；
3. 非 git 环境（本机拷目录跑测试）只提示跳过，不误报。

`scan` / `morning` / `diagnose` 三个 job 都在装依赖前先跑它。

### 2. 依赖版本锁定（`requirements.txt`）
```
google-genai==2.34.0
requests==2.34.2
```
无人值守的机器人不能容忍「上游悄悄升了个 patch」。锁版本 = 让 CI 行为可复现，
升级必须是一次显式动作（改版本号 → 跑全量测试 → 两个冒烟 → 再合并）。
测试 `test_requirements_are_pinned` 用正则强制每行都是 `name==x.y[.z]`。

### 3. Action 钉到 commit SHA
```
actions/checkout@11d5960a326750d5838078e36cf38b85af677262   # v4
actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065  # v5
```
tag（`@v4`）可以被上游改指向，SHA 不能。测试断言：所有 `uses:` 的版本段必须是
40 位十六进制、不得是 `main/master/latest`、且必须带 `# vN` 注释（否则半年后没人看得懂）。

### 4. 禁止 `git merge -X ours` 静默覆盖（`scripts/commit_state.sh` + `scripts/merge_state.py`）
旧脚本在 rebase 失败时执行 `git merge -X ours origin/main`。对代码文件没问题，
对 `state.json` 是**数据丢失**：

- 远端刚记下「某条新闻已送达」，本地这次运行不知道 → merge 后记录消失 → 下一轮重复推送；
- 远端刚写下今天的早报日期锁，本地 merge 掉 → 同一天再发一次早报。

两边都是「刚跑出来的真相」，谁都不能整份覆盖谁，只能按语义合并：

| 字段 | 合并规则 |
|---|---|
| `events` | 按 `event_id` 取并集；同 ID 取投递进度更靠前者（`COMMITTED > DELIVERED > DELIVERY_UNCERTAIN > DELIVERY_FAILED > PENDING/RETRYABLE > VERIFIED > DISCOVERED`），进度相同取 `updated_at` 更晚 |
| `chunks` | 按 `chunk_index` 合并，已送达段（`DELIVERED` + `message_id`）不被降级 |
| `processed_urls` | 并集，保留最后 200 条 |
| `news_pool` | 按链接并集，同链接保留发布时间更晚者，按时间倒序截断 150 |
| 日期锁 / 时间戳 | 取更晚的 |
| `ai_stats` | 同一天取 `calls` 更大者，**不累加**（两边统计的是同一批调用） |
| `circuit_breakers` | 按 provider 取 `open_until` 更晚者 |

写回用 `tmp + os.replace` 原子替换，不留半截文件。

**本批在此处抓到两个真实 bug：**

1. `_merge_pool` 去重时只更新了 `seen` 字典，`out` 列表里留的还是旧对象，
   结果「更新的那份」算进去了却没生效 —— 早报按发布时间过滤，等于把刚抓到的新条目
   当旧闻丢掉。已改为记录下标并写回槽位（`test_pool_union_dedup_sorted_desc` 抓到的）。
2. `commit_state.sh` 里 `python scripts/merge_state.py` 依赖 cwd。
   CI 里 cwd 就是仓库根目录所以没事，但被绝对路径调用时（测试夹具、手工调试）
   会变成「找不到文件」的假故障。改为按 `BASH_SOURCE` 定位脚本目录，
   并在文件缺失时明确 `::error`，绝不退回整份覆盖。

配套测试：`test_git_commit.py::test_remote_conflict_merges_instead_of_overwriting`
把旧断言「冲突后只剩本地」改成「两边都必须保留」——这不是放宽，
是**收紧**：旧行为会丢远端记录，新断言直接检查并集。

### 5. 事件裁剪保护（`state_store._prune_events`）
事件表超过 200 条会裁剪。但「结果未知 / 待重试 / 待投递」的事件如果被裁掉，
那条等人工确认的新闻就随第 201 条一起静默消失。现在三类未完结状态被 pin 住不裁。
`test_prune_keeps_unfinished_events` 用 213 条事件验证：三个 pinned 全留，
普通事件裁到 keep 上限附近。

### 6. 移除写死的主帅映射（`bot.py`）
原 prompt 里有一张「最新事实防错映射表」：曼城 Guardiola、切尔西 Maresca、
曼联 Amorim、利物浦 Slot、阿森纳 Arteta、热刺 Postecoglou。

问题不是「写得对不对」，而是**这类表必然过期**：换帅是英超最高频的变动，
写死的主帅名单几周内就会变成错误事实，然后被模型当成官方口径写进推送。
比「不知道」严重得多。

替换为「人事信息铁律」：
- 教练 / 老板 / 管理层姓名与其所属俱乐部**只能来自素材原文**；
- 素材没写就一律不许写；
- 严禁凭训练记忆补写；记忆与素材冲突时无条件以素材为准；
- 单一来源的换帅传闻按 `SINGLE_SOURCE` 处理，只能写「有报道称」。

测试 `TestNoHardcodedStaff` 直接扫 `bot.py` 源码，六个名字一个都不许出现。

### 7. 心跳看门狗（`scripts/watchdog.py`）
前五批修的都是「发出去的内容对不对」，但还有一类故障是**彻底静默**：
cron-job.org 停了、schedule 被跳过、Secrets 过期、仓库被归档……
每一轮都正常退出，Actions 上什么都看不到，只有用户那边「好几天没消息」。

看门狗独立于 `bot.py` 运行（`contents: read`，只读 state.json），看两个心跳：

| 检查 | 阈值 | 说明 |
|---|---|---|
| `last_scan_at` | > 3 小时 | 扫描本应每 10 分钟一次 |
| `last_morning_report_date` | 北京 08:00 后仍不是今天 | 早报每天 06:00 |

配套：`bot.run_scan_mode` 每轮都写 `state["last_scan_at"]`（在 `_merge_pool` 之前），
`NO_NEW_ARTICLE` 分支也补了 `save_state`——否则「今天真没新闻」和「根本没在跑」
在看门狗眼里长得一模一样。

空仓库宽限：没有任何历史痕迹时给一次机会，避免刚 clone 就红叉教人忽略告警。

---

## 三、新增/修改文件

| 文件 | 变更 |
|---|---|
| `bot.py` | 早报目标时间常量与偏差告警；`last_scan_at` 心跳；删除两处主帅映射表 |
| `requirements.txt` | 两个依赖锁到精确版本 |
| `.github/workflows/scanner.yml` | 早报 cron 契约注释；watchdog job；Action 钉 SHA；三个 job 加缓存守卫 |
| `scripts/check_no_cache.sh` | 新增：字节码跟踪守卫 |
| `scripts/merge_state.py` | 新增：state.json 语义合并 |
| `scripts/watchdog.py` | 新增：心跳看门狗 |
| `scripts/commit_state.sh` | 移除 `merge -X ours`；按脚本目录定位合并器；文件缺失明确报错 |
| `tests/test_maintenance_v12.py` | 新增 42 用例 |
| `tests/test_git_commit.py` | 冲突断言改为「并集不丢数据」（收紧） |
| `tests/run_all.py` | 注册新模块 |
| `README.md` | 补模块表、调度说明、早报时间契约提醒 |

---

## 四、遗留问题（需要你裁决）

1. **低情报量报告 vs 抓取全挂仍不完全可分**。任务 17 的 `_source_health` 已经能区分
   「源全挂 → exit 1」和「源正常但没新闻 → exit 0」，但早报路径的
   `_send_low_info_morning` 在「池子空」时发的仍是同一条「低情报量的一天」文案。
   是否要在文案里区分「今天真没事」和「抓取异常」？后者其实应该报红。
2. **早报路径没有黑标闸门**。突发扫描有「严肃新闻红线」，早报汇总走的是另一套 prompt，
   目前只有证据等级约束。若某天池子里混进死亡/重伤类素材，早报的措辞不受红线规则约束。
3. **`datetime.utcnow()` 已废弃**（`bot.py:237` telemetry）。Python 3.12+ 会告警，
   未来版本会移除。改动会影响日志时间戳格式（从裸时间变成带 `+00:00`），
   现有解析方（若有）需要同步，因此本批没动。

---

## 五、结论

第六批 17 项任务全部落地。本批的特点是没有新功能，全是护栏：
时间契约、供应链固定、合并语义、裁剪保护、心跳告警。

价值不在「今天多推了一条新闻」，而在于**下一次改动不会悄悄把系统改坏**——
尤其是早报时间这一条：它现在被三处（cron、常量、测试）同时钉住，
任何单边修改都会立刻失败。
