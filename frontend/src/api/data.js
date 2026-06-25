import api from './index'

export function getSignals() {
  return api.get('/signals')
}

export function createSignal(data) {
  return api.post('/signals', data)
}

export function updateSignal(id, data) {
  return api.put(`/signals/${id}`, data)
}

export function deleteSignal(id) {
  return api.delete(`/signals/${id}`)
}

export function getBacktest(code, market) {
  return api.get('/backtest', { params: { code, market } })
}

export function getBacktestBottom(code, market) {
  return api.get('/backtest_bottom', { params: { code, market } })
}

export function getBacktestStrong(code, market) {
  return api.get('/backtest_strong', { params: { code, market } })
}

export function getBacktestCombo(code, market) {
  return api.get('/backtest_combo', { params: { code, market } })
}

export function getBacktestEnhanced(code, market) {
  return api.get('/backtest_enhanced', { params: { code, market } })
}

export function getTechnicalSignals() {
  return api.get('/technical_signals')
}

export function getNavHistory() {
  return api.get('/sim/nav_history')
}

export function getPerformanceSummary() {
  return api.get('/live/performance_summary')
}

export function getTodaySignals() {
  return api.get('/sim/today_signals')
}

export function getPositions() {
  return api.get('/positions')
}

export function getTrackStats() {
  return api.get('/track/stats')
}

export function getTrackHistory() {
  return api.get('/track/history')
}

export function getTrackCurve() {
  return api.get('/track/curve')
}

export function getSimAccount() {
  return api.get('/sim_account')
}

export function getPnlSummary(mode = "live") {
  return api.get("/pnl/summary", { params: { mode } })
}

export function getMarketAttribution(mode = "live") {
  return api.get("/pnl/market_attribution", { params: { mode } })
}
