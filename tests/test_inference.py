"""ML 推理管线测试 — 模型可用性 + 配置 + 预测完整性"""

import pytest
from pathlib import Path
from quant_app.utils.model_loader import get_model_path
from quant_app.utils.config import config

DATA_DIR = Path(__file__).parent.parent / "data"


class TestModelAvailability:
    """验证模型文件存在且降级链完整"""

    def test_v11_0_exists(self):
        path = get_model_path("v11.0")
        assert path and path.exists(), "V11.0 模型文件不存在: %s" % path

    def test_v8_1_exists(self):
        path = get_model_path("v8.1")
        assert path and path.exists(), "V8.1 fallback 模型不存在: %s" % path

    def test_feature_config_exists(self):
        cfg = DATA_DIR / "feature_config_ml_stock_model_v11_0.json"
        assert cfg.exists(), "V11.0 特征配置文件不存在: %s" % cfg

    def test_oos_model_exists(self):
        oos = DATA_DIR / "ml_stock_model_v11_0_oos.pkl"
        assert oos.exists(), "V11.0 OOS 模型不存在: %s" % oos


class TestConfigSingleton:
    """验证 Config 单例有效性"""

    def test_mysql_config(self):
        assert config.mysql.host, "MySQL host 为空"
        assert config.mysql.port > 0, "MySQL port 无效"
        assert config.mysql.database, "MySQL database 为空"

    def test_mysql_url(self):
        url = config.mysql.url
        assert url.startswith("mysql+pymysql://"), "url 格式错误: %s" % url

    def test_data_dir(self):
        assert config.data_dir.exists(), "data_dir 不存在: %s" % config.data_dir


@pytest.mark.slow
class TestModelLoading:
    """模型加载测试（慢，需加载 65MB 文件）"""

    def test_v11_0_load(self):
        from quant_app.utils.model_loader import load_model
        bundle = load_model("v11.0")
        assert bundle is not None, "V11.0 模型加载失败"
        assert "models" in bundle, "缺少 models"
        assert "feature_cols" in bundle, "缺少 feature_cols"
        assert len(bundle["models"]) == 11, "应为 11 子模型，实际 %s" % len(bundle["models"])

    def test_v8_1_load(self):
        from quant_app.utils.model_loader import load_model
        bundle = load_model("v8.1")
        assert bundle is not None, "V8.1 加载失败"
        assert "models" in bundle, "V8.1 缺少 models"


@pytest.mark.slow
def test_predict_smoke():
    """冒烟测试：最新交易日 top10 成交额股票预测（需DB+模型）"""
    from quant_app.utils.model_loader import load_model
    from quant_app.utils.config import get_db_config
    import pymysql

    bundle = load_model("v11.0")
    if bundle is None:
        pytest.skip("V11.0 模型不可用")

    conn = pymysql.connect(**get_db_config())
    try:
        cur = conn.cursor()
        cur.execute("SELECT MAX(trade_date) FROM daily_price")
        latest = cur.fetchone()[0]
        if not latest:
            pytest.skip("daily_price 表无数据")
        cur.execute(
            "SELECT ts_code FROM daily_price WHERE trade_date = %s "
            "ORDER BY amount DESC LIMIT 10",
            (latest,)
        )
        ts_codes = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        conn.close()

    if len(ts_codes) < 5:
        pytest.skip("当日仅有 %s 只股票" % len(ts_codes))

    from ml_predict import predict_batch
    results = predict_batch(ts_codes)
    assert results is not None, "predict_batch 返回 None"
    assert len(results) > 0, "预测结果为空"
    print("冒烟测试 OK: %s 只股票" % len(results))
