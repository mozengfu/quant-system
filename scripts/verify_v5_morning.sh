#!/bin/zsh
# 6/15 一次性 9:30-9:45 V5 验证
# 4 个时点检查 V5 是否真在跑, /buy 能不能成交, 持仓对齐
set -e
LOG="/Users/mozengfu/workspace/quant-system/logs/verify_v5_$(date +%Y%m%d_%H%M%S).log"
exec > "$LOG" 2>&1

echo "=== 一次性 V5 验证任务启动 $(date) ==="
echo "调度点: 9:30 / 9:35 / 9:40 / 9:45"
echo

# 等待到 9:30
while [ "$(date +%H%M)" \< "0930" ]; do
    sleep 20
done

run_check() {
    local label="$1"
    echo
    echo "=== $label (时间 $(date +%H:%M:%S)) ==="
    echo "--- V5 行情 mtime ---"
    ssh -i ~/.ssh/id_ed25519_qmt mozf@192.168.10.25 "dir C:\\Users\\Public\\qmt_market.json C:\\Users\\Public\\qmt_balance.json" 2>&1 | grep -E "qmt_(market|balance)"
    echo "--- /buy 测试 (期望 <5s 201) ---"
    local result=$(curl -s -o /tmp/v5_buy.txt -w "%{http_code} %{time_total}" --max-time 30 \
        -X POST -H "Content-Type: application/json" \
        -d '{"code":"000001","price":10.0,"amount":100}' \
        http://192.168.10.25:1430/buy)
    echo "  /buy HTTP=$result  body=$(cat /tmp/v5_buy.txt 2>/dev/null | head -c 100)"
    echo "--- cmd.json 状态 ---"
    ssh -i ~/.ssh/id_ed25519_qmt mozf@192.168.10.25 "type C:\\Users\\Public\\qmt_cmd.json" 2>&1
    echo "--- /position ---"
    curl -s --max-time 5 http://192.168.10.25:1430/position | python3 -c "
import json, sys
data = json.load(sys.stdin)
for p in data:
    print(f'  {p.get(\"code\")} {p.get(\"name\")} {p.get(\"volume\")}股')
"
    echo "--- sim_signals PENDING ---"
    python3 -c "
import sys
sys.path.insert(0, '/Users/mozengfu/workspace/quant-system')
from quant_app.utils.config import get_db_config
import pymysql
c = pymysql.connect(**get_db_config(connect_timeout=5))
cu = c.cursor()
cu.execute(\"SELECT COUNT(*) FROM sim_signals WHERE status='待执行'\")
print(f'  PENDING: {cu.fetchone()[0]} 条')
cu.execute(\"SELECT COUNT(*) FROM sim_signals WHERE DATE(created_at)='2026-06-15' AND status='已执行'\")
print(f'  今日已执行: {cu.fetchone()[0]} 条')
c.close()
"
}

# 4 个时点检查
run_check "9:30 第一次 (开盘 + monitor 应该开始)"
sleep 300  # 5 分钟
run_check "9:35 第二次 (morning 窗口期 / scanner)"
sleep 300
run_check "9:40 第三次"
sleep 300
run_check "9:45 第四次 (汇总节点)"

echo
echo "=== 4 次检查完成, 汇总 ==="
echo "日志: $LOG"
