-- Main-wave 三段式预测模型 — 新增 6 个数据表
-- 用途: 龙虎榜/机构席位/涨停股/研报 个股级强信号 + 衍生 + 标签

-- 1. 龙虎榜每日上榜 (top_list)
CREATE TABLE IF NOT EXISTS top_list (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    trade_date    DATE        NOT NULL,
    ts_code       VARCHAR(20) NOT NULL,
    name          VARCHAR(50),
    close         DECIMAL(10,2),
    pct_change    DECIMAL(10,4),
    turnover_rate DECIMAL(10,4),
    amount        DECIMAL(18,2),
    l_sell        DECIMAL(18,2),
    l_buy         DECIMAL(18,2),
    l_amount      DECIMAL(18,2),
    net_amount    DECIMAL(18,2),
    net_rate      DECIMAL(10,4),
    amount_rate   DECIMAL(10,4),
    float_values  DECIMAL(18,2),
    reason        VARCHAR(100),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_ts (ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 2. 机构席位 (top_inst)
CREATE TABLE IF NOT EXISTS top_inst (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    trade_date  DATE        NOT NULL,
    ts_code     VARCHAR(20) NOT NULL,
    exalter     VARCHAR(50) NOT NULL,
    buy         DECIMAL(18,2),
    buy_rate    DECIMAL(10,4),
    sell        DECIMAL(18,2),
    sell_rate   DECIMAL(10,4),
    net_buy     DECIMAL(18,2),
    side        VARCHAR(2),
    reason      VARCHAR(100),
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_date_code (trade_date, ts_code),
    KEY idx_exalter (exalter),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 3. 涨停股 (limit_list_d)
CREATE TABLE IF NOT EXISTS limit_list_d (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    trade_date     DATE        NOT NULL,
    ts_code        VARCHAR(20) NOT NULL,
    industry       VARCHAR(50),
    name           VARCHAR(50),
    close          DECIMAL(10,2),
    pct_chg        DECIMAL(10,4),
    amount         DECIMAL(18,2),
    limit_amount   DECIMAL(18,2),
    float_mv       DECIMAL(18,2),
    total_mv       DECIMAL(18,2),
    turnover_ratio DECIMAL(10,4),
    fd_amount      DECIMAL(18,2),
    first_time     VARCHAR(10),
    last_time      VARCHAR(10),
    open_times     INT,
    up_stat        VARCHAR(20),
    limit_times    INT,
    limit_type     VARCHAR(2),
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_code (trade_date, ts_code),
    KEY idx_date (trade_date),
    KEY idx_industry (industry)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 4. 研报 (research_report)
CREATE TABLE IF NOT EXISTS research_report (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    trade_date   DATE        NOT NULL,
    title        VARCHAR(255),
    report_type  VARCHAR(50),
    author       VARCHAR(100),
    name         VARCHAR(50),
    ts_code      VARCHAR(20),
    inst_csname  VARCHAR(100),
    ind_name     VARCHAR(50),
    url          VARCHAR(500),
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_date_code (trade_date, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 5. 板块接力序 (sector_relay_state)
CREATE TABLE IF NOT EXISTS sector_relay_state (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    trade_date      DATE        NOT NULL,
    sector_type     VARCHAR(20) NOT NULL,
    sector_name     VARCHAR(50) NOT NULL,
    relay_index     INT         NOT NULL,
    ts_code         VARCHAR(20) NOT NULL,
    name            VARCHAR(50),
    pct_chg         DECIMAL(10,4),
    is_limit_up     TINYINT     DEFAULT 0,
    has_breakout    TINYINT     DEFAULT 0,
    score           DECIMAL(10,2),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_date_sector_code (trade_date, sector_type, sector_name, ts_code),
    KEY idx_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 6. 主升浪标签 (main_wave_labels)
CREATE TABLE IF NOT EXISTS main_wave_labels (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    trade_date      DATE        NOT NULL,
    ts_code         VARCHAR(20) NOT NULL,
    label           TINYINT     NOT NULL,
    return_3d       DECIMAL(10,4),
    return_5d       DECIMAL(10,4),
    max_drawdown_3d DECIMAL(10,4),
    trigger_type    VARCHAR(20),
    sector_name     VARCHAR(50),
    industry        VARCHAR(50),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_date (trade_date),
    KEY idx_code (ts_code),
    KEY idx_label (label)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
