import api from './index'

export function getRecommend() {
  return api.get('/recommend')
}

export function getRecommendStrong() {
  return api.get('/recommend/strong')
}

export function getRecommendV11(forceRefresh = false) {
  return api.get('/recommend/v11', { params: forceRefresh ? { force_refresh: true } : {} })
}

export function doMLScan(market = 'all', sort = 'top10') {
  return api.get('/scan/ml', { params: { market, sort } })
}

export function getMLTop15() {
  return api.get('/ml_top15')
}

export function getAiSimPerformance() {
  return api.get('/scan/aimodel')
}

export function runAiSimToday() {
  return api.post('/ai_sim/run')
}
