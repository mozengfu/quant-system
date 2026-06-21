#!/bin/bash
# 日内做T监控 — 启动/停止脚本
# 用法: bash scripts/start_intraday_t.sh start [mode]
#       bash scripts/start_intraday_t.sh status
#       bash scripts/start_intraday_t.sh stop

ACTION="${1:-start}"
MODE="${2:-dryrun}"
PIDFILE="logs/intraday_t.pid"
LOGFILE="logs/intraday_t_monitor.log"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

case "$ACTION" in
  start)
    if [ -f "$DIR/$PIDFILE" ] && kill -0 "$(cat "$DIR/$PIDFILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$DIR/$PIDFILE"))"
      exit 0
    fi
    cd "$DIR"
    nohup python3 scripts/intraday_t_monitor.py --mode "$MODE" >> "$DIR/$LOGFILE" 2>&1 &
    echo $! > "$DIR/$PIDFILE"
    echo "started pid=$! mode=$MODE"
    ;;
  status)
    if [ -f "$DIR/$PIDFILE" ] && kill -0 "$(cat "$DIR/$PIDFILE")" 2>/dev/null; then
      echo "running (pid $(cat "$DIR/$PIDFILE"))"
    else
      echo "not running"
    fi
    ;;
  stop)
    if [ -f "$DIR/$PIDFILE" ]; then
      kill "$(cat "$DIR/$PIDFILE")" 2>/dev/null && echo "stopped" || echo "stop failed"
      rm -f "$DIR/$PIDFILE"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|status|stop} [mode=dryrun|sim|real]"
    exit 1
    ;;
esac
