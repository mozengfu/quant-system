#!/usr/bin/env python3
"""精细调试ml_daily_top5卡顿点"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['PYTHONUNBUFFERED'] = '1'

import logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s',
                    datefmt='%H:%M:%S', force=True)
# 仅关键模块DEBUG
for noisy in ('urllib3', 'pymysql', 'asyncio'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

print('[STEP 0] 启动', flush=True)
t_start = time.time()

# 在导入前注入进度探针
import ml_predict as mp
print(f'[STEP 1] import ml_predict +{time.time()-t_start:.1f}s', flush=True)

orig_load_model = mp.load_model
def timed_load_model(version):
    t0 = time.time()
    print(f'[LOAD] 开始加载 {version}', flush=True)
    r = orig_load_model(version)
    print(f'[LOAD] {version} 完成 +{time.time()-t0:.1f}s -> {"OK" if r else "None"}', flush=True)
    return r
mp.load_model = timed_load_model

import quant_app.services.strategy_service as ss
print(f'[STEP 2] import strategy_service +{time.time()-t_start:.1f}s', flush=True)

orig_bt = ss._block_trade_bonus
def bt_wrapper(*a, **k):
    t0 = time.time()
    r = orig_bt(*a, **k)
    dt = time.time() - t0
    if dt > 0.3:
        print(f'[BT] slow {dt:.2f}s code={a[0] if a else "?"}', flush=True)
    return r
ss._block_trade_bonus = bt_wrapper

orig_dh = ss._dragon_holder_bonus
def dh_wrapper(*a, **k):
    t0 = time.time()
    r = orig_dh(*a, **k)
    dt = time.time() - t0
    if dt > 0.3:
        print(f'[DH] slow {dt:.2f}s code={a[0] if a else "?"}', flush=True)
    return r
ss._dragon_holder_bonus = dh_wrapper

# patch build_features_v11_inference
from scripts.predict_v11 import build_features_v11_inference
orig_bf = build_features_v11_inference
def bf_wrapper(conn, codes, **kw):
    t0 = time.time()
    print(f'[BF] build_features_v11_inference START codes={len(codes)}', flush=True)
    r = orig_bf(conn, codes, **kw)
    print(f'[BF] build_features_v11_inference END +{time.time()-t0:.1f}s shape={r.shape if r is not None and hasattr(r, "shape") else r}', flush=True)
    return r
import scripts.predict_v11
scripts.predict_v11.build_features_v11_inference = bf_wrapper

# patch _ensemble_scores（在ml_predict模块里）
orig_es = mp._ensemble_scores
def es_wrapper(*a, **k):
    t0 = time.time()
    print(f'[ES] _ensemble_scores START', flush=True)
    r = orig_es(*a, **k)
    print(f'[ES] _ensemble_scores END +{time.time()-t0:.1f}s', flush=True)
    return r
mp._ensemble_scores = es_wrapper

print(f'[STEP 3] 调用 main +{time.time()-t_start:.1f}s', flush=True)

from ml_daily_top5 import main
try:
    main()
except Exception as e:
    import traceback
    traceback.print_exc()
    print(f'[ERR] {type(e).__name__}: {e}', flush=True)

print(f'[DONE] +{time.time()-t_start:.1f}s', flush=True)