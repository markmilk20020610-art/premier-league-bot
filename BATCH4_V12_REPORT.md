# V1.2 第四批执行报告（任务 12–13）

范围：突发新闻 12h 硬时效、URL 清洗只清跟踪参数。
原则：增量修改；不删测试、不放宽断言；不引入付费服务；不输出真实密钥。

测试结果（真实执行，非声称）：
- `python tests/run_all.py` → **206 用例，失败 0，错误 0，耗时 46.9s**
- 本批新增 `tests/test_batch4_v12.py` → **23 用例**
- 基线 183 → 206（+23），原有 183 项全部保留并通过
- `python scripts/smoke_run.py` → 通过（`投递事件 [('COMMITTED', 9001)]`）
- `python scripts/smoke_run.py --quota` → 通过

---

## 任务 12｜突发新闻 12h 硬时效

### 问题定位
`fetch_rss_sources()` 的时效过滤是三级退让，最后一级直接把门槛拆了：

```python
fresh = _filter_by_freshness(rss_items, FRESHNESS_HOURS)          # 8h
if len(fresh) < 12: fresh = _filter_by_freshness(..., 24)          # 24h
if len(fresh) < 12 and len(rss_items) > len(fresh):
    fresh = rss_items                                             # 全量，不限时间
```

两个洞：

1. **退回全量**：安静时段（凌晨、国际比赛日）抓不到 12 条新稿时，三天前的稿子会一起
   进候选池，靠 Prompt 里一句「只要突发」兜底。模型没有时间概念，它只看到一段文字，
   于是**越是没新闻的时候，越容易把旧闻当突发推出去**——这正好是最需要准确的时候。
2. **`_filter_by_freshness` 的 `age is None → 保留`**：源没给 `pubDate` 时条目被无条件放行，
   「没有时间戳」成了绕过时效的后门。一篇三天前的稿子只要缺日期就能当突发。

### 修复

**输入端（候选池）**

```python
FRESHNESS_HOURS = 8              # 优先窗口
FRESHNESS_HARD_CAP_HOURS = 12    # 硬上限，任何情况下不得突破
MIN_FRESH_CANDIDATES = 12
FRESHNESS_ALLOW_UNKNOWN = False  # 发布时间未知是否放行（默认否）
```

流程改为：8h → 不够就放宽到 **12h** → 到此为止。删除「退回全量」分支，
`FRESHNESS_FALLBACK_HOURS`（24h）常量整体移除（有测试断言它已不存在）。

`_filter_by_freshness()` 改为返回 `(保留条目, 时间不可信条数)`，并新增三处拒绝：
- `published is None` → 排除（可配置开关 `FRESHNESS_ALLOW_UNKNOWN`，默认关）
- 发布时间在未来超过 5 分钟容差 → 排除（时钟不可信）
- 超过窗口 → 排除

日志必须说清每一条的去向：

```
[INFO] 时效硬过滤：37 条 → 9 条（窗口 12h / 硬上限 12h；超时排除 26 条，发布时间未知或不可信排除 2 条）
::warning title=No Fresh Candidates::12h 内没有任何带明确发布时间的新闻，本轮不喂模型
```

**输出端（推送前二次闸门）**

输入端卡住候选还不够：模型可能引用新闻池（保留 72h）或 grounding 记录里的旧文章。
因此 `_finalize_result()` 新增第三道闸门，按**证据文章自己的发布时间**复核：

```python
def _breaking_freshness_gate(data, published_index):
    # evidence_url 归一化 → 查发布时间索引 → 超过 12h 直接拒绝
```

- 证据文章在索引里且 > 12h → `STALE_BREAKING_NEWS`，打 `::warning title=Stale Breaking News::`
- 证据文章发布时间在未来 → 同样拒绝
- 证据文章不在本轮集合里（模型引用了集合外页面）→ 无法判定，不拦（不能凭空拦截）

`published_index` 由 `_published_index(rss_items, news_pool)` 构建，在 `run_scan_mode`
里传入。`_finalize_result` 的第三个参数是可选关键字参数，旧调用方式（两个参数）不受影响。

### 验收测试（`TestTask12BreakingFreshness`，12 项）
- **核心反例**：只有 30 小时前的旧闻时，候选池为空（旧逻辑会退回全量）
- 发布时间未知 → 排除；发布时间在未来 → 排除
- 8h 内只有 3 条时放宽到 12h，恰好收进 6 条（10h 的进来、13h 的进不来）
- 8h 内够 12 条时不放宽（实际用到 8h 窗口）
- 硬上限常量确为 12，且 `FRESHNESS_FALLBACK_HOURS` 已不存在
- 证据文章 30h 前 → `STALE_BREAKING_NEWS` 且原因里带小时数
- 证据文章 2h 前 → 通过；11.5h（紧贴边界）→ 通过
- 证据文章不在索引里 → 不拦
- 证据文章发布时间在未来 → `STALE_BREAKING_NEWS`
- 不传索引（旧调用方式）→ 通过

### 需要你知道的取舍
「发布时间未知即排除」会让**长期不提供 `pubDate` 的源彻底失效**。这是有意的：
无法证明「刚发生」，就不能算突发。代价是这个源的稿子再也进不了突发池
（仍可通过其它源的同题报道进入）。日志里 `发布时间未知或不可信排除 N 条` 就是给这件事
留的观测口；若实测发现某个重要源因此失联，把 `FRESHNESS_ALLOW_UNKNOWN` 置 True 即可回退，
而不需要改逻辑。

---

## 任务 13｜URL 清洗只清跟踪参数

### 问题定位
`clean_url()` 把整个查询串抹掉，只留 `scheme + netloc + path`：

```
https://x.com/article?id=1001  →  https://x.com/article
https://x.com/article?id=1002  →  https://x.com/article     ← 两条不同的新闻撞成一条
```

后果是「已处理」误判：第二条新闻被判为重复，静默不发——又一个「绿勾但没消息」的来源。
反过来，跟踪参数（`utm_*`、`oc=5`、`fbclid`…）确实必须清掉，否则同一篇稿子换个后缀
就能绕过拦截，被重复推送。两者必须分开处理。

### 修复

```python
TRACKING_PARAM_PREFIXES = ("utm_", "at_", "pk_", "mtm_", "hsa_", "_hs", "ns_")
TRACKING_PARAMS = {oc, ocid, gclid, fbclid, msclkid, igshid, ito, CMP, ref, ref_src,
                   source, campaign, mc_cid, _ga, smid, spm, ...}
```

`clean_url()` 新规则：
- 只剔除跟踪参数，**保留**文章身份参数
- 剩余参数按参数名排序（`?a=1&b=2` 与 `?b=2&a=1` 等价）
- scheme / host 小写化，去掉 `#fragment`，去掉路径末尾多余的 `/`（根路径除外）
- 幂等：`clean_url(clean_url(u)) == clean_url(u)`

实测：

```
https://news.google.com/rss/articles/CBMiABC?oc=5
  → https://news.google.com/rss/articles/CBMiABC
https://x.com/a?id=1001&utm_source=rss  → https://x.com/a?id=1001
https://x.com/a?id=1002&utm_source=rss  → https://x.com/a?id=1002   ← 不再相撞
https://www.theguardian.com/.../x?CMP=share_btn_tw&utm_source=twitter
  → https://www.theguardian.com/.../x
```

**升级兼容**：`state.json` 里的历史 `processed_urls` 是旧规则（剥光查询串）写下的。
只比新键会让老数据全部失配，把已推过的新闻重推一遍。因此新增：

```python
def _processed_sets(urls):   # → (exact, lossy)
def _matches_processed(url, sets)
```

- `exact`：新规则键，精确比对
- `lossy`：只收「清洗后完全没有查询串」的历史键，按旧形态兜底命中
- 新规则写下的键带身份参数，**不进** `lossy`——否则 `?id=1002` 会被 `?id=1001` 误伤，
  那正是本次要修的 bug

### 验收测试（`TestTask13UrlCleaning`，11 项）
跟踪参数七种形态全清｜**核心反例**：`?id=1001` 与 `?id=1002` 必须不同｜
混合场景只清跟踪参数、保留身份参数｜参数顺序归一｜fragment 去除｜
末尾斜杠归一（根路径不动）｜host/协议大小写归一｜幂等｜空串/非法输入不炸｜
旧规则历史键仍能命中｜新规则键不会被宽松比对误伤

---

## 改动文件

| 文件 | 改动 |
|---|---|
| `bot.py` | 时效常量改为 8h/12h 硬上限，删除 24h 与全量回退；`_filter_by_freshness` 返回元组并拒绝时间未知/未来时间；新增 `_published_index` / `_breaking_freshness_gate` / 闸门三；`clean_url` 改为只清跟踪参数；新增 `_legacy_clean_url` / `_processed_sets` / `_matches_processed` |
| `tests/test_batch4_v12.py` | **新增**，23 用例 |
| `tests/run_all.py` | 注册新测试模块 |

## 未做（留给后续批次）

- 投递失败时扫描 Job 仍返回 0（退出码虚报）→ 第五批任务 17
- 早报路径没有黑标闸门 → 待裁决
- 真实 GitHub Actions 运行验证：本批只在本地跑通 206 项测试与两个冒烟场景，
  尚未有线上运行记录，不作线上验证声明。
- 建议线上观察一次 `时效硬过滤` 日志里的「发布时间未知排除 N 条」，
  确认没有重要源因缺 `pubDate` 整体失联。
