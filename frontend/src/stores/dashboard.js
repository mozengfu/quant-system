import { defineStore } from 'pinia'
import api from '../api/index'

export const useDashboardStore = defineStore('dashboard', {
  state: () => ({
    // 市场状态
    market: { state: '加载中', position_ratio: 80, index: null, change_pct: null },
    // 账户
    balance: { 可用金额: 0, 总资产: 0, 股票市值: 0 },
    // 持仓
    positions: [],
    // ML 候选
    mlCandidates: [],
    // 交易日志
    trades: [],
    // ML/绩效
    ml: { status: 'normal' },
    performance: {},
    // 系统
    system: { heartbeat: null, scanTime: null, qmtConnected: false },
    // 加载状态
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
        const [statusRes, positionRes, balanceRes, tradesRes, recommendRes] = await Promise.allSettled([
          api.get('/pipeline/status'),
          api.get('/trading/positions'),
          api.get('/trading/balance'),
          api.get('/trading/trades', { params: { limit: 20 } }),
          api.get('/recommend/v11', { params: { top_n: 5 } }),
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

        // 系统状态：从 performance 提取额外信息
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
