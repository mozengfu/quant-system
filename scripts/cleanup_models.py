#!/usr/bin/env python3
"""
模型清理工具 - 保留 N 个最新版本，旧版本移入 archive/

用法:
    python3 scripts/cleanup_models.py              # 保留最新 2 个版本
    python3 scripts/cleanup_models.py --keep 3      # 保留 3 个
    python3 scripts/cleanup_models.py --dry-run     # 预览不执行
"""
import os
import sys
import shutil
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# 按优先级排序的版本列表（最新优先）
VERSION_ORDER = ["v6.5", "v6.4", "v6.3", "v6.2", "v6"]


def _underscore_version(ver):
    """v6.2 -> v6_2"""
    return ver.replace(".", "_")


def main():
    keep = 2
    dry_run = False
    for arg in sys.argv[1:]:
        if arg.startswith("--keep="):
            keep = int(arg.split("=")[1])
        elif arg == "--dry-run":
            dry_run = True

    versions_to_archive = VERSION_ORDER[keep:]
    if not versions_to_archive:
        print(f"当前保留 {keep} 个版本，无需清理")
        return

    moved = 0
    for ver in versions_to_archive:
        uver = _underscore_version(ver)

        # 处理 .pkl 文件
        pkl_pattern = os.path.join(DATA_DIR, f"ml_stock_model_{uver}.pkl")
        for src in glob.glob(pkl_pattern):
            dst = os.path.join(ARCHIVE_DIR, os.path.basename(src))
            if dry_run:
                print(f"[DRY-RUN] 将移动: {src} -> {dst}")
            else:
                shutil.move(src, dst)
                print(f"已移动: {src} -> {dst}")
            moved += 1

        # 处理 .json 配置文件
        json_pattern = os.path.join(DATA_DIR, f"feature_config_{uver}.json")
        for src in glob.glob(json_pattern):
            dst = os.path.join(ARCHIVE_DIR, os.path.basename(src))
            if dry_run:
                print(f"[DRY-RUN] 将移动: {src} -> {dst}")
            else:
                shutil.move(src, dst)
                print(f"已移动: {src} -> {dst}")
            moved += 1

    if moved == 0:
        print(f"未找到可清理的文件（保留 {keep} 个版本）")
    elif not dry_run:
        print(f"\n清理完成，共移动 {moved} 个文件到 {ARCHIVE_DIR}")
    else:
        print(f"\n[DRY-RUN] 预览完成，将移动 {moved} 个文件")


if __name__ == "__main__":
    main()
