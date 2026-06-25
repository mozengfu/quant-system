<template>
  <div class="dashboard">
    <!-- 刷新通知 -->
    <div v-if="store.error" class="error-bar">
      <el-alert :title="store.error" type="error" show-icon :closable="true" @close="store.error = null" />
    </div>

    <!-- 状态卡片行 -->
    <el-row :gutter="16" class="card-row">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-header">市场状态</div>
          <div class="stat-body">
            <div class="stat-value">{{ store.market.state_name || store.market.state || '-' }}</div>
            <div class="stat-detail">
              <span v-if="store.market.change_pct != null" :class="store.market.change_pct >= 0 ? 'profit' : 'loss'">
                {{ store.market.change_pct >= 0 ? '+' : '' }}{{ store.market.change_pct }}%
              </span>
              <span v-if="store.market.position_ratio != null" style="margin-left:12px">
                仓位建议 {{ store.market.position_ratio }}%
              </span>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-header">账户资产</div>
          <div class="stat-body">
            <div class="stat-value account-value">{{ fmt(store.balance.总资产 || 0) }}</div>
            <div class="stat-detail">可用 {{ fmt(store.balance.可用金额 || 0) }}</div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-header">累计收益</div>
          <div class="stat-body">
            <div class="stat-value" :class="(store.performance.total_return || 0) >= 0 ? 'profit' : 'loss'">
              {{ store.performance.total_return != null ? (store.performance.total_return >= 0 ? '+' : '') + (store.performance.total_return * 100).toFixed(2) + '%' : '-' }}
            </div>
            <div class="stat-detail">
              夏普 {{ store.performance.sharpe != null ? store.performance.sharpe : '-' }}
              &nbsp;胜率 {{ store.performance.win_rate != null ? (store.performance.win_rate * 100).toFixed(1) + '%' : '-' }}
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-header">系统健康</div>
          <div class="stat-body">
            <div class="stat-value">
              <el-tag :type="store.system.qmtConnected ? 'success' : 'danger'" size="small" effect="dark">
                QMT {{ store.system.qmtConnected ? '在线' : '离线' }}
              </el-tag>
            </div>
            <div class="stat-detail">模型 {{ store.ml.status === 'normal' ? '正常' : store.ml.status }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 持仓表格 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="section-header">
          <span>持仓明细 ({{ positions.length }})</span>
          <span class="section-extra">
            总盈亏 <span :class="totalPnl >= 0 ? 'profit' : 'loss'">{{ totalPnl >= 0 ? '+' : '' }}{{ totalPnl.toFixed(2) }}</span>
          </span>
        </div>
      </template>
      <PositionTable :positions="positions" />
    </el-card>

    <!-- 候选 + 交易日志 两列 -->
    <el-row :gutter="16">
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="section-card">
          <template #header>
            <div class="section-header">
              <span>ML 候选 ({{ mlCandidates.length }})</span>
              <el-tag size="small" type="info" effect="plain">V11.2 板RPS</el-tag>
            </div>
          </template>
          <el-table :data="mlCandidates" stripe size="small" empty-text="暂无候选">
            <el-table-column label="代码" width="80">
              <template #default="{ row }">{{ row.代码 || row.ts_code || '-' }}</template>
            </el-table-column>
            <el-table-column label="名称" width="100">
              <template #default="{ row }">{{ row.名称 || row.name || '-' }}</template>
            </el-table-column>
            <el-table-column label="ML概率" width="90" align="right">
              <template #default="{ row }">
                <span class="profit">{{ (row.ML得分 || row.ml_prob || 0) >= 0.5 ? '✓' : '' }} {{ (row.ML得分 || row.ml_prob || 0).toFixed(3) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="现价" width="90" align="right">
              <template #default="{ row }">{{ (row.现价 || row.price || 0).toFixed(2) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
      <el-col :xs="24" :lg="12">
        <el-card shadow="never" class="section-card">
          <template #header>
            <div class="section-header">
              <span>最近交易 ({{ trades.length }})</span>
            </div>
          </template>
          <OrderHistory :trades="trades" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { useDashboardStore } from '../stores/dashboard'
import PositionTable from '../components/trading/PositionTable.vue'
import OrderHistory from '../components/trading/OrderHistory.vue'

const store = useDashboardStore()
const refreshInterval = ref(null)

const positions = computed(() => store.positions)
const mlCandidates = computed(() => store.mlCandidates)
const trades = computed(() => store.trades)
const totalPnl = computed(() => store.totalPnl)

function fmt(v) {
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function startRefresh() {
  store.refreshAll()
  refreshInterval.value = setInterval(() => {
    store.refreshAll()
  }, 60000)
}

onMounted(() => {
  startRefresh()
})

onUnmounted(() => {
  if (refreshInterval.value) {
    clearInterval(refreshInterval.value)
    refreshInterval.value = null
  }
})
</script>

<style scoped>
.dashboard {
  max-width: 1400px;
  margin: 0 auto;
}
.card-row {
  margin-bottom: 16px;
}
.stat-card {
  border-radius: 4px;
  margin-bottom: 16px;
}
.stat-card :deep(.el-card__body) {
  padding: 16px 20px;
}
.stat-header {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}
.stat-body {
  line-height: 1.4;
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 4px;
}
.stat-detail {
  font-size: 12px;
  color: #909399;
}
.account-value {
  color: #409eff;
}
.section-card {
  border-radius: 4px;
  margin-bottom: 16px;
}
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  font-size: 14px;
}
.section-extra {
  font-size: 12px;
  font-weight: normal;
  color: #606266;
}
.error-bar {
  margin-bottom: 12px;
}
</style>
