-- Migration 001: 扩宽 strategy 列 30 → 50
-- 时间: 2026-06-18
-- 原因: 5因子重构后,新 strategy 字段值为
--       "周线板RPS+ML(V11.0(板RPS周线))" = 26 字符,
--       已接近 varchar(30) 上限,model_ver 再长就会 truncate 报错。
--       提前扩到 varchar(50) 留余量。
-- 影响表: sim_signals / sim_positions / strategy_trade_log

ALTER TABLE sim_signals         MODIFY strategy VARCHAR(50) DEFAULT NULL;
ALTER TABLE sim_positions       MODIFY strategy VARCHAR(50) DEFAULT NULL;
ALTER TABLE strategy_trade_log  MODIFY strategy VARCHAR(50) NOT NULL;
