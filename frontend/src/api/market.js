import api from './index'

export function getPipelineStatus() {
  return api.get('/pipeline/status')
}

export function getMarketPremarket() {
  return api.get('/market/premarket')
}

export function getMarketState() {
  return api.get('/market/state')
}

export function getHotSectors() {
  return api.get('/sectors/hot')
}

export function getStockFundFlow(tsCode) {
  return api.get(`/stock/${tsCode}/fund_flow`)
}

export function getMainforceScan() {
  return api.get('/mainforce_scan')
}
