"""
V11 + Stage 3 集成预测器
- V11 提供 ml_prob (5日收益预测)
- Stage 3 提供 main_wave_prob (主升浪启动概率)
- 双高过滤: V11 >= 0.5 AND Stage 3 >= 0.1 才入选
- 单高 (V11 >= 0.7) 也接受
- 同时考虑板块动量 (Stage 2) 加权
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql

from quant_app.models.main_wave_detector_v1 import predict as predict_main_wave
from quant_app.models.market_direction_v1 import predict as predict_market
from quant_app.models.sector_rotation_v1 import predict as predict_sectors
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


class V11EnhancedPredictor:
    """V11 概率 + Stage 3 主升浪概率 集成"""

    def __init__(self, conn=None):
        self.conn = conn
        if self.conn is None:
            self.conn = pymysql.connect(**get_db_config())

    def run_predict(self, as_of_date: str, top_n: int = 5) -> dict:
        """
        主入口: 跑 Stage 1 → Stage 2 → V11+Stage3 集成

        Returns:
            {
              'as_of_date': str,
              'market': {...},
              'hot_sectors': [...],
              'candidates': [{ts_code, name, v11_prob, mw_prob, ensemble_score, sector, ...}],
              'gate': str,
            }
        """
        # 1) Stage 1: 大盘
        market = predict_market(self.conn, as_of_date)
        if market['direction'] == 'strong_down' and market['prob'] >= 0.6:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': [],
                'candidates': [],
                'gate': 'strong_down',
            }

        # 2) Stage 2: 热点板块
        hot_sectors = predict_sectors(self.conn, as_of_date, top_n=8)
        if not hot_sectors:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': [],
                'candidates': [],
                'gate': 'no_hot_sector',
            }
        sector_names = [s['sector_name'] for s in hot_sectors]
        sector_score_map = {s['sector_name']: s['score'] for s in hot_sectors}

        # 3) 候选池
        cur = self.conn.cursor()
        placeholders = ','.join(['%s'] * len(sector_names))
        cur.execute(f"""
            SELECT DISTINCT ts_code FROM stock_info
            WHERE industry IN ({placeholders})
              AND list_date < %s
        """, (*sector_names, as_of_date))
        candidate_codes = [r[0] for r in cur.fetchall()]

        if not candidate_codes:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': hot_sectors,
                'candidate_pool_size': 0,
                'candidates': [],
                'gate': 'no_candidates',
            }

        # 4) V11 批量预测
        logger.info(f"V11 预测 {len(candidate_codes)} 只股票")
        from ml_predict import predict_batch
        v11_preds = predict_batch(candidate_codes, db_conn=self.conn, as_of_date=as_of_date)

        # 5) Stage 3 主升浪概率
        logger.info("Stage 3 评分")
        df_mw = predict_main_wave(self.conn, candidate_codes, as_of_date, sector_score_map)
        if df_mw is None or df_mw.empty:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': hot_sectors,
                'candidate_pool_size': len(candidate_codes),
                'candidates': [],
                'gate': 'mw_no_data',
            }
        df_mw = df_mw.set_index('ts_code')

        # 6) 集成: V11 + Stage 3 投票
        candidates = []
        for code in candidate_codes:
            v11_p = v11_preds.get(code, {})
            v11_prob = float(v11_p.get('probability', 0.5))
            v11_pred_ret = float(v11_p.get('predicted_return', 0))
            mw_prob = float(df_mw.loc[code, 'main_wave_prob']) if code in df_mw.index else 0

            # 集成规则:
            # 双高: V11 >= 0.55 AND mw >= 0.1 → 强烈买入
            # 单高 V11: V11 >= 0.70 (无论 mw) → 买入
            # 单高 mw: V11 >= 0.5 AND mw >= 0.3 → 买入
            # 其他: 不入选
            buy_signal = False
            signal_strength = 0
            if v11_prob >= 0.55 and mw_prob >= 0.10:
                buy_signal = True
                signal_strength = 3  # 双高
            elif v11_prob >= 0.70:
                buy_signal = True
                signal_strength = 2  # V11 强
            elif v11_prob >= 0.50 and mw_prob >= 0.30:
                buy_signal = True
                signal_strength = 2  # MW 强
            elif v11_prob >= 0.60 and mw_prob >= 0.05:
                buy_signal = True
                signal_strength = 1  # 弱

            if not buy_signal:
                continue

            # 综合分 (0-100)
            # 70% V11 + 30% Stage 3 (V11 是生产模型, Stage 3 增强)
            ensemble = v11_prob * 70 + mw_prob * 30

            candidates.append({
                'ts_code': code,
                'v11_prob': v11_prob,
                'v11_pred_ret': v11_pred_ret,
                'main_wave_prob': mw_prob,
                'ensemble_score': ensemble,
                'signal_strength': signal_strength,
            })

        # 按 ensemble 排序
        candidates.sort(key=lambda x: -x['ensemble_score'])

        # 7) 加股票名 + 行业
        if candidates:
            top_codes = [c['ts_code'] for c in candidates[:50]]
            placeholders2 = ','.join(['%s'] * len(top_codes))
            cur.execute(f"SELECT ts_code, name, industry FROM stock_info WHERE ts_code IN ({placeholders2})", top_codes)
            info_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            for c in candidates:
                name, ind = info_map.get(c['ts_code'], ('', 'OTHER'))
                c['name'] = name
                c['industry'] = ind

        return {
            'as_of_date': as_of_date,
            'market': market,
            'hot_sectors': hot_sectors,
            'candidate_pool_size': len(candidate_codes),
            'candidates': candidates[:top_n * 2],  # 给多一点, 让外面再过滤
            'gate': 'ok',
        }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    p = V11EnhancedPredictor()
    for d in ['2026-05-15', '2026-05-29', '2026-06-05', '2026-06-09']:
        r = p.run_predict(d)
        print(f"\n=== {d} ===")
        print(f"  market: {r['market']['direction']} (p={r['market']['prob']:.2f})")
        print(f"  pool: {r['candidate_pool_size']}, gate: {r['gate']}")
        if r['candidates']:
            print("  Top 5:")
            for c in r['candidates'][:5]:
                sig = '★★' if c['signal_strength'] == 3 else '★' if c['signal_strength'] == 2 else '·'
                print(f"    {sig} {c['ts_code']} {c.get('name', ''):<8s} "
                      f"v11={c['v11_prob']:.2f} mw={c['main_wave_prob']:.2f} ens={c['ensemble_score']:.1f} ind={c.get('industry', '?')}")
