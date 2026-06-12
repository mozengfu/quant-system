import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { getMarketPremarket, getMarketState } from '../api/market'

export const useMarketStore = defineStore('market', () => {
  const state = ref(null)
  const premarket = ref(null)
  const loading = ref(false)

  const stateLabel = computed(() => state.value?.state || '--')
  const stateColor = computed(() => {
    const s = state.value?.state
    if (s === '恐慌' || s === '恐慌清仓') return 'danger'
    if (s === '阻断') return 'warning'
    if (s === '逆市') return 'warning'
    if (s === '偏弱') return 'info'
    return 'success'
  })
  const positionRatio = computed(() => state.value?.position_ratio ?? 100)

  async function fetchState() {
    loading.value = true
    try {
      const [stateRes, preRes] = await Promise.allSettled([
        getMarketState(),
        getMarketPremarket(),
      ])
      if (stateRes.status === 'fulfilled') state.value = stateRes.value
      if (preRes.status === 'fulfilled') premarket.value = preRes.value?.data
    } finally {
      loading.value = false
    }
  }

  function applyWsUpdate(data) {
    state.value = data
  }

  return { state, premarket, loading, stateLabel, stateColor, positionRatio, fetchState, applyWsUpdate }
})
