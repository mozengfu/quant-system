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

    <!-- 加载遮罩 -->
    <div v-if="store.loading && !store.lastUpdated" class="load-mask">
      <div class="load-spin"></div>
      <span>加载中...</span>
    </div>

    <!-- 1. KPI 行 -->
    <div class="kpi-row">
      <div class="kpi" @click="toggleChart">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          市场
        </div>
        <div class="kpi-body">
          <span class="kpi-v">{{ marketLabel }}</span>
          <span v-if="store.market.change_pct != null" :class="['kpi-tag', store.market.change_pct >= 0 ? 'up' : 'dn']">{{ fmtPct(store.market.change_pct) }}</span>
        </div>
        <div class="kpi-s">建议仓位 {{ store.market.position_ratio ?? '-' }}% <span v-if="store.market.index" class="dim">{{ store.market.index }}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
          总资产
        </div>
        <div class="kpi-body">
          <span class="kpi-v ac">{{ fmtMoney(store.balance.total_asset) }}</span>
        </div>
        <div class="kpi-s">可用 {{ fmtMoney(store.balance.available) }} <span class="dim">市值 {{ fmtMoney(store.balance.market_value) }}</span></div>
      </div>
      <div class="kpi">
        <div class="kpi-h">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>
          累计收益
        </div>
        <div :class="['kpi-v', pnlCls(store.performanceSummary?.total_return ?? store.performance.total_return)]">{{ perfReturnLabel }}</div>
        <div class="kpi-s">夏普 {{ shapely(store.performanceSummary?.sharpe ?? store.performance.sharpe) }} <span class="dim">胜率 {{ winRateLabel }}</span></div>
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
          ML {{ store.ml.status === 'normal' ? '正常' : (store.ml.status || '-') }} <span class="dim">{{ store.ml.feature_count || '' }}特征</span>
        </div>
      </div>
    </div>

    <!-- 2. 收益曲线 (点击切换) -->
    <div v-if="showChart && store.navHistory.dates.length > 1" class="chart-wrap">
      <div class="sec">
        <div class="sec-h">
          <div class="sec-t">净值曲线</div>
          <div class="sec-m">近 {{ store.navHistory.dates.length }} 个交易日</div>
          <button class="sec-close" @click="showChart = false">&times;</button>
        </div>
        <div class="chart-box">
          <VChart :option="chartOption" autoresize style="height:260px;width:100%" />
        </div>
      </div>
    </div>

    <!-- 3. 主网格 -->
    <div class="main-g">
      <!-- 左：持仓 -->
      <div class="sec">
        <div class="sec-h">
          <div class="sec-t">持仓 <span class="badge">{{ store.positions.length }}</span></div>
          <div class="sec-m">
            总盈亏 <span :class="pnlCls(store.totalPnl)">{{ fmtSigned(store.totalPnl) }}</span>
          </div>
        </div>
        <div class="sec-b">
          <table class="tbl" v-if="store.positions.length">
            <thead>
              <tr>
                <th></th>
                <th>名称</th>
                <th class="r">成本</th>
                <th class="r">现价</th>
                <th class="r">盈亏</th>
                <th class="r">盈亏%</th>
                <th class="r">市值</th>
                <th class="c">策略</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(p, i) in store.positions" :key="p.ts_code || i">
                <td class="idx">{{ i + 1 }}</td>
                <td>
                  <span class="sn">{{ p.name || '-' }}</span>
                  <span class="scode">{{ shortCode(p.ts_code) }}</span>
                </td>
                <td class="r mono">{{ p.cost_price?.toFixed(2) ?? '-' }}</td>
                <td class="r mono">{{ p.current_price?.toFixed(2) ?? '-' }}</td>
                <td :class="['r mono', pnlCls(p.pnl)]">{{ fmtSigned(p.pnl) }}</td>
                <td :class="['r mono', pnlCls(p.pnl_pct)]">{{ fmtPct(p.pnl_pct) }}</td>
                <td class="r mono">{{ fmtMoney(p.market_value, false) }}</td>
                <td class="c"><span class="tg tg-p">ML</span></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="empty">
            <p>暂无持仓</p>
            <span class="empty-s">盘后扫描后将生成候选</span>
          </div>
        </div>
      </div>

      <!-- 右：ML 候选 + 扫描信号 -->
      <div class="cols">
        <!-- ML 候选 -->
        <div class="sec" v-if="store.mlCandidates.length">
          <div class="sec-h">
            <div class="sec-t">ML 候选</div>
            <span class="sec-tg">V11.2</span>
          </div>
          <table class="tbl">
            <thead>
              <tr>
                <th></th>
                <th>名称</th>
                <th class="r">现价</th>
                <th class="r">涨幅</th>
                <th class="r">ML</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in store.mlCandidates" :key="c.code || i">
                <td class="idx">{{ i + 1 }}</td>
                <td>
                  <span class="sn">{{ c.name || '-' }}</span>
                  <span class="scode">{{ c.code }}</span>
                </td>
                <td class="r mono">{{ c.price?.toFixed(2) ?? '-' }}</td>
                <td :class="['r mono', pctClass(c.pct_chg)]">{{ c.pct_chg }}</td>
                <td class="r">
                  <div class="pb-wrap">
                    <span class="pb-tr"><span class="pb-fill" :style="{ width: (c.ml_score * 25) + '%' }"></span></span>
                    <span class="pb-txt">{{ c.ml_score?.toFixed(2) }}</span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 扫描信号 -->
        <div class="sec" v-if="store.scannerSignals.length">
          <div class="sec-h">
            <div class="sec-t">实时扫描</div>
            <span class="sec-tg" style="color:#fbbf24;background:rgba(251,191,36,0.1)">盘中</span>
          </div>
          <table class="tbl">
            <thead>
              <tr>
                <th></th>
                <th>名称</th>
                <th class="r">综合分</th>
                <th class="r">ML</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(sig, i) in store.scannerSignals" :key="sig.ts_code || i">
                <td class="idx">{{ i + 1 }}</td>
                <td>
                  <span class="sn">{{ sig.stock_name || sig.name || '-' }}</span>
                  <span class="scode">{{ shortCode(sig.ts_code) }}</span>
                </td>
                <td class="r">
                  <span :class="['score', scoreClass(sig.composite_score)]">
                    {{ sig.composite_score ?? '-' }}
                  </span>
                </td>
                <td class="r mono">{{ sig.ml_prob?.toFixed(2) ?? sig.ml_score?.toFixed(2) ?? '-' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 4. 最近交易 -->
    <div class="sec" v-if="store.trades.length" style="margin-top:8px;">
      <div class="sec-h">
        <div class="sec-t">交易记录</div>
        <span class="sec-m">最近 {{ store.trades.length }} 笔</span>
      </div>
      <table class="tbl">
        <thead>
          <tr>
            <th>时间</th>
            <th>名称</th>
            <th class="c">方向</th>
            <th class="r">价格</th>
            <th class="r">数量</th>
            <th class="r">金额</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(t, i) in store.trades" :key="t.order_id || i">
            <td class="tm">{{ t.trade_time ? t.trade_time.slice(0,16) : t.trade_date || '-' }}</td>
            <td>
              <span class="sn">{{ t.stock_name || '-' }}</span>
              <span class="scode">{{ shortCode(t.ts_code) }}</span>
            </td>
            <td class="c">
              <span :class="['tg', (t.action || '').includes('卖') ? 'tg-sell' : 'tg-buy']">
                {{ t.action || '-' }}
              </span>
            </td>
            <td class="r mono">{{ t.price ? t.price.toFixed(2) : '-' }}</td>
            <td class="r mono">{{ t.quantity ?? '-' }}</td>
            <td class="r mono">{{ t.amount ? fmtMoney(t.amount, false) : '-' }}</td>
            <td class="reason">{{ t.reason || '-' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 底部 -->
    <div class="ft">
      <span v-if="store.lastUpdated">更新 {{ store.lastUpdated }}</span>
      <span v-if="store.system.scanTime"> | 盘后扫描 {{ store.system.scanTime }}</span>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent])

const store = useDashboardStore()
const showChart = ref(false)
let timer = null

function toggleChart() { showChart.value = !showChart.value }

const marketLabel = computed(() => {
  const s = store.market.state_name || store.market.state || ''
  const m = { '强劲': '强势', '正常': '平稳', '弱势': '弱势', '恐慌': '恐慌' }
  return m[s] || s
})

const perfReturnLabel = computed(() => {
  const v = store.performanceSummary?.total_return ?? store.performance.total_return
  if (v == null) return '-'
  return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'
})

const winRateLabel = computed(() => {
  const v = store.performanceSummary?.win_rate ?? store.performance.win_rate
  if (v == null) return '-'
  return (v * 100).toFixed(1) + '%'
})

const chartOption = computed(() => {
  const d = store.navHistory
  if (!d.dates || !d.dates.length) return {}
  return {
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(30,30,46,0.92)',
      borderColor: '#2d2d3f',
      textStyle: { color: '#d4d4d8', fontSize: 12 },
      formatter: (params) => {
        const p = params[0]
        return `<div style="font-size:12px">${p.axisValue}</div><div style="font-size:13px;font-weight:700;color:#60a5fa">¥${Number(p.value).toLocaleString('zh-CN', {minimumFractionDigits:2})}</div>`
      },
    },
    grid: { left: 50, right: 16, top: 20, bottom: 24 },
    xAxis: {
      type: 'category',
      data: d.dates,
      axisLine: { lineStyle: { color: '#2d2d3f' } },
      axisLabel: { color: '#6b7280', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: '#27272a', type: 'dashed' } },
      axisLabel: { color: '#6b7280', fontSize: 10, formatter: (v) => '¥' + (v / 10000).toFixed(1) + '万' },
    },
    series: [{
      type: 'line',
      data: d.values,
      smooth: true,
      showSymbol: false,
      lineStyle: { width: 2, color: '#818cf8' },
      areaStyle: {
        color: {
          type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [
            { offset: 0, color: 'rgba(129,140,248,0.25)' },
            { offset: 1, color: 'rgba(129,140,248,0.02)' },
          ],
        },
      },
    }],
  }
})

function shortCode(tc) {
  if (!tc) return ''
  return tc.split('.')[0]
}

function fmtMoney(v, sym = true) {
  if (v == null) return '-'
  const n = Number(v)
  if (sym) {
    return '¥' + n.toLocaleString('zh-CN', { minimumFractionDigits: 2 })
  }
  return n.toLocaleString('zh-CN', { minimumFractionDigits: 2 })
}

function fmtPct(v) {
  if (v == null) return '-'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%'
}

function fmtSigned(v) {
  if (v == null) return '-'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(2)
}

function pnlCls(v) {
  if (v == null) return ''
  return Number(v) > 0 ? 'up' : (Number(v) < 0 ? 'dn' : '')
}

function pctClass(pct) {
  if (!pct || pct === '0.00%') return ''
  return pct.startsWith('+') ? 'up' : 'dn'
}

function scoreClass(s) {
  if (s == null) return ''
  if (s >= 80) return 'sc-hi'
  if (s >= 60) return 'sc-md'
  return 'sc-lo'
}

function shapely(v) {
  if (v == null) return '-'
  return v.toFixed(2)
}

onMounted(() => {
  store.refreshAll()
  timer = setInterval(() => store.refreshAll(), 30000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped>
.dashboard { position: relative; }

/* Error */
.error-bar { display: flex; align-items: center; gap: 6px; padding: 8px 14px; background: rgba(248,113,113,0.1); border: 1px solid rgba(248,113,113,0.2); border-radius: 6px; margin-bottom: 8px; font-size: 12px; color: #fca5a5; }
.err-icon { font-size: 14px; }
.err-close { margin-left: auto; background: none; border: none; color: #fca5a5; font-size: 16px; cursor: pointer; }

/* Load */
.load-mask { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; background: rgba(17,17,27,0.7); z-index: 10; font-size: 13px; color: #6b7280; }
.load-spin { width: 28px; height: 28px; border: 2px solid #2d2d3f; border-top-color: #818cf8; border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

.fade-enter-active, .fade-leave-active { transition: opacity 0.25s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* KPI */
.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 8px; }
.kpi { background: #1e1e2e; border: 1px solid #2d2d3f; border-radius: 8px; padding: 12px 14px; cursor: pointer; transition: border-color 0.15s; }
.kpi:hover { border-color: #3f3f5c; }
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

/* Chart */
.chart-wrap { margin-bottom: 8px; }
.chart-box { padding: 8px 12px; }

/* Section */
.sec { background: #1e1e2e; border: 1px solid #2d2d3f; border-radius: 8px; overflow: hidden; }
.sec-h { display: flex; align-items: center; padding: 9px 14px; border-bottom: 1px solid #2d2d3f; }
.sec-t { font-size: 12.5px; font-weight: 600; color: #d4d4d8; flex: 1; }
.sec-m { font-size: 11.5px; color: #a1a1aa; }
.sec-tg { display: inline-flex; font-size: 10px; color: #818cf8; background: rgba(129,140,248,0.1); padding: 2px 7px; border-radius: 4px; }
.sec-close { background: none; border: none; color: #6b7280; font-size: 18px; cursor: pointer; padding: 0 4px; }
.sec-b { padding: 0; }

.badge { display: inline-flex; align-items: center; justify-content: center; background: #2d2d3f; color: #a1a1aa; font-size: 10.5px; font-weight: 600; min-width: 18px; height: 16px; border-radius: 8px; padding: 0 5px; }

.main-g { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.cols { display: flex; flex-direction: column; gap: 8px; }

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
.sn { font-weight: 500; }
.scode { margin-left: 5px; color: #6b7280; font-size: 10.5px; }

/* Tag */
.tg { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; line-height: 1.4; }
.tg-p { background: rgba(96,165,250,0.15); color: #93c5fd; }
.tg-w { background: rgba(251,191,36,0.15); color: #fcd34d; }
.tg-buy { background: rgba(52,211,153,0.15); color: #6ee7b7; }
.tg-sell { background: rgba(248,113,113,0.15); color: #fca5a5; }

/* 概率条 */
.pb-wrap { display: inline-flex; align-items: center; gap: 5px; width: 100%; max-width: 150px; }
.pb-tr { flex: 1; height: 5px; background: #27272a; border-radius: 3px; overflow: hidden; }
.pb-fill { display: block; height: 100%; background: linear-gradient(90deg, #6366f1, #818cf8); border-radius: 3px; transition: width 0.4s; }
.pb-txt { font-size: 11px; font-weight: 600; color: #818cf8; white-space: nowrap; min-width: 35px; text-align: right; }

/* Score */
.score { display: inline-block; padding: 1px 5px; border-radius: 3px; font-size: 11.5px; font-weight: 700; }
.sc-hi { background: rgba(52,211,153,0.15); color: #34d399; }
.sc-md { background: rgba(251,191,36,0.15); color: #fbbf24; }
.sc-lo { background: rgba(248,113,113,0.15); color: #f87171; }

/* Color */
.up { color: #34d399; }
.dn { color: #f87171; }
.dim { color: #6b7280; }

/* Empty */
.empty { display: flex; flex-direction: column; align-items: center; gap: 5px; padding: 30px 20px; color: #52525b; }
.empty p { font-size: 12.5px; margin: 0; }
.empty-s { font-size: 10.5px; color: #3f3f5c; }

/* Footer */
.ft { display: flex; align-items: center; gap: 4px; font-size: 10.5px; color: #52525b; padding: 4px 0 6px; }

/* Responsive */
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
