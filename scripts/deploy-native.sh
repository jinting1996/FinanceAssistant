#!/usr/bin/env bash
# PanWatch 本地裸机部署脚本(不依赖 Docker):venv + 前端构建 + uvicorn 后台进程
#
# 用法:
#   bash scripts/deploy-native.sh              # 部署/升级并启动,默认端口 18123
#   PANWATCH_PORT=9000 bash scripts/deploy-native.sh   # 自定义端口
#   bash scripts/deploy-native.sh stop         # 停止
#   bash scripts/deploy-native.sh restart      # 重启(不重新装依赖/构建)
#   bash scripts/deploy-native.sh status       # 查看运行状态
#   bash scripts/deploy-native.sh logs         # 跟踪日志
#   SKIP_FRONTEND=1 bash scripts/deploy-native.sh      # 只改了后端时跳过前端构建
#
# 依赖:python3(≥3.10)、node + pnpm(仅前端构建需要)。
# 数据:SQLite 等运行时文件在 ./data,升级不受影响。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PORT="${PANWATCH_PORT:-18123}"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
PID_FILE="$ROOT/data/panwatch-native.pid"
LOG_FILE="$ROOT/data/panwatch-native.log"

mkdir -p "$ROOT/data"

_pid() {
  [ -f "$PID_FILE" ] && cat "$PID_FILE" 2>/dev/null || true
}

_alive() {
  local pid
  pid="$(_pid)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

do_stop() {
  if _alive; then
    local pid
    pid="$(_pid)"
    echo "==> 停止 PanWatch (pid=$pid)..."
    kill "$pid"
    for _ in $(seq 1 15); do
      _alive || break
      sleep 1
    done
    if _alive; then
      echo "进程未退出,强制结束"
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "已停止"
  else
    echo "未在运行"
    rm -f "$PID_FILE"
  fi
}

do_start() {
  if _alive; then
    echo "已在运行 (pid=$(_pid)),先执行 stop 或用 restart" >&2
    exit 1
  fi
  if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误: 端口 $PORT 已被占用" >&2
    exit 1
  fi
  echo "==> 启动 PanWatch (端口 $PORT)..."
  nohup "$PY" -m uvicorn server:app --host 0.0.0.0 --port "$PORT" \
    >> "$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  echo -n "==> 等待服务就绪"
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      echo
      echo "✅ 部署完成: http://127.0.0.1:$PORT"
      echo "   日志: bash scripts/deploy-native.sh logs"
      echo "   停止: bash scripts/deploy-native.sh stop"
      return 0
    fi
    _alive || { echo; echo "⚠ 进程已退出,最近日志:"; tail -30 "$LOG_FILE"; exit 1; }
    echo -n "."
    sleep 2
  done
  echo
  echo "⚠ 60秒内健康检查未通过,最近日志:" >&2
  tail -30 "$LOG_FILE" >&2
  exit 1
}

do_status() {
  if _alive; then
    echo "运行中 (pid=$(_pid), 端口 $PORT): http://127.0.0.1:$PORT"
  else
    echo "未运行"
  fi
}

case "${1:-deploy}" in
  stop)    do_stop; exit 0 ;;
  restart) do_stop; do_start; exit 0 ;;
  status)  do_status; exit 0 ;;
  logs)    exec tail -f -n 200 "$LOG_FILE" ;;
  deploy)  ;;
  *) echo "未知命令: $1(支持 deploy/stop/restart/status/logs)" >&2; exit 1 ;;
esac

# ---------- deploy: 依赖 + 构建 + (重)启动 ----------

command -v python3 >/dev/null 2>&1 || { echo "错误: 未找到 python3" >&2; exit 1; }

# 1) Python venv + 依赖
if [ ! -x "$PY" ]; then
  echo "==> 创建虚拟环境 .venv"
  python3 -m venv "$VENV"
fi
echo "==> 安装/更新 Python 依赖"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt

# 2) 前端构建(SKIP_FRONTEND=1 跳过)
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  if command -v pnpm >/dev/null 2>&1; then
    echo "==> 构建前端 (frontend/dist)"
    (cd frontend && pnpm install --silent && pnpm build)
  elif [ -f frontend/dist/index.html ]; then
    echo "⚠ 未安装 pnpm,复用已有的 frontend/dist 构建产物"
  else
    echo "错误: 未安装 pnpm 且不存在 frontend/dist,无法提供前端页面。" >&2
    echo "  安装: npm install -g pnpm(或 corepack enable)" >&2
    exit 1
  fi
elif [ ! -f frontend/dist/index.html ]; then
  echo "⚠ SKIP_FRONTEND=1 但 frontend/dist 不存在,页面将不可用(仅 API)"
fi

# 3) .env 准备
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠ 已从 .env.example 生成 .env,建议编辑 AUTH_USERNAME/AUTH_PASSWORD/JWT_SECRET/AI_API_KEY"
fi

# 4) 重启进程
if _alive; then
  do_stop
fi
do_start
