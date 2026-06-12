import { ref } from 'vue'

export function useWebSocket(url, handlers = {}) {
  let ws = null
  let reconnectTimer = null
  const connected = ref(false)

  function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return
    try {
      ws = new WebSocket(url)
    } catch (e) {
      console.error('[WS] 连接失败:', e)
      scheduleReconnect()
      return
    }

    ws.onopen = () => {
      connected.value = true
      handlers.onOpen?.()
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        handlers.onMessage?.(data)
        const type = data?.type
        if (type && handlers[type]) {
          handlers[type](data)
        }
      } catch (e) {
        console.error('[WS] 消息解析失败:', e)
      }
    }

    ws.onclose = () => {
      connected.value = false
      handlers.onClose?.()
      scheduleReconnect()
    }

    ws.onerror = () => {
      handlers.onError?.()
    }
  }

  function scheduleReconnect() {
    if (reconnectTimer) return
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 5000)
  }

  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      ws.close()
      ws = null
    }
    connected.value = false
  }

  return { connect, disconnect, connected }
}
