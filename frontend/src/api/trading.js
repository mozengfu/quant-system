import api from './index'

export function connectTrading() {
  return api.post('/trading/connect')
}

export function getBalance() {
  return api.get('/trading/balance')
}

export function getTradingPositions() {
  return api.get('/trading/positions')
}

export function getOrders() {
  return api.get('/trading/orders')
}

export function buyStock(code, price, amount, name = '') {
  return api.post('/trading/buy', { code, price, amount, name })
}

export function sellStock(code, price, amount, name = '') {
  return api.post('/trading/sell', { code, price, amount, name })
}

export function cancelOrder(orderId) {
  return api.post(`/trading/cancel/${orderId}`)
}

export function cancelAllOrders() {
  return api.post('/trading/cancel-all')
}

export function marketOrder(code, price, amount, side, name = '') {
  return api.post('/trading/market-order', { code, price, amount, side, name })
}

export function batchOrder(orders) {
  return api.post('/trading/batch-order', { orders })
}

export function getTradingStatus() {
  return api.get('/trading/status')
}
