<template>
  <header class="app-header">
    <div class="header-left">
      <span class="header-title">量化交易系统</span>
      <span class="header-subtitle">V11.2 板RPS周线</span>
    </div>
    <div class="header-right">
      <el-tag v-if="store.system.qmtConnected" type="success" size="small" effect="dark">QMT 已连接</el-tag>
      <el-tag v-else type="danger" size="small" effect="dark">QMT 断开</el-tag>
      <span class="header-time" v-if="store.lastUpdated">更新 {{ store.lastUpdated }}</span>
      <el-button :icon="Refresh" circle size="small" @click="refresh" :loading="store.loading" />
    </div>
  </header>
</template>

<script setup>
import { Refresh } from '@element-plus/icons-vue'
import { useDashboardStore } from '../../stores/dashboard'

const store = useDashboardStore()
function refresh() {
  store.refreshAll()
}
</script>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-title {
  font-size: 16px;
  font-weight: 600;
  color: #303133;
}
.header-subtitle {
  font-size: 12px;
  color: #909399;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-time {
  font-size: 12px;
  color: #909399;
}
</style>
