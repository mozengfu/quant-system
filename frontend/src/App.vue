<script setup>
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppHeader from './components/layout/AppHeader.vue'
import AppSidebar from './components/layout/AppSidebar.vue'

const route = useRoute()
const sidebarDrawerVisible = ref(false)
const isMobile = ref(false)

function checkMobile() {
  isMobile.value = window.innerWidth < 768
}

function toggleSidebar() {
  sidebarDrawerVisible.value = !sidebarDrawerVisible.value
}

// 切换路由时关掉移动端抽屉
watch(route, () => {
  if (isMobile.value) sidebarDrawerVisible.value = false
})

onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
})
onUnmounted(() => {
  window.removeEventListener('resize', checkMobile)
})
</script>

<template>
  <div class="app-container">
    <AppHeader :is-mobile="isMobile" @toggle-sidebar="toggleSidebar" />
    <div class="app-body">
      <AppSidebar v-if="!isMobile" class="app-sidebar" />
      <el-drawer
        v-model="sidebarDrawerVisible"
        :with-header="false"
        size="200px"
        class="mobile-sidebar-drawer"
        :z-index="2000"
      >
        <AppSidebar @menu-click="sidebarDrawerVisible = false" />
      </el-drawer>
      <el-main class="app-main">
        <router-view />
      </el-main>
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
.app-sidebar {
  width: 200px;
  flex-shrink: 0;
}
.app-main {
  flex: 1;
  overflow-y: auto;
  background: #f5f7fa;
  padding: 20px;
}

/* 移动端适配 */
@media (max-width: 767px) {
  .app-main {
    padding: 12px !important;
  }
  .mobile-sidebar-drawer .el-drawer__body {
    padding: 0;
  }
  /* 表格横向滚动 */
  .el-table {
    width: 100%;
    overflow-x: auto;
  }
  .el-table__body-wrapper {
    overflow-x: auto;
  }
  /* 卡片内边距压缩 */
  .el-card__body {
    padding: 14px !important;
  }
}

/* 全局状态色 */
.profit { color: #ef4444; }
.loss { color: #10b981; }
.neutral { color: #606266; }
</style>
