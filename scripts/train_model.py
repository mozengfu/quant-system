#!/usr/bin/env python3
"""
统一 ML 训练入口

用法:
    python3 scripts/train_model.py               # 训练最新 v6.5
    python3 scripts/train_model.py v6.5           # 指定版本
    python3 scripts/train_model.py --list         # 列出可用版本
    python3 scripts/train_model.py v6.5 --quick   # 快速模式
"""
import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERSIONS = {
    "v6": {"module": "ml_train_v6", "file": "ml_train_v6.py"},
    "v6.2": {"module": "ml_train_v6_2", "file": "ml_train_v6_2.py"},
    "v6.3": {"module": "ml_train_v6_3", "file": "ml_train_v6_3.py"},
    "v6.4": {"module": "ml_train_v6_4", "file": "ml_train_v6_4.py"},
    "v6.5": {"module": "ml_train_v6_5", "file": "ml_train_v6_5.py"},
}
LATEST = "v6.5"


def list_versions():
    print("可用训练版本:")
    for v in VERSIONS:
        tag = " (最新)" if v == LATEST else ""
        print(f"  {v}{tag}")
    print(f"\n默认: {LATEST}")


def main():
    if "--list" in sys.argv:
        list_versions()
        return

    version = LATEST
    for arg in sys.argv[1:]:
        if arg in VERSIONS:
            version = arg
            break

    if version not in VERSIONS:
        print(f"未知版本: {version}")
        list_versions()
        sys.exit(1)

    info = VERSIONS[version]
    print(f"开始训练 {version} ({info['file']})...")

    import importlib
    try:
        mod = importlib.import_module(info["module"])
        mod.main()
        print(f"\n✅ {version} 训练完成")
    except Exception as e:
        print(f"\n❌ {version} 训练失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
