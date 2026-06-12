<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { getPerformanceSummary, getNavHistory } from '../../api/data'
import { fmtPct, fmtPrice } from '../../utils/format'

const perf = ref(null)
const loading = ref(false)

async function fetchData() {
  loading.value = true
  try {
    const [perfRes] = await Promise.allSettled([
      getPerformanceSummary(),
    ])
    if (perfRes.status === 'fulfilled') perf.value = perfRes.value
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<template>
  <el-card shadow="never" class="pipeline-card">
    <template #header>
      <div class="card-header">
        <span><el-icon><DataBoard /></el-icon> 阶段 4: 绩效追踪</span>
        <el-button text size="small" @click="fetchData">刷新</el-button>
      </div>
    </template>
    <el-row :gutter="16">
      <el-col :span="6">
        <div class="stat-item">
          <div class="stat-label">月收益</div>
          <div class="stat-value" :class="(perf?.month_return || 0) >= 0 ? 'profit' : 'loss'">
            {{ fmtPct(perf?.month_return) }}
          </div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-item">
          <div class="stat-label">夏普比率</div>
          <div class="stat-value" :class="(perf?.sharpe || 0) >= 1.5 ? 'profit' : 'neutral'">
            {{ perf?.sharpe?.toFixed(2) || '--' }}
          </div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-item">
          <div class="stat-label">胜率</div>
          <div class="stat-value">{{ perf?.win_rate ? (perf.win_rate * 100).toFixed(1) + '%' : '--' }}</div>
        </div>
      </el-col>
      <el-col :span="6">
        <div class="stat-item">
          <div class="stat-label">最大回撤</div>
          <div class="stat-value loss">{{ perf?.max_drawdown ? (-Math.abs(perf.max_drawdown)).toFixed(1) + '%' : '--' }}</div>
        </div>
      </el-col>
    </el-row>
  </el-card>
</template>

<style scoped>
.stat-item { text-align: center; padding: 8px 0; }
.stat-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.stat-value { font-size: 22px; font-weight: 700; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
</style>
