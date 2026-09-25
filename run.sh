#!/usr/bin/env bash
# 情绪app 启动包装：加载 .env 后执行 `python -m emotion_app "$@"`
#
# 为什么需要它：应用只读环境变量（不解析 .env 文件），
# 而把令牌写进 shell 历史或每次手敲都不合适 —— 统一从这里注入。
#
# 用法：
#   ./run.sh check      # 只校验配置
#   ./run.sh serve      # 起服务
#   ./run.sh token      # 生成一个新令牌
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

if [[ ! -f .env ]]; then
  echo "❌ 缺少 .env —— 复制 .env.example 为 .env 并填好令牌" >&2
  exit 1
fi

# set -a 让 source 进来的变量自动导出给子进程
set -a
# shellcheck disable=SC1091
source .env
set +a

exec .venv/bin/python -m emotion_app "$@"
