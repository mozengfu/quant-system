<script setup>
import { onMounted, onUnmounted, reactive } from 'vue'
import MarketStateCard from '../components/pipeline/MarketStateCard.vue'
import MlPipelineCard from '../components/pipeline/MlPipelineCard.vue'
import SignalExecutionCard from '../components/pipeline/SignalExecutionCard.vue'
import PerformanceCard from '../components/pipeline/PerformanceCard.vue'
import { getPipelineStatus } from '../api/market'
import { useWebSocket } from '../utils/ws'
import { useMarketStore } from '../stores/market'

const marketStore = useMarketStore()
const pipelineState = reactive({
  market: null,
  ml: { model: 'V11.0', rank_ic: null },
  performance: null,
})

let refreshTimer = null

async function fetchPipelineStatus() {
  try {
    const r = await getPipelineStatus()
    if (r?.market) pipelineState.market = r.market
    if (r?.ml) pipelineState.ml = { ...pipelineState.ml, ...r.ml }
    if (r?.performance) pipelineState.performance = r.performance
  } catch (e) {
    console.error('[Pipeline] 管线状态获取失败:', e.message || e)
  }
}

// WebSocket 实时更新
const ws = useWebSocket(
  `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/ws`,
  {
    market_update: (data) => {
      pipelineState.market = data
      marketStore.applyWsUpdate(data)
    },
    predict_done: (data) => {
      if (data?.ml) pipelineState.ml = { ...pipelineState.ml, ...data.ml }
    },
  },
)

onMounted(() => {
  fetchPipelineStatus()
  ws.connect()
  refreshTimer = setInterval(fetchPipelineStatus, 60000)
})

onUnmounted(() => {
  ws.disconnect()
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="pipeline-view">
    <div class="view-header">
      <h2>管线工作台</h2>
      <el-button size="small" @click="fetchPipelineStatus">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
    </div>
    <el-steps :active="4" simple class="pipeline-steps" style="margin-bottom: 16px;">
      <el-step title="市场感知" icon="TrendCharts" />
      <el-step title="ML 推理" icon="DataAnalysis" />
      <el-step title="信号执行" icon="Connection" />
      <el-step title="绩效追踪" icon="DataBoard" />
    </el-steps>
    <div class="pipeline-cards">
      <MarketStateCard :state="pipelineState.market" />
      <MlPipelineCard :state="pipelineState.ml" />
      <SignalExecutionCard />
      <PerformanceCard :state="pipelineState.performance" />
    </div>
  </div>
</template>

<style scoped>
.pipeline-view { max-width: 1200px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.pipeline-cards { display: flex; flex-direction: column; gap: 12px; }
:deep(.pipeline-card) { border-radius: 8px; }
:deep(.el-card__header) { padding: 12px 16px; font-size: 14px; font-weight: 600; }
</style>
