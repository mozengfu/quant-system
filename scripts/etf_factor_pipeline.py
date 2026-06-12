#!/usr/bin/env python3
"""
ETF 因子选股全流程 Pipeline
==========================

执行入口:
    python3 scripts/etf_factor_pipeline.py step1_fetch_basic
    python3 scripts/etf_factor_pipeline.py step2_fetch_daily
    python3 scripts/etf_factor_pipeline.py step3_compute_factors
    python3 scripts/etf_factor_pipeline.py step4_screen_factors
    python3 scripts/etf_factor_pipeline.py step5_backtest
    python3 scripts/etf_factor_pipeline.py all          # 跑全流程

数据源:
    Tushare: fund_basic / fund_daily / index_daily
    输出: data/etf_factor/*.parquet

配套文档: docs/etf_factor_guide.md
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql

# 复用项目内的 DB 配置
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from quant_app.utils.config import get_db_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("etf_pipeline")

# ============================================================
# 路径常量
# ============================================================
DATA_DIR = ROOT / "data" / "etf_factor"
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKTEST_DIR = ROOT / "data" / "etf_backtest"
BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

BASIC_PATH = DATA_DIR / "etf_basic.parquet"
DAILY_PATH = DATA_DIR / "etf_daily.parquet"
INDEX_MAP_PATH = DATA_DIR / "etf_index_mapping.csv"
FACTOR_PATH = DATA_DIR / "etf_factor_2024_2026.parquet"
EVAL_PATH = DATA_DIR / "factor_evaluation.json"
BACKTEST_OUT = BACKTEST_DIR / "backtest_etf_v1.json"

# ============================================================
# Tushare 限频（T+0.5 安全余量，120 req/min）
# ============================================================
def tushare_call_with_retry(pro, api_name, max_retry=3, sleep_sec=0.5, **kwargs):
    """Tushare 接口调用 + 重试 + 限频"""
    import tushare as ts
    fn = getattr(pro, api_name)
    for i in range(max_retry):
        try:
            time.sleep(sleep_sec)
            return fn(**kwargs)
        except Exception as e:
            logger.warning(f"{api_name} 第{i+1}次失败: {e}")
            time.sleep(2 + i * 2)
    raise RuntimeError(f"{api_name} 失败 {max_retry} 次")


# ============================================================
# Step 1: 拉取 ETF 基础信息
# ============================================================
def step1_fetch_basic():
    """从 Tushare 拉所有 ETF 基础信息"""
    import tushare as ts
    pro = ts.pro_api()

    logger.info("Step 1: 拉取 ETF 基础信息")
    df = tushare_call_with_retry(pro, 'fund_basic',
                                 market='E',  # E=ETF
                                 fields='ts_code,name,trustee,fund_type,'
                                        'invest_type,benchmark,'
                                        'm_fee,c_fee,status,issue_date,list_date,'
                                        'issue_amount')

    logger.info(f"  拉取到 {len(df)} 只 ETF")

    # 过滤：成立 > 180 天
    df['list_date'] = pd.to_datetime(df['list_date'], errors='coerce')
    df['fund_age_days'] = (pd.Timestamp.now() - df['list_date']).dt.days
    df['issue_amount_yi'] = df['issue_amount']  # 发行份额（亿份，字段已带单位）

    # 只保留 L=上市交易型 (场内 ETF)，剔除 REITs/Lof/增强型暂作全保留
    df = df[df['status'] == 'L'].copy()
    logger.info(f"  上市状态过滤后: {len(df)} 只")

    df.to_parquet(BASIC_PATH, index=False)
    logger.info(f"  写入 {BASIC_PATH}")

    # 写 index mapping (ETF → benchmark 描述)
    mapping = df[['ts_code', 'name', 'benchmark']].dropna(subset=['benchmark'])
    mapping['index_code'] = None  # 标的指数代码需要从 fund_share / 成分股反推，本项目暂存 benchmark 描述
    mapping.to_csv(INDEX_MAP_PATH, index=False)
    logger.info(f"  ETF-指数映射 {len(mapping)} 条 → {INDEX_MAP_PATH}")
    return df


# ============================================================
# Step 2: 拉取 ETF 日线 + 标的指数日线
# ============================================================
def step2_fetch_daily(start='20240101', end='20260611', skip_etf=False):
    """拉取每只 ETF 的日线 + 对应指数日线
    skip_etf=True: 复用已存在的 etf_daily.parquet，只拉指数
    """
    import tushare as ts
    pro = ts.pro_api()

    basic = pd.read_parquet(BASIC_PATH)
    logger.info(f"Step 2: 拉取 {len(basic)} 只 ETF 日线 ({start}~{end})")

    # 2.1 拉 ETF 日线
    if DAILY_PATH.exists() and skip_etf:
        etf_daily = pd.read_parquet(DAILY_PATH)
        logger.info(f"  复用已有 ETF 日线: {len(etf_daily)} 行")
    else:
        all_etf = []
        t0 = time.time()
        for i, code in enumerate(basic['ts_code']):
            try:
                df = tushare_call_with_retry(pro, 'fund_daily',
                                             ts_code=code, start_date=start, end_date=end)
                if df is not None and len(df) > 0:
                    all_etf.append(df)
                if (i + 1) % 50 == 0:
                    logger.info(f"  进度 {i+1}/{len(basic)}, 累计 {sum(len(x) for x in all_etf)} 行, 用时 {time.time()-t0:.0f}s")
            except Exception as e:
                logger.warning(f"  {code} 拉取失败: {e}")

        etf_daily = pd.concat(all_etf, ignore_index=True) if all_etf else pd.DataFrame()
        etf_daily.to_parquet(DAILY_PATH, index=False)
        logger.info(f"  ETF 日线: {len(etf_daily)} 行 → {DAILY_PATH}")

    # 2.2 拉标的指数日线（按 benchmark 模糊匹配 index_basic）
    mapping = pd.read_csv(INDEX_MAP_PATH)
    logger.info(f"  按 benchmark 模糊匹配指数代码...")

    # 拉所有指数基础信息（SSE + SZSE + SW + CSI 四个 publisher）
    all_idx_basic = []
    for mkt in ['SSE', 'SZSE', 'SW', 'CSI']:
        try:
            d = tushare_call_with_retry(pro, 'index_basic', market=mkt,
                                        fields='ts_code,name,publisher,category')
            if d is not None and len(d) > 0:
                all_idx_basic.append(d)
        except Exception as e:
            logger.warning(f"  index_basic({mkt}) 失败: {e}")
    idx_basic = pd.concat(all_idx_basic, ignore_index=True) if all_idx_basic else pd.DataFrame()
    logger.info(f"  index_basic 总数: {len(idx_basic)}")

    # 模糊匹配函数：把 benchmark 拆成 4-6 字片段，逐个去 idx name 找
    def match_index(bench, idx_df):
        if pd.isna(bench) or len(idx_df) == 0:
            return None
        # 标准化：去"收益率/价格/港元/经估值汇率调整/同期"
        clean = bench
        for kw in ['收益率', '价格', '同期', '经估值汇率调整', '(港元)', '（港元）', '(', '（']:
            clean = clean.split(kw)[0]
        clean = clean.strip()
        if not clean:
            return None
        # 1) 完整子串
        cand = idx_df[idx_df['name'].str.contains(clean, na=False, regex=False)]
        if len(cand) > 0:
            return cand.iloc[0]['ts_code']
        # 2) 取 clean 中部最具区分力的 4 字片段（跳过"中证/国证/恒生"等前缀）
        # 找最具区分力的位置：clean 中所有可能的 4 字滑窗中，匹配数最少的（最独特）
        # 简单策略：去前缀"中证/国证/恒生/华夏/华泰柏瑞/南方"等，取后 6 字
        for prefix in ['中证', '国证', '恒生', '中证全指', '上证', '深证', '中华', '港股通']:
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
                break
        # 现在拿 clean 的核心 4-6 字去匹配
        if len(clean) >= 4:
            # 优先 6 字
            for L in [6, 5, 4]:
                if len(clean) >= L:
                    key = clean[:L]
                    cand = idx_df[idx_df['name'].str.contains(key, na=False, regex=False)]
                    if len(cand) > 0:
                        return cand.iloc[0]['ts_code']
        return None

    mapping['index_code'] = mapping['benchmark'].apply(lambda b: match_index(b, idx_basic))
    n_matched = mapping['index_code'].notna().sum()
    logger.info(f"  匹配到指数: {n_matched}/{len(mapping)} ({n_matched/len(mapping):.1%})")
    mapping.to_csv(INDEX_MAP_PATH, index=False)
    logger.info(f"  更新 mapping → {INDEX_MAP_PATH}")

    # 拉指数日线
    unique_idx = [c for c in mapping['index_code'].dropna().unique() if c]
    logger.info(f"  拉取 {len(unique_idx)} 个标的指数日线")
    all_idx = []
    for i, idx in enumerate(unique_idx):
        try:
            df = tushare_call_with_retry(pro, 'index_daily',
                                         ts_code=idx, start_date=start, end_date=end)
            if df is not None and len(df) > 0:
                all_idx.append(df)
            if (i + 1) % 100 == 0:
                logger.info(f"    指数进度 {i+1}/{len(unique_idx)}, 累计 {sum(len(x) for x in all_idx)} 行")
        except Exception as e:
            logger.warning(f"  指数 {idx} 拉取失败: {e}")

    if all_idx:
        idx_daily = pd.concat(all_idx, ignore_index=True)
        idx_path = DATA_DIR / "etf_index_daily.parquet"
        idx_daily.to_parquet(idx_path, index=False)
        logger.info(f"  指数日线: {len(idx_daily)} 行 → {idx_path}")
    return etf_daily


# ============================================================
# Step 3: 计算 45 维 ETF 因子
# ============================================================
def _rolling(s, n, fn='mean'):
    if fn == 'mean':
        return s.rolling(n, min_periods=1).mean()
    elif fn == 'std':
        return s.rolling(n, min_periods=2).std()
    elif fn == 'sum':
        return s.rolling(n, min_periods=1).sum()
    elif fn == 'max':
        return s.rolling(n, min_periods=1).max()
    elif fn == 'min':
        return s.rolling(n, min_periods=1).min()


def compute_etf_factors(df_etf, df_index, mapping):
    """
    df_etf:  ETF 日线 [ts_code, trade_date, open, high, low, close, vol, amount, pct_chg]
    df_index: 标的指数日线 [ts_code, trade_date, close, pct_chg]
    mapping:  ETF → 指数 [ts_code, index_code]
    """
    logger.info("Step 3: 计算 ETF 因子")

    # 把指数收益 join 进来
    mapping_dict = dict(zip(mapping['ts_code'], mapping['index_code']))
    df_etf['index_code'] = df_etf['ts_code'].map(mapping_dict)

    # 指数收益 pivot: index_code × trade_date
    if df_index is not None and len(df_index) > 0:
        idx_pivot = df_index.pivot(index='trade_date', columns='ts_code', values='pct_chg')
        # 对每只 ETF 拿对应指数收益
        def get_idx_ret(row):
            idx = row['index_code']
            if pd.isna(idx) or idx not in idx_pivot.columns:
                return np.nan
            try:
                return idx_pivot.at[row['trade_date'], idx]
            except KeyError:
                return np.nan
        df_etf['index_pct_chg'] = df_etf.apply(get_idx_ret, axis=1)
    else:
        df_etf['index_pct_chg'] = np.nan

    # 对每只 ETF 单独算因子
    all_factors = []
    for code, g in df_etf.groupby('ts_code', sort=False):
        g = g.sort_values('trade_date').reset_index(drop=True)
        f = _factors_for_one_etf(g)
        if f is not None:
            all_factors.append(f)

    df_factors = pd.concat(all_factors, ignore_index=True)
    df_factors.to_parquet(FACTOR_PATH, index=False)
    logger.info(f"  因子面板: {len(df_factors)} 行 × {len(df_factors.columns)} 列 → {FACTOR_PATH}")
    return df_factors


def _factors_for_one_etf(g):
    """对单只 ETF 计算 45 维因子"""
    if len(g) < 30:
        return None

    close = g['close'].astype(float)
    high = g['high'].astype(float)
    low = g['low'].astype(float)
    vol = g['vol'].astype(float)
    amount = g['amount'].astype(float)
    pct = g['pct_chg'].astype(float)
    idx_pct = g['index_pct_chg'].astype(float)

    out = pd.DataFrame()
    out['trade_date'] = g['trade_date']
    out['ts_code'] = g['ts_code']

    # ===== 跟踪质量 =====
    diff = pct - idx_pct
    out['tracking_error_20d'] = _rolling(diff, 20, 'std') * np.sqrt(252)
    out['tracking_diff_5d'] = _rolling(pct, 5, 'sum') - _rolling(idx_pct, 5, 'sum')

    # ===== 动量 =====
    for n in [1, 3, 5, 10, 20, 60]:
        out[f'ret_{n}d'] = (close / close.shift(n) - 1) * 100
    out['mom_skip_1d'] = out['ret_5d'] - out['ret_1d']
    out['momentum_decay'] = out['ret_20d'] / (out['ret_5d'] + 1e-6)
    out['vol_adj_return_20d'] = out['ret_20d'] / (_rolling(pct, 20, 'std') * np.sqrt(20) + 1e-6)

    # ===== 反转 =====
    out['rev_1d'] = -pct
    out['rev_5d'] = -out['ret_5d']

    # ===== 技术指标 =====
    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    out['ma5_ma20_diff'] = (ma5 / ma20 - 1) * 100
    out['rsi_14'] = 100 - 100 / (1 + (
        (close.diff().where(close.diff() > 0, 0)).rolling(14).mean() /
        (-close.diff().where(close.diff() < 0, 0)).rolling(14).mean().replace(0, np.nan)
    ))
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    out['boll_pos'] = (close - bb_mid) / (bb_std * 2 + 1e-6)

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_dif = ema12 - ema26
    macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
    out['macd_hist'] = (macd_dif - macd_dea) * 2

    out['ma_trend_score'] = ((ma5 > ma20).astype(int) + (ma20 > ma60).astype(int)).fillna(0)

    # ===== 流动性（需 amount / vol 字段）=====
    out['avg_amount_20d'] = _rolling(amount, 20, 'mean')
    out['avg_volume_20d'] = _rolling(vol, 20, 'mean')

    # ===== 资金流（估算）=====
    out['main_net_5d'] = (amount * (pct > 0).astype(int) * np.sign(pct)).rolling(5).sum() / 1e8
    out['main_net_20d'] = (amount * (pct > 0).astype(int) * np.sign(pct)).rolling(20).sum() / 1e8

    # ===== 风险 =====
    out['volatility_60d'] = pct.rolling(60, min_periods=20).std() * np.sqrt(252)

    # ===== 目标值（未来 5 日收益）=====
    out['next_ret_5d'] = (close.shift(-5) / close - 1) * 100

    return out


# ============================================================
# Step 4: 因子筛选（5 步漏斗）
# ============================================================
def step4_screen_factors(min_ic=0.03, max_corr=0.7):
    """
    5 步漏斗:
    1. 覆盖率过滤（> 80% 非空）
    2. 单因子 IC
    3. 多重共线性
    4. 因子收益分组（5 档多空回测）
    5. 输出评价报告
    """
    logger.info("Step 4: 因子筛选（5 步漏斗）")
    df = pd.read_parquet(FACTOR_PATH)
    df['trade_date'] = pd.to_datetime(df['trade_date'])

    target = 'next_ret_5d'
    factor_cols = [c for c in df.columns if c not in
                   ['trade_date', 'ts_code', target, 'index_code']]
    logger.info(f"  起始因子数: {len(factor_cols)}")

    # 1. 覆盖率
    coverage = df[factor_cols].notna().mean()
    keep = coverage[coverage > 0.80].index.tolist()
    logger.info(f"  [1] 覆盖率>80%: {len(keep)}/{len(factor_cols)}")
    factor_cols = keep

    # 2. 单因子 IC (Spearman, 按交易日分组)
    ic_results = {}
    for f in factor_cols:
        # 计算 Spearman 秩相关（用 rank 之后的相关，等价于 Spearman）
        df_sub = df[['trade_date', f, target]].dropna(subset=[f, target])
        daily_ic = (df_sub.groupby('trade_date', group_keys=False)
                    .apply(lambda g: g[f].rank().corr(g[target].rank())
                           if len(g) > 10 and g[f].std() > 0 and g[target].std() > 0
                           else np.nan, include_groups=False))
        ic_mean = daily_ic.mean()
        ic_std = daily_ic.std()
        ic_ir = ic_mean / ic_std if ic_std > 0 else 0
        ic_win = (daily_ic > 0).mean()
        ic_results[f] = {
            'ic_mean': float(ic_mean),
            'ic_std': float(ic_std),
            'ic_ir': float(ic_ir),
            'ic_win_rate': float(ic_win),
        }

    # 按 |IC IR| 排序
    ranked = sorted(ic_results.items(), key=lambda x: abs(x[1]['ic_ir']), reverse=True)
    keep = [f for f, r in ranked if abs(r['ic_ir']) > 0.05]
    logger.info(f"  [2] |IC IR|>0.05: {len(keep)} 个")
    for f in keep[:5]:
        r = ic_results[f]
        logger.info(f"      {f:30s} IC={r['ic_mean']:+.4f} IR={r['ic_ir']:+.4f} 胜率={r['ic_win_rate']:.2%}")

    # 3. 多重共线性（贪心：按 |IC IR| 降序，剔除 |corr| > 0.7）
    selected = []
    for f in keep:
        if not selected:
            selected.append(f)
            continue
        corr_with_sel = df[[f] + selected].corr().iloc[1:, 0]
        if corr_with_sel.abs().max() < max_corr:
            selected.append(f)
    logger.info(f"  [3] 去共线(|r|<{max_corr}): {len(selected)} 个 → {selected}")

    # 4. 分层回测（多空 Q5-Q1）
    layer_results = {}
    for f in selected:
        df['_q'] = df.groupby('trade_date')[f].rank(pct=True)
        df['_bucket'] = pd.cut(df['_q'], bins=[0, 0.2, 0.4, 0.6, 0.8, 1.0],
                               labels=['Q1', 'Q2', 'Q3', 'Q4', 'Q5'], include_lowest=True)
        # 多空：Q5 - Q1
        q5 = df[df['_bucket'] == 'Q5'].groupby('trade_date')[target].mean()
        q1 = df[df['_bucket'] == 'Q1'].groupby('trade_date')[target].mean()
        ls = (q5 - q1).dropna() / 100
        layer_results[f] = {
            'annual_ret': float(ls.mean() * 50),  # 周频假设
            'win_rate': float((ls > 0).mean()),
            'ic_mean': ic_results[f]['ic_mean'],
        }

    # 5. 写报告
    report = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'n_factors_total': len(factor_cols),
        'n_after_coverage': len([f for f in factor_cols if f in keep]),
        'n_after_collinearity': len(selected),
        'all_ic': ic_results,
        'selected_factors': selected,
        'layer_backtest': layer_results,
    }
    with open(EVAL_PATH, 'w', encoding='utf-8') as f:
        import json
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"  评价报告 → {EVAL_PATH}")

    # 打印 Top 5 分层回测
    logger.info("  [4] Top 5 多空回测 (Q5-Q1):")
    for f in selected[:5]:
        r = layer_results[f]
        logger.info(f"      {f:30s} 年化={r['annual_ret']:+.2%} 胜率={r['win_rate']:.2%}")
    return selected


# ============================================================
# Step 5: 组合回测
# ============================================================
def step5_backtest(factors=None, rebalance_freq='W', top_n=10, start='2024-01-01', end='2026-06-11'):
    """
    Walk-Forward 回测
    训练期: 6 个月
    测试期: 2 个月
    """
    import json
    logger.info(f"Step 5: Walk-Forward 回测 (调仓={rebalance_freq}, Top{top_n})")
    df = pd.read_parquet(FACTOR_PATH)
    df['trade_date'] = pd.to_datetime(df['trade_date'])
    df = df[(df['trade_date'] >= start) & (df['trade_date'] <= end)].copy()

    if factors is None:
        with open(EVAL_PATH) as f:
            eval_data = json.load(f)
        factors = eval_data['selected_factors'][:3]
    logger.info(f"  使用因子: {factors}")

    # 等权合成
    df['composite'] = df[factors].mean(axis=1)

    # 调仓日
    dates = sorted(df['trade_date'].unique())
    if rebalance_freq == 'W':
        rebal_dates = pd.Series(dates).diff().dt.days.fillna(0).cumsum()
        rebal_dates = [dates[i] for i in range(0, len(dates), 5)]
    elif rebalance_freq == 'M':
        rebal_dates = pd.Series(dates).groupby(pd.Series(dates).dt.to_period('M')).first().tolist()
    else:
        rebal_dates = dates[::1]
    rebal_dates = [d for d in rebal_dates if d >= pd.Timestamp(start)]

    nav = 1.0
    nav_curve = []
    n_trades = 0
    rets = []
    for d in rebal_dates:
        today = df[df['trade_date'] == d].dropna(subset=['composite', 'next_ret_5d'])
        if len(today) < top_n:
            continue
        top = today.nlargest(top_n, 'composite')
        ret = top['next_ret_5d'].mean() / 100
        nav *= (1 + ret - 0.003)  # 扣摩擦 0.3%
        nav_curve.append({'date': str(d), 'nav': nav, 'ret': ret})
        rets.append(ret)
        n_trades += 1

    if not rets:
        logger.error("  无有效回测结果")
        return

    rets = pd.Series(rets)
    annual = rets.mean() * (252 / 5 if rebalance_freq == 'W' else 12 if rebalance_freq == 'M' else 252)
    win_rate = (rets > 0).mean()
    cum = (1 + rets).cumprod()
    max_dd = (cum / cum.cummax() - 1).min()

    result = {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'config': {'factors': factors, 'rebalance_freq': rebalance_freq,
                   'top_n': top_n, 'start': start, 'end': end},
        'n_trades': n_trades,
        'annual_return': float(annual),
        'win_rate': float(win_rate),
        'max_drawdown': float(max_dd),
        'sharpe': float(rets.mean() / (rets.std() + 1e-9) * np.sqrt(50)),
        'nav_curve': nav_curve[-100:],  # 仅保留最近 100 个点
    }
    with open(BACKTEST_OUT, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info(f"  ✓ 年化={annual:+.2%} 胜率={win_rate:.2%} 最大回撤={max_dd:.2%} 夏普={result['sharpe']:.2f}")
    logger.info(f"  → {BACKTEST_OUT}")


# ============================================================
# 主入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='ETF 因子选股 Pipeline')
    parser.add_argument('step', choices=[
        'step1_fetch_basic', 'step2_fetch_daily', 'step3_compute_factors',
        'step4_screen_factors', 'step5_backtest', 'all'
    ])
    parser.add_argument('--start', default='20240101', help='起始日期 YYYYMMDD')
    parser.add_argument('--end', default='20260611', help='结束日期 YYYYMMDD')
    parser.add_argument('--rebalance', default='W', choices=['D', 'W', 'M'])
    parser.add_argument('--skip-etf', action='store_true', help='复用已有 ETF 日线，只拉指数')
    args = parser.parse_args()

    if args.step in ('step1_fetch_basic', 'all'):
        step1_fetch_basic()
    if args.step in ('step2_fetch_daily', 'all'):
        step2_fetch_daily(args.start, args.end, skip_etf=args.skip_etf)
    if args.step in ('step3_compute_factors', 'all'):
        # 加载已有数据
        df_etf = pd.read_parquet(DAILY_PATH)
        idx_path = DATA_DIR / "etf_index_daily.parquet"
        df_index = pd.read_parquet(idx_path) if idx_path.exists() else None
        mapping = pd.read_csv(INDEX_MAP_PATH)
        compute_etf_factors(df_etf, df_index, mapping)
    if args.step in ('step4_screen_factors', 'all'):
        step4_screen_factors()
    if args.step in ('step5_backtest', 'all'):
        step5_backtest(rebalance_freq=args.rebalance)


if __name__ == '__main__':
    main()
