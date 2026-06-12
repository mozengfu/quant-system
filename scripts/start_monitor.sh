#!/bin/bash
cd /Users/mozengfu/workspace/quant-system
mkdir -p logs

# 先杀掉旧进程
pkill -f "market_monitor.py" 2>/dev/null
sleep 1

nohup python3 scripts/market_monitor.py > logs/market_monitor.log 2>&1 &
disown
sleep 2

if ps aux | grep -v grep | grep -q "market_monitor.py"; then
    echo "市场监控已启动"
    tail -3 logs/market_monitor.log
else
    echo "启动失败"
    cat logs/market_monitor.log
fi
