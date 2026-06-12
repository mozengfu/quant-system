"""
三段式 Top-Down 预测器 — 串联 Stage 1 / 2 / 3

调用:
    from quant_app.pipeline.topdown_predictor import TopDownPredictor
    p = TopDownPredictor()
    result = p.run_predict(as_of_date='2026-06-09')
    # result: {
    #   'market': {direction, prob, expected_return, ...},
    #   'hot_sectors': [{sector_name, score, ...}, ...],
    #   'candidates': [{ts_code, name, final_score, ...}, ...]
    # }
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


class TopDownPredictor:
    """三段式 Top-Down 预测器"""

    def __init__(self, conn=None):
        self.conn = conn
        if self.conn is None:
            self.conn = pymysql.connect(**get_db_config())

    def run_predict(self, as_of_date: str) -> dict:
        """
        主入口: 跑三段预测, 返回完整结果

        Args:
            as_of_date: 信号日 (T)
        Returns:
            {
              'as_of_date': str,
              'market': {direction, prob, expected_return, ...},
              'hot_sectors': [...],
              'candidate_pool_size': int,
              'candidates': [{ts_code, name, final_score, ml_score, main_wave_prob, sector}, ...]
            }
        """
        # 1) Stage 1: 大盘
        logger.info(f"Stage 1: market direction @ {as_of_date}")
        market = predict_market(self.conn, as_of_date)
        logger.info(f"  → {market['direction']} (prob={market['prob']}, er={market['expected_return']})")

        # 风险闸门: 大盘 strong_down → 直接返回空
        if market['direction'] == 'strong_down' and market['prob'] >= 0.6:
            logger.info("  Market strong_down with high prob → return empty")
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': [],
                'candidate_pool_size': 0,
                'candidates': [],
                'gate': 'strong_down',
            }

        # 2) Stage 2: 热点板块
        logger.info(f"Stage 2: hot sectors @ {as_of_date}")
        hot_sectors = predict_sectors(self.conn, as_of_date, top_n=8)
        logger.info(f"  → {len(hot_sectors)} sectors")
        if not hot_sectors:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': [],
                'candidate_pool_size': 0,
                'candidates': [],
                'gate': 'no_hot_sector',
            }
        sector_names = [s['sector_name'] for s in hot_sectors]
        sector_score_map = {s['sector_name']: s['score'] for s in hot_sectors}

        # 3) 候选池: 热点板块内的全 A 股
        logger.info(f"Stage 3: candidate pool from {len(sector_names)} sectors")
        candidate_codes = self._get_candidates_in_sectors(sector_names, as_of_date)
        logger.info(f"  → {len(candidate_codes)} candidates")
        if not candidate_codes:
            return {
                'as_of_date': as_of_date,
                'market': market,
                'hot_sectors': hot_sectors,
                'candidate_pool_size': 0,
                'candidates': [],
                'gate': 'no_candidates',
            }

        # 4) Stage 3: 主升浪分类器
        logger.info(f"Stage 3: main_wave scoring on {len(candidate_codes)} stocks")
        df_mw = predict_main_wave(self.conn, candidate_codes, as_of_date, sector_score_map)
        df_mw = df_mw.set_index('ts_code')

        # 5) 融合打分
        df_mw['main_wave_prob_pct'] = df_mw['main_wave_prob'] * 100
        if 'sector_score' not in df_mw.columns:
            df_mw['sector_score'] = 0
        else:
            df_mw['sector_score'] = df_mw['sector_score'].fillna(0)
        # final = 0.4 * main_wave_prob_pct + 0.4 * ml_v11 + 0.2 * sector_score
        # ml_v11 暂用 main_wave_prob 代理 (v11 模型加载简化)
        df_mw['ml_v11'] = df_mw['main_wave_prob_pct']  # 简化: 用 mw_prob 代替
        df_mw['final_score'] = (
            0.4 * df_mw['main_wave_prob_pct']
            + 0.4 * df_mw['ml_v11']
            + 0.2 * df_mw['sector_score']
        )

        # 6) 加股票名 + 行业
        df_mw = df_mw.reset_index()
        name_map = self._get_stock_names(df_mw['ts_code'].tolist())
        ind_map = self._get_industries(df_mw['ts_code'].tolist())
        df_mw['name'] = df_mw['ts_code'].map(name_map).fillna('')
        df_mw['industry'] = df_mw['ts_code'].map(ind_map).fillna('OTHER')

        # 7) 排序 + 取 Top N
        df_mw = df_mw.sort_values('final_score', ascending=False)
        # 分置信度
        def _conf(row):
            if row['final_score'] >= 80: return 'high'
            if row['final_score'] >= 60: return 'mid'
            return 'low'
        df_mw['confidence'] = df_mw.apply(_conf, axis=1)

        candidates = df_mw.head(20).to_dict('records')
        return {
            'as_of_date': as_of_date,
            'market': market,
            'hot_sectors': hot_sectors,
            'candidate_pool_size': len(df_mw),
            'candidates': candidates,
            'gate': 'ok',
        }

    def _get_candidates_in_sectors(self, sector_names: list[str], as_of_date: str) -> list[str]:
        """从热点板块中拉候选股票"""
        with self.conn.cursor() as c:
            placeholders = ','.join(['%s'] * len(sector_names))
            c.execute(f"""
                SELECT DISTINCT ts_code
                FROM stock_info
                WHERE industry IN ({placeholders})
                  AND list_date < %s
            """, (*sector_names, as_of_date))
            return [r[0] for r in c.fetchall()]

    def _get_stock_names(self, codes: list[str]) -> dict:
        with self.conn.cursor() as c:
            placeholders = ','.join(['%s'] * len(codes))
            c.execute(f"SELECT ts_code, name FROM stock_info WHERE ts_code IN ({placeholders})", codes)
            return {r[0]: r[1] for r in c.fetchall()}

    def _get_industries(self, codes: list[str]) -> dict:
        with self.conn.cursor() as c:
            placeholders = ','.join(['%s'] * len(codes))
            c.execute(f"SELECT ts_code, industry FROM stock_info WHERE ts_code IN ({placeholders})", codes)
            return {r[0]: r[1] for r in c.fetchall()}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    p = TopDownPredictor()
    result = p.run_predict('2026-06-03')
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
