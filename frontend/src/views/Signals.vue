<script setup>
import { ref, onMounted } from 'vue'
import { fmtPrice, fmtDate } from '../utils/format'
import api from '../api'

const trades = ref([])
const total = ref(0)
const loading = ref(false)

async function fetchData() {
  loading.value = true
  try {
    const r = await api.get('/trading/trades', { params: { limit: 100 } })
    if (r?.trades) {
      trades.value = r.trades
      total.value = r.total || 0
    }
  } catch (e) {
    // 静默处理，无数据时显示空状态
  } finally {
    loading.value = false
  }
}

function actionType(action) {
  const buy = action === 'BUY' || action === '买入'
  return buy ? 'danger' : 'success'
}

function actionText(action) {
  if (action === 'BUY') return '买入'
  if (action === 'SELL') return '卖出'
  return action || '--'
}

onMounted(fetchData)
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>QMT 实盘成交记录</h2>
      <div class="header-actions">
        <el-text v-if="total" size="small" type="info">共 {{ total }} 笔</el-text>
        <el-button size="small" @click="fetchData">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </div>

    <el-table :data="trades" v-loading="loading" stripe>
      <el-table-column label="成交时间" width="160">
        <template #default="{ row }">
          {{ row.trade_time || row.trade_date || row.created_at || '--' }}
        </template>
      </el-table-column>
      <el-table-column prop="ts_code" label="代码" width="110" />
      <el-table-column prop="stock_name" label="名称" width="100" />
      <el-table-column label="方向" width="65">
        <template #default="{ row }">
          <el-tag :type="actionType(row.action)" size="small" effect="plain">
            {{ actionText(row.action) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="price" label="成交价" width="90" align="right" :formatter="(r) => fmtPrice(r.price)" />
      <el-table-column prop="quantity" label="数量" width="80" align="right" />
      <el-table-column prop="amount" label="金额" width="110" align="right" :formatter="(r) => fmtPrice(r.amount)" />
      <el-table-column label="来源" width="80">
        <template #default="{ row }">
          <el-tag v-if="row.source === 'qmt_live'" size="small" type="success" effect="plain">QMT实时</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">本地</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="原因" min-width="150" show-overflow-tooltip />
    </el-table>
    <div v-if="!trades.length && !loading" class="empty-hint">
      暂无实盘成交记录。通过交易控制台买入/卖出后，记录会自动保存。
    </div>
  </div>
</template>

<style scoped>
.view { max-width: 1200px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.empty-hint { text-align: center; color: #909399; padding: 40px; }
</style>
