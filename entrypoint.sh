#!/bin/sh
# =============================================================================
# 容器入口：按角色启动不同进程
#   参数/环境变量 HQ_ROLE: gateway(默认) | recorder | bridge | shell
#
#   docker run ... hqcam-gateway            -> gateway
#   docker run ... hqcam-gateway recorder   -> 分段录像
#   docker run ... hqcam-gateway bridge     -> Home Assistant MQTT 桥
#   docker run ... hqcam-gateway shell      -> 进容器排查
# =============================================================================
set -eu

ROLE="${HQ_ROLE:-}"
if [ -z "$ROLE" ]; then
  ROLE="${1:-gateway}"
fi
PORT="${HQ_PORT:-8899}"
HOST="${HQ_HOST:-0.0.0.0}"

case "$ROLE" in
  gateway)
    echo "[*] 角色: gateway  ($HOST:$PORT)"
    exec python /app/scripts/hq_live_server.py --host "$HOST" --port "$PORT"
    ;;

  recorder)
    OUT="${HQ_REC_OUT:-/data/rec}"
    mkdir -p "$OUT"
    echo "[*] 角色: recorder  输出=$OUT"
    # shellcheck disable=SC2086
    exec python /app/scripts/hq_recorder.py \
      --url "${HQ_REC_URL:-http://127.0.0.1:$PORT/av.ts}" \
      --out "$OUT" \
      --seg "${HQ_REC_SEG:-300}" \
      --keep-days "${HQ_REC_KEEP_DAYS:-0}" \
      --keep-gb "${HQ_REC_KEEP_GB:-0}" \
      --min-free-gb "${HQ_REC_MIN_FREE_GB:-0}" \
      --cleanup-every "${HQ_REC_CLEANUP_EVERY:-300}" \
      ${HQ_REC_MKV:+--mkv}
    ;;

  bridge)
    : "${MQTT_HOST:?bridge 角色需要设置 MQTT_HOST}"
    GW="${HQ_GW:-http://127.0.0.1:$PORT}"
    echo "[*] 角色: bridge  broker=$MQTT_HOST:${MQTT_PORT:-1883}  网关=$GW"
    # shellcheck disable=SC2086
    exec python /app/scripts/ha_bridge.py \
      --broker "$MQTT_HOST" \
      --port "${MQTT_PORT:-1883}" \
      ${MQTT_USER:+--user "$MQTT_USER"} \
      ${MQTT_PASS:+--password "$MQTT_PASS"} \
      --gw "$GW" \
      --snap-interval "${MQTT_SNAP_INTERVAL:-10}"
    ;;

  shell|sh|bash)
    exec sh
    ;;

  *)
    # 未知角色 -> 当作命令直接执行
    exec "$@"
    ;;
esac
