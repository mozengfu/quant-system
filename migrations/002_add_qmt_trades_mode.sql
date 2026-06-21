-- 实盘/模拟交易分离：qmt_trades 加 mode 列
-- 2026-06-19
ALTER TABLE qmt_trades ADD COLUMN mode VARCHAR(10) DEFAULT 'live' COMMENT 'live=实盘 simulation=模拟';
