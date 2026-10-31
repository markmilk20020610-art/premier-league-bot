#!/usr/bin/env bash
# 只提交 state.json 并把结果推回远端。
#
# 设计要点：
#   1. 只 add/commit state.json，绝不顺手把别的改动带进去；
#   2. 不用 || true 掩盖失败：add / commit / rebase / push 任何关键步骤失败都 exit 非 0，
#      让 Job 变红，而不是留一个「看起来成功」的绿勾；
#   3. V1.2 第六批：rebase 冲不掉时**不再**用 `git merge -X ours` 整份覆盖远端。
#      state.json 不是代码，整份覆盖会丢掉远端刚记下的「已送达」「今日早报已发」，
#      直接后果是重复推送 / 同一天发两次早报。改为调用 scripts/merge_state.py 做语义合并，
#      再 reset --soft 到远端之上重新提交，得到干净的线性历史。
#   4. push 有限重试，最终仍失败则明确报错。
#
# 用法：bash scripts/commit_state.sh [state 文件] [最大尝试次数]

set -uo pipefail

STATE_FILE="${1:-state.json}"
MAX_ATTEMPTS="${2:-3}"
REMOTE_STATE=".state-remote.json"

# 语义合并脚本按「本脚本所在目录」定位，而不是按 cwd：
# CI 里两者相同，但被别处（测试夹具 / 手工调试）以绝对路径调用时，
# 依赖 cwd 会变成「找不到 scripts/merge_state.py」的假故障。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MERGE_PY="$SCRIPT_DIR/merge_state.py"

fail() {
  echo "::error title=State Commit Failed::$1"
  exit 1
}

if [ ! -f "$STATE_FILE" ]; then
  echo "[INFO] $STATE_FILE 不存在，跳过提交"
  exit 0
fi

if [ ! -f "$MERGE_PY" ]; then
  fail "缺少 $MERGE_PY，无法做 state 语义合并（不允许退回整份覆盖）"
fi

git config --local user.email "action@github.com"
git config --local user.name "GitHub Action"

git add -- "$STATE_FILE" || fail "git add $STATE_FILE 失败"

if git diff --cached --quiet; then
  echo "[INFO] $STATE_FILE 无变化，跳过提交"
  exit 0
fi

git commit -m "Auto-update state.json [skip ci]" -- "$STATE_FILE" || fail "git commit 失败"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
  fail "处于 detached HEAD，无法推送（checkout 时请保留 ref）"
fi

merge_with_remote() {
  # 用语义合并替代 merge -X ours：两边都是「刚跑出来的真相」，谁都不能整份覆盖谁
  git fetch --quiet origin "$BRANCH" || return 1
  rm -f "$REMOTE_STATE"
  if git show "origin/$BRANCH:$STATE_FILE" > "$REMOTE_STATE" 2>/dev/null; then
    python "$MERGE_PY" "$STATE_FILE" "$REMOTE_STATE" || {
      rm -f "$REMOTE_STATE"
      return 1
    }
  else
    echo "[WARN] 远端没有 $STATE_FILE，跳过合并，直接以本地为准"
  fi
  rm -f "$REMOTE_STATE"

  # 把当前提交挪到远端之上：工作区保留合并后的 state.json，历史保持线性
  git reset --soft "origin/$BRANCH" || return 1
  git add -- "$STATE_FILE" || return 1
  if git diff --cached --quiet; then
    echo "[INFO] 合并后与远端一致，无需新提交"
    return 0
  fi
  git commit -m "Auto-update state.json [skip ci]" -- "$STATE_FILE" || return 1
  return 0
}

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  echo "[INFO] 第 $attempt/$MAX_ATTEMPTS 次同步并推送 origin/$BRANCH …"

  if git pull --rebase --autostash --quiet; then
    if git push --quiet; then
      echo "[SUCCESS] $STATE_FILE 已推送到 origin/$BRANCH"
      exit 0
    fi
    echo "[WARN] push 被拒（远端可能已前进），重试前先重新同步"
  else
    echo "[WARN] rebase 失败，改用「按语义合并 state」策略（不再整份覆盖远端）"
    git rebase --abort 2>/dev/null || true
    if merge_with_remote; then
      if git push --quiet; then
        echo "[SUCCESS] $STATE_FILE 已推送（语义合并策略）"
        exit 0
      fi
      echo "[WARN] 语义合并后 push 仍被拒，进入下一轮重试"
    else
      echo "[WARN] 语义合并失败，进入下一轮重试"
    fi
  fi

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    SLEEP=$((attempt * 2))
    echo "[INFO] ${SLEEP}s 后重试…"
    sleep "$SLEEP"
  fi
done

fail "推送 $STATE_FILE 失败（已重试 $MAX_ATTEMPTS 次），本地状态未同步到仓库"
