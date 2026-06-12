<script setup>
import { useRoute } from 'vue-router'

const route = useRoute()
const emit = defineEmits(['menu-click'])

const navs = [
  { path: '/', icon: 'Monitor', label: '管线工作台' },
  { path: '/trading', icon: 'Coin', label: '实盘交易' },
  { path: '/recommend', icon: 'DataAnalysis', label: 'ML 推荐' },
  { path: '/signals', icon: 'List', label: 'QMT 成交' },
  { path: '/backtest', icon: 'Histogram', label: 'ML 回测' },
  { path: '/pnl', icon: 'Money', label: '实盘收益' },
  { path: '/analysis', icon: 'Search', label: '个股分析' },
  { path: '/market', icon: 'TrendCharts', label: '大盘研判' },
  { path: '/admin', icon: 'Setting', label: '管理后台' },
]

function handleSelect() {
  emit('menu-click')
}
</script>

<template>
  <el-menu
    :default-active="route.path"
    router
    :collapse="false"
    class="app-sidebar"
    @select="handleSelect"
  >
    <el-menu-item v-for="nav in navs" :key="nav.path" :index="nav.path">
      <el-icon>
        <component :is="nav.icon" />
      </el-icon>
      <span>{{ nav.label }}</span>
    </el-menu-item>
  </el-menu>
</template>

<style scoped>
.app-sidebar {
  height: 100%;
  border-right: 1px solid #e4e7ed;
}
</style>
