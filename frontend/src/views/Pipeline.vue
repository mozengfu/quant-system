<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import MarketStateCard from '../components/pipeline/MarketStateCard.vue'
import MlPipelineCard from '../components/pipeline/MlPipelineCard.vue'
import SignalExecutionCard from '../components/pipeline/SignalExecutionCard.vue'
import PerformanceCard from '../components/pipeline/PerformanceCard.vue'
import { getPipelineStatus, getStrategyCompare } from '../api/market'
import { useWebSocket } from '../utils/ws'
import { useMarketStore } from '../stores/market'

const marketStore = useMarketStore()
const pipelineState = reactive({
  market: null,
  ml: { model: 'V11.0', rank_ic: null },
  performance: null,
})

const strategyCompare = ref(null)
const loadingCompare = ref(false)

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

async function fetchStrategyCompare() {
  loadingCompare.value = true
  try {
    strategyCompare.value = await getStrategyCompare()
  } catch (e) {
    console.error('[策略对比] 获取失败:', e)
  }
  loadingCompare.value = false
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
  fetchStrategyCompare()
  ws.connect()
  refreshTimer = setInterval(() => {
    fetchPipelineStatus()
    fetchStrategyCompare()
  }, 60000)
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

    <!-- 策略对比 -->
    <el-card class="pipeline-card" style="margin-top: 12px;">
      <template #header>
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <span>📊 策略对比</span>
          <el-button size="small" @click="fetchStrategyCompare">刷新</el-button>
        </div>
      </template>
      <div v-if="loadingCompare" style="text-align:center;padding:20px;color:#999;">加载中...</div>
      <div v-else-if="!strategyCompare || strategyCompare.error" style="text-align:center;padding:20px;color:#999;">
        {{ strategyCompare?.error || '暂无数据' }}
      </div>
      <template v-else>
        <el-row :gutter="12">
          <el-col :span="12" v-for="s in strategyCompare.strategies" :key="s.strategy">
            <el-card :shadow="s.strategy === strategyCompare.best_strategy ? 'always' : 'hover'"
                     :style="s.strategy === strategyCompare.best_strategy ? 'border:2px solid #67C23A;' : ''">
              <div style="font-size:16px;font-weight:600;margin-bottom:8px;">
                {{ s.strategy }}
                <el-tag v-if="s.strategy === strategyCompare.best_strategy" type="success" size="small" style="margin-left:6px;">最佳</el-tag>
              </div>
              <el-descriptions :column="2" size="small" border>
                <el-descriptions-item label="交易次数">{{ s.total_trades }}</el-descriptions-item>
                <el-descriptions-item label="持仓中">{{ s.holding }}</el-descriptions-item>
                <el-descriptions-item label="胜率">{{ s.win_rate }}%</el-descriptions-item>
                <el-descriptions-item label="平均收益">{{ s.avg_return_pct }}%</el-descriptions-item>
                <el-descriptions-item label="累计盈亏">
                  <span :style="{color:s.total_pnl>=0?'#f56c6c':'#67c23a'}">{{ s.total_pnl>=0?'+':'' }}{{ s.total_pnl }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="平均持有">{{ s.avg_hold_days }}天</el-descriptions-item>
                <el-descriptions-item label="最佳">{{ s.best_trade_pct }}%</el-descriptions-item>
                <el-descriptions-item label="最差">{{ s.worst_trade_pct }}%</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </template>
    </el-card>
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
