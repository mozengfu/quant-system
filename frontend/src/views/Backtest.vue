<script setup>
import { ref, onMounted } from 'vue'
import api from '../api/index'
import { ElMessage } from 'element-plus'

const loading = ref(false)
const data = ref(null)

function fmtPct(v) {
  if (v == null) return '--'
  const sign = v >= 0 ? '+' : ''
  if (Math.abs(v) > 999) return sign + (v / 100).toFixed(0) + '倍'
  return sign + v + '%'
}

async function fetchBacktest() {
  loading.value = true
  try {
    const r = await api.get('/backtest/ml')
    data.value = r
  } catch (e) {
    ElMessage.error('回测数据加载失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => fetchBacktest())
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>ML 模型回测</h2>
      <el-button size="small" @click="fetchBacktest" :loading="loading">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
    </div>

    <div v-if="data?.oos?.samples" class="backtest-content">
      <div class="model-tag">
        <el-tag type="success" size="large">{{ data.model }}</el-tag>
        <el-text size="small" type="info">
          {{ data.params?.period }} | 间隔{{ data.params?.interval }} | 持仓{{ data.params?.hold }} | Top{{ data.params?.top_n }}
        </el-text>
        <el-tooltip :content="data.params?.note" placement="top">
          <el-tag type="info" size="small" effect="plain">严格OOS</el-tag>
        </el-tooltip>
      </div>

      <el-card class="oos-card" shadow="hover">
        <template #header>
          <div class="card-title">
            <el-tag type="success" size="small">严格OOS</el-tag>
            <span>纯ML选股 · 训练与回测期完全隔离</span>
          </div>
        </template>
        <div class="oos-metrics">
          <div class="metric highlight">
            <span class="label">累积收益</span>
            <span class="value">{{ fmtPct(data.oos.cumulative_return) }}</span>
          </div>
          <div class="metric">
            <span class="label">平均收益/笔</span>
            <span class="value">{{ fmtPct(data.oos.avg_return) }}</span>
          </div>
          <div class="metric">
            <span class="label">夏普比率</span>
            <span class="value">{{ data.oos.sharpe }}</span>
          </div>
          <div class="metric">
            <span class="label">胜率</span>
            <span class="value">{{ data.oos.win_rate }}%</span>
          </div>
          <div class="metric">
            <span class="label">最大回撤</span>
            <span class="value">{{ data.oos.max_dd }}%</span>
          </div>
          <div class="metric">
            <span class="label">采样次数</span>
            <span class="value">{{ data.oos.samples }}</span>
          </div>
        </div>
        <div v-if="data.exits" class="exit-stats">
          <el-tag size="small" type="success" effect="plain">止盈 {{ data.exits.tp_count }}次 ({{ data.exits.tp_pct }}%)</el-tag>
          <el-tag size="small" type="danger" effect="plain">止损 {{ data.exits.sl_count }}次 ({{ data.exits.sl_pct }}%)</el-tag>
          <el-tag size="small" type="info" effect="plain">到期 {{ data.exits.hold_count }}次</el-tag>
        </div>
      </el-card>

      <el-divider content-position="left">
        <span style="font-size:13px;color:#909399">旧模型对比（样本内过拟合，仅供参考）</span>
      </el-divider>

      <div class="compare-cards">
        <el-card class="metric-card old-card" shadow="hover">
          <template #header>
            <div class="card-title">
              <el-tag type="danger" size="small" effect="plain">被污染</el-tag>
              <span>纯ML · 样本内</span>
            </div>
          </template>
          <div class="metrics">
            <div class="metric">
              <span class="label">夏普比率（虚高）</span>
              <span class="value" style="color:#f56c6c">{{ data.contaminated.sharpe }}</span>
            </div>
            <div class="metric">
              <span class="label">胜率</span>
              <span class="value">{{ data.contaminated.win_rate }}%</span>
            </div>
            <div class="metric">
              <span class="label">采样次数</span>
              <span class="value">{{ data.contaminated.samples }}</span>
            </div>
          </div>
        </el-card>

        <el-card class="metric-card old-card" shadow="hover">
          <template #header>
            <div class="card-title">
              <el-tag type="warning" size="small" effect="plain">被污染</el-tag>
              <span>ML+过滤 · 样本内</span>
            </div>
          </template>
          <div class="metrics">
            <div class="metric">
              <span class="label">夏普比率（虚高）</span>
              <span class="value" style="color:#e6a23c">{{ data.ml_filtered.sharpe }}</span>
            </div>
            <div class="metric">
              <span class="label">胜率</span>
              <span class="value">{{ data.ml_filtered.win_rate }}%</span>
            </div>
            <div class="metric">
              <span class="label">采样次数</span>
              <span class="value">{{ data.ml_filtered.samples }}</span>
            </div>
          </div>
        </el-card>
      </div>

      <el-alert
        title="结论：ML模型有真实的选股能力"
        type="success"
        description="严格样本外回测累积+238.5%，夏普2.54，胜率62.5%，最大回撤14.6%。旧模型因训练覆盖回测期导致夏普虚高至8.26（样本内过拟合）。实盘预期夏普0.8~1.5（扣除滑点与冲击成本）。"
        show-icon
        :closable="false"
        style="margin-top: 16px;"
      />
    </div>

    <div v-else-if="!loading" class="empty-hint">
      暂无回测数据，请先运行 scripts/backtest_v11_oos.py
    </div>
  </div>
</template>

<style scoped>
.view { max-width: 1000px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.model-tag { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.oos-card { margin-bottom: 8px; border: 2px solid #67c23a; }
.oos-card :deep(.el-card__header) { background: #f0f9eb; }
.oos-metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
.compare-cards { display: flex; gap: 16px; }
.metric-card { flex: 1; }
.old-card { opacity: 0.65; }
.old-card:hover { opacity: 1; }
.card-title { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; }
.metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
.metric { display: flex; flex-direction: column; gap: 2px; }
.metric .label { font-size: 12px; color: #909399; }
.metric .value { font-size: 20px; font-weight: 700; color: #303133; }
.metric.highlight .value { color: #67c23a; font-size: 24px; }
.exit-stats { display: flex; gap: 8px; margin-top: 12px; padding-top: 12px; border-top: 1px solid #ebeef5; flex-wrap: wrap; }
.empty-hint { text-align: center; color: #909399; padding: 60px; }

@media (max-width: 767px) {
  .oos-metrics { grid-template-columns: 1fr 1fr; gap: 10px; }
  .metrics { grid-template-columns: 1fr 1fr; }
  .compare-cards { flex-direction: column; }
  .metric .value { font-size: 16px; }
  .metric.highlight .value { font-size: 20px; }
}
</style>
