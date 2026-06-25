import { defineStore } from 'pinia'
import api from '../api/index'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    market: { state: '加载中', position_ratio: 80, index: null, change_pct: null },
    balance: { 可用金额: 0, 总资产: 0, 股票市值: 0 },
    positions: [],
    mlCandidates: [],
    scannerSignals: [],
    trades: [],
    ml: { status: 'normal' },
    performance: {},
    system: { heartbeat: null, scanTime: null, qmtConnected: false },
    loading: false,
    error: null,
    lastUpdated: null,
  }),

  getters: {
    positionCount: (state) => state.positions.length,
    totalPnl: (state) => {
      if (!state.positions.length) return 0
      return state.positions.reduce((s, p) => s + (p.profit || 0), 0)
    },
    mlCandidateCount: (state) => state.mlCandidates.length,
  },

  actions: {
    async refreshAll() {
      this.loading = true
      this.error = null
      try {
        const [statusRes, positionRes, balanceRes, tradesRes, recommendRes, scannerRes] = await Promise.allSettled([
          api.get('/pipeline/status'),
          api.get('/trading/positions'),
          api.get('/trading/balance'),
          api.get('/trading/trades', { params: { limit: 20 } }),
          api.get('/recommend/v11', { params: { top_n: 5 } }),
          api.get('/scanner/signals'),
        ])

        if (statusRes.status === 'fulfilled') {
          const s = statusRes.value
          this.market = s.market || {}
          this.ml = s.ml || {}
          this.performance = s.performance || {}
          const trading = s.trading || {}
          this.system.qmtConnected = trading.executor === 'QMT'
        }

        if (positionRes.status === 'fulfilled') {
          this.positions = positionRes.value || []
        }

        if (balanceRes.status === 'fulfilled') {
          this.balance = balanceRes.value || {}
        }

        if (tradesRes.status === 'fulfilled') {
          this.trades = (tradesRes.value || []).slice(0, 20)
        }

        if (recommendRes.status === 'fulfilled') {
          const r = recommendRes.value
          this.mlCandidates = r.推荐股票 || r.stocks || []
        }

        if (scannerRes.status === 'fulfilled') {
          const r = scannerRes.value
          this.scannerSignals = (r.signals || []).slice(0, 10)
        }

        if (statusRes.status === 'fulfilled') {
          if (this.ml.last_predict) {
            this.system.scanTime = this.ml.last_predict
          }
        }

        this.lastUpdated = new Date().toLocaleTimeString('zh-CN', { hour12: false })
      } catch (e) {
        this.error = e.message || '数据加载失败'
      } finally {
        this.loading = false
      }
    },

    setHeartbeat(time) {
      this.system.heartbeat = time
    },
  },
})
