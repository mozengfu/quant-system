"""集中式 JSON 编码器 — 处理 numpy/pandas 类型的序列化"""

import json
import numpy as np


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.bool_, np.integer)):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


def safe_json_dumps(data, **kwargs):
    """safe json dumps for data containing numpy types"""
    return json.dumps(data, cls=NpEncoder, **kwargs)
