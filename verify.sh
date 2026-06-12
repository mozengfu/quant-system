#!/bin/bash
# 量化系统重启后验证脚本
# 用法: bash verify.sh

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; FAIL=0

check() { if [ $? -eq 0 ]; then echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS+1)); else echo -e "  ${RED}✗${NC} $1"; FAIL=$((FAIL+1)); fi }

echo "========================================"
echo "  量化系统重启验证 $(date '+%H:%M:%S')"
echo "========================================"

echo ""; echo "--- 进程 ---"
pgrep -f "app\.py" > /dev/null && echo -e "  ${GREEN}✓${NC} app.py 运行中 (PID $(pgrep -f app_api.py | head -1))" && PASS=$((PASS+1)) || { echo -e "  ${RED}✗${NC} app.py 未运行"; FAIL=$((FAIL+1)); }
pgrep -f "market_monitor.py" > /dev/null && echo -e "  ${GREEN}✓${NC} market_monitor.py 运行中" && PASS=$((PASS+1)) || { echo -e "  ${YELLOW}⚠${NC} market_monitor.py 未运行（可能未load）"; }
pgrep -f "frpc" > /dev/null && echo -e "  ${GREEN}✓${NC} frpc 隧道运行中" && PASS=$((PASS+1)) || { echo -e "  ${YELLOW}⚠${NC} frpc 未运行"; }

echo ""; echo "--- 端口 ---"
lsof -ti :5001 > /dev/null 2>&1 && check ":5001 已监听" || check ":5001 未监听"
curl -s -o /dev/null -w "%{http_code}" http://localhost:5001/api/pipeline/status 2>/dev/null | grep -q 200 && check "本地API响应正常" || check "本地API无响应"

echo ""; echo "--- 外部可达性 ---"
curl -s -o /dev/null -w "%{http_code}" https://lh.mozengfu.com.cn/api/pipeline/status 2>/dev/null | grep -q 200 && check "HTTPS API可达" || check "HTTPS API不可达"
curl -s -o /dev/null -w "%{http_code}" https://lh.mozengfu.com.cn/app/ 2>/dev/null | grep -q 200 && check "前端页面可达" || check "前端页面不可达"

echo ""; echo "--- QMT 连接 ---"
if curl -s --connect-timeout 3 http://192.168.10.25:1430/status 2>/dev/null | grep -q "ok\|connected"; then
    echo -e "  ${GREEN}✓${NC} QMT HTTP服务 (192.168.10.25:1430) 连通"
    PASS=$((PASS+1))
else
    echo -e "  ${YELLOW}⚠${NC} QMT HTTP服务不可达（可能Win未开或未到开盘时间）"
fi

echo ""; echo "--- 数据文件 ---"
[ -f /Users/mozengfu/workspace/quant-system/data/market_state.json ] && check "market_state.json 存在" || check "market_state.json 缺失"
[ -f /Users/mozengfu/workspace/quant-system/data/backtest_v11_oos.json ] && check "backtest_v11_oos.json 存在" || check "backtest_v11_oos.json 缺失"
[ -f /Users/mozengfu/workspace/quant-system/data/ml_stock_model_v11_0_oos.pkl ] && check "OOS模型文件存在" || check "OOS模型文件缺失"

echo ""; echo "--- 前端 ---"
[ -f /Users/mozengfu/workspace/quant-system/frontend/dist/index.html ] && check "前端dist存在" || check "前端dist缺失"

echo ""; echo "========================================"
echo -e "  结果: ${GREEN}${PASS}通过${NC} / ${RED}${FAIL}失败${NC}"
echo "========================================"

if [ $FAIL -eq 0 ]; then
    echo -e "\n${GREEN}✓ 系统正常，可以开始交易。${NC}"
else
    echo -e "\n${RED}存在 ${FAIL} 项失败，请检查。${NC}"
fi
