import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getPositions, getNavHistory, getPerformanceSummary, getTodaySignals } from '../api/data'

export const usePortfolioStore = defineStore('portfolio', () => {
  const positions = ref([])
  const navHistory = ref([])
  const performance = ref(null)
  const todaySignals = ref([])
  const loading = ref(false)

  const totalMarketValue = computed(() => positions.value.reduce((s, p) => s + (p.market_value || 0), 0))
  const totalProfit = computed(() => positions.value.reduce((s, p) => s + (p.profit_loss || 0), 0))
  const signalCount = computed(() => todaySignals.value.length)

  async function fetchAll() {
    loading.value = true
    try {
      const [posRes, navRes, perfRes, sigRes] = await Promise.allSettled([
        getPositions(),
        getNavHistory(),
        getPerformanceSummary(),
        getTodaySignals(),
      ])
      if (posRes.status === 'fulfilled') positions.value = posRes.value?.positions || posRes.value || []
      if (navRes.status === 'fulfilled') navHistory.value = navRes.value || []
      if (perfRes.status === 'fulfilled') performance.value = perfRes.value
      if (sigRes.status === 'fulfilled') todaySignals.value = sigRes.value || []
    } finally {
      loading.value = false
    }
  }

  return { positions, navHistory, performance, todaySignals, loading, totalMarketValue, totalProfit, signalCount, fetchAll }
})
