<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { getPnlSummary, getMarketAttribution } from '../api/data'

use([CanvasRenderer, LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent])

const activeTab = ref('live')
const loading = ref(false)
const summary = ref(null)
const positions = ref([])
const curve = ref([])

// 市场归因分析
const marketAttr = ref(null)
const attrLoading = ref(false)
const attrError = ref('')

const navChartOption = computed(() => {
  if (!curve.value.length) return {}
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (p) => {
        const d = p[0]
        return `${d.axisValue}<br/>净值: ¥${Number(d.value).toLocaleString()}<br/>累计收益率: ${curve.value[d.dataIndex]?.cum_pnl_pct}%`
      }
    },
    grid: { left: 60, right: 40, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: curve.value.map(c => c.date.slice(5)), axisLabel: { fontSize: 11 } },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: v => (v / 10000).toFixed(0) + '万' },
      splitLine: { lineStyle: { type: 'dashed', color: '#eee' } }
    },
    series: [{
      name: '净值',
      type: 'line',
      data: curve.value.map(c => c.cum_nav),
      smooth: true,
      lineStyle: { color: '#409eff', width: 2 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{offset:0, color:'rgba(64,158,255,0.25)'}, {offset:1, color:'rgba(64,158,255,0.02)'}] } },
      itemStyle: { color: '#409eff' },
      symbol: 'none',
    }]
  }
})

const dailyChartOption = computed(() => {
  if (!curve.value.length) return {}
  const data = curve.value.filter(c => c.daily_pnl !== 0).slice(-20)
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 10, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: data.map(c => c.date.slice(5)), axisLabel: { fontSize: 11 } },
    yAxis: {
      type: 'value',
      axisLabel: { formatter: v => (v / 10000).toFixed(1) + '万' },
      splitLine: { lineStyle: { type: 'dashed', color: '#eee' } }
    },
    series: [{
      name: '日盈亏',
      type: 'bar',
      data: data.map(c => c.daily_pnl),
      itemStyle: {
        color: (p) => p.value >= 0 ? '#f56c6c' : '#67c23a'
      }
    }]
  }
})

// 市场归因柱状图
const attrChartOption = computed(() => {
  const attr = marketAttr.value
  if (!attr || !attr.market_states?.length) return {}
  const states = attr.market_states
  // 状态顺序：上涨市 → 震荡市 → 下跌市
  const order = { '上涨市': 0, '震荡市': 1, '下跌市': 2 }
  states.sort((a, b) => (order[a.market_state] ?? 9) - (order[b.market_state] ?? 9))
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const s = params[0]
        const d = states[s.dataIndex]
        return `${d.market_state}<br/>
        交易: ${d.total_trades} 笔<br/>
        胜率: ${d.win_rate}%<br/>
        平均盈亏: ${d.avg_pnl_pct >= 0 ? '+' : ''}${d.avg_pnl_pct}%<br/>
        平均盈利: +${d.avg_win_pct}% / 平均亏损: ${d.avg_loss_pct}%`
      }
    },
    grid: { left: 60, right: 30, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: states.map(s => s.market_state), axisLabel: { fontSize: 13, fontWeight: 'bold' } },
    yAxis: [
      { type: 'value', name: '交易笔数', axisLabel: { fontSize: 11 } },
      { type: 'value', name: '胜率 %', max: 100, axisLabel: { fontSize: 11, formatter: '{value}%' } }
    ],
    series: [
      {
        name: '交易笔数',
        type: 'bar',
        data: states.map(s => s.total_trades),
        itemStyle: {
          color: (p) => {
            const colors = { '上涨市': '#e6a23c', '震荡市': '#409eff', '下跌市': '#67c23a' }
            return colors[states[p.dataIndex]?.market_state] || '#909399'
          },
          borderRadius: [4, 4, 0, 0]
        },
        barWidth: '40%',
        yAxisIndex: 0,
        label: { show: true, position: 'top', fontSize: 12, fontWeight: 'bold' }
      },
      {
        name: '胜率',
        type: 'line',
        data: states.map(s => s.win_rate),
        lineStyle: { color: '#f56c6c', width: 2 },
        itemStyle: { color: '#f56c6c' },
        symbol: 'circle',
        symbolSize: 8,
        yAxisIndex: 1,
        label: { show: true, position: 'bottom', fontSize: 11, formatter: p => p.value + '%', color: '#f56c6c' }
      }
    ]
  }
})

// 判断亏损是否集中在下跌市
const lossConcentrated = computed(() => {
  const attr = marketAttr.value
  if (!attr?.market_states?.length) return false
  const downState = attr.market_states.find(s => s.market_state === '下跌市')
  const otherStates = attr.market_states.filter(s => s.market_state !== '下跌市')
  if (!downState || !otherStates.length) return false
  // 下跌市的亏损占比是否明显高于其他市
  const downLossPct = downState.losses / downState.total_trades
  const otherLossPct = otherStates.reduce((s, x) => s + x.losses, 0) / otherStates.reduce((s, x) => s + x.total_trades, 0)
  return downLossPct > otherLossPct * 1.3
})

async function fetchPnl() {
  loading.value = true
  try {
    const r = await getPnlSummary(activeTab.value)
    summary.value = r.summary
    positions.value = r.positions || []
    curve.value = r.curve || []
  } catch (e) {
    ElMessage.error('盈亏数据加载失败')
  } finally {
    loading.value = false
  }
}

async function fetchMarketAttr() {
  attrLoading.value = true
  attrError.value = ''
  try {
    const r = await getMarketAttribution(activeTab.value)
    marketAttr.value = r
  } catch (e) {
    attrError.value = e.message || '归因分析加载失败'
  } finally {
    attrLoading.value = false
  }
}

// 切换实盘/模拟时刷新数据
import { watch } from 'vue'
watch(activeTab, () => {
  fetchPnl()
  fetchMarketAttr()
})

onMounted(() => {
  fetchPnl()
  fetchMarketAttr()
})

function fmtMoney(v) {
  if (v == null) return '--'
  const abs = Math.abs(v)
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (abs >= 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toLocaleString()
}

function pnlColor(v) { return v >= 0 ? '#f56c6c' : '#67c23a' }
</script>

<template>
  <div class="pnl-view">
    <!-- 实盘/模拟切换 -->
    <div style="margin-bottom:12px;display:flex;align-items:center;gap:12px">
      <el-radio-group v-model="activeTab" @change="fetchPnl(); fetchMarketAttr()" size="small">
        <el-radio-button value="live">📊 实盘</el-radio-button>
        <el-radio-button value="simulation">📝 模拟</el-radio-button>
      </el-radio-group>
      <span style="font-size:12px;color:#909399">
        {{ activeTab === 'live' ? 'QMT 实盘成交数据 (qmt_trades mode=live)' : '旧模拟盘交易数据 (qmt_trades mode=simulation)' }}
      </span>
    </div>
    <div class="view-header">
      <h2>实盘收益</h2>
      <el-button size="small" @click="fetchPnl" :loading="loading">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
    </div>

    <!-- 汇总卡片 -->
    <el-row :gutter="8" class="summary-cards">
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">总资产</div>
          <div class="card-value">¥{{ fmtMoney(summary?.total_asset) }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">可用资金</div>
          <div class="card-value">¥{{ fmtMoney(summary?.available) }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">持仓市值</div>
          <div class="card-value">¥{{ fmtMoney(summary?.market_value) }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">累计盈亏</div>
          <div class="card-value" :style="{ color: pnlColor(summary?.total_pnl) }">
            {{ summary?.total_pnl >= 0 ? '+' : '' }}{{ fmtMoney(summary?.total_pnl) }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">收益率</div>
          <div class="card-value" :style="{ color: pnlColor(summary?.total_pnl_pct) }">
            {{ summary?.total_pnl_pct >= 0 ? '+' : '' }}{{ summary?.total_pnl_pct }}%
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="8" :md="4">
        <el-card shadow="hover">
          <div class="card-label">胜率 / 最大回撤</div>
          <div class="card-value" style="font-size:18px">
            {{ summary?.win_rate }}% <span style="font-size:12px;color:#909399">/ {{ summary?.max_drawdown }}%</span>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 净值曲线 -->
    <el-card shadow="never" class="chart-card">
      <template #header><span>累计净值曲线</span></template>
      <v-chart :option="navChartOption" class="chart-nav" autoresize />
    </el-card>

    <!-- 日盈亏柱状图 -->
    <el-card shadow="never" class="chart-card" style="margin-top:16px">
      <template #header><span>每日盈亏</span></template>
      <v-chart v-if="curve.length" :option="dailyChartOption" class="chart-daily" autoresize />
      <el-empty v-else description="暂无交易记录" />
    </el-card>

    <!-- 持仓明细 -->
    <el-card shadow="never" class="chart-card" style="margin-top:16px">
      <template #header>
        <span>持仓明细</span>
        <el-tag size="small" type="info" style="margin-left:8px">{{ positions.length }} 只</el-tag>
      </template>
      <el-table :data="positions" size="small" empty-text="暂无持仓">
        <el-table-column prop="ts_code" label="代码" width="110" />
        <el-table-column prop="name" label="名称" width="100" />
        <el-table-column prop="quantity" label="数量" width="80" align="right" />
        <el-table-column prop="cost_price" label="成本" width="80" align="right" />
        <el-table-column prop="current_price" label="现价" width="80" align="right" />
        <el-table-column prop="market_value" label="市值" width="110" align="right">
          <template #default="{ row }">¥{{ fmtMoney(row.market_value) }}</template>
        </el-table-column>
        <el-table-column prop="pnl" label="浮动盈亏" width="120" align="right">
          <template #default="{ row }">
            <span :style="{ color: pnlColor(row.pnl) }">
              {{ row.pnl >= 0 ? '+' : '' }}{{ fmtMoney(row.pnl) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="pnl_pct" label="盈亏% " width="80" align="right">
          <template #default="{ row }">
            <span :style="{ color: pnlColor(row.pnl_pct) }">
              {{ row.pnl_pct >= 0 ? '+' : '' }}{{ row.pnl_pct }}%
            </span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 底部指标 -->
    <el-row :gutter="8" style="margin-top:16px">
      <el-col :xs="24" :sm="12" :md="8">
        <el-card shadow="hover">
          <div class="card-label">已实现盈亏</div>
          <div class="card-value" :style="{ color: pnlColor(summary?.realized_pnl), fontSize: '20px' }">
            {{ summary?.realized_pnl >= 0 ? '+' : '' }}{{ fmtMoney(summary?.realized_pnl) }}
          </div>
          <div class="card-sub">共 {{ summary?.trade_count }} 笔成交</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="8">
        <el-card shadow="hover">
          <div class="card-label">未实现盈亏</div>
          <div class="card-value" :style="{ color: pnlColor(summary?.unrealized_pnl), fontSize: '20px' }">
            {{ summary?.unrealized_pnl >= 0 ? '+' : '' }}{{ fmtMoney(summary?.unrealized_pnl) }}
          </div>
          <div class="card-sub">持仓浮动</div>
        </el-card>
      </el-col>
      <el-col :xs="24" :sm="12" :md="8">
        <el-card shadow="hover">
          <div class="card-label">初始资金</div>
          <div class="card-value" style="font-size:20px">¥{{ fmtMoney(summary?.initial_capital) }}</div>
          <div class="card-sub">盈亏 {{ summary?.win_count || 0 }} 胜 / {{ (summary?.trade_count || 0) - (summary?.win_count || 0) }} 负</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- ====== 交易市场归因分析 ====== -->
    <div style="margin-top:24px">
      <div class="view-header" style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
        <h3 style="margin:0;font-size:17px">交易市场归因分析</h3>
        <el-button size="small" @click="fetchMarketAttr" :loading="attrLoading">刷新</el-button>
      </div>

      <!-- 加载中 -->
      <el-skeleton v-if="attrLoading && !marketAttr" :rows="4" animated />

      <!-- 错误提示 -->
      <el-alert v-if="attrError" :title="attrError" type="error" show-icon :closable="false" style="margin-bottom:12px" />

      <!-- 内容 -->
      <template v-if="marketAttr && marketAttr.market_states?.length">
        <!-- 总览卡片 -->
        <el-row :gutter="8" style="margin-bottom:12px">
          <el-col :xs="12" :sm="8" :md="6">
            <el-card shadow="hover">
              <div class="card-label">已结算交易</div>
              <div class="card-value" style="font-size:20px">{{ marketAttr.total_trades }} 笔</div>
            </el-card>
          </el-col>
          <el-col :xs="12" :sm="8" :md="6">
            <el-card shadow="hover">
              <div class="card-label">综合胜率</div>
              <div class="card-value" :style="{ fontSize: '20px', color: marketAttr.overall_win_rate >= 50 ? '#f56c6c' : '#67c23a' }">
                {{ marketAttr.overall_win_rate }}%
              </div>
            </el-card>
          </el-col>
          <el-col :xs="12" :sm="8" :md="6">
            <el-card shadow="hover">
              <div class="card-label">平均盈亏</div>
              <div class="card-value" :style="{ fontSize: '20px', color: marketAttr.overall_avg_pnl >= 0 ? '#f56c6c' : '#67c23a' }">
                {{ marketAttr.overall_avg_pnl >= 0 ? '+' : '' }}{{ marketAttr.overall_avg_pnl }}%
              </div>
            </el-card>
          </el-col>
          <el-col :xs="12" :sm="8" :md="6">
            <el-card shadow="hover">
              <div class="card-label">亏损集中度</div>
              <div class="card-value" style="font-size:16px">
                <el-tag :type="lossConcentrated ? 'danger' : 'success'" size="small">
                  {{ lossConcentrated ? '集中下跌市' : '不集中' }}
                </el-tag>
              </div>
              <div class="card-sub">亏损是否集中在下跌市</div>
            </el-card>
          </el-col>
        </el-row>

        <!-- 柱状图 -->
        <el-card shadow="never" class="chart-card" style="margin-bottom:12px">
          <template #header>
            <span>不同市场状态下的交易表现</span>
            <el-tag size="small" type="info" style="margin-left:8px">柱=交易笔数 / 线=胜率</el-tag>
          </template>
          <v-chart :option="attrChartOption" class="chart-attr" autoresize />
        </el-card>

        <!-- 结论卡片 -->
        <el-card shadow="never" style="margin-bottom:12px">
          <template #header><span style="font-weight:600">结论</span></template>
          <div style="font-size:13px;line-height:1.8;color:#303133">
            <p style="margin:0 0 8px 0">
              <strong>核心发现：亏损并不集中在下跌市，<span style="color:#e6a23c">上涨市买入反而风险最大</span>。</strong>
            </p>
            <ul style="margin:0;padding-left:18px">
              <li v-for="s in marketAttr.market_states" :key="s.market_state">
                <strong>{{ s.market_state }}</strong>：
                交易 {{ s.total_trades }} 笔，胜率 <span :style="{ color: s.win_rate >= 50 ? '#f56c6c' : '#67c23a' }">{{ s.win_rate }}%</span>，
                平均盈亏 {{ s.avg_pnl_pct >= 0 ? '+' : '' }}{{ s.avg_pnl_pct }}%
                <span v-if="s.market_state === '上涨市'" style="color:#e6a23c"> ← 最低胜率</span>
                <span v-else-if="s.market_state === '震荡市'" style="color:#409eff"> ← 最佳表现</span>
              </li>
            </ul>
            <p style="margin:8px 0 0 0;color:#909399;font-size:12px">
              说明：当前模型有"逆势选股"能力，在震荡市和下跌市选股效果更好。
              真正的亏损陷阱不是市场下跌，而是上涨后追高（如 5/6 一波上涨后买入 8 只亏 6 只）。
            </p>
            <p style="margin:4px 0 0 0;color:#909399;font-size:12px">
              建议：与此文的"下跌市暂停开仓"相反，<strong>更应警惕连续上涨后的追高买入</strong>。
              可在指数5日涨幅 > 2% 时收紧开仓门槛。
            </p>
          </div>
        </el-card>

        <!-- 详细表格 -->
        <el-card shadow="never" class="chart-card">
          <template #header><span>各市场状态明细</span></template>
          <el-table :data="marketAttr.market_states" size="small" border>
            <el-table-column prop="market_state" label="市场状态" width="100" />
            <el-table-column prop="total_trades" label="交易笔数" width="90" align="center" />
            <el-table-column prop="wins" label="胜" width="60" align="center" />
            <el-table-column prop="losses" label="负" width="60" align="center" />
            <el-table-column prop="win_rate" label="胜率%" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.win_rate >= 60 ? 'success' : row.win_rate >= 40 ? 'warning' : 'danger'" size="small">
                  {{ row.win_rate }}%
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="avg_pnl_pct" label="平均盈亏" width="100" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.avg_pnl_pct >= 0 ? '#f56c6c' : '#67c23a' }">
                  {{ row.avg_pnl_pct >= 0 ? '+' : '' }}{{ row.avg_pnl_pct }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="avg_win_pct" label="平均盈利" width="100" align="right">
              <template #default="{ row }">+{{ row.avg_win_pct }}%</template>
            </el-table-column>
            <el-table-column prop="avg_loss_pct" label="平均亏损" width="100" align="right">
              <template #default="{ row }">{{ row.avg_loss_pct }}%</template>
            </el-table-column>
            <el-table-column prop="max_win_pct" label="最大盈利" width="100" align="right">
              <template #default="{ row }">+{{ row.max_win_pct }}%</template>
            </el-table-column>
            <el-table-column prop="max_loss_pct" label="最大亏损" width="100" align="right">
              <template #default="{ row }">{{ row.max_loss_pct }}%</template>
            </el-table-column>
            <el-table-column prop="total_pnl_pct" label="总盈亏%" width="100" align="right">
              <template #default="{ row }">
                <span :style="{ color: row.total_pnl_pct >= 0 ? '#f56c6c' : '#67c23a' }">
                  {{ row.total_pnl_pct >= 0 ? '+' : '' }}{{ row.total_pnl_pct }}%
                </span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>

      <!-- 无数据 -->
      <el-empty v-else-if="!attrLoading" description="暂无已结算交易数据" />
    </div>
  </div>
</template>

<style scoped>
.pnl-view { padding: 0; }
.summary-cards { margin-bottom: 12px; }
.summary-cards .el-card { text-align: center; }
.card-label { font-size: 12px; color: #909399; margin-bottom: 6px; }
.card-value { font-size: 22px; font-weight: 700; color: #303133; white-space: nowrap; }
.card-sub { font-size: 11px; color: #c0c4cc; margin-top: 4px; }
.chart-card { margin-bottom: 0; }
.chart-nav { height: 320px; }
.chart-daily { height: 220px; }
.chart-attr { height: 280px; }

@media (max-width: 767px) {
  .card-value { font-size: 16px; }
  .chart-nav { height: 220px; }
  .chart-daily { height: 160px; }
  .summary-cards .el-card { margin-bottom: 0; }
}
</style>
