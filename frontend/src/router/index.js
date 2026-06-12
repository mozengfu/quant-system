import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'Pipeline',
    component: () => import('../views/Pipeline.vue'),
    meta: { title: '管线工作台' },
  },
  {
    path: '/trading',
    name: 'Trading',
    component: () => import('../views/Trading.vue'),
    meta: { title: '实盘交易' },
  },
  {
    path: '/recommend',
    name: 'Recommend',
    component: () => import('../views/Recommend.vue'),
    meta: { title: 'ML 推荐' },
  },
  {
    path: '/signals',
    name: 'Signals',
    component: () => import('../views/Signals.vue'),
    meta: { title: 'QMT 实盘委托' },
  },
  {
    path: '/backtest',
    name: 'Backtest',
    component: () => import('../views/Backtest.vue'),
    meta: { title: 'ML 模型回测' },
  },
  {
    path: '/pnl',
    name: 'Pnl',
    component: () => import('../views/Pnl.vue'),
    meta: { title: '实盘收益' },
  },
  {
    path: '/analysis',
    name: 'Analysis',
    component: () => import('../views/Analysis.vue'),
    meta: { title: '个股分析' },
  },
  {
    path: '/market',
    name: 'Market',
    component: () => import('../views/MarketAnalysis.vue'),
    meta: { title: '大盘研判' },
  },
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { title: '登录' },
  },
  {
    path: '/admin',
    name: 'Admin',
    component: () => import('../views/Admin.vue'),
    meta: { title: '管理后台' },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

export default router
