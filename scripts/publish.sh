#!/usr/bin/env bash
# =============================================================================
# 一键发布：自检 → 提交 → 推送
#
# 为什么要有这个脚本
# ------------------
# 2026-10-31 的事故：`.gitignore` 里明明写了 `__pycache__/`，但因为用 GitHub
# 网页「Add files via upload」上传，pyc 还是被提交进去了，CI 的守卫直接红。
# 人工记流程记不住，所以把「提交前必须做的三件事」写死成脚本，少做一件就不让提交。
#
# 用法
# ----
#   bash scripts/publish.sh -m "fix: 说清改了什么"                  # 提交已跟踪文件的修改
#   bash scripts/publish.sh -m "feat: xxx" bot.py tests/new.py      # 新增文件必须显式列出
#   bash scripts/publish.sh -m "docs: xxx" --skip-tests             # 只改文档时可跳过测试
#   bash scripts/publish.sh -m "xxx" --dry-run                      # 只检查不提交
#
# 固定顺序（任一步失败立即停止，绝不「先提交再说」）
# --------------------------------------------------
#   1. 环境检查：在仓库里、在 main 分支、没有卡住的 merge/rebase
#   2. 仓库卫生：索引里不得有 __pycache__ / *.pyc / *.pyo
#   3. 跑快速测试：python tests/run_all.py --fast
#   4. 暂存：只加你指定的路径，或 -u（只加已跟踪文件的修改），**从不 git add -A**
#   5. 空提交检查 + 展示将要提交的清单
#   6. commit + push（永不 force）；被机器人提交顶掉时自动 rebase 重试，最多 3 次
# =============================================================================

set -uo pipefail

MSG=""
SKIP_TESTS=0
DRY_RUN=0
PATHS=()

fail() {
  echo "::error title=Publish Aborted::$1"
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message)
      MSG="${2:-}"
      shift 2
      ;;
    -m=*|--message=*)
      MSG="${1#*=}"
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=1
      shift
      ;;
    --dry-run|-n)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    --)
      shift
      while [ $# -gt 0 ]; do PATHS+=("$1"); shift; done
      ;;
    -*)
      fail "未知参数：$1（用 --help 看用法）"
      ;;
    *)
      PATHS+=("$1")
      shift
      ;;
  esac
done

[ -n "$MSG" ] || fail "必须用 -m 提供提交说明，例如：-m \"fix: 修正早报时间\""

# ---------- 1. 环境检查 ----------
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || fail "当前目录不是 git 仓库"
cd "$ROOT" || fail "无法进入仓库根目录"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || fail "当前分支是 $BRANCH，不是 main；本脚本只允许在 main 上发布"

if [ -f .git/MERGE_HEAD ]; then
  fail "仓库处于 merge 冲突未解决状态，先处理完再发布"
fi
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  fail "仓库处于 rebase 中途，先 git rebase --abort 或 --continue"
fi

echo "[INFO] 仓库：$ROOT"
echo "[INFO] 分支：$BRANCH"

# ---------- 2. 仓库卫生 ----------
BAD="$(git ls-files | grep -E '(^|/)__pycache__/|\.py[co]$' || true)"
if [ -n "$BAD" ]; then
  echo "----- 被跟踪的字节码 -----"
  echo "$BAD"
  echo "--------------------------"
  fail "索引里有 __pycache__ / *.pyc，先执行：git rm -r --cached --ignore-unmatch __pycache__"
fi
echo "[OK] 仓库卫生：无被跟踪的字节码"

# ---------- 3. 快速测试 ----------
if [ "$SKIP_TESTS" -eq 1 ]; then
  echo "[WARN] 已用 --skip-tests 跳过测试（只建议在纯文档改动时这么做）"
else
  echo "[INFO] 运行快速测试…"
  if ! python tests/run_all.py --fast; then
    fail "测试未通过，拒绝提交"
  fi
  echo "[OK] 测试通过"
fi

# ---------- 4. 暂存 ----------
if [ "${#PATHS[@]}" -gt 0 ]; then
  echo "[INFO] 只暂存指定路径：${PATHS[*]}"
  git add -- "${PATHS[@]}" || fail "git add 失败"
else
  # 没有指定路径时只加「已跟踪文件的修改」，绝不把未跟踪的新文件偷偷带进来
  git add -u || fail "git add -u 失败"
fi

# 新文件提醒：如果工作区有未跟踪文件，明确说出来，避免「以为提交了其实没有」
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
  echo "[WARN] 以下文件未被跟踪、本次不会提交（确实要提交请把它们作为参数传进来）："
  echo "$UNTRACKED" | sed 's/^/       /'
fi

# ---------- 5. 空提交检查 ----------
if git diff --cached --quiet; then
  echo "[INFO] 暂存区为空，没有可提交的改动"
  exit 0
fi

echo "[INFO] 将要提交的改动："
git diff --cached --stat

if [ "$DRY_RUN" -eq 1 ]; then
  echo "[DRY-RUN] 只检查不提交。去掉 --dry-run 即可真正发布。"
  exit 0
fi

# ---------- 6. 提交 + 推送 ----------
git commit -m "$MSG" --quiet || fail "git commit 失败"
SHA="$(git rev-parse --short HEAD)"
echo "[OK] 已提交 $SHA"

export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never

# 推送被拒（non-fast-forward）在这里是**常态**而不是异常：机器人每轮扫描都会往
# main 提交一次 "Auto-update state.json [skip ci]"，本地很容易落后一两个提交。
# 因此这里自动 fetch + rebase 后重试，最多 3 次。
# 两条硬约束：永不 force push；rebase 冲突就整体放弃，把提交留在本地交给人处理。
push_with_rebase() {
  local attempt=1
  local max=3
  local out=""
  local rc=0
  while [ "$attempt" -le "$max" ]; do
    # 刻意不用 `git push ... | tail`：管道的退出码是 tail 的，
    # 只有在 pipefail 打开时才等于 git 的——不能把正确性寄托在调用方的 shell 选项上。
    out="$(git push origin "$BRANCH" 2>&1)"
    rc=$?
    printf '%s\n' "$out" | tail -3
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    if [ "$attempt" -ge "$max" ]; then
      return 1
    fi
    echo "[WARN] push 被拒（第 $attempt 次）：远端已被自动提交推进，rebase 后重试"
    if ! git fetch --quiet origin "$BRANCH"; then
      echo "[ERROR] git fetch 失败，无法自动 rebase"
      return 1
    fi
    if ! git rebase "origin/$BRANCH" --quiet; then
      git rebase --abort >/dev/null 2>&1 || true
      echo "[ERROR] rebase 到 origin/$BRANCH 时冲突，已回滚 rebase"
      return 1
    fi
    SHA="$(git rev-parse --short HEAD)"
    echo "[INFO] rebase 完成，当前提交 $SHA"
    attempt=$((attempt + 1))
  done
  return 1
}

if ! push_with_rebase; then
  fail "git push 失败；提交已保存在本地 $SHA，排查后手动 git push 即可"
fi

echo "[SUCCESS] 已推送 $SHA 到 origin/$BRANCH"
echo "[提示] push 会触发 verify job（check_no_cache + 快速测试）；"
echo "       想看结果：https://github.com/$(git remote get-url origin | sed -E 's#.*github.com[:/](.+)\.git#\1#')/actions"
