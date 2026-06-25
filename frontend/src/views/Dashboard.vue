<template>
  <div class="dashboard">
    <!-- 错误条 -->
    <transition name="fade">
      <div v-if="store.error" class="error-bar">
        <span class="err-icon">&#9888;</span>
        <span>{{ store.error }}</span>
        <button class="err-close" @click="store.error = null">&times;</button>
      </div>
    </transition>

    <div v-if="store.loading && !store.lastUpdated" class="load-mask">
      <div class="load-spin"></div>
      <span>加载中...</span>
    </div>

    <!-- 1. KPI 行: 4 卡片 -->
    <div class="kpi-row">
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          市场
        </div>
        <div class="kpi-body">
          <span class="kpi-v">{{ marketLabel }}</span>
          <span v-if="store.market.change_pct != null" :class="['kpi-tag', store.market.change_pct >= 0 ? 'up' : 'dn']">{{ fmtPct(store.market.change_pct) }}</span>
        </div>
        <div class="kpi-s">仓位 {{ store.market.position_ratio ?? '-' }}%  <span v-if="store.market.index" class="dim">{{ store.market.index }}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          总资产
        </div>
        <div class="kpi-v ac">{{ fmtMoney(totalAsset) }}</div>
        <div class="kpi-s">可用 {{ fmtMoney(availCash) }}  <span class="dim">市值 {{ fmtMoney(marketValue) }}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
          累计收益
        </div>
        <div :class="['kpi-v', pnlCls(cumPnl)]">{{ cumPnlLabel }}</div>
        <div class="kpi-s">夏普 {{ perf.sharpe?.toFixed(2) ?? '-' }}  <span class="dim">胜率 {{ winRateLabel }}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
          系统
        </div>
        <div class="kpi-body">
          <span :class="['kpi-dot', store.system.qmtConnected ? 'on' : 'off']"></span>
          <span class="kpi-v sm">QMT {{ store.system.qmtConnected ? '已连接' : '离线' }}</span>
        </div>
        <div class="kpi-s">
          ML {{ store.ml.status === 'normal' ? '正常' : (store.ml.status || '-') }}
          <span class="dim" v-if="store.ml.last_predict"> 最后预测 {{ fmtDate(store.ml.last_predict) }}</span>
        </div>
      </div>
    </div>

    <!-- 2. 主网格: 左 持仓 | 右 ML 候选 -->
    <div class="main-g">
      <!-- 持仓 -->
      <div class="sec">
        <div class="sec-h">
          <div class="sec-t">持仓 <span class="badge">{{ positions.length }}</span></div>
          <div class="sec-m" v-if="positions.length">
            总盈亏 <span :class="pnlCls(posSumPnl)">{{ fmtSigned(posSumPnl) }}</span>
            <span class="md">|</span>
            仓位 {{ posRatio }}%
          </div>
        </div>
        <div class="sec-b">
          <table class="tbl" v-if="positions.length">
            <thead>
              <tr>
                <th></th>
                <th>代码</th>
                <th>名称</th>
                <th class="r">成本</th>
                <th class="r">现价</th>
                <th class="r">盈亏</th>
                <th class="r">盈亏%</th>
                <th class="r">持仓</th>
                <th class="c">持有</th>
                <th class="c">策略</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(p, i) in positions" :key="p.code || p.ts_code || i">
                <td class="idx">{{ i + 1 }}</td>
                <td class="mono">{{ p.code || shortCode(p.ts_code) }}</td>
                <td>
                  <span class="sn">{{ p.stock_name || p.name || '-' }}</span>
                </td>
                <td class="r mono">{{ p.cost_price?.toFixed(2) ?? '-' }}</td>
                <td class="r mono">{{ p.current_price?.toFixed(2) ?? '-' }}</td>
                <td :class="['r mono', pnlCls(p.profit)]">{{ fmtSigned(p.profit) }}</td>
                <td :class="['r mono', pnlCls(p.pnl_pct)]">{{ fmtPct(p.pnl_pct) }}</td>
                <td class="r mono">{{ p.shares ?? '-' }}</td>
                <td class="c">
                  <span v-if="(p.days_held ?? 0) === 0" class="tg tg-w sm">T+1</span>
                  <span v-else>{{ p.days_held ?? '-' }}d</span>
                </td>
                <td class="c"><span :class="['tg', p.strategy === 'scanner' ? 'tg-w' : 'tg-p']">{{ strategyText(p.strategy) }}</span></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#3f3f5c" stroke-width="1.5"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
            <p>暂无持仓</p>
          </div>
        </div>
      </div>

      <!-- ML 候选 -->
      <div class="sec">
        <div class="sec-h">
          <div class="sec-t">ML 候选 <span class="badge">{{ mlCandidates.length }}</span></div>
          <div class="sec-tg">V11.2 板RPS</div>
        </div>
        <div class="sec-b">
          <table class="tbl" v-if="mlCandidates.length">
            <thead>
              <tr>
                <th></th>
                <th>代码</th>
                <th>名称</th>
                <th class="r">ML 概率</th>
                <th class="r">参考价</th>
                <th class="r">建议量</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in mlCandidates" :key="c.代码 || c.ts_code || i">
                <td class="idx">{{ i + 1 }}</td>
                <td class="mono">{{ c.代码 || shortCode(c.ts_code) }}</td>
                <td class="sn">{{ c.名称 || c.name || '-' }}</td>
                <td class="r">
                  <div class="pb-wrap">
                    <span class="pb-tr"><span class="pb-fill" :style="{ width: mlPct(c) + '%' }"></span></span>
                    <span class="pb-txt">{{ mlPct(c) }}%</span>
                  </div>
                </td>
                <td class="r mono">{{ fmtPrice(c.现价 ?? c.price ?? c.current_price) }}</td>
                <td class="r mono">{{ c.建议数量 || c.suggested_shares || c.份额 || '-' }}</td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#3f3f5c" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
            <p>暂无候选</p>
            <span class="empty-s">盘后 scan 未运行或休市</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 3. 实时扫描信号 (仅盘中) -->
    <div class="sec" v-if="scannerSignals.length">
      <div class="sec-h">
        <div class="sec-t">实时扫描 <span class="badge">{{ scannerSignals.length }}</span></div>
        <div class="sec-m">板RPS 盘中</div>
      </div>
      <div class="sec-b">
        <table class="tbl">
          <thead>
            <tr>
              <th></th>
              <th>代码</th>
              <th>名称</th>
              <th class="r">综合分</th>
              <th class="r">现价</th>
              <th class="r">涨跌幅</th>
              <th class="r">量比</th>
              <th class="r">ML 概率</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(s, i) in scannerSignals" :key="s.代码 || s.code || s.ts_code || i">
              <td class="idx">{{ i + 1 }}</td>
              <td class="mono">{{ s.代码 || s.code || shortCode(s.ts_code) }}</td>
              <td>{{ s.名称 || s.name || '-' }}</td>
              <td class="r"><span :class="['score', scoreCls(s.composite_score ?? s.score)]">{{ (s.composite_score ?? s.score ?? 0).toFixed(1) }}</span></td>
              <td class="r mono">{{ fmtPrice(s.现价 ?? s.price) }}</td>
              <td :class="['r mono', pnlCls(s.change_pct)]">{{ s.change_pct != null ? fmtPct(s.change_pct) : '-' }}</td>
              <td class="r mono">{{ (s.量比 ?? s.volume_ratio ?? '').toFixed?.(2) ?? s.量比 ?? s.volume_ratio ?? '-' }}</td>
              <td class="r"><span v-if="s.ml_prob != null" class="ml-txt">{{ (s.ml_prob * 100).toFixed(1) }}%</span><span v-else class="dim">-</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 4. 最近交易 -->
    <div class="sec" v-if="recentTrades.length">
      <div class="sec-h">
        <div class="sec-t">最近交易 <span class="badge">{{ recentTrades.length }}</span></div>
      </div>
      <div class="sec-b">
        <table class="tbl">
          <thead>
            <tr>
              <th>时间</th>
              <th>代码</th>
              <th>名称</th>
              <th class="c">方向</th>
              <th class="r">价格</th>
              <th class="r">数量</th>
              <th class="r">金额</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(t, i) in recentTrades" :key="t.order_id || t.trade_id || i">
              <td class="tm">{{ t.trade_time || t.created_at ? fmtTime(t.trade_time || t.created_at) : '-' }}</td>
              <td class="mono">{{ shortCode(t.ts_code) || t.code || '-' }}</td>
              <td>{{ t.stock_name || t.name || '-' }}</td>
              <td class="c">
                <span :class="['tg', t.action === '买入' || t.action === 'buy' ? 'tg-buy' : 'tg-sell']">
                  {{ t.action || '-' }}
                </span>
              </td>
              <td class="r mono">{{ t.price?.toFixed(2) ?? '-' }}</td>
              <td class="r mono">{{ t.quantity ?? t.shares ?? '-' }}</td>
              <td class="r mono">{{ fmtMoney(t.amount ?? t.trade_amount) }}</td>
              <td class="reason">{{ t.reason || '-' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 底部 bar -->
    <div class="ft" v-if="store.lastUpdated">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#52525b" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
      {{ fmtTimeFull(store.lastUpdated) }}
      <span class="dim">· 每 30s 自动刷新</span>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useDashboardStore } from '../stores/dashboard'

const store = useDashboardStore()
const interval = ref(null)

// ---- 计算属性 ----
const marketLabel = computed(() => store.market.state_name || store.market.state || '加载中')

const totalAsset = computed(() => {
  const b = store.balance
  return b.total_asset ?? b.总资产 ?? 0
})
const availCash = computed(() => {
  const b = store.balance
  return b.available ?? b.可用金额 ?? 0
})
const marketValue = computed(() => {
  const b = store.balance
  return b.market_value ?? b.股票市值 ?? 0
})

const perf = computed(() => store.performance || {})
const cumPnl = computed(() => {
  const ta = totalAsset.value
  const mv = marketValue.value
  const ac = availCash.value
  // 如果能从 performance 拿到
  if (perf.value.total_pnl != null) return perf.value.total_pnl
  if (perf.value.total_return != null) return perf.value.total_return
  // 从余额推导 (仅当 balance 包含 累计盈亏)
  const cb = store.balance.累计盈亏
  if (cb != null) return cb
  return null
})

const cumPnlLabel = computed(() => {
  if (cumPnl.value == null) return '-'
  return fmtSigned(cumPnl.value)
})

const winRateLabel = computed(() => {
  const p = perf.value
  const wr = p.win_rate ?? p.winRate
  if (wr != null) return (wr * 100).toFixed(0) + '%'
  return '-'
})

const positions = computed(() => store.positions || [])
const posSumPnl = computed(() => positions.value.reduce((s, p) => s + (Number(p.profit) || 0), 0))
const posRatio = computed(() => {
  const ta = totalAsset.value
  if (!ta) return 0
  return ((marketValue.value / ta) * 100).toFixed(0)
})

const mlCandidates = computed(() => store.mlCandidates || [])
const scannerSignals = computed(() => store.scannerSignals || [])
const recentTrades = computed(() => {
  const t = store.trades || []
  // 可能是 {trades: [...]} 结构
  if (Array.isArray(t)) return t.slice(0, 15)
  if (t.trades && Array.isArray(t.trades)) return t.trades.slice(0, 15)
  return []
})

// ---- 辅助函数 ----
function shortCode(ts) {
  if (!ts) return '-'
  return ts.replace(/\.(SH|SZ)$/, '')
}
function strategyText(s) {
  if (s === 'scanner') return '扫描'
  if (s === 'ml' || s === 'ML') return 'ML'
  return s || '-'
}
function mlPct(c) {
  const v = c.ml_prob ?? c.ML概率 ?? c.probability ?? 0
  return Math.round(Number(v) * 100)
}
function fmtPrice(v) {
  if (v == null) return '-'
  return '¥' + Number(v).toFixed(2)
}
function fmtSigned(v) {
  if (v == null) return '-'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2)
}
function fmtPct(v) {
  if (v == null) return '-'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}
function fmtMoney(v) {
  if (v == null) return '-'
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2 })
}
function pnlCls(v) {
  if (v == null) return ''
  return Number(v) > 0 ? 'up' : Number(v) < 0 ? 'dn' : ''
}
function scoreCls(v) {
  if (v == null) return ''
  if (v >= 80) return 'sc-hi'
  if (v >= 60) return 'sc-md'
  return 'sc-lo'
}
function fmtTime(d) {
  if (!d) return '-'
  try {
    const dt = new Date(d)
    if (isNaN(dt.getTime())) return String(d).slice(0, 16)
    const pad = n => String(n).padStart(2, '0')
    return `${pad(dt.getHours())}:${pad(dt.getMinutes())}`
  } catch { return String(d).slice(0, 16) }
}
function fmtTimeFull(d) {
  if (!d) return ''
  try {
    const dt = new Date(d)
    if (isNaN(dt.getTime())) return String(d)
    const pad = n => String(n).padStart(2, '0')
    return `${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`
  } catch { return String(d) }
}
function fmtDate(d) {
  if (!d) return ''
  try {
    const dt = new Date(d)
    if (isNaN(dt.getTime())) return String(d).slice(0, 10)
    const pad = n => String(n).padStart(2, '0')
    return `${pad(dt.getMonth()+1)}-${pad(dt.getDate())}`
  } catch { return String(d).slice(0, 10) }
}

function start() { store.refreshAll(); interval.value = setInterval(() => store.refreshAll(), 30000) }
onMounted(() => start())
onUnmounted(() => { if (interval.value) clearInterval(interval.value) })
</script>

<style scoped>
.dashboard {
  max-width: 1440px; margin: 0 auto;
  display: flex; flex-direction: column; gap: 8px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}

/* 错误/加载 */
.error-bar { display: flex; align-items: center; gap: 8px; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.25); border-radius: 6px; padding: 7px 12px; font-size: 12.5px; color: #fca5a5; }
.err-icon { font-size: 14px; }
.err-close { margin-left: auto; background: none; border: none; color: #fca5a5; cursor: pointer; font-size: 16px; padding: 0 4px; }
.load-mask { display: flex; align-items: center; justify-content: center; gap: 10px; padding: 50px 0; color: #6b7280; font-size: 13px; }
.load-spin { width: 18px; height: 18px; border: 2px solid #2d2d3f; border-top-color: #818cf8; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.fade-enter-active, .fade-leave-active { transition: opacity 0.25s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* KPI */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.kpi { background: #1e1e2e; border: 1px solid #2d2d3f; border-radius: 8px; padding: 12px 14px; }
.kpi-h { display: flex; align-items: center; gap: 5px; font-size: 10.5px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 5px; }
.kpi-h svg { flex-shrink: 0; }
.kpi-body { display: flex; align-items: baseline; gap: 8px; }
.kpi-v { font-size: 20px; font-weight: 700; color: #e4e4e7; line-height: 1.2; }
.kpi-v.sm { font-size: 14px; font-weight: 600; }
.kpi-v.ac { color: #60a5fa; }
.kpi-tag { font-size: 11.5px; font-weight: 600; padding: 1px 6px; border-radius: 4px; background: rgba(255,255,255,0.04); }
.kpi-s { font-size: 11px; color: #6b7280; margin-top: 3px; }
.kpi-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.kpi-dot.on { background: #34d399; box-shadow: 0 0 5px rgba(52,211,153,0.4); }
.kpi-dot.off { background: #f87171; box-shadow: 0 0 5px rgba(248,113,113,0.4); }

/* Section */
.sec { background: #1e1e2e; border: 1px solid #2d2d3f; border-radius: 8px; overflow: hidden; }
.sec-h { display: flex; align-items: center; justify-content: space-between; padding: 9px 14px; border-bottom: 1px solid #2d2d3f; }
.sec-t { display: flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; color: #d4d4d8; }
.sec-m { font-size: 11.5px; color: #a1a1aa; }
.sec-tg { font-size: 10px; color: #818cf8; background: rgba(129,140,248,0.1); padding: 2px 7px; border-radius: 4px; }
.sec-b { padding: 0; }

.badge { display: inline-flex; align-items: center; justify-content: center; background: #2d2d3f; color: #a1a1aa; font-size: 10.5px; font-weight: 600; min-width: 18px; height: 16px; border-radius: 8px; padding: 0 5px; }
.md { margin: 0 5px; color: #3f3f5c; }

.main-g { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

/* Table */
.tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
.tbl thead th { text-align: left; padding: 7px 10px; font-weight: 600; color: #6b7280; font-size: 10px; text-transform: uppercase; letter-spacing: 0.3px; border-bottom: 1px solid #2d2d3f; background: #181825; white-space: nowrap; }
.tbl tbody td { padding: 6px 10px; color: #d4d4d8; border-bottom: 1px solid #27272a; white-space: nowrap; }
.tbl tbody tr:hover { background: rgba(255,255,255,0.03); }
.tbl tbody tr:last-child td { border-bottom: none; }
.tbl .r { text-align: right; }
.tbl .c { text-align: center; }
td.idx { color: #52525b; font-size: 10.5px; width: 20px; text-align: center; }
td.mono { font-family: 'SF Mono','Fira Code','Cascadia Code',monospace; font-size: 11.5px; }
td.tm { font-size: 11px; color: #6b7280; white-space: nowrap; }
td.reason { font-size: 11px; color: #a1a1aa; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sn { white-space: nowrap; }

/* Tag */
.tg { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; line-height: 1.4; }
.tg.sm { font-size: 9.5px; padding: 0 4px; }
.tg-p { background: rgba(96,165,250,0.15); color: #93c5fd; }
.tg-w { background: rgba(251,191,36,0.15); color: #fcd34d; }
.tg-buy { background: rgba(52,211,153,0.15); color: #6ee7b7; }
.tg-sell { background: rgba(248,113,113,0.15); color: #fca5a5; }

/* 概率条 */
.pb-wrap { display: inline-flex; align-items: center; gap: 5px; width: 100%; max-width: 150px; }
.pb-tr { flex: 1; height: 5px; background: #27272a; border-radius: 3px; overflow: hidden; }
.pb-fill { display: block; height: 100%; background: linear-gradient(90deg, #6366f1, #818cf8); border-radius: 3px; transition: width 0.4s; }
.pb-txt { font-size: 11px; font-weight: 600; color: #818cf8; white-space: nowrap; min-width: 35px; text-align: right; }
.ml-txt { font-size: 11.5px; font-weight: 600; color: #818cf8; }

/* Score */
.score { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 11.5px; font-weight: 700; }
.sc-hi { background: rgba(52,211,153,0.15); color: #34d399; }
.sc-md { background: rgba(251,191,36,0.15); color: #fbbf24; }
.sc-lo { background: rgba(248,113,113,0.15); color: #f87171; }

/* 颜色 */
.up { color: #34d399; }
.dn { color: #f87171; }
.dim { color: #6b7280; }

/* 空状态 */
.empty { display: flex; flex-direction: column; align-items: center; gap: 5px; padding: 30px 20px; color: #52525b; }
.empty p { font-size: 12.5px; margin: 0; }
.empty-s { font-size: 10.5px; color: #3f3f5c; }

/* 底部 */
.ft { display: flex; align-items: center; gap: 4px; font-size: 10.5px; color: #52525b; padding: 2px 0 6px; }

/* 响应式 */
@media (max-width: 1024px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 767px) {
  .kpi-row { grid-template-columns: repeat(2, 1fr); gap: 6px; }
  .kpi { padding: 9px 10px; }
  .kpi-v { font-size: 17px; }
  .main-g { grid-template-columns: 1fr; }
  .tbl { font-size: 11px; }
  .tbl thead th, .tbl tbody td { padding: 5px 6px; }
  .pb-wrap { max-width: 100px; }
  td.reason { max-width: 100px; }
}
</style>
