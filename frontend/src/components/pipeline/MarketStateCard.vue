<script setup>
import { computed, onMounted, ref } from 'vue'
import { getMarketPremarket } from '../../api/market'

const props = defineProps({
  state: Object,
})

const premarket = ref(null)
const loading = ref(false)

const stateTagType = computed(() => {
  const s = props.state?.state_name || props.state?.state || ''
  if (s === '恐慌' || s === '恐慌清仓' || s === 'panic' || s === 'fear') return 'danger'
  if (s === '阻断' || s === 'block') return 'warning'
  if (s === '逆市') return 'warning'
  if (s === '偏弱' || s === 'weak') return 'info'
  if (s === '常态' || s === 'normal') return 'success'
  return 'info'
})

const stateLabel = computed(() => {
  return props.state?.state_name || props.state?.state || '加载中...'
})

// 指数数据：优先用 premarket API，降级到 pipeline state
const shIndex = computed(() => premarket.value?.data?.indices?.['上证指数'])
const indexValue = computed(() => shIndex.value?.最新价 ?? props.state?.index ?? '--')
const indexChange = computed(() => shIndex.value?.涨跌幅 ?? props.state?.change_pct ?? 0)

async function fetchPremarket() {
  loading.value = true
  try {
    premarket.value = await getMarketPremarket()
  } finally {
    loading.value = false
  }
}

onMounted(fetchPremarket)
</script>

<template>
  <el-card shadow="never" class="pipeline-card">
    <template #header>
      <div class="card-header">
        <span><el-icon><TrendCharts /></el-icon> 阶段 1: 市场感知</span>
        <div class="card-header-right">
          <el-tag :type="stateTagType" effect="dark" size="small">
            {{ stateLabel }}
          </el-tag>
          <el-button text size="small" @click="fetchPremarket">刷新</el-button>
        </div>
      </div>
    </template>
    <el-row :gutter="16">
      <el-col :span="12">
        <div class="stat-item">
          <div class="stat-label">上证指数</div>
          <div class="stat-value" :class="indexChange >= 0 ? 'profit' : 'loss'">
            {{ typeof indexValue === 'number' ? indexValue.toFixed(2) : indexValue }}
          </div>
          <div class="stat-sub" :class="indexChange >= 0 ? 'profit' : 'loss'">
            {{ indexChange >= 0 ? '+' : '' }}{{ Number(indexChange).toFixed(2) }}%
          </div>
        </div>
      </el-col>
      <el-col :span="12">
        <div class="stat-item">
          <div class="stat-label">仓位建议</div>
          <div class="stat-value" :class="(state?.position_ratio || 100) < 50 ? 'loss' : 'profit'">
            {{ state?.position_ratio ?? '--' }}%
          </div>
          <div class="stat-sub">更新: {{ state?.update_time ? state.update_time.slice(0, 16) : '--' }}</div>
        </div>
      </el-col>
    </el-row>
  </el-card>
</template>

<style scoped>
.stat-item { text-align: center; padding: 8px 0; }
.stat-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.stat-value { font-size: 24px; font-weight: 700; }
.stat-sub { font-size: 12px; margin-top: 2px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.card-header-right { display: flex; align-items: center; gap: 8px; }
</style>
