<template>
  <div class="dashboard">
    <!-- 错误条 -->
    <transition name="fade">
      <div v-if="store.error" class="error-bar">
        <span class="error-icon">&#9888;</span>
        <span>{{ store.error }}</span>
        <button class="error-close" @click="store.error = null">&times;</button>
      </div>
    </transition>

    <div v-if="store.loading && !store.lastUpdated" class="loading-overlay">
      <div class="loading-spinner"></div>
      <span>加载中...</span>
    </div>

    <!-- 1. 指标卡片 -->
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
          </svg>
          市场状态
        </div>
        <div class="kpi-row">
          <span class="kpi-value">{{ marketStateLabel }}</span>
          <span v-if="store.market.change_pct != null" :class="['kpi-change', store.market.change_pct >= 0 ? 'up' : 'down']">
            {{ fmtPct(store.market.change_pct) }}
          </span>
        </div>
        <div class="kpi-sub">仓位 {{ store.market.position_ratio ?? '-' }}%</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>
          </svg>
          总资产
        </div>
        <div class="kpi-value accent">{{ fmtMoney(store.balance.总资产) }}</div>
        <div class="kpi-sub">可用 {{ fmtMoney(store.balance.可用金额) }} / 市值 {{ fmtMoney(store.balance.股票市值) }}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/>
            <polyline points="17 6 23 6 23 12"/>
          </svg>
          累计收益
        </div>
        <div :class="['kpi-value', (cumPnl || 0) >= 0 ? 'up' : 'down']">{{ cumPnlLabel }}</div>
        <div class="kpi-sub">
          夏普 {{ store.performance.sharpe?.toFixed(2) ?? '-' }}
          <span class="kpi-divider">|</span>
          胜率 {{ winRate }}
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
          </svg>
          系统健康
        </div>
        <div class="kpi-row">
          <span :class="['kpi-dot', store.system.qmtConnected ? 'online' : 'offline']"></span>
          <span class="kpi-value sm">QMT {{ store.system.qmtConnected ? '已连接' : '离线' }}</span>
        </div>
        <div class="kpi-sub">模型 {{ store.ml.status === 'normal' ? '正常' : (store.ml.status || '-') }}</div>
      </div>
    </div>

    <!-- 2. 主网格: 左-持仓 / 右-ML候选 -->
    <div class="main-grid">
      <!-- 左: 持仓 -->
      <div class="section">
        <div class="section-header">
          <div class="section-title">
            持仓
            <span class="section-badge">{{ positions.length }}</span>
          </div>
          <div class="section-meta" v-if="positions.length">
            <span>总盈亏 </span>
            <span :class="posTotalPnl >= 0 ? 'up' : 'down'">{{ fmtSigned(posTotalPnl) }}</span>
            <span class="meta-divider">|</span>
            <span>仓位 {{ portfolioRatio }}%</span>
          </div>
        </div>
        <div class="section-body">
          <table class="dt" v-if="positions.length">
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th class="r">成本</th>
                <th class="r">现价</th>
                <th class="r">盈亏</th>
                <th class="r">盈亏%</th>
                <th class="r">持仓</th>
                <th class="c">持有</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="p in positions" :key="p.code || p.ts_code">
                <td class="mono">{{ p.code || '-' }}</td>
                <td>
                  <span class="stock-name">{{ p.stock_name || '-' }}</span>
                  <span :class="['tag', p.strategy === 'scanner' ? 'tag-warn' : 'tag-primary']">
                    {{ strategyLabel(p.strategy) }}
                  </span>
                </td>
                <td class="r mono">{{ p.cost_price?.toFixed(2) ?? '-' }}</td>
                <td class="r mono">{{ p.current_price?.toFixed(2) ?? '-' }}</td>
                <td :class="['r mono', pnlCls(p.profit)]">{{ fmtSigned(p.profit) }}</td>
                <td :class="['r mono', pnlCls(p.pnl_pct)]">{{ fmtPct(p.pnl_pct) }}</td>
                <td class="r mono">{{ p.shares ?? '-' }}</td>
                <td class="c">
                  <span v-if="(p.days_held ?? 0) === 0" class="tag tag-warn sm">T+1</span>
                  <span v-else>{{ p.days_held ?? '-' }}d</span>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3f3f5c" stroke-width="1.5">
              <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
            <p>暂无持仓</p>
          </div>
        </div>
      </div>

      <!-- 右: ML 候选 -->
      <div class="section">
        <div class="section-header">
          <div class="section-title">
            ML 候选
            <span class="section-badge">{{ mlCandidates.length }}</span>
          </div>
          <div class="section-tag">V11.2 板RPS</div>
        </div>
        <div class="section-body">
          <table class="dt" v-if="mlCandidates.length">
            <thead>
              <tr>
                <th>代码</th>
                <th>名称</th>
                <th class="r">ML概率</th>
                <th class="r">参考价</th>
                <th class="r">建议量</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in mlCandidates" :key="c.代码 || c.ts_code">
                <td class="mono">{{ c.代码 || c.ts_code || '-' }}</td>
                <td>{{ c.名称 || c.name || '-' }}</td>
                <td class="r">
                  <div class="prob">
                    <span class="prob-track">
                      <span class="prob-fill" :style="{ width: mlProbPct(c) + '%' }"></span>
                    </span>
                    <span class="prob-val">{{ (mlProbVal(c) * 100).toFixed(1) }}%</span>
                  </div>
                </td>
                <td class="r mono">{{ fmtPrice(c) }}</td>
                <td class="r mono">{{ c.建议数量 || c.suggested_shares || c.份额 || '-' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#3f3f5c" stroke-width="1.5">
              <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
            </svg>
            <p>暂无可选候选</p>
            <span class="empty-sub">盘后 scan 未运行或今日无信号</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 3. 实时扫描信号 -->
    <div class="section" v-if="scannerSignals.length">
      <div class="section-header">
        <div class="section-title">
          实时扫描信号
          <span class="section-badge">{{ scannerSignals.length }}</span>
        </div>
      </div>
      <div class="section-body">
        <table class="dt">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th class="r">综合分</th>
              <th class="r">现价</th>
              <th class="r">涨跌幅</th>
              <th class="r">量比</th>
              <th class="r">ML概率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="s in scannerSignals" :key="s.代码 || s.ts_code || s.code">
              <td class="mono">{{ s.代码 || s.ts_code || s.code || '-' }}</td>
              <td>{{ s.名称 || s.name || '-' }}</td>
              <td class="r">
                <span :class="['score', scoreCls(s.composite_score || s.score)]">{{ (s.composite_score || s.score || 0).toFixed(1) }}</span>
              </td>
              <td class="r mono">{{ s.现价 || s.price?.toFixed(2) || '-' }}</td>
              <td :class="['r mono', pnlCls(s.change_pct)]">{{ s.change_pct != null ? fmtPct(s.change_pct) : '-' }}</td>
              <td class="r mono">{{ s.量比 || s.volume_ratio || '-' }}</td>
              <td class="r">
                <span v-if="s.ml_prob != null" class="prob-text">{{ (s.ml_prob * 100).toFixed(1) }}%</span>
                <span v-else class="dim">-</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 4. 最近交易日志 -->
    <div class="section" v-if="recentTrades.length">
      <div class="section-header">
        <div class="section-title">
          最近交易
          <span class="section-badge">{{ recentTrades.length }}</span>
        </div>
      </div>
      <div class="section-body">
        <table class="dt">
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th class="r">价格</th>
              <th class="r">数量</th>
              <th class="r">金额</th>
              <th>方向</th>
              <th class="r">盈亏</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in recentTrades" :key="t.id || t.trade_id">
              <td class="mono">{{ t.code || t.ts_code || '-' }}</td>
              <td>{{ t.name || t.stock_name || '-' }}</td>
              <td class="r mono">{{ (t.price || t.trade_price)?.toFixed(2) ?? '-' }}</td>
              <td class="r mono">{{ t.shares || t.volume || '-' }}</td>
              <td class="r mono">{{ fmtMoney(t.amount || t.trade_amount) }}</td>
              <td>
                <span :class="['tag', tradeDirection === 'buy' || t.direction === '买入' ? 'tag-buy' : 'tag-sell']">
                  {{ t.direction || t.action || '-' }}
                </span>
              </td>
              <td :class="['r mono', pnlCls(t.profit_pnl || t.pnl)]">{{ (t.profit_pnl ?? t.pnl) != null ? fmtSigned(t.profit_pnl || t.pnl) : '-' }}</td>
              <td class="time-cell">{{ formatTime(t.trade_time || t.created_at || t.time) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 更新时间 -->
    <div class="update-bar" v-if="store.lastUpdated">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#52525b" stroke-width="2">
        <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
      </svg>
      上次更新 {{ store.lastUpdated }}
      <span class="update-interval">（每 30s 自动刷新）</span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useDashboardStore } from '../stores/dashboard'

const store = useDashboardStore()
const refreshInterval = ref(null)

const marketStateLabel = computed(() => {
  const m = store.market
  if (m.state_name) return m.state_name
  if (m.state) return m.state
  if (m.index) return m.index
  return '加载中'
})

const positions = computed(() => store.positions || [])
const mlCandidates = computed(() => store.mlCandidates || [])
const scannerSignals = computed(() => store.scannerSignals || [])
const recentTrades = computed(() => (store.trades || []).slice(0, 15))

const posTotalPnl = computed(() => {
  return positions.value.reduce((s, p) => s + (Number(p.profit) || 0), 0)
})

const cumPnl = computed(() => {
  if (store.performance.total_pnl != null) return store.performance.total_pnl
  const b = store.balance
  const init = b.总资产 ? b.总资产 - (b.累计盈亏 || 0) : null
  if (init && b.总资产) return b.总资产 - init
  return null
})

const cumPnlLabel = computed(() => {
  if (cumPnl.value == null) return '-'
  return fmtSigned(cumPnl.value)
})

const totalInvested = computed(() => {
  if (store.performance.total_invested != null) return store.performance.total_invested
  const b = store.balance
  if (b.总资产 && b.累计盈亏 != null) return b.总资产 - b.累计盈亏
  return null
})

const winRate = computed(() => {
  const p = store.performance
  if (p.win_rate != null) return (p.win_rate * 100).toFixed(0) + '%'
  if (p.winRate != null) return (p.winRate * 100).toFixed(0) + '%'
  return '-'
})

const portfolioRatio = computed(() => {
  const b = store.balance
  if (!b.总资产) return 0
  return ((b.股票市值 || 0) / b.总资产 * 100).toFixed(0)
})

function strategyLabel(s) {
  if (s === 'scanner') return '扫描'
  if (s === 'ml' || s === 'ML') return 'ML'
  return s || '-'
}

function mlProbVal(c) {
  return c.ml_prob ?? c.ML概率 ?? c.probability ?? 0
}
function mlProbPct(c) {
  return Math.round((mlProbVal(c) || 0) * 100)
}
function fmtPrice(c) {
  const v = c.现价 ?? c.price ?? c.current_price
  return v != null ? '¥' + Number(v).toFixed(2) : '-'
}
function fmtSigned(v) {
  if (v == null) return '-'
  const s = Number(v)
  return (s >= 0 ? '+' : '') + s.toFixed(2)
}
function fmtPct(v) {
  if (v == null) return '-'
  const s = Number(v)
  return (s >= 0 ? '+' : '') + s.toFixed(2) + '%'
}
function fmtMoney(v) {
  if (v == null) return '-'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
function pnlCls(v) {
  if (v == null) return ''
  return Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : ''
}
function scoreCls(v) {
  if (v == null) return ''
  if (v >= 80) return 'score-high'
  if (v >= 60) return 'score-mid'
  return 'score-low'
}
function formatTime(t) {
  if (!t) return '-'
  const d = new Date(t)
  if (isNaN(d.getTime())) return String(t).slice(0, 16)
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function startRefresh() {
  store.refreshAll()
  refreshInterval.value = setInterval(() => store.refreshAll(), 30000)
}

onMounted(() => { startRefresh() })
onUnmounted(() => {
  if (refreshInterval.value) {
    clearInterval(refreshInterval.value)
    refreshInterval.value = null
  }
})
</script>

<style scoped>
.dashboard {
  max-width: 1440px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ====== 错误条 / 加载 ====== */
.error-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(239, 68, 68, 0.15);
  border: 1px solid rgba(239, 68, 68, 0.3);
  border-radius: 6px;
  padding: 8px 14px;
  font-size: 13px;
  color: #fca5a5;
}
.error-icon { font-size: 15px; }
.error-close {
  margin-left: auto;
  background: none;
  border: none;
  color: #fca5a5;
  cursor: pointer;
  font-size: 18px;
  line-height: 1;
  padding: 0 4px;
}
.loading-overlay {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 60px 0;
  color: #6b7280;
  font-size: 13px;
}
.loading-spinner {
  width: 20px;
  height: 20px;
  border: 2px solid #2d2d3f;
  border-top-color: #818cf8;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.3s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* ====== KPI 卡片 ====== */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.kpi-card {
  background: #1e1e2e;
  border: 1px solid #2d2d3f;
  border-radius: 8px;
  padding: 14px 16px;
}
.kpi-label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 6px;
}
.kpi-label svg { flex-shrink: 0; }
.kpi-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.kpi-value {
  font-size: 22px;
  font-weight: 700;
  color: #e4e4e7;
  line-height: 1.2;
}
.kpi-value.sm { font-size: 14px; font-weight: 600; }
.kpi-value.accent { color: #60a5fa; }
.kpi-change {
  font-size: 12px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(255,255,255,0.04);
}
.kpi-sub {
  font-size: 11px;
  color: #6b7280;
  margin-top: 4px;
}
.kpi-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.kpi-dot.online { background: #34d399; box-shadow: 0 0 6px rgba(52,211,153,0.4); }
.kpi-dot.offline { background: #f87171; box-shadow: 0 0 6px rgba(248,113,113,0.4); }

/* ====== Section ====== */
.section {
  background: #1e1e2e;
  border: 1px solid #2d2d3f;
  border-radius: 8px;
  overflow: hidden;
}
.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 16px;
  border-bottom: 1px solid #2d2d3f;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #d4d4d8;
}
.section-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: #2d2d3f;
  color: #a1a1aa;
  font-size: 11px;
  font-weight: 600;
  min-width: 20px;
  height: 18px;
  border-radius: 9px;
  padding: 0 6px;
}
.section-meta {
  font-size: 11.5px;
  color: #a1a1aa;
}
.meta-divider {
  margin: 0 6px;
  color: #3f3f5c;
}
.section-tag {
  font-size: 10.5px;
  color: #818cf8;
  background: rgba(129,140,248,0.1);
  padding: 2px 8px;
  border-radius: 4px;
}
.section-body { padding: 0; }

.main-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

/* ====== 数据表格 ====== */
.dt {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}
.dt thead th {
  text-align: left;
  padding: 8px 12px;
  font-weight: 600;
  color: #6b7280;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
  border-bottom: 1px solid #2d2d3f;
  background: #181825;
  white-space: nowrap;
}
.dt tbody td {
  padding: 7px 12px;
  color: #d4d4d8;
  border-bottom: 1px solid #27272a;
  white-space: nowrap;
}
.dt tbody tr:hover { background: rgba(255,255,255,0.03); }
.dt tbody tr:last-child td { border-bottom: none; }
.dt .r { text-align: right; }
.dt .c { text-align: center; }
td.mono { font-family: 'SF Mono','Fira Code','Cascadia Code',monospace; }
td.time-cell { font-size: 11px; color: #6b7280; white-space: nowrap; }

.stock-name { margin-right: 5px; }

/* ====== Tag ====== */
.tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10.5px;
  font-weight: 600;
  line-height: 1.4;
}
.tag.sm { font-size: 10px; padding: 0 5px; }
.tag-primary { background: rgba(96,165,250,0.15); color: #93c5fd; }
.tag-warn { background: rgba(251,191,36,0.15); color: #fcd34d; }
.tag-buy { background: rgba(52,211,153,0.15); color: #6ee7b7; }
.tag-sell { background: rgba(248,113,113,0.15); color: #fca5a5; }

/* ====== ML 概率条 ====== */
.prob {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  max-width: 160px;
}
.prob-track {
  flex: 1;
  height: 6px;
  background: #27272a;
  border-radius: 3px;
  overflow: hidden;
}
.prob-fill {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #6366f1, #818cf8);
  border-radius: 3px;
  transition: width 0.4s ease;
}
.prob-val {
  font-size: 11.5px;
  font-weight: 600;
  color: #818cf8;
  white-space: nowrap;
  min-width: 42px;
  text-align: right;
}
.prob-text { font-size: 12px; font-weight: 600; color: #818cf8; }

/* ====== Score ====== */
.score {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 12px;
  font-weight: 700;
}
.score-high { background: rgba(52,211,153,0.15); color: #34d399; }
.score-mid { background: rgba(251,191,36,0.15); color: #fbbf24; }
.score-low { background: rgba(248,113,113,0.15); color: #f87171; }

/* ====== 上下 / 颜色 ====== */
.up { color: #34d399; }
.down { color: #f87171; }
.dim { color: #6b7280; }

/* ====== 空状态 ====== */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 36px 20px;
  color: #52525b;
}
.empty p { font-size: 13px; margin: 0; }
.empty-sub { font-size: 11px; color: #3f3f5c; }

/* ====== 更新时间 ====== */
.update-bar {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: #52525b;
  padding: 4px 0 8px;
}
.update-interval { color: #3f3f5c; }

/* ====== 响应式 ====== */
@media (max-width: 1024px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 767px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .kpi-card { padding: 10px 12px; }
  .kpi-value { font-size: 18px; }
  .main-grid { grid-template-columns: 1fr; }
  .dt { font-size: 11.5px; }
  .dt thead th,
  .dt tbody td { padding: 6px 8px; }
  .prob { max-width: 120px; }
}
</style>
