<template>
  <header class="hdr">
    <div class="hdr-l">
      <span class="hdr-logo">&#9632;</span>
      <span class="hdr-t">量化交易系统</span>
      <span class="hdr-v">V11.2</span>
    </div>
    <div class="hdr-r">
      <span class="hdr-st">
        <span :class="['hdr-dot', store.system.qmtConnected ? 'on' : 'off']"></span>
        <span :class="store.system.qmtConnected ? 'st-on' : 'st-off'">QMT {{ store.system.qmtConnected ? '已连接' : '断开' }}</span>
      </span>
      <span class="hdr-tm" v-if="store.lastUpdated">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        {{ store.lastUpdated }}
      </span>
      <button class="hdr-rf" @click="store.refreshAll()" :disabled="store.loading" title="刷新">
        <svg :class="{ spin: store.loading }" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
        </svg>
      </button>
    </div>
  </header>
</template>

<script setup>
import { useDashboardStore } from '../../stores/dashboard'
const store = useDashboardStore()
</script>

<style scoped>
.hdr {
  display: flex; align-items: center; justify-content: space-between;
  height: 44px; padding: 0 16px;
  background: #1e1e2e; border-bottom: 1px solid #2d2d3f; flex-shrink: 0;
}
.hdr-l { display: flex; align-items: center; gap: 8px; }
.hdr-logo { color: #818cf8; font-size: 13px; }
.hdr-t { font-size: 14px; font-weight: 700; color: #e4e4e7; }
.hdr-v { font-size: 10px; color: #6b7280; padding: 1px 6px; background: rgba(129,140,248,0.08); border-radius: 4px; }
.hdr-r { display: flex; align-items: center; gap: 12px; }
.hdr-st { display: flex; align-items: center; gap: 5px; font-size: 11.5px; }
.hdr-dot { width: 6px; height: 6px; border-radius: 50%; }
.hdr-dot.on { background: #34d399; box-shadow: 0 0 4px rgba(52,211,153,0.4); }
.hdr-dot.off { background: #f87171; box-shadow: 0 0 4px rgba(248,113,113,0.4); }
.st-on { color: #34d399; }
.st-off { color: #f87171; }
.hdr-tm { display: flex; align-items: center; gap: 3px; font-size: 10.5px; color: #6b7280; }
.hdr-rf { display: flex; align-items: center; justify-content: center; width: 28px; height: 28px; border: 1px solid #2d2d3f; border-radius: 5px; background: transparent; color: #a1a1aa; cursor: pointer; transition: all 0.15s; }
.hdr-rf:hover { background: #2d2d3f; color: #e4e4e7; }
.hdr-rf:disabled { opacity: 0.5; cursor: not-allowed; }
@keyframes spin { to { transform: rotate(360deg); } }
.spin { animation: spin 1s linear infinite; }
</style>
