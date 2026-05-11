#!/usr/bin/env python3
"""
智能量化系统 v2.0 - 主入口

模块化结构:
- app_core.py: 核心业务逻辑（数据获取、选股策略、技术指标等）
- app_api.py: FastAPI 路由层

启动方式: python3 app.py
"""
import uvicorn

# 导入 FastAPI app 实例并启动
from app_api import app

if __name__ == "__main__":
    uvicorn.run(
        "app_api:app",
        host="0.0.0.0",
        port=5001,
        reload=False,
        log_level="info"
    )
