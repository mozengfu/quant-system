#!/bin/zsh
# ========== 量化系统数据自动刷新脚本 ==========
# 用途：盘中定时刷新stock_pool.json / recommend_cache.json / premarket_analysis.json
# 数据源：东方财富 + Tushare MySQL
# 刷新策略：
#   - stock_pool.json: 每30分钟刷新（选股池实时变化）
#   - recommend_cache.json: 每30分钟刷新（推荐策略基于实时评分）
#   - premarket_analysis.json: 每30分钟刷新（大盘状态实时更新）

export PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin
QUANT_DIR=/Users/mozengfu/workspace/quant-system
PYTHON3=/usr/bin/python3
LOG_DIR=$QUANT_DIR/logs
mkdir -p $LOG_DIR

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "=== 量化系统数据自动刷新 ==="

# 1. 刷新 stock_pool.json（调用 API 内部接口刷新缓存）
log "刷新 stock_pool.json..."
curl -s http://localhost:5001/api/scan/strong?force_refresh=true > /dev/null 2>&1
if [ $? -eq 0 ]; then
    log "✅ stock_pool.json 刷新成功"
else
    log "⚠️ stock_pool.json 刷新失败（量化系统可能未运行）"
fi

# 2. 刷新 recommend_cache.json
log "刷新 recommend_cache.json..."
curl -s "http://localhost:5001/api/recommend?force_refresh=true" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    log "✅ recommend_cache.json 刷新成功"
else
    log "⚠️ recommend_cache.json 刷新失败"
fi

# 3. 刷新 premarket_analysis.json
log "刷新 premarket_analysis.json..."
curl -s "http://localhost:5001/api/market/premarket?force_refresh=true" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    log "✅ premarket_analysis.json 刷新成功"
else
    log "⚠️ premarket_analysis.json 刷新失败"
fi

log "=== 数据刷新完成 ==="
