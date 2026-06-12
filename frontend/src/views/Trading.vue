<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useTradingStore } from '../stores/trading'
import { ElMessage } from 'element-plus'
import AccountSummary from '../components/trading/AccountSummary.vue'
import OrderPanel from '../components/trading/OrderPanel.vue'
import PositionTable from '../components/trading/PositionTable.vue'
import OrderHistory from '../components/trading/OrderHistory.vue'

const store = useTradingStore()
const loading = ref(false)
const orderPanelRef = ref(null)
let refreshTimer = null

async function handleConnect() {
  try {
    await store.connect()
    ElMessage.success('已连接 QMT 交易服务')
  } catch (e) {
    ElMessage.error('连接失败: ' + e.message)
  }
}

async function handleRefresh() {
  loading.value = true
  try {
    await store.refresh()
  } finally {
    loading.value = false
  }
}

function handleFillOrder(row) {
  if (orderPanelRef.value) {
    orderPanelRef.value.fillFromPosition(row)
  }
}

onMounted(() => {
  store.checkStatus()
  if (store.connected) {
    handleRefresh()
  }
  refreshTimer = setInterval(() => {
    if (store.connected) {
      store.refresh()
    }
  }, 30000)
})

onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div class="trading-view">
    <div class="view-header">
      <h2><el-icon><Coin /></el-icon> 实盘交易</h2>
      <div class="header-actions">
        <el-tag v-if="store.connected" type="success" effect="dark" size="small">
          ✅ 已连接
        </el-tag>
        <el-button type="primary" size="small" @click="handleConnect" :disabled="store.connected">
          连接
        </el-button>
        <el-button size="small" @click="handleRefresh">
          <el-icon><Refresh /></el-icon> 刷新
        </el-button>
      </div>
    </div>

    <AccountSummary :balance="store.balance" :connected="store.connected" />

    <div class="trading-main">
      <div class="trading-left">
        <OrderPanel ref="orderPanelRef" @order-placed="handleRefresh" />
      </div>
      <div class="trading-right">
        <PositionTable
          :positions="store.positions"
          :loading="store.loading"
          @fill-order="handleFillOrder"
          @refresh="handleRefresh"
        />
        <div style="margin-top: 12px;">
          <OrderHistory
            :orders="store.orders"
            :loading="store.loading"
            @refresh="handleRefresh"
          />
        </div>
      </div>
    </div>

    <div class="status-bar" v-if="store.connectionStatus">
      <el-text size="small" type="info">
        系统状态: {{ store.connectionStatus }} | 最后刷新: {{ new Date().toLocaleTimeString('zh-CN') }}
      </el-text>
    </div>
  </div>
</template>

<style scoped>
.trading-view { max-width: 1400px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; display: flex; align-items: center; gap: 8px; }
.header-actions { display: flex; align-items: center; gap: 8px; }
.trading-main {
  display: flex;
  gap: 16px;
  margin-top: 16px;
  align-items: flex-start;
}
.trading-left {
  width: 320px;
  flex-shrink: 0;
}
.trading-right {
  flex: 1;
  min-width: 0;
}
.status-bar {
  margin-top: 12px;
  padding: 8px 12px;
  background: #f5f7fa;
  border-radius: 4px;
}

@media (max-width: 767px) {
  .trading-main {
    flex-direction: column;
  }
  .trading-left {
    width: 100%;
  }
}
</style>
