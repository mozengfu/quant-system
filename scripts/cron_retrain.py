#!/usr/bin/env python3
"""
定时重训练入口（crontab调用）
每月1号凌晨3点自动执行，结果发飞书通知

用法: python3 scripts/cron_retrain.py
"""
import os, sys, subprocess, logging, traceback
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

LOG_DIR = os.path.join(BASE, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

FEISHU_WEBHOOK = os.environ.get('FEISHU_BOT_WEBHOOK', '')
SSH_OPTS = '-o StrictHostKeyChecking=no -o ConnectTimeout=10'
WIN_USER = 'quant@192.168.10.39'


def send_feishu(msg):
    """发飞书通知"""
    if not FEISHU_WEBHOOK:
        logger.info(f"[飞书](跳过,未配置webhook): {msg}")
        return
    try:
        import requests
        data = {"msgtype": "text", "text": {"content": msg}}
        requests.post(FEISHU_WEBHOOK, json=data, timeout=10)
    except Exception as e:
        logger.warning(f"飞书发送失败: {e}")


def main():
    start = datetime.now()
    logger.info("=" * 50)
    logger.info("月度重训练开始")
    logger.info("=" * 50)

    # 检查训练机是否在线
    try:
        r = subprocess.run(['ping', '-c', '1', '-W', '3', '192.168.10.39'],
                          capture_output=True, timeout=5)
        win_online = r.returncode == 0
    except:
        win_online = False

    result_msg = ""
    success = False

    try:
        # Mac 快速重训是默认方案（Top500，排除微盘噪声）
        # Windows 在线时才切到全量训练（更多特征，但WF IC不一定更高）
        if win_online:
            logger.info("Windows 训练机在线，执行全量训练")
            result_msg = _train_on_windows()
            success = True
        else:
            logger.info("Windows 不在线，执行 Mac 快速重训（Top500，已验证效果不低于全量）")
            result_msg = _train_on_mac()
            success = True
    except Exception as e:
        err_msg = f"重训练异常: {e}\n{traceback.format_exc()[-500:]}"
        logger.error(err_msg)
        result_msg = f"❌ 重训练失败\n{str(e)[:200]}"
        success = False

    elapsed = (datetime.now() - start).total_seconds()
    result_msg += f"\n耗时: {elapsed:.0f}s ({elapsed/60:.1f}min)"

    # 飞书通知
    title = "✅ 模型重训练完成" if success else "❌ 模型重训练失败"
    send_feishu(f"{title}\n{result_msg}")
    logger.info(f"飞书通知已发送: {title}")

    # 写日志
    log_path = os.path.join(LOG_DIR, f"retrain_{datetime.now().strftime('%Y%m%d')}.log")
    with open(log_path, 'w') as f:
        f.write(result_msg)
    logger.info(f"日志: {log_path}")

    return 0 if success else 1


def _train_on_windows():
    """Windows 全量训练"""
    cmd = f'ssh {SSH_OPTS} {WIN_USER} "cd C:\\Users\\quant\\quant-system && python ml_train_v11_2_board_rps.py"'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"Windows训练失败: {r.stderr[-300:]}")

    # 提取IC
    ic_line = ""
    for line in r.stdout.split('\n'):
        if '全数据等权融合' in line:
            ic_line = line.strip()
            break

    # 传回模型
    ts = datetime.now().strftime('%Y%m%d')
    remote = f'{WIN_USER}:C:/Users/quant/quant-system/data/ml_stock_model_v11_0.pkl'
    local = os.path.join(BASE, 'data', f'ml_stock_model_v11_0_{ts}.pkl')

    r2 = subprocess.run(['scp', *SSH_OPTS.split(), remote, local],
                       capture_output=True, text=True, timeout=120)
    if r2.returncode != 0:
        raise RuntimeError(f"模型传回失败: {r2.stderr}")

    # 部署
    import shutil
    current = os.path.join(BASE, 'data', 'ml_stock_model_v11_0.pkl')
    backup = os.path.join(BASE, 'data', 'ml_stock_model_v11_0_pre_retrain.pkl')
    if os.path.exists(current):
        shutil.copy2(current, backup)
    shutil.copy2(local, current)
    os.remove(local)

    size = os.path.getsize(current) / 1024 / 1024
    return f"✅ Windows全量训练完成\n模型: V11.2(板RPS) {size:.0f}MB\n{ic_line}"


def _train_on_mac():
    """Mac 快速重训练（Top500）"""
    r = subprocess.run(
        [sys.executable, os.path.join(BASE, 'scripts', 'retrain_v11_fast.py')],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"Mac训练失败: {r.stderr[-300:]}")

    # 提取WF IC
    ic_line = ""
    for line in r.stdout.split('\n'):
        if '平均 RankIC' in line:
            ic_line = line.strip()

    # retrain_v11_fast.py 自动保存到 data/ml_stock_model_v11_0_mac_retrain.pkl
    # 需要打包并部署到生产路径
    import joblib, shutil
    src = os.path.join(BASE, 'data', 'ml_stock_model_v11_0_mac_retrain.pkl')
    dst = os.path.join(BASE, 'data', 'ml_stock_model_v11_0.pkl')

    # 重新打包为兼容格式
    r2 = subprocess.run(
        [sys.executable, os.path.join(BASE, 'scripts', 'rebundle_model.py')],
        capture_output=True, text=True, timeout=60)

    size = os.path.getsize(dst) / 1024 / 1024
    return f"✅ Mac快速重训完成\n模型: V11.0(Top500) {size:.0f}MB\n{ic_line}"


if __name__ == '__main__':
    sys.exit(main())
