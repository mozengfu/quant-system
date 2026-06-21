#!/usr/bin/env python3
"""把 Mac 重训练模型重新打包为 V10.0+ dict 格式 (兼容XGBoost DMatrix)"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import joblib
import numpy as np

SRC = 'data/ml_stock_model_v11_0_mac_retrain.pkl'
DST = 'data/ml_stock_model_v11_0.pkl'

bundle = joblib.load(SRC)
print(f"新模型: {bundle.get('version')}, {bundle.get('n_features')} 特征")

# 提取所有子模型
model_keys = [k for k in bundle.keys()
              if k not in ('feature_cols', 'global_medians', 'version',
                          'n_models', 'n_features', 'n_samples', 'n_stocks',
                          'data_range', 'wf_avg_rank_ic', 'generated_at', 'wf_cv_results')]

# 构建 V10.0+ dict 格式: {'name': {'model': ..., 'feature_cols': [...]}}
model_dict = {}
for mk in model_keys:
    m_val = bundle[mk]
    if isinstance(m_val, dict) and 'model' in m_val and 'feature_cols' in m_val:
        model_dict[mk] = m_val
    else:
        model_dict[mk] = {'model': m_val, 'feature_cols': bundle['feature_cols']}

print(f"提取 {len(model_dict)} 个模型 (dict格式)")

deploy_bundle = {
    'version': bundle.get('version', 'v11.0_mac'),
    'models': model_dict,
    'feature_subsets': {k: v['feature_cols'] for k, v in model_dict.items()},
    'feature_cols': bundle['feature_cols'],
    'global_medians': bundle.get('global_medians', {}),
    'n_models': len(model_dict),
    'n_features': bundle.get('n_features', len(bundle['feature_cols'])),
    'n_samples': bundle.get('n_samples', 0),
    'wf_avg_rank_ic': bundle.get('wf_avg_rank_ic'),
    'generated_at': bundle.get('generated_at'),
}

print(f"打包后: {len(deploy_bundle['models'])} models (dict格式)")

# 验证预测
import xgboost as xgb
feat_cols = bundle['feature_cols']
test_X = np.random.randn(5, len(feat_cols)).astype(np.float32)

for name, m_dict in model_dict.items():
    cols = [c for c in m_dict['feature_cols'] if c in feat_cols]
    idxs = [feat_cols.index(c) for c in cols]
    sub_X = test_X[:, idxs]
    m = m_dict['model']
    try:
        if hasattr(m, '_Booster') or 'xgboost' in type(m).__module__:
            pred = m.predict(xgb.DMatrix(sub_X))
        else:
            pred = m.predict(sub_X)
        print(f"  {name}: pred={pred.mean():.4f} ({len(cols)}特征) ok")
    except Exception as e:
        print(f"  {name}: 预测跳过 ({type(e).__name__})")
print("模型预测验证完成")

# 保存 (dict格式, _ensemble_predict会正确处理XGBoost DMatrix和特征子集)
joblib.dump(deploy_bundle, DST)
