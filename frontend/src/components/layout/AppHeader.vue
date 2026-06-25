<template>
  <header class="app-header">
    <div class="header-left">
      <span class="header-icon">&#9632;</span>
      <span class="header-title">量化交易系统</span>
      <span class="header-subtitle">V11.2 板RPS周线</span>
    </div>
    <div class="header-right">
      <span class="status-item">
        <span :class="['status-dot', store.system.qmtConnected ? 'online' : 'offline']"></span>
        <span :class="store.system.qmtConnected ? 'status-online' : 'status-offline'">
          QMT {{ store.system.qmtConnected ? '已连接' : '断开' }}
        </span>
      </span>
      <span class="header-time" v-if="store.lastUpdated">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
        </svg>
        {{ store.lastUpdated }}
      </span>
      <button class="refresh-btn" @click="refresh" :disabled="store.loading" title="刷新数据">
        <svg :class="{ spinning: store.loading }" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
        </svg>
      </button>
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
  background: #1e1e2e;
  border-bottom: 1px solid #2d2d3f;
  flex-shrink: 0;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.header-icon {
  color: #818cf8;
  font-size: 14px;
}
.header-title {
  font-size: 15px;
  font-weight: 700;
  color: #e4e4e7;
}
.header-subtitle {
  font-size: 11px;
  color: #6b7280;
  padding: 2px 8px;
  background: rgba(129, 140, 248, 0.08);
  border-radius: 4px;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 14px;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
}
.status-dot.online { background: #34d399; box-shadow: 0 0 5px rgba(52,211,153,0.4); }
.status-dot.offline { background: #f87171; box-shadow: 0 0 5px rgba(248,113,113,0.4); }
.status-online { color: #34d399; }
.status-offline { color: #f87171; }

.header-time {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: #6b7280;
}
.refresh-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid #2d2d3f;
  border-radius: 6px;
  background: transparent;
  color: #a1a1aa;
  cursor: pointer;
  transition: all 0.15s;
}
.refresh-btn:hover {
  background: #2d2d3f;
  color: #e4e4e7;
}
.refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
@keyframes spin { to { transform: rotate(360deg); } }
.spinning { animation: spin 1s linear infinite; }
</style>
