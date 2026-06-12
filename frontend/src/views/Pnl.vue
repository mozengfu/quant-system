<script setup>
import { ref, onMounted, computed } from 'vue'
import { ElMessage } from 'element-plus'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { getPnlSummary } from '../api/data'

use([CanvasRenderer, LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent])

const loading = ref(false)
const summary = ref(null)
const positions = ref([])
const curve = ref([])

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

async function fetchPnl() {
  loading.value = true
  try {
    const r = await getPnlSummary()
    summary.value = r.summary
    positions.value = r.positions || []
    curve.value = r.curve || []
  } catch (e) {
    ElMessage.error('盈亏数据加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => fetchPnl())

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

@media (max-width: 767px) {
  .card-value { font-size: 16px; }
  .chart-nav { height: 220px; }
  .chart-daily { height: 160px; }
  .summary-cards .el-card { margin-bottom: 0; }
}
</style>
