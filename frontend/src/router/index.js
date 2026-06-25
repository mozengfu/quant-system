import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: () => import('../views/Dashboard.vue'),
    meta: { title: '交易看板' },
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

export default router
