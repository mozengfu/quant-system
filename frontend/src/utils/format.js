export function fmtPrice(val) {
  if (val == null || isNaN(val)) return '--'
  return Number(val).toFixed(2)
}

export function fmtPct(val) {
  if (val == null || isNaN(val)) return '--'
  const v = Number(val)
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`
}

export function fmtMoney(val) {
  if (val == null || isNaN(val)) return '--'
  const v = Number(val)
  if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toFixed(2)
}

export function fmtDate(d) {
  if (!d) return '--'
  return String(d)
}

export function profitClass(val) {
  if (val == null) return ''
  const v = Number(val)
  if (v > 0) return 'profit'
  if (v < 0) return 'loss'
  return ''
}

// 根据 ts_code 判断市场
export function getMarketByCode(code) {
  if (!code) return 'sz'
  if (code.endsWith('.SH') || code.startsWith('6')) return 'sh'
  return 'sz'
}

// 东方财富 K 线链接
export function klineUrl(code) {
  if (!code) return 'javascript:;'
  const mkt = getMarketByCode(code)
  const c = code.replace(/\.(SH|SZ)$/, '')
  return `https://quote.eastmoney.com/concept/${mkt}${c}.html`
}

// 标准化股票数据（兼容中文/英文字段名）
export function normalizeStock(item) {
  if (!item) return null
  const code = item.代码 || item.ts_code || item.code || ''
  const hasSuffix = code.includes('.')
  return {
    ts_code: hasSuffix ? code : (code.startsWith('6') ? code + '.SH' : code + '.SZ'),
    code: code,
    name: item.名称 || item.name || item.stock_name || '',
    price: item.现价 || item.price || 0,
    pct_chg: item.涨跌幅 || item.pct_chg || 0,
    ml_prob: item.ML得分 || item.ml_prob || item.ML_score || 0,
    reason: item.策略来源 || item.reason || '',
    stop_loss: item.止损价 || item.stop_loss || 0,
  }
}
