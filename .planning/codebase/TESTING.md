# 测试现状与规划

## 当前状态（2026-05-05）

这个项目**没有任何自动化测试**。

- `tests/` 目录下有 `__init__.py`，内容为 `# tests - 测试文件`，仅此而已
- `requirements.txt` 中没有测试框架（无 `pytest`、`unittest`、`nose`）
- 没有 `pyproject.toml`、`setup.cfg`、`setup.py` 等配置文件
- 没有 CI 配置
- 没有测试覆盖率工具、类型检查器（mypy）或 linter（flake8/pylama）

项目采用手动验证方式：运行脚本，人工检查输出。

### 现有的类测试脚本（不是真正的测试，而是手动调试/分析工具）

| 文件 | 用途 |
|---|---|
| `backtest_combo_v4.py` 等回测脚本 | 回测策略历史表现，输出胜率/收益率等指标 |
| `debug_features.py` | 手动检查 ML 特征分布 |
| `check_labels_v2.py` | 检查训练标签分布 |
| `check_model.py` | 检查已训练的模型文件 |
| `debug_prediction.py` | 检查 ML 预测输出 |
| `test_atr.py` | 测试 ATR 指标计算（接近单元测试，但无断言） |
| `test_tushare_news.py` | 手动测试 Tushare 接口 |

## 测试需求分析

### 核心架构特征（影响测试策略）

1. **IO密集型**：几乎所有代码都依赖 MySQL、Tushare API、实时行情 HTTP 接口
2. **时间敏感**：大量函数依赖 `datetime.now()` 和交易日历
3. **副作用广泛**：函数同时读写 MySQL、JSON 文件、全局缓存
4. **模块边界模糊**：`app_core.py` 作为 re-export 枢纽，使得依赖关系复杂
5. **混合结构**：旧单体代码与模块化代码并存

### 建议的分层测试策略

#### 第一层：纯函数单元测试（优先覆盖）

以下模块的函数不依赖 IO，可独立测试：

- `quant_app/utils/indicators.py` — `calculate_ema`、`calculate_macd`、`calculate_kdj`、`calculate_bollinger_bands`、`calculate_atr`
- `quant_app/utils/auth.py` — `hash_pw`、`verify_pw`、`make_token`
- `quant_app/utils/config.py` — `get_db_config`
- `quant_app/services/technical_service.py` — 技术指标计算（尾值适配层）
- `quant_app/services/realtime_service.py` — `_get_limit_pct`（纯逻辑判断）

#### 第二层：Mocked 集成测试

核心业务逻辑通过 mock 外部依赖来测试：

- `quant_app/services/market_service.py` — mock `pymysql` 和 `tushare` 后测试数据处理逻辑
- `quant_app/services/strategy_service.py` — mock 行情数据和 MySQL 后测试评分逻辑
- `quant_app/utils/persistence.py` — 使用 tempfile mock JSON 文件测试读写逻辑
- `ml_train_v6.py` — 提供固定的 DataFrame 测试特征构建和标签计算
- `ml_predict.py` — 提供固定特征矩阵测试预测管道

#### 第三层：端到端回归测试（后期）

真实 MySQL 数据库 + 行情 API 的完整链路测试，用独立的测试数据库隔离。

## 推荐的测试框架与工具

| 工具 | 用途 |
|---|---|
| `pytest` | 主测试框架 |
| `pytest-cov` | 覆盖率报告 |
| `unittest.mock` / `pytest-mock` | 外部依赖 mock |
| `freezegun` | 冻结时间（交易日判断、缓存过期等） |
| `pytest-mysql` | MySQL 集成测试（可选，后期） |

安装：
```
pip install pytest pytest-cov pytest-mock freezegun
```

## 建议的测试目录结构

```
tests/
├── __init__.py
├── conftest.py              # 共享 fixture
├── test_config.py           # 配置测试
├── test_indicators.py       # 技术指标测试（纯函数，优先实现）
├── test_auth.py             # 认证测试
├── test_market_service.py   # 行情服务测试（mocked）
├── test_strategy_service.py # 策略服务测试（mocked）
├── test_persistence.py      # 持久化测试
├── test_technical_service.py # 技术指标适配层测试
├── test_market_state.py     # 市场状态测试
├── test_routes/             # API 路由测试（可选）
│   ├── test_auth_routes.py
│   └── test_strategy_routes.py
└── fixtures/                # 测试数据
    ├── sample_daily_price.csv
    ├── sample_market_index.csv
    └── sample_stock_info.csv
```

## 优先建议：先写哪类测试

1. **`test_indicators.py`** — 纯函数，有明确输入输出，回测正确性依赖它。是所有量化逻辑的基石。
2. **`test_auth.py`** — 密码哈希/验证/令牌生成，安全和正确性关键。
3. **`test_persistence.py`** — JSON 文件的原子写入和并发保护，数据完整性关键。
4. **`test_market_service.py`** — 最核心的业务逻辑，Mock MySQL 后测试数据查询和计算结果。
5. **`test_strategy_service.py`** — 评分和选股逻辑，Mock 行情数据后验证排序和筛选。

## 关键测试场景

### 技术指标（`test_indicators.py`）

- EMA：输入固定序列，验证输出值
- MACD：验证 DIF/DEA/HIST 的数值和长度
- KDJ：验证 K/D/J 的范围（0-100）
- BOLL：验证 upper ≥ middle ≥ lower
- ATR：验证非负值

### 认证（`test_auth.py`）

- `hash_pw` 返回 bcrypt 哈希
- `verify_pw` 正确密码返回 `(True, None)`
- `verify_pw` 错误密码返回 `(False, None)`
- `verify_pw` 旧格式（SHA256）兼容
- `make_token` 返回 64 位 hex 字符串

### 持久化（`test_persistence.py`）

- `_atomic_json_dump` 原子写入不损坏
- `save_access_log` 写入后读取验证内容
- `load_users` / `save_users` 读写一致性
- 多线程并发写入不丢失数据

### 市场服务（`test_market_service.py`，Mocked）

- `get_recent_trade_dates` 返回正确的日期列表
- `calculate_rps` 输入涨幅列表，验证 RPS 分位值
- `get_stock_history_from_db` 返回正确字段

### 策略评分（`test_strategy_service.py`，Mocked）

- `score_stock_c30` 输入 mock 行情数据，验证评分字段完整
- `analyze_stock` 验证返回字典结构
- 各评分维度的边界值（涨停、跌停、停牌等）

### ML 模块（`test_ml.py`，可选）

- 特征构建正确性（shift 处理、rolling 窗口）
- 标签计算（`target_5d` 收益率的正确性）
- 模型加载与推断（提供固定特征矩阵，验证输出形状）

## Mock 策略

### MySQL mock

```python
import pytest
from unittest.mock import patch, MagicMock

@pytest.fixture
def mock_db_conn():
    """创建 mock MySQL 连接"""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    return conn

def test_get_recent_trade_dates(mock_db_conn):
    with patch('pymysql.connect', return_value=mock_db_conn):
        pass  # test body
```

### Tushare mock

```python
@pytest.fixture
def mock_tushare_pro():
    """创建 mock Tushare pro API"""
    pro = MagicMock()
    # mock daily return
    df = pd.DataFrame({
        'ts_code': ['000001.SZ', '000002.SZ'],
        'trade_date': ['20260505', '20260505'],
        'pct_chg': [2.5, -1.2],
        'close': [15.0, 8.5],
    })
    pro.daily.return_value = df
    return pro
```

### 时间冻结

```python
from freezegun import freeze_time

@freeze_time("2026-05-05 10:00:00")
def test_is_trading_time():
    from quant_app.services.realtime_service import _is_trading_time
    assert _is_trading_time() == True

@freeze_time("2026-05-05 18:00:00")
def test_is_trading_time_after_hours():
    from quant_app.services.realtime_service import _is_trading_time
    assert _is_trading_time() == False
```

## 配置

建议在项目根目录创建 `pyproject.toml`：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
markers = [
    "slow: 需要外部连接的测试",
    "mysql: 需要真实 MySQL 数据库的测试",
]

[tool.coverage.run]
source = ["quant_app", "app_core", "app_api", "market_state", "ml_predict"]
omit = ["*/__pycache__/*", "*/tests/*"]

[tool.coverage.report]
show_missing = true
fail_under = 30
```

## 现有测试相关工具和脚本

项目现有的一些调试脚本如果改造成测试会很有价值：

- `debug_features.py` — 特征构建的正确性可以转化为 `test_feature_construction()`
- `check_labels_v2.py` — 标签分布检查可转化为 `test_label_distribution()`
- `test_atr.py` — ATR 计算已经是最接近单元测试的形式
- `backtest_combo_v*.py` — 这些更适合作为回测报告生成器而非测试
