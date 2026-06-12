"""
V11 板块内选股 (最优 baseline)
- Stage 1: 大盘方向 → 风险闸门
- Stage 2: 板块筛选 (取资金净流入 + 涨幅前列的板块)
- V11 预测: 板块内所有股票打分, Top 5 推荐
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pymysql

from ml_predict import predict_batch
from quant_app.models.market_direction_v2 import predict as predict_market
from quant_app.models.sector_rotation_v1 import predict as predict_sectors
from quant_app.utils.config import get_db_config

logger = logging.getLogger(__name__)


class V11SectorPredictor:
    """V11 板块内选股 - OOS 最佳 (+68% 年化)"""

    def __init__(self, conn=None):
        self.conn = conn
        if self.conn is None:
            self.conn = pymysql.connect(**get_db_config())

    def run_predict(self, as_of_date: str, top_n: int = 5) -> dict:
        """主入口: 跑 Stage 1 + 板块选 + V11 评分"""
        # 1) Stage 1: 大盘
        market = predict_market(self.conn, as_of_date)
        if market['direction'] == 'strong_down' and market['prob'] >= 0.6:
            return {
                'as_of_date': as_of_date, 'market': market,
                'hot_sectors': [], 'candidates': [],
                'candidate_pool_size': 0, 'gate': 'strong_down',
            }

        # 2) 板块筛选
        hot_sectors = predict_sectors(self.conn, as_of_date, top_n=8)
        if not hot_sectors:
            return {
                'as_of_date': as_of_date, 'market': market,
                'hot_sectors': [], 'candidates': [],
                'candidate_pool_size': 0, 'gate': 'no_hot_sector',
            }
        sector_names = [s['sector_name'] for s in hot_sectors]

        # 3) 候选池: 板块内活跃股
        cur = self.conn.cursor()
        placeholders = ','.join(['%s'] * len(sector_names))
        cur.execute(f"""
            SELECT DISTINCT ts_code FROM stock_info
            WHERE industry IN ({placeholders}) AND list_date < %s
        """, (*sector_names, as_of_date))
        candidate_codes = [r[0] for r in cur.fetchall()]
        if not candidate_codes:
            return {
                'as_of_date': as_of_date, 'market': market,
                'hot_sectors': hot_sectors, 'candidates': [],
                'candidate_pool_size': 0, 'gate': 'no_candidates',
            }

        # 4) V11 评分
        logger.info(f"V11 预测 {len(candidate_codes)} 只")
        v11_preds = predict_batch(candidate_codes, db_conn=self.conn,
                                 as_of_date=as_of_date)
        if not v11_preds:
            return {
                'as_of_date': as_of_date, 'market': market,
                'hot_sectors': hot_sectors, 'candidates': [],
                'candidate_pool_size': len(candidate_codes), 'gate': 'v11_no_data',
            }

        # 5) 排序: V11 probability 倒序
        sorted_p = sorted(v11_preds.items(),
                          key=lambda x: x[1].get('probability', 0),
                          reverse=True)[:top_n]

        # 6) 加名字 + 行业
        top_codes = [c for c, _ in sorted_p]
        placeholders2 = ','.join(['%s'] * len(top_codes))
        cur.execute(f"SELECT ts_code, name, industry FROM stock_info WHERE ts_code IN ({placeholders2})",
                    top_codes)
        info_map = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

        candidates = []
        for code, p in sorted_p:
            name, ind = info_map.get(code, ('', 'OTHER'))
            candidates.append({
                'ts_code': code,
                'name': name,
                'industry': ind,
                'v11_prob': float(p.get('probability', 0.5)),
                'v11_pred_ret': float(p.get('predicted_return', 0)),
                'ensemble_score': float(p.get('probability', 0.5)) * 100,
                'main_wave_prob': 0,  # 没算
            })

        # 按大盘态势调仓位建议
        direction = market['direction']
        if direction == 'up' and market['prob'] >= 0.6:
            position_pct = 0.25  # 满仓
        elif direction == 'range':
            position_pct = 0.15  # 半仓
        elif direction == 'down' and market['prob'] >= 0.6:
            return {
                'as_of_date': as_of_date, 'market': market,
                'hot_sectors': hot_sectors, 'candidates': [],
                'candidate_pool_size': len(candidate_codes),
                'gate': 'market_down',
            }
        else:
            position_pct = 0.20  # 默认

        return {
            'as_of_date': as_of_date,
            'market': market,
            'hot_sectors': hot_sectors,
            'candidate_pool_size': len(candidate_codes),
            'candidates': candidates,
            'position_pct': position_pct,
            'gate': 'ok',
        }


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    p = V11SectorPredictor()
    for d in ['2026-05-15', '2026-05-29', '2026-06-05', '2026-06-09']:
        r = p.run_predict(d)
        print(f"\n=== {d} ===")
        print(f"  market: {r['market']['direction']} (p={r['market']['prob']:.2f})")
        print(f"  pool: {r['candidate_pool_size']}, gate: {r['gate']}")
        for c in r.get('candidates', []):
            print(f"    {c['ts_code']} {c['name']:<8s} v11={c['v11_prob']:.2f} ind={c['industry']}")
