#!/usr/bin/env bash
#
# 启动 KnowFlow 问数服务（knowflow-analytics）。
#
# 配置来源：
#   - RAGFlow 的 local.env  → 服务间共享密钥、RAGFlow 地址
#   - RAGFlow 的 docker/.env → PostgreSQL 连接信息
# 租户与模型都不在这里配置：登录身份随请求传递，RAGFlow 用当前登录
# 用户的「设置默认模型」执行生成，前端换模型即时生效。
#
# 用法：
#   ./start_analytics.sh            前台启动
#   ./start_analytics.sh -d         后台启动，日志见 /tmp/knowflow-analytics.log
#
set -euo pipefail

ANALYTICS_DIR="$(cd "$(dirname "$0")" && pwd)"
# local.env 与 docker/.env 属于 RAGFlow，位于上一级仓库根目录。
REPO_ROOT="$(dirname "$ANALYTICS_DIR")"
LOG_FILE="/tmp/knowflow-analytics.log"
PORT=9395

die() { echo "错误：$*" >&2; exit 1; }

# 从 KEY=VALUE 形式的文件里取值，不 source，避免执行到无关内容。
read_env() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  sed -n "s/^${key}=//p" "$file" | tail -1
}

[ -f "$REPO_ROOT/local.env" ] || die "找不到 $REPO_ROOT/local.env"
[ -x "$ANALYTICS_DIR/.venv/bin/uvicorn" ] || die "缺少虚拟环境，请先在本目录执行 uv sync"

SERVICE_SECRET="$(read_env "$REPO_ROOT/local.env" KNOWFLOW_ANALYTICS_SERVICE_SECRET)"
[ -n "$SERVICE_SECRET" ] || die "local.env 缺少 KNOWFLOW_ANALYTICS_SERVICE_SECRET（RAGFlow 与本服务必须一致）"

RAGFLOW_SECRET="$(read_env "$REPO_ROOT/local.env" RAGFLOW_SECRET_KEY)"
[ -n "$RAGFLOW_SECRET" ] || die "local.env 缺少 RAGFLOW_SECRET_KEY"

RAGFLOW_URL="$(read_env "$REPO_ROOT/local.env" RAGFLOW_BASE_URL)"
RAGFLOW_URL="${RAGFLOW_URL:-http://127.0.0.1:9380}"

PG_USER="$(read_env "$REPO_ROOT/docker/.env" POSTGRES_USER)"
PG_PASSWORD="$(read_env "$REPO_ROOT/docker/.env" POSTGRES_PASSWORD)"
PG_PORT="$(read_env "$REPO_ROOT/docker/.env" POSTGRES_PORT)"
PG_HOST="${ANALYTICS_PG_HOST:-127.0.0.1}"
[ -n "$PG_USER" ] && [ -n "$PG_PASSWORD" ] || die "docker/.env 缺少 POSTGRES_USER / POSTGRES_PASSWORD"

# Catalog 库存放语义模型版本，必须与业务数据源分开，
# 否则服务自己的元数据表会作为可建模的业务表暴露给用户。
CATALOG_DB="${ANALYTICS_CATALOG_DB:-analytics_catalog}"
SOURCE_DB="${ANALYTICS_SOURCE_DB:-$(read_env "$REPO_ROOT/docker/.env" POSTGRES_DBNAME)}"
[ -n "$SOURCE_DB" ] || die "无法确定业务数据源库，请设置 ANALYTICS_SOURCE_DB"

export KNOWFLOW_ANALYTICS_CATALOG_DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT:-5432}/${CATALOG_DB}"
export KNOWFLOW_ANALYTICS_DATASOURCE_DATABASE_URL="postgresql+psycopg://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT:-5432}/${SOURCE_DB}"
export KNOWFLOW_ANALYTICS_SERVICE_SECRET="$SERVICE_SECRET"
export KNOWFLOW_ANALYTICS_RAGFLOW_BASE_URL="$RAGFLOW_URL"
export KNOWFLOW_ANALYTICS_RAGFLOW_SERVICE_TOKEN="$RAGFLOW_SECRET"
# 开发期自动建表；正式部署改用版本化迁移。
export KNOWFLOW_ANALYTICS_AUTO_CREATE_SCHEMA=true
# 与 RAGFlow BFF 的 AI 建议超时保持一致，避免上游还在等、下游已放弃。
export KNOWFLOW_ANALYTICS_MODEL_GATEWAY_TIMEOUT_SECONDS="${ANALYTICS_MODEL_TIMEOUT:-240}"
# 免费额度的模型端点扛不住一键建模的并发扇出；设为 1 可完全串行。
export KNOWFLOW_ANALYTICS_MODELING_MAX_CONCURRENCY="${ANALYTICS_MODELING_CONCURRENCY:-5}"

# 本机可调参数，取自 knowflow-analytics/local.env（不进版本库，见 local.env.example）。
# AnalyticsSettings 的 env_file=None，只认进程环境变量：改同目录的 .env 对本路径无效，
# 必须在这里 export 出去。已在环境里显式设过的值优先，方便临时覆盖单次启动。
local_setting() {
  local key="$1" fallback="$2" from_file
  from_file="$(read_env "$ANALYTICS_DIR/local.env" "$key")"
  printf '%s' "${!key:-${from_file:-$fallback}}"
}
export KNOWFLOW_ANALYTICS_MINIMUM_EVALUATION_CASES="$(local_setting KNOWFLOW_ANALYTICS_MINIMUM_EVALUATION_CASES 30)"
export KNOWFLOW_ANALYTICS_MINIMUM_ACCURACY="$(local_setting KNOWFLOW_ANALYTICS_MINIMUM_ACCURACY 1.0)"
export KNOWFLOW_ANALYTICS_SELF_CONSISTENCY_NUMBER="$(local_setting KNOWFLOW_ANALYTICS_SELF_CONSISTENCY_NUMBER 1)"
export KNOWFLOW_ANALYTICS_WEAK_METRIC_ADJUDICATION_MODE="$(local_setting KNOWFLOW_ANALYTICS_WEAK_METRIC_ADJUDICATION_MODE shadow)"
export KNOWFLOW_ANALYTICS_SEMANTIC_INTENT_ADJUDICATION_MODE="$(local_setting KNOWFLOW_ANALYTICS_SEMANTIC_INTENT_ADJUDICATION_MODE shadow)"
export KNOWFLOW_ANALYTICS_ANALYSIS_OBJECT_ADJUDICATION_MODE="$(local_setting KNOWFLOW_ANALYTICS_ANALYSIS_OBJECT_ADJUDICATION_MODE shadow)"
export KNOWFLOW_ANALYTICS_CONFIRMATION_MEMORY_TTL_SECONDS="$(local_setting KNOWFLOW_ANALYTICS_CONFIRMATION_MEMORY_TTL_SECONDS 2592000)"
# 问数「执行过程」要给出真正执行的物理 SQL 才谈得上可确认。关闭时物理 SQL 在
# 查询当时就不写入诊断产物（历史轮无法补看），过程面板只到业务名 S2SQL 为止。
export KNOWFLOW_ANALYTICS_ALLOW_DEBUG_SQL="$(local_setting KNOWFLOW_ANALYTICS_ALLOW_DEBUG_SQL true)"
# 多轮改写：追问继承上一轮的口径。实测（2026-09-01，rel_72ffa832）同一会话里
# 「华东的净金额」→「按渠道拆分」得到 渠道×净金额；新会话问同一句只能回落到
# 默认计数指标 订单数量。代价是有上一轮成功记录时每轮多一次模型调用。
# 已知边界：改写发生在候选选中之后，「那环比呢」这类映射不到任何语义对象的
# 纯指代追问仍会 NO_SEMANTIC_MAPPING，改写救不了。
export KNOWFLOW_ANALYTICS_MULTI_TURN_ENABLED="$(local_setting KNOWFLOW_ANALYTICS_MULTI_TURN_ENABLED true)"

if lsof -ti ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  die "端口 $PORT 已被占用，先停止旧进程：kill \$(lsof -ti :$PORT -sTCP:LISTEN)"
fi

echo "问数服务配置："
echo "  RAGFlow    : $RAGFLOW_URL"
echo "  Catalog 库 : $CATALOG_DB"
echo "  数据源库   : $SOURCE_DB"
echo "  租户/模型  : 跟随当前登录用户及其「设置默认模型」"

cd "$ANALYTICS_DIR"

# 热重载默认开启，改完 src 下的代码无需手动重启。
# 只监听 src：否则 .venv 安装、日志写入都会触发无谓的重启。
# 设 ANALYTICS_RELOAD=0 关闭（压测或对比性能时用）。
# macOS 自带 bash 3.2，配合 set -u 时空数组展开会直接报错，
# 因此这里用普通字符串而不是数组。参数本身不含空格，词分割是安全的。
RELOAD_ARGS=""
if [ "${ANALYTICS_RELOAD:-1}" = "1" ]; then
  RELOAD_ARGS="--reload --reload-dir src"
  echo "  热重载     : 开启（监听 src/）"
else
  echo "  热重载     : 关闭"
fi

if [ "${1:-}" = "-d" ]; then
  # shellcheck disable=SC2086
  nohup .venv/bin/uvicorn knowflow_analytics.server:create_app \
    --factory --host 127.0.0.1 --port "$PORT" $RELOAD_ARGS > "$LOG_FILE" 2>&1 &
  echo "已后台启动 (PID $!)，日志：$LOG_FILE"
else
  # shellcheck disable=SC2086
  exec .venv/bin/uvicorn knowflow_analytics.server:create_app \
    --factory --host 127.0.0.1 --port "$PORT" $RELOAD_ARGS
fi
