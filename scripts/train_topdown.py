#!/usr/bin/env python3
"""
TopDown V1 — 三层模型训练脚本

时间划分: 训练 2024-01-01 ~ 2025-09-30, 回测 2025-10-01 ~ 2026-05-15

用法:
  python scripts/train_topdown.py all             # 训练全部三层
  python scripts/train_topdown.py market          # 仅训练 Layer 1
  python scripts/train_topdown.py sector           # 仅训练 Layer 2
  python scripts/train_topdown.py wave             # 仅训练 Layer 3
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql

from quant_app.utils.config import get_db_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

TRAIN_START = '2024-01-01'
TRAIN_END = '2025-09-30'


def train_market(conn):
    """训练 Layer 1: 大盘气象"""
    logger.info("=" * 60)
    logger.info(f"Training Layer 1: Market Direction V3 ({TRAIN_START} ~ {TRAIN_END})")
    logger.info("=" * 60)
    from quant_app.models.market_direction_v3 import train
    bundle = train(conn, start_date=TRAIN_START, end_date=TRAIN_END)
    if bundle:
        logger.info(f"Layer1 complete: CV acc={bundle['cv_acc_mean']:.3f}, f1={bundle['cv_f1_mean']:.3f}")
    return bundle


def train_sector(conn):
    """训练 Layer 2: 热点板块"""
    logger.info("=" * 60)
    logger.info(f"Training Layer 2: Sector Heat V1 ({TRAIN_START} ~ {TRAIN_END})")
    logger.info("=" * 60)
    from quant_app.models.sector_heat_v1 import train
    bundle = train(conn, start_date=TRAIN_START, end_date=TRAIN_END)
    if bundle:
        logger.info(f"Layer2 complete: CV RankIC={bundle['cv_rank_ic_mean']:.4f}")
    return bundle


def train_wave(conn):
    """训练 Layer 3: 主升浪捕手"""
    logger.info("=" * 60)
    logger.info(f"Training Layer 3: Wave Catcher V1 ({TRAIN_START} ~ {TRAIN_END})")
    logger.info("=" * 60)
    from quant_app.models.wave_catcher_v1 import train
    bundle = train(conn, start_date=TRAIN_START, end_date=TRAIN_END)
    if bundle:
        logger.info(f"Layer3 complete: CV AUC={bundle['cv_auc_mean']:.4f}, "
                    f"Prec={bundle['cv_prec_mean']:.4f}, Rec={bundle['cv_recall_mean']:.4f}")
    return bundle


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    conn = pymysql.connect(**get_db_config())

    try:
        if cmd == 'all':
            train_market(conn)
            train_sector(conn)
            train_wave(conn)
            logger.info("\nAll three layers trained successfully!")
            logger.info("  Models saved to data/models/")
        elif cmd == 'market':
            train_market(conn)
        elif cmd == 'sector':
            train_sector(conn)
        elif cmd == 'wave':
            train_wave(conn)
        else:
            print(f"Unknown command: {cmd}")
            print(__doc__)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
