#!/usr/bin/env bash
# 只提交 state.json 并把结果推回远端。
#
# 设计要点（对应整改任务书 2.3）：
#   1. 只 add/commit state.json，绝不顺手把别的改动带进去；
#   2. 不用 || true 掩盖失败：add / commit / rebase / push 任何关键步骤失败都 exit 非 0，
#      让 Job 变红，而不是留一个「看起来成功」的绿勾；
#   3. 远端冲突先试 rebase；rebase 冲不掉就用 merge -X ours 保留本地状态（本地是刚跑出来的最新真相）；
#   4. push 有限重试，最终仍失败则明确报错。
#
# 用法：bash scripts/commit_state.sh [state 文件] [最大尝试次数]

set -uo pipefail

STATE_FILE="${1:-state.json}"
MAX_ATTEMPTS="${2:-3}"

fail() {
  echo "::error title=State Commit Failed::$1"
  exit 1
}

if [ ! -f "$STATE_FILE" ]; then
  echo "[INFO] $STATE_FILE 不存在，跳过提交"
  exit 0
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

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  echo "[INFO] 第 $attempt/$MAX_ATTEMPTS 次同步并推送 origin/$BRANCH …"

  if git pull --rebase --autostash --quiet; then
    if git push --quiet; then
      echo "[SUCCESS] $STATE_FILE 已推送到 origin/$BRANCH"
      exit 0
    fi
    echo "[WARN] push 被拒（远端可能已前进），重试前先重新同步"
  else
    echo "[WARN] rebase 失败，中止后改用「保留本地状态」策略合并远端"
    git rebase --abort 2>/dev/null || true
    if git fetch --quiet origin "$BRANCH" \
       && git merge -X ours --no-edit --quiet "origin/$BRANCH"; then
      echo "[INFO] 已用 merge -X ours 合并远端变更（state.json 以本地为准）"
      if git push --quiet; then
        echo "[SUCCESS] $STATE_FILE 已推送（merge 策略）"
        exit 0
      fi
    else
      git merge --abort 2>/dev/null || true
    fi
  fi

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    SLEEP=$((attempt * 2))
    echo "[INFO] ${SLEEP}s 后重试…"
    sleep "$SLEEP"
  fi
done

fail "推送 $STATE_FILE 失败（已重试 $MAX_ATTEMPTS 次），本地状态未同步到仓库"
