# 编码约定

> 基于 /Users/mozengfu/workspace/quant-system/ 的实际代码模式整理。
> 不是规范起草，而是对现有实践的记录。
> 新代码应与这些模式保持一致，除非有明确理由打破。

## 文件与目录命名

- Python 文件：`snake_case.py`（例如 `app_core.py`、`market_service.py`、`feishu_alerts.py`）
- 目录：`snake_case/`（例如 `quant_app/`、`scripts/`、`templates/`）
- 可执行脚本：带上 `#!/usr/bin/env python3` shebang，文件有执行权限（`-rwx------` 或 `-rwxr-xr-x`）
- HTML 模板：`lowercase_with_underscores.html`（例如 `market_analysis.html`、`strategy_v41.html`）
- 静态资源：放入 `static/` 目录，子目录按类型分（`static/css/`、`static/icons/`）

## 命名规范

| 类别 | 风格 | 示例 |
|---|---|---|
| 函数/方法 | `snake_case` | `get_stock_realtime()`、`calculate_ema()` |
| 变量 | `snake_case` | `ts_code`、`today_str`、`db_config` |
| 常量 | `UPPER_SNAKE_CASE` | `BASE_DIR`、`DATA_DIR`、`STOP_LOSS_PCT`、`EXCLUDE_PREFIXES` |
| 私有函数 | 前缀 `_` | `_try_tencent()`、`_atomic_json_dump()` |
| 模块级缓存 | 前缀 `_` + dict | `_cache`、`_state_cache`、`_quote_cache` |
| FastAPI 路由 | lowercase + hyphen | `/api/market/premarket`、`/api/auth/login` |
| 路由标签 | 字符串 | `tags=["auth"]`、`tags=["market"]` |

注意：代码库**不**使用 Python 类型注解。函数签名无 `-> ReturnType` 和 `param: Type`。

## 导入组织

按三块分组，块间空一行：

```python
import os, json, time, logging, sys       # 标准库
import pandas as pd                        # 第三方
from quant_app.utils.config import ...     # 应用内部
```

具体模式：
- 标准库一行多模块用逗号分隔（`import os, json, time, logging`）
- `pandas` 和 `numpy` 惯用别名：`import pandas as pd`、`import numpy as np`
- `pymysql`、`fastapi`、`lightgbm` 这些第三方库全名导入
- 应用内部通过 `quant_app` 包导入，如 `from quant_app.utils.config import get_db_config`
- `app_core.py` 作为 re-export 枢纽：从 `quant_app.services.*` 和 `quant_app.utils.*` 导入后重新暴露；`app_api.py` 和路由模块再从 `app_core` 导入
- 脚本文件（`scripts/` 下）用 `sys.path.insert(0, ...)` 确保能找到 `quant_app` 包
- 避免循环依赖时使用函数内延迟导入（import inside function），参见 `app_core.py` 中的 `get_current_user()`

## 代码风格

- Python 3.12+，无类型注解
- 部分文件标注 `# -*- coding: utf-8 -*-` 编码声明
- 中文用于：模块 docstring、注释、业务逻辑字符串（如返回给前端的字段名 `"现价"`、`"涨跌幅"`）
- 英文用于：代码标识符（变量名、函数名、类名）、日志消息、Git 提交
- 代码库中同时存在两种结构：
  - 旧单体式：`app_core.py` ~140KB 混杂多种功能
  - 模块化重构：`quant_app/` 包按职责分离

## 函数设计

- **长函数常见**：`strategy_service.py` 和 `market_service.py` 中函数可达数百行（例如 `analyze_stock()`、`scan_daily_pool()`）
- **返回值不统一**：成功返回 dict/list，失败返回 `None` 或 `{"error": "..."}` 字典
- **Fallback 链模式**：函数内依次尝试多个数据源，前一个失败自动切下一个
  ```python
  result = _try_tencent(code, market)
  if not result:
      result = _try_eastmoney(code, market)
  if not result:
      result = _try_aliyun(code, market)
  ```
- **缓存 + TTL 模式**：`_get_cached(key)` / `_set_cache(key, data)` 配合模块级 `_cache` 字典
- **纯函数**与**有副作用函数**混在一起（如 `update_stock_results()` 同时读写 MySQL、JSON 文件和全局缓存）

## 错误处理

全部使用 `try/except Exception`（约 230 处），模式单一：

```python
try:
    # 可能失败的操作
    result = some_api_call()
    return result
except Exception as e:
    logger.warning(f"操作描述失败: {e}")
    return None  # 或 {"error": "..."}、[] 等默认值
```

特点：
- 永远捕获 `Exception`，不细分异常类型
- `logger.warning` 用于预期内的上游失败（API 超时、数据库查询空结果等）
- `logger.error` 用于意外失败（模型加载、配置文件缺失等），部分附带 `exc_info=True`
- `logger.info` 记录正常流程的关键节点
- 没有自定义异常类
- FastAPI 路由中使用 `HTTPException(status_code=401, detail="未登录")` 处理认证失败
- `app_api.py` 中有全局异常处理器:
  ```python
  @app.exception_handler(Exception)
  async def global_exception_handler(request, exc):
      logger.error(f"未处理异常: {exc}", exc_info=True)
      return StarletteJSONResponse(status_code=500, content={"detail": "服务器内部错误"})
  ```

## 日志

```python
import logging
logger = logging.getLogger(__name__)
```

级别使用：
- `logger.info` — 关键状态变更（模型加载、数据记录数量、服务启动/停止）
- `logger.warning` — 可恢复的失败（API 调用失败、文件不存在、数据解析异常）
- `logger.error` — 意外错误（配置缺失、模型加载失败），部分带 `exc_info=True`

入口点（`app.py`、`app_api.py`、各脚本 `__main__`）配置 basicConfig：
```python
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
```

无结构化日志、无日志关联 ID、无日志轮转配置。

## 注释与文档

- 每个模块有模块级 docstring（中文），简要描述模块功能
- 公共函数有 docstring 描述功能和参数（部分函数带参数说明，但不一致）
- 用 `# ========== 标题 ==========` 分隔代码节
- No `TODO`、`FIXME` 标记（除 CLAUDE.md）
- 敏感逻辑、fallback 链、兼容处理处有中文行内注释

## 模块设计

```
quant-system/
├── quant_app/              # 模块化包（进行中）
│   ├── utils/              # 工具层：config, auth, indicators, persistence, model_loader
│   ├── services/           # 业务层：market, strategy, backtest, realtime, technical, notification
│   ├── routes/             # API 层：auth, dashboard, strategy, market, admin, pages
│   └── models/             # 未实现（空文件）
├── scripts/                # 独立自动化脚本（cron 任务、回测、数据工具）
├── app_core.py             # 遗留的 re-export 枢纽（~140KB，包含 market_service + strategy_service + persistence 的全部 re-export）
├── app_api.py              # FastAPI 应用组装和启动逻辑
├── app.py                  # 入口（uvicorn.run）
├── ml_predict.py           # ML 推理（尚未迁入 quant_app）
├── ml_train_v6.py          # ML 训练（独立）
├── market_state.py         # 市场状态检测（尚未迁入 quant_app）
├── data/                   # JSON 数据文件和 ML 模型 .pkl 文件
├── templates/              # Jinja2 HTML 模板
├── static/                 # 静态文件（CSS、图标等）
└── tests/                  # 空（仅有 __init__.py）
```

依赖方向：`routes → services → utils`，但 `app_core` 打破了这一结构（任何模块都可通过引入 `app_core` 访问几乎所有函数）。

## 跨模块数据流

读写 MySQL 时，每个函数直接建立自己的连接（`pymysql.connect(...)`），没有连接池管理中间层（除 `DBUtils` 在 `requirements.txt` 中但未见使用）。

```python
def get_db():
    return pymysql.connect(**DB_CONFIG)  # 每个函数各自创建连接
```

`get_db_config()` 统一提供配置字典（`quant_app/utils/config.py`），但创建连接在调用方。
