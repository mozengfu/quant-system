<script setup>
import { fmtPrice, fmtPct } from '../../utils/format'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useTradingStore } from '../../stores/trading'

const props = defineProps({
  orders: { type: Array, default: () => [] },
  loading: Boolean,
})

const emit = defineEmits(['refresh'])
const store = useTradingStore()

const statusMap = {
  'pending': { type: 'warning', text: '待成交' },
  'filled': { type: 'success', text: '已成交' },
  'partial': { type: 'info', text: '部分成交' },
  'canceled': { type: 'info', text: '已撤单' },
  'rejected': { type: 'danger', text: '已拒绝' },
}

function getStatus(row) {
  if (row.status === 'pending' || row.status === '已报单') return { type: 'warning', text: '已报单' }
  if (row.status === 'filled' || row.status === '已成交') return { type: 'success', text: '已成交' }
  if (row.status === 'canceled' || row.status === '已撤单') return { type: 'info', text: '已撤单' }
  if (row.status === 'rejected') return { type: 'danger', text: '已拒绝' }
  return { type: 'info', text: row.status || '--' }
}

async function handleCancel(orderId) {
  try {
    await ElMessageBox.confirm('确认撤单？', '撤单确认')
    await store.cancel(orderId)
    ElMessage.success('撤单成功')
    emit('refresh')
  } catch (e) {
    if (e !== 'cancel') {
      ElMessage.error('撤单失败: ' + (e.message || e))
    }
  }
}
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>委托查询</span>
        <el-button text size="small" @click="emit('refresh')">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </template>
    <el-table :data="orders" size="small" stripe max-height="300" v-loading="loading">
      <el-table-column prop="created_at" label="时间" width="140" />
      <el-table-column prop="ts_code" label="代码" width="100" />
      <el-table-column prop="name" label="名称" min-width="80" />
      <el-table-column label="方向" width="65">
        <template #default="{ row }">
          <el-tag :type="row.action === 'BUY' || row.action === '买入' ? 'danger' : 'success'" size="small" effect="plain">
            {{ row.action === 'BUY' || row.action === '买入' ? '买入' : '卖出' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="price" label="价格" width="80" align="right" :formatter="(r) => fmtPrice(r.price)" />
      <el-table-column prop="quantity" label="数量" width="70" align="right" />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag :type="getStatus(row).type" size="small" effect="plain">
            {{ getStatus(row).text }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="65">
        <template #default="{ row }">
          <el-button
            v-if="row.status === 'pending' || row.status === '已报单'"
            text
            size="small"
            type="danger"
            @click="handleCancel(row.order_id || row.id)"
          >
            撤单
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <div v-if="!orders.length && !loading" class="empty-hint">
      暂无委托记录
    </div>
  </el-card>
</template>

<style scoped>
.card-header { display: flex; justify-content: space-between; align-items: center; }
.empty-hint { text-align: center; color: #909399; padding: 20px; font-size: 13px; }
</style>
