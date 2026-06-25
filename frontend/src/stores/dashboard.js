import { defineStore } from 'pinia'
import api from '../api/index'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    // 管线状态
    market: { state: '加载中', position_ratio: 80, index: null, change_pct: null },
    ml: { status: 'normal', model: 'v11.0', feature_count: 131, last_predict: null },
    system: { heartbeat: null, scanTime: null, qmtConnected: false },
    performance: { sharpe: null, win_rate: null, max_drawdown: null, total_return: null },

    // 账户
    balance: { total_asset: 0, available: 0, market_value: 0 },
    positions: [],
    trades: [],

    // ML 推荐 & 扫描信号
    mlCandidates: [],
    scannerSignals: [],

    // 绩效曲线
    navHistory: { dates: [], values: [] },
    performanceSummary: null,

    loading: false,
    error: null,
    lastUpdated: null,
  }),

  getters: {
    positionCount: (state) => state.positions.length,
    totalPnl: (state) => state.positions.reduce((s, p) => s + (p.pnl || 0), 0),
  },

  actions: {
    async refreshAll() {
      this.loading = true
      this.error = null
      try {
        const [statusRes, positionRes, balanceRes, tradesRes, recommendRes, scannerRes, navRes] = await Promise.allSettled([
          api.get('/pipeline/status'),
          api.get('/trading/positions'),
          api.get('/trading/balance'),
          api.get('/trading/trades', { params: { limit: 20 } }),
          api.get('/recommend/v11', { params: { top_n: 5 } }),
          api.get('/scanner/signals'),
          api.get('/live/performance_summary'),
        ])

        // ---- /api/pipeline/status ----
        if (statusRes.status === 'fulfilled') {
          const s = statusRes.value || {}
          this.market = s.market || this.market
          this.ml = s.ml || this.ml
          const trading = s.trading || {}
          this.system.qmtConnected = trading.executor === 'QMT'
          if (s.performance) {
            this.performance = {
              sharpe: s.performance.sharpe ?? null,
              win_rate: s.performance.win_rate ?? null,
              max_drawdown: s.performance.max_drawdown ?? null,
              total_return: s.performance.total_return ?? null,
            }
          }
          if (this.ml.last_predict) {
            this.system.scanTime = this.ml.last_predict
          }
        }

        // ---- /api/trading/balance ----
        if (balanceRes.status === 'fulfilled') {
          const b = balanceRes.value || {}
          this.balance = {
            total_asset: b.total_asset ?? 0,
            available: b.available ?? 0,
            market_value: b.market_value ?? 0,
          }
        }

        // ---- /api/trading/positions ----
        if (positionRes.status === 'fulfilled') {
          const r = positionRes.value || {}
          this.positions = (r.positions || []).map(p => ({
            ts_code: p.ts_code || '',
            name: p.name || '',
            quantity: p.quantity || 0,
            cost_price: p.cost_price || 0,
            current_price: p.current_price || 0,
            market_value: p.market_value || 0,
            pnl: p.pnl || 0,
            pnl_pct: p.pnl_pct || 0,
          }))
        }

        // ---- /api/trading/trades ----
        if (tradesRes.status === 'fulfilled') {
          const r = tradesRes.value || {}
          this.trades = (r.trades || []).slice(0, 20)
        }

        // ---- /api/recommend/v11 ----
        if (recommendRes.status === 'fulfilled') {
          const r = recommendRes.value
          if (r && r.推荐股票) {
            this.mlCandidates = r.推荐股票.map(c => ({
              code: c.代码 || '',
              name: c.名称 || '',
              industry: c.行业 || '',
              price: c.现价 || 0,
              pct_chg: c.涨跌幅 || '0.00%',
              ml_score: c.ML得分 || 0,
              strategy: c.策略来源 || '',
              stop_loss: c.止损价 || 0,
            }))
          }
        }

        // ---- /api/scanner/signals ----
        if (scannerRes.status === 'fulfilled') {
          const r = scannerRes.value
          this.scannerSignals = (r.signals || []).slice(0, 15)
        }

        // ---- /api/live/performance_summary ----
        if (navRes.status === 'fulfilled') {
          const p = navRes.value
          if (p && !p.error) {
            this.performanceSummary = p
            this.navHistory = {
              dates: p.nav_dates || [],
              values: p.nav_values || [],
            }
          }
        }

        this.lastUpdated = new Date().toLocaleTimeString('zh-CN', { hour12: false })
      } catch (e) {
        this.error = e.message || '数据加载失败'
      } finally {
        this.loading = false
      }
    },
  },
})
