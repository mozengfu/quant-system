<script setup>
import { ref, onMounted, computed } from 'vue'
import { fmtPrice, fmtPct, profitClass } from '../../utils/format'
import { useTradingStore } from '../../stores/trading'
import api from '../../api'

const trading = useTradingStore()
const qmtPositions = ref([])
const todayTrades = ref([])
const loading = ref(false)

const totalProfit = computed(() => qmtPositions.value.reduce((s, p) => s + (p.pnl || 0), 0))

async function fetchData() {
  loading.value = true
  try {
    await trading.checkStatus()
    // 从 QMT 拿持仓
    if (trading.connected) {
      const [posRes, trdRes] = await Promise.allSettled([
        api.get('/trading/positions'),
        api.get('/trading/trades', { params: { limit: 10 } }),
      ])
      if (posRes.status === 'fulfilled') qmtPositions.value = posRes.value?.positions || []
      if (trdRes.status === 'fulfilled') todayTrades.value = trdRes.value?.trades?.slice(0, 10) || []
    }
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
        <span><el-icon><Connection /></el-icon> 阶段 3: 信号 & 执行</span>
        <div class="card-header-right">
          <el-tag size="small" :type="trading.connected ? 'success' : 'info'" effect="plain">
            执行器: QMT {{ trading.connected ? '已连接' : '未连接' }}
          </el-tag>
          <el-button text size="small" @click="fetchData">刷新</el-button>
        </div>
      </div>
    </template>
    <el-row :gutter="16">
      <el-col :span="12">
        <h4 class="section-subtitle">近期待执行</h4>
        <el-table :data="todayTrades" size="small" max-height="200" v-if="todayTrades.length" stripe>
          <el-table-column prop="ts_code" label="代码" width="100" />
          <el-table-column prop="stock_name" label="名称" min-width="90" />
          <el-table-column label="方向" width="65">
            <template #default="{ row }">
              <el-tag :type="row.action === 'BUY' ? 'danger' : 'success'" size="small" effect="plain">
                {{ row.action === 'BUY' ? '买入' : '卖出' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="数量" width="70" align="right">
            <template #default="{ row }">{{ row.quantity }}</template>
          </el-table-column>
          <el-table-column prop="price" label="价格" width="80" align="right" :formatter="(r) => fmtPrice(r.price)" />
        </el-table>
        <div v-else class="empty-hint">暂无待执行交易</div>
      </el-col>
      <el-col :span="12">
        <h4 class="section-subtitle">当前持仓 ({{ qmtPositions.length }})</h4>
        <el-table :data="qmtPositions" size="small" max-height="200" v-if="qmtPositions.length" stripe>
          <el-table-column prop="ts_code" label="代码" width="100" />
          <el-table-column prop="name" label="名称" min-width="90" />
          <el-table-column prop="quantity" label="数量" width="70" align="right" />
          <el-table-column label="盈亏" width="90" align="right">
            <template #default="{ row }">
              <span :class="profitClass(row.pnl)">
                {{ row.pnl >= 0 ? '+' : '' }}{{ fmtPrice(row.pnl) }}
              </span>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="empty-hint">暂无持仓</div>
      </el-col>
    </el-row>
    <div class="card-footer">
      <el-text size="small" type="info" v-if="qmtPositions.length">
        总盈亏: <span :class="profitClass(totalProfit)">{{ totalProfit >= 0 ? '+' : '' }}{{ fmtPrice(totalProfit) }}</span>
      </el-text>
      <el-button type="primary" size="small" @click="$router.push('/trading')" style="float: right;">
        前往交易控制台
      </el-button>
    </div>
  </el-card>
</template>

<style scoped>
.section-subtitle { font-size: 13px; font-weight: 600; margin-bottom: 8px; color: #606266; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.card-header-right { display: flex; align-items: center; gap: 8px; }
.card-footer { margin-top: 12px; border-top: 1px solid #f0f0f0; padding-top: 8px; min-height: 28px; }
.empty-hint { text-align: center; color: #909399; padding: 20px; font-size: 13px; }
</style>
