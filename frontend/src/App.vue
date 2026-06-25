<script setup>
import AppHeader from './components/layout/AppHeader.vue'
</script>

<template>
  <div class="app-container">
    <AppHeader />
    <div class="app-body">
      <router-view />
    </div>
  </div>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #11111b;
  color: #d4d4d8;
}
#app {
  background: #11111b;
}
.app-container {
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: #11111b;
}
.app-body {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px;
  background: #11111b;
}

@media (max-width: 767px) {
  .app-body { padding: 12px !important; }
}

/* element-plus 全局暗色覆盖 */
.el-alert { border-radius: 6px; }
.el-loading-mask { background: rgba(17, 17, 27, 0.8); }

/* 滚动条 */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #1e1e2e; }
::-webkit-scrollbar-thumb { background: #3f3f5c; border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: #52527a; }
</style>
