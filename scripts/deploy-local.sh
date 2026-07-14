#!/usr/bin/env bash
# PanWatch 本地一键部署脚本(Docker Compose,从源码构建)
#
# 用法:
#   bash scripts/deploy-local.sh            # 部署/升级,默认端口 18123
#   PANWATCH_PORT=9000 bash scripts/deploy-local.sh   # 自定义端口
#   bash scripts/deploy-local.sh --down     # 停止并移除容器(数据卷保留)
#   bash scripts/deploy-local.sh --logs     # 跟踪查看日志
#
# 数据(SQLite/运行时文件)持久化在 docker volume `panwatch_data`,升级不丢。

set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PANWATCH_PORT:-18123}"
export PANWATCH_PORT="$PORT"

# ---- docker / compose 检查 ----
if ! command -v docker >/dev/null 2>&1; then
  echo "错误: 未安装 docker,请先安装 Docker Desktop / docker-ce" >&2
  exit 1
fi
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "错误: 未找到 docker compose(v2)或 docker-compose(v1)" >&2
  exit 1
fi

case "${1:-}" in
  --down)
    "${COMPOSE[@]}" down
    echo "已停止。数据卷 panwatch_data 保留;彻底清除数据: docker volume rm panwatch_data"
    exit 0
    ;;
  --logs)
    exec "${COMPOSE[@]}" logs -f --tail=200 panwatch
    ;;
esac

# ---- .env 准备 ----
if [ ! -f .env ]; then
  cp .env.example .env
  echo "⚠ 已从 .env.example 生成 .env,请编辑其中的 AUTH_USERNAME/AUTH_PASSWORD/JWT_SECRET/AI_API_KEY 后重新运行。"
  echo "  (首次可直接继续,登录账号用 .env.example 的默认值,但强烈建议改掉)"
  read -r -p "继续部署? [y/N] " answer
  [ "${answer:-n}" = "y" ] || [ "${answer:-n}" = "Y" ] || exit 0
fi

# ---- 端口占用检查 ----
if command -v lsof >/dev/null 2>&1 && lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "错误: 端口 $PORT 已被占用,换一个: PANWATCH_PORT=xxxx bash scripts/deploy-local.sh" >&2
  exit 1
fi

# ---- 构建并启动 ----
echo "==> 构建镜像并启动(宿主机端口 $PORT → 容器 8000)..."
"${COMPOSE[@]}" up -d --build

# ---- 等待健康检查 ----
echo -n "==> 等待服务就绪"
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' panwatch 2>/dev/null || echo starting)
  if [ "$status" = "healthy" ]; then
    echo
    echo "✅ 部署完成: http://127.0.0.1:$PORT"
    echo "   API 文档:  http://127.0.0.1:$PORT/docs"
    echo "   查看日志:  bash scripts/deploy-local.sh --logs"
    echo "   停止服务:  bash scripts/deploy-local.sh --down"
    exit 0
  fi
  echo -n "."
  sleep 2
done

echo
echo "⚠ 服务在 60 秒内未通过健康检查,查看日志排查:" >&2
"${COMPOSE[@]}" logs --tail=50 panwatch >&2
exit 1
