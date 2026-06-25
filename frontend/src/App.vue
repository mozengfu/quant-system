<script setup>
import AppHeader from './components/layout/AppHeader.vue'
</script>

<template>
  <div class="app-container">
    <AppHeader />
    <div class="app-body">
      <div class="app-main">
        <router-view />
      </div>
    </div>
  </div>
</template>

<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body, #app { height: 100%; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }

.app-container {
  height: 100vh;
  display: flex;
  flex-direction: column;
}
.app-body {
  flex: 1;
  display: flex;
  overflow: hidden;
}
.app-main {
  flex: 1;
  overflow-y: auto;
  background: #f0f2f5;
  padding: 24px;
}

@media (max-width: 767px) {
  .app-main {
    padding: 12px !important;
  }
  .el-card__body {
    padding: 14px !important;
  }
}

.profit { color: #ef4444; }
.loss { color: #10b981; }
.neutral { color: #606266; }
</style>
