#!/bin/zsh
# ========== 量化系统数据自动刷新脚本 ==========
# 用途：盘中定时刷新推荐缓存 / 选股池 / 大盘状态 / 追踪结果
# crontab 调用，不走 API（无需认证），直接调 Python

export PATH=/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin
QUANT_DIR=/Users/mozengfu/workspace/quant-system
PYTHON3=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3
LOG_DIR=$QUANT_DIR/logs
mkdir -p $LOG_DIR

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

log "=== 量化系统数据自动刷新 ==="

# 1. 刷新推荐缓存 + 记录追踪
log "刷新推荐 + 追踪记录..."
$PYTHON3 -c "
import pymysql, json, os, sys
sys.path.insert(0, '$QUANT_DIR')
os.chdir('$QUANT_DIR')
from quant_app.utils.config import config
from quant_app.services.strategy_service import generate_v4_ml_top5
from quant_app.utils.persistence import record_recommendation, save_track_data, load_track_data

conn = pymysql.connect(**config.mysql.get_connection_params())
top3 = generate_v4_ml_top5(conn, top_n=3)
conn.close()

if top3:
    recs = []
    for s in top3:
        price = float(s.get('close', s.get('price', 0)))
        recs.append({
            '代码': s['ts_code'].split('.')[0],
            '名称': s['name'],
            '行业': s.get('industry', ''),
            '现价': price,
            '综合评分': int(s.get('blended_score', s.get('v4_score', 0))),
        })
    record_recommendation(recs, 'ML选股策略 TOP3')
    print(f'    推荐已记录: {len(recs)} 只', flush=True)
else:
    print('    推荐无候选', flush=True)
" >> $LOG_DIR/auto_refresh.log 2>&1
if [ $? -eq 0 ]; then
    log "✅ 推荐 + 追踪记录刷新成功"
else
    log "⚠️ 推荐刷新失败"
fi

# 2. 刷新市场大盘状态
log "刷新大盘状态..."
$PYTHON3 -c "
import sys, os
sys.path.insert(0, '$QUANT_DIR')
os.chdir('$QUANT_DIR')
from quant_app.services.market_service import get_tushare_pro
from quant_app.utils.config import config
from datetime import datetime, timedelta
import pymysql

# 拉取最新指数数据
pro = get_tushare_pro()
indexes = [('000001.SH', '上证指数'), ('399001.SZ', '深证成指'),
           ('399006.SZ', '创业板指'), ('000300.SH', '沪深300')]
conn = pymysql.connect(**config.mysql.get_connection_params())
cur = conn.cursor()
today = datetime.now()
start = (today - timedelta(days=10)).strftime('%Y%m%d')

for code, name in indexes:
    df = pro.index_daily(ts_code=code, start_date=start)
    if df is None or df.empty:
        continue
    for _, row in df.iterrows():
        cur.execute('''INSERT INTO market_index_daily
            (index_code, index_name, trade_date, close_price, change_pct, volume, amount)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE close_price=VALUES(close_price), change_pct=VALUES(change_pct)''',
            (code, name, row['trade_date'], row['close'], row['pct_chg'],
             row.get('vol', 0), row.get('amount', 0)))
conn.commit()
cur.close()
conn.close()
print('    大盘指数刷新完成', flush=True)
" >> $LOG_DIR/auto_refresh.log 2>&1
if [ $? -eq 0 ]; then
    log "✅ 大盘指数刷新成功"
else
    log "⚠️ 大盘指数刷新失败"
fi

# 3. 更新追踪结果（计算1d/1w/1m收益）
log "更新追踪结果..."
$PYTHON3 -c "
import sys, os
sys.path.insert(0, '$QUANT_DIR')
os.chdir('$QUANT_DIR')
from quant_app.utils.persistence import update_stock_results, TRACK_UPDATE_CACHE
TRACK_UPDATE_CACHE['last_update'] = 0
update_stock_results()
data = __import__('quant_app.utils.persistence', fromlist=['load_track_data']).load_track_data()
stats = data.get('stats', {})
print(f'    追踪更新: 胜率{stats.get(\"win_rate\",0)}% 总盈亏{stats.get(\"total_pnl\",0)}', flush=True)
" >> $LOG_DIR/auto_refresh.log 2>&1
if [ $? -eq 0 ]; then
    log "✅ 追踪结果刷新成功"
else
    log "⚠️ 追踪结果刷新失败"
fi

log "=== 数据刷新完成 ==="
