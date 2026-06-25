-- 003: sim_signals 加 order_id 列 + 建 trade_risk_checks 风控审计表
-- 背景:
--   1. /orders 端点 SELECT order_id FROM sim_signals 报 Unknown column (1054) → 400,
--      get_orders() 永远空。加列后桥可落库并查询真实 QMT 委托号。
--   2. pre_trade_check.py 已实现 8 项下单前检查但 trade_risk_checks 表缺失,
--      每次检查 INSERT 报错被吞成 warning, 风控审计失效。
-- 红线: DB schema 变更, 本文件由人工/运维执行, 不自动 apply。

-- 1. sim_signals 加 order_id 列 (QMT 真实委托号, v23 回写)
ALTER TABLE sim_signals
  ADD COLUMN order_id VARCHAR(30) NULL COMMENT 'QMT真实委托号, v23回写' AFTER status,
  ADD INDEX idx_sim_signals_order_id (order_id);

-- 2. 风控审计表 (pre_trade_check.PreTradeChecker._record_risk_check 写入)
CREATE TABLE IF NOT EXISTS trade_risk_checks (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  ts_code      VARCHAR(20) NOT NULL COMMENT '股票代码',
  check_name   VARCHAR(50) NOT NULL COMMENT '检查项: trading_time/price_deviation/daily_circuit_breaker/duplicate_order 等',
  passed       TINYINT(1) NOT NULL COMMENT '1通过 0拒绝',
  detail       VARCHAR(255) NULL COMMENT '检查详情',
  check_time   DATETIME NOT NULL COMMENT '检查时间',
  INDEX idx_risk_check_time (check_time),
  INDEX idx_risk_check_code (ts_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='下单前风控检查审计记录';
