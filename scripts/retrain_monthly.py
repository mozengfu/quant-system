#!/usr/bin/env python3
"""
月度模型重训练 — 一键执行
用法: python3 scripts/retrain_monthly.py

流程:
  1. 远程连接 Windows 训练机 (192.168.10.39)
  2. 运行 ml_train_v11_2_board_rps.py（带板RPS特征）
  3. 传回新模型到 Mac
  4. 备份旧模型，部署新模型
  5. 清理临时文件
"""
import os, sys, logging, json
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
DATA = BASE / 'data'
WIN_CMD = 'cd C:\\Users\\quant\\quant-system &&'
WIN_USER = 'quant@192.168.10.39'
SSH_OPTS = '-o StrictHostKeyChecking=no'


def step(msg):
    logger.info(f"\n{'='*50}")
    logger.info(msg)
    logger.info('='*50)


def main():
    # Step 1: 检查 Windows 在线
    step("1/6 检查 Windows 训练机")
    import subprocess
    r = subprocess.run(['ping', '-c', '1', '-W', '3', '192.168.10.39'], capture_output=True, timeout=5)
    if r.returncode != 0:
        logger.error("训练机不在线，先开机。请手动执行: ssh quant@192.168.10.39")
        sys.exit(1)
    logger.info("✅ 训练机在线")

    # Step 2: 检查训练脚本是否存在
    step("2/6 检查训练脚本")
    r = subprocess.run(
        ['ssh', *SSH_OPTS.split(), f'{WIN_USER}',
         f'{WIN_CMD} if exist ml_train_v11_2_board_rps.py (echo OK) else (echo MISSING)'],
        capture_output=True, text=True, timeout=10)
    if 'MISSING' in r.stdout:
        logger.error("Windows 上缺少 ml_train_v11_2_board_rps.py，先同步")
        sys.exit(1)
    logger.info("✅ 训练脚本就绪")

    # Step 3: 在 Windows 上训练
    step("3/6 Windows 全量训练（约15分钟）")
    logger.info("训练中，请稍候...")
    r = subprocess.run(
        ['ssh', *SSH_OPTS.split(), f'{WIN_USER}',
         f'{WIN_CMD} python ml_train_v11_2_board_rps.py'],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        logger.error(f"训练失败:\n{r.stderr[-500:]}")
        sys.exit(1)
    # 提取WF IC
    for line in r.stdout.split('\n'):
        if '全数据等权融合' in line:
            logger.info(f"  {line.strip()}")
        if 'Rank IC' in line and '均值' in r.stdout.split('\n')[max(0, r.stdout.split('\n').index(line)-3)]:
            logger.info(f"  {line.strip()}")
    logger.info("✅ 训练完成")

    # Step 4: 传回模型
    step("4/6 传回模型到 Mac")
    ts = datetime.now().strftime('%Y%m%d_%H%M')
    new_model = f'ml_stock_model_v11_0_{ts}.pkl'
    r = subprocess.run(
        ['scp', *SSH_OPTS.split(),
         f'{WIN_USER}:C:/Users/quant/quant-system/data/ml_stock_model_v11_0.pkl',
         str(DATA / new_model)],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        logger.error(f"传回失败: {r.stderr}")
        sys.exit(1)
    size = os.path.getsize(DATA / new_model) / 1024 / 1024
    logger.info(f"✅ 已传回: {new_model} ({size:.0f}MB)")

    # Step 5: 部署
    step("5/6 部署新模型")
    # 备份当前模型
    old_backup = DATA / 'ml_stock_model_v11_0_pre_retrain.pkl'
    current = DATA / 'ml_stock_model_v11_0.pkl'
    if current.exists():
        import shutil
        shutil.copy2(current, old_backup)
        logger.info(f"✅ 旧模型已备份: {old_backup.name}")

    # 部署新模型
    import shutil
    shutil.copy2(DATA / new_model, current)
    logger.info(f"✅ 新模型已部署: {current}")

    # Step 6: 验证
    step("6/6 验证新模型")
    r = subprocess.run(
        ['python3', '-c',
         'from ml_predict import _load_best_model; b,v=_load_best_model(); '
         f'print(f\"版本:{{b.get(\\\"version\\\",\\\"?\\\")}} 模型:{{b.get(\\\"n_models\\\",\\\"?\\\")}} 特征:{{b.get(\\\"n_features\\\",\\\"?\\\")}}\")'],
        capture_output=True, text=True, timeout=30)
    print(r.stdout)
    if r.returncode == 0:
        logger.info("✅ 模型验证通过")
    else:
        logger.warning(f"模型验证异常: {r.stderr}")

    # 清理临时文件
    os.remove(DATA / new_model)

    step("✅ 月度重训练完成")


if __name__ == '__main__':
    main()
