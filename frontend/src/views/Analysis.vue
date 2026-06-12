<script setup>
import { ref } from 'vue'
import { fmtPrice, fmtPct } from '../utils/format'
import api from '../api'

const code = ref('')
const result = ref(null)
const loading = ref(false)
const error = ref('')

function parseResult(r) {
  // 后端返回中文key，提取前端需要的字段
  const basic = r['一、基础数据'] || {}
  const score = r['五、综合评分'] || {}
  const tech = r['二、技术面'] || {}
  const summary = r['七、总结'] || {}
  const fund = r['三、资金面'] || {}
  const fundamental = r['四、基本面'] || {}

  const name = r['股票名称'] || ''
  const tsCode = r['股票代码'] || ''
  const rawPrice = (basic['现价'] || '0').replace('元', '')
  const rawPct = (basic['涨跌幅'] || '0%').replace('%', '').replace('+', '')
  const rawScore = (score['总分'] || '0').replace('/100', '')

  return {
    ts_code: tsCode,
    name: name,
    price: parseFloat(rawPrice) || 0,
    pct_chg: parseFloat(rawPct) || 0,
    score: parseFloat(rawScore) || 0,
    advice: score['操作建议'] || '--',
    stop_loss: score['止损价'] || '--',
    target: score['目标价'] || '--',
    risk: score['风险等级'] || '--',
    reason: score['建议理由'] || '--',
    trend: tech['均线趋势'] || '--',
    rps: tech['RPS评分'] || '--',
    vol_analysis: tech['量价配合'] || '--',
    main_flow: fund['主力净流入'] || '--',
    main_flow_pct: fund['主力净流入占比'] || '--',
    industry: fundamental['行业'] || '--',
    pe: fundamental['PE'] || '--',
    pb: fundamental['PB'] || '--',
    roe: fundamental['ROE'] || '--',
    market_view: (summary['大盘情绪'] || '--').slice(0, 30),
    risk_tips: summary['风险提示'] || [],
    model_view: summary['模型观点'] || '--',
    op_ref: summary['操作参考'] || '--',
    raw: r,
  }
}

async function doAnalysis() {
  if (!code.value) return
  loading.value = true
  error.value = ''
  result.value = null
  try {
    const c = code.value.trim()
    const market = c.startsWith('6') ? 'SH' : 'SZ'
    const r = await api.get(`/analysis/${market}/${c}`)
    result.value = parseResult(r)
  } catch (e) {
    error.value = e.message || '查询失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>个股分析</h2>
    </div>
    <div class="search-bar">
      <el-input v-model="code" placeholder="输入股票代码，如 000559" style="max-width: 220px;" @keyup.enter="doAnalysis" />
      <el-button type="primary" @click="doAnalysis" :loading="loading">分析</el-button>
      <el-text size="small" type="info">6开头=沪市，其他=深市</el-text>
    </div>

    <div v-if="error" class="error-msg">
      <el-alert :title="error" type="error" show-icon :closable="false" />
    </div>

    <div v-if="result" class="result-area">
      <!-- 第一行：基本信息 -->
      <el-descriptions :column="2" :mobile-columns="1" border size="small">
        <el-descriptions-item label="代码">{{ result.ts_code }}</el-descriptions-item>
        <el-descriptions-item label="名称">{{ result.name }}</el-descriptions-item>
        <el-descriptions-item label="现价">{{ fmtPrice(result.price) }}</el-descriptions-item>
        <el-descriptions-item label="涨跌幅">
          <span :style="{ color: result.pct_chg >= 0 ? '#f56c6c' : '#67c23a', fontWeight: 700 }">
            {{ fmtPct(result.pct_chg) }}
          </span>
        </el-descriptions-item>
      </el-descriptions>

      <!-- 第二行：评分与建议 -->
      <el-descriptions :column="2" :mobile-columns="1" border size="small" style="margin-top:12px">
        <el-descriptions-item label="综合评分">
          <el-tag :type="result.score >= 70 ? 'success' : result.score >= 50 ? 'warning' : 'danger'" size="small">
            {{ result.score }}/100
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="操作建议">{{ result.advice }}</el-descriptions-item>
        <el-descriptions-item label="风险等级">{{ result.risk }}</el-descriptions-item>
        <el-descriptions-item label="止损价">{{ result.stop_loss }}</el-descriptions-item>
      </el-descriptions>

      <!-- 第三行：技术面 -->
      <el-descriptions :column="2" :mobile-columns="1" border size="small" style="margin-top:12px">
        <el-descriptions-item label="均线趋势">{{ result.trend }}</el-descriptions-item>
        <el-descriptions-item label="量价配合">{{ result.vol_analysis }}</el-descriptions-item>
        <el-descriptions-item label="RPS">{{ result.rps }}</el-descriptions-item>
        <el-descriptions-item label="行业">{{ result.industry }}</el-descriptions-item>
      </el-descriptions>

      <!-- 第四行：资金面 + 基本面 -->
      <el-descriptions :column="2" :mobile-columns="1" border size="small" style="margin-top:12px">
        <el-descriptions-item label="主力净流入">{{ result.main_flow }}</el-descriptions-item>
        <el-descriptions-item label="PE">{{ result.pe }}</el-descriptions-item>
        <el-descriptions-item label="PB">{{ result.pb }}</el-descriptions-item>
        <el-descriptions-item label="ROE">{{ result.roe }}</el-descriptions-item>
      </el-descriptions>

      <!-- 建议理由 -->
      <el-card shadow="never" style="margin-top:12px">
        <template #header><span style="font-weight:600">建议理由</span></template>
        <p style="margin:0;color:#606266;font-size:13px">{{ result.reason }}</p>
      </el-card>

      <!-- 模型观点 & 风险 -->
      <el-row :gutter="12" style="margin-top:12px">
        <el-col :xs="24" :sm="12">
          <el-card shadow="never">
            <template #header><span style="font-weight:600">市场观点</span></template>
            <p style="margin:0;font-size:13px;color:#606266">{{ result.market_view || '暂无' }}</p>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12">
          <el-card shadow="never">
            <template #header><span style="font-weight:600">模型观点</span></template>
            <p style="margin:0;font-size:13px;color:#606266">{{ result.model_view || '暂无' }}</p>
          </el-card>
        </el-col>
      </el-row>

      <!-- 风险提示 -->
      <el-card v-if="result.risk_tips?.length" shadow="never" style="margin-top:12px">
        <template #header><span style="font-weight:600;color:#e6a23c">风险提示</span></template>
        <ul style="margin:0;padding-left:18px;font-size:13px;color:#606266">
          <li v-for="(tip, i) in result.risk_tips" :key="i">{{ tip }}</li>
        </ul>
      </el-card>
    </div>

    <div v-else-if="!loading" class="empty-hint">
      输入代码，点击分析
    </div>
  </div>
</template>

<style scoped>
.view { max-width: 1100px; }
.view-header { margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.search-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
.error-msg { margin-bottom: 16px; }
.result-area { margin-top: 8px; }
.empty-hint { text-align: center; color: #909399; padding: 40px; }
</style>
