#!/usr/bin/env bash
# =============================================================================
# 仓库卫生守卫：不允许把 Python 字节码缓存提交进仓库
#
# 为什么要有这个检查：
#   1. __pycache__/*.pyc 会进 git 历史，每次 checkout 都多几 MB，长期只增不减；
#   2. 更严重的是「代码没同步」会被掩盖 —— 本机跑过的旧字节码如果被带上 CI，
#      排查问题时看到的行为和源码对不上，是最难查的一类幽灵 bug；
#   3. 不同 Python 小版本（3.10 / 3.11 / 3.12）的 pyc 魔数不兼容，
#      提交上来对别人毫无用处，纯属噪音。
#
# 本脚本只做只读检查，不删除任何文件；发现违规直接让 Job 失败并给出修复命令。
# =============================================================================
set -uo pipefail

# 在非 git 环境（例如本机直接把目录拷出来跑测试）下不应误报失败，
# 只提示跳过，保证脚本在任何地方执行都是安全的。
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "::notice title=No Git Repo::当前目录不是 git 仓库，跳过字节码跟踪检查"
  exit 0
fi

FAIL=0

# ---- 检查 1：有没有被 git 跟踪的 __pycache__ 目录或 .pyc/.pyo/.pyd 文件 ----
TRACKED="$(git ls-files | grep -E '(^|/)__pycache__/|\.py[cod]$' || true)"
if [ -n "$TRACKED" ]; then
  echo "::error title=Committed Bytecode::仓库里存在被跟踪的 __pycache__ / *.pyc，请执行 git rm -r --cached 后重新提交"
  echo "----- 违规文件 -----"
  echo "$TRACKED"
  echo "--------------------"
  FAIL=1
fi

# ---- 检查 2：.gitignore 必须把字节码挡住，否则下次还会被误提交 ----
if [ -f .gitignore ]; then
  if ! grep -qE '(^|/)__pycache__/?$|\*\.py\[cod\]' .gitignore; then
    echo "::error title=gitignore Missing Bytecode Rule::.gitignore 缺少 __pycache__ / *.py[cod] 规则"
    FAIL=1
  fi
else
  echo "::error title=gitignore Missing::仓库根目录没有 .gitignore"
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  exit 1
fi

echo "::notice title=Cache Guard OK::未发现被跟踪的字节码缓存"
exit 0
