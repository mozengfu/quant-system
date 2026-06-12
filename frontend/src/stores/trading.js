import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  connectTrading, getBalance, getTradingPositions,
  getOrders, buyStock, sellStock, cancelOrder, getTradingStatus,
  marketOrder,
} from '../api/trading'

export const useTradingStore = defineStore('trading', () => {
  const connected = ref(false)
  const balance = ref(null)
  const positions = ref([])
  const orders = ref([])
  const loading = ref(false)
  const connectionStatus = ref('未连接')

  async function connect() {
    connectionStatus.value = '连接中...'
    try {
      const r = await connectTrading()
      connected.value = true
      connectionStatus.value = '✅ 已连接'
      await refresh()
      return r
    } catch (e) {
      connected.value = false
      connectionStatus.value = '❌ 连接失败'
      throw e
    }
  }

  async function checkStatus() {
    try {
      const r = await getTradingStatus()
      connected.value = r.connected
      connectionStatus.value = r.detail
      return r
    } catch {
      connected.value = false
      connectionStatus.value = '未连接'
    }
  }

  async function refresh() {
    loading.value = true
    try {
      const [balRes, posRes, ordRes] = await Promise.allSettled([
        getBalance(),
        getTradingPositions(),
        getOrders(),
      ])
      if (balRes.status === 'fulfilled' && balRes.value?.status === 'ok') {
        balance.value = balRes.value
      }
      if (posRes.status === 'fulfilled' && posRes.value?.status === 'ok') {
        positions.value = posRes.value.positions || []
      }
      if (ordRes.status === 'fulfilled') {
        orders.value = ordRes.value?.orders || ordRes.value || []
      }
    } finally {
      loading.value = false
    }
  }

  async function buy(code, price, amount, name) {
    const r = await buyStock(code, price, amount, name)
    await refresh()
    return r
  }

  async function sell(code, price, amount, name) {
    const r = await sellStock(code, price, amount, name)
    await refresh()
    return r
  }

  async function marketBuy(code, price, amount, name) {
    const r = await marketOrder(code, price, amount, 'BUY', name)
    await refresh()
    return r
  }

  async function marketSell(code, price, amount, name) {
    const r = await marketOrder(code, price, amount, 'SELL', name)
    await refresh()
    return r
  }

  async function cancel(orderId) {
    const r = await cancelOrder(orderId)
    await refresh()
    return r
  }

  return {
    connected, balance, positions, orders, loading, connectionStatus,
    connect, checkStatus, refresh, buy, sell, marketBuy, marketSell, cancel,
  }
})
