-- Migration 002: create intraday_t_log table for intraday T strategy
-- Date: 2026-06-18
-- Reason: T strategy needs an independent log table for T signals / executions / PnL.
--         It does NOT reuse sim_signals because semantics differ:
--           - sim_signals     : stock-picking signals
--           - intraday_t_log  : intra-day T operations on existing positions
--
-- Field notes:
--   id                auto-increment primary key
--   ts_code           ticker, e.g. "000559.SZ"
--   stock_name        ticker name
--   trade_date        trading day of the T action (DATE, for intraday dedup/aggregation)
--   direction         'sell_high' | 'buy_back' | 'force_close' | 'skip'
--   base_position_id  FK -> sim_positions.id of the base position (NULL on buy_back)
--   t_position_id     FK -> sim_positions.id of the T position (NULL on sell_high/force_close)
--   shares            shares traded in this action
--   price             executed price
--   vwap              current VWAP estimate at decision time (NULL if not accumulated)
--   pct_from_vwap     (price - vwap) / vwap * 100, deviation from VWAP in percent
--   intraday_pct      (price - prev_close) / prev_close * 100, today's cumulative pct
--   target_pct        target profit margin for this T action
--   realized_pnl      realized PnL in CNY
--   realized_pnl_pct  same as above in percent
--   reason            signal reason / free text
--   status            'filled' | 'cancelled' | 'skipped' (skipped by risk control)
--   executor_mode     'sim' | 'remote' | 'dryrun'
--   created_at        row insert timestamp

CREATE TABLE IF NOT EXISTS intraday_t_log (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    ts_code           VARCHAR(16)  NOT NULL,
    stock_name        VARCHAR(64),
    trade_date        DATE         NOT NULL,
    direction         VARCHAR(16)  NOT NULL,
    base_position_id  BIGINT,
    t_position_id     BIGINT,
    shares            INT          NOT NULL,
    price             DECIMAL(10,3) NOT NULL,
    vwap              DECIMAL(10,3),
    pct_from_vwap     DECIMAL(8,4),
    intraday_pct      DECIMAL(8,4),
    target_pct        DECIMAL(8,4),
    realized_pnl      DECIMAL(12,2) DEFAULT 0,
    realized_pnl_pct  DECIMAL(8,4)  DEFAULT 0,
    reason            VARCHAR(255),
    status            VARCHAR(16)  DEFAULT 'filled',
    executor_mode     VARCHAR(16)  DEFAULT 'sim',
    created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_ts_code (ts_code),
    INDEX idx_trade_date (trade_date),
    INDEX idx_direction (direction)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
