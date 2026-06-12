"""
TopDown V1 — 三层集成管线

predict_topdown(as_of_date):
  1. Layer1.predict() → regime, position_multiplier
  2. Layer2.predict() → sector_heat ranking → top5 hot sectors
  3. 候选池 = Top300成交额 ∩ Top5热点板块成分股
  4. Layer3.predict() → wave_prob (带 market_features + sector_features)
  5. 综合评分: 0.3×market + 0.3×sector + 0.4×wave
  6. 返回 TopN 推荐
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logger = logging.getLogger(__name__)


def predict_topdown(conn, as_of_date: str, top_n: int = 5,
                    market_model=None, sector_model=None, wave_model=None):
    """三层自上而下预测

    Args:
        conn: pymysql 连接
        as_of_date: 预测日期 'YYYY-MM-DD'
        top_n: 返回推荐数量
        market_model: 预加载的市场模型 bundle (None=自动加载)
        sector_model: 预加载的板块模型 bundle
        wave_model: 预加载的个股模型 bundle

    Returns:
        dict with:
          - recommendations: list of {ts_code, wave_prob, sector, final_score, ...}
          - market_regime: Layer1 输出
          - hot_sectors: Layer2 输出
          - position_multiplier: 仓位系数
    """
    from quant_app.models.market_direction_v3 import predict as predict_market
    from quant_app.models.sector_heat_v1 import predict as predict_sector
    from quant_app.models.wave_catcher_v1 import _load_board_mapping, _load_turnover_top_n
    from quant_app.models.wave_catcher_v1 import predict as predict_wave

    # ── Layer 1: 大盘气象 ──
    logger.info(f"[Layer1] Predicting market regime for {as_of_date}...")
    market_result = predict_market(conn, as_of_date)
    logger.info(f"  Direction: {market_result['direction']}, "
                f"Bull prob: {market_result.get('probs', {}).get('bull', 0):.2f}, "
                f"Position multiplier: {market_result['position_multiplier']:.2f}")

    # ── Layer 2: 热点板块 ──
    logger.info(f"[Layer2] Predicting sector heat for {as_of_date}...")
    sector_result = predict_sector(conn, as_of_date)
    top_sectors = sector_result.get('top_sectors', [])
    logger.info(f"  Top5 sectors: {top_sectors}")

    # ── 候选池构建 ──
    logger.info("[Pipeline] Building candidate pool...")
    # Top300 成交额
    top300 = _load_turnover_top_n(conn, as_of_date, 300)

    # 获取板块成分股
    board_map = _load_board_mapping(conn)
    # 反向映射: sector_name → [ts_codes]
    sector_stocks = {}
    for code, board in board_map.items():
        if board not in sector_stocks:
            sector_stocks[board] = []
        sector_stocks[board].append(code)

    # 候选池: Top300 ∩ Top20热点板块成分股 (弱Layer2时宽范围)
    all_sectors = sector_result.get('sectors', [])
    top_n_sectors = [s['sector_name'] for s in all_sectors[:20]] if all_sectors else top_sectors[:20] if len(top_sectors) > 20 else top_sectors

    if top_n_sectors:
        hot_stocks = set()
        for sn in top_n_sectors:
            hot_stocks.update(sector_stocks.get(sn, []))
        candidates = [c for c in top300 if c in hot_stocks]
    else:
        candidates = top300

    # 候选太少则放宽为Top300全部
    if len(candidates) < 30:
        logger.info(f"  Only {len(candidates)} in hot sectors, expanding to full Top300")
        candidates = top300

    logger.info(f"  Candidates: {len(candidates)} (Top300 ∩ HotSectors)")

    # ── Layer 3: 主升浪捕手 ──
    logger.info(f"[Layer3] Predicting wave probabilities for {len(candidates)} stocks...")
    wave_result = predict_wave(
        conn, candidates, as_of_date,
        market_features=market_result,
        sector_features=sector_result
    )

    # ── 综合评分 ──
    logger.info("[Pipeline] Computing final scores...")
    scored = []
    for code in candidates:
        w = wave_result.get(code, {'wave_prob': 0.5, 'is_main_wave': False})

        # Layer1 信号: bull=1, range=0.5, bear=0 (统一为仓位系数)
        market_signal = market_result['position_multiplier']

        # Layer2 信号: 股票所属板块的热度
        stock_board = board_map.get(code, '')
        sector_signal = 0.5  # 默认
        for s in sector_result.get('sectors', []):
            if s.get('sector_name') == stock_board:
                sector_signal = s.get('heat_score', 50) / 100
                break

        # 综合评分
        final_score = (
            0.2 * market_signal +
            0.3 * sector_signal +
            0.5 * w['wave_prob']
        )

        scored.append({
            'ts_code': code,
            'sector': stock_board,
            'wave_prob': w['wave_prob'],
            'market_signal': round(market_signal, 3),
            'sector_signal': round(sector_signal, 3),
            'final_score': round(final_score, 4),
            'is_main_wave': w['is_main_wave'],
        })

    scored.sort(key=lambda x: x['final_score'], reverse=True)
    recommendations = scored[:top_n]

    return {
        'recommendations': recommendations,
        'market_regime': {
            'direction': market_result['direction'],
            'position_multiplier': market_result['position_multiplier'],
            'probs': market_result.get('probs', {}),
        },
        'hot_sectors': {
            'top5': top_sectors,
            'all': sector_result.get('sectors', [])[:10],
        },
        'candidate_count': len(candidates),
        'as_of_date': as_of_date,
    }


def predict_topdown_simple(conn, as_of_date: str, top_n: int = 5) -> list:
    """简化版: 仅返回推荐股票列表"""
    result = predict_topdown(conn, as_of_date, top_n)
    return [r['ts_code'] for r in result['recommendations']]


if __name__ == '__main__':
    import pymysql

    from quant_app.utils.config import get_db_config

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    conn = pymysql.connect(**get_db_config())

    print("=" * 60)
    print("TopDown V1 — Three-Layer Prediction")
    print("=" * 60)

    for d in ['2026-05-15', '2026-06-05', '2026-06-09']:
        result = predict_topdown(conn, d, top_n=5)
        print(f"\n── {d} ──")
        print(f"  Market: {result['market_regime']['direction']} "
              f"(pos_mul={result['market_regime']['position_multiplier']:.2f})")
        print(f"  Hot Sectors: {result['hot_sectors']['top5']}")
        print(f"  Candidates: {result['candidate_count']}")
        print("  Recommendations:")
        for i, r in enumerate(result['recommendations']):
            print(f"    {i+1}. {r['ts_code']} ({r['sector']}) "
                  f"score={r['final_score']:.3f} wave={r['wave_prob']:.3f}")

    conn.close()
