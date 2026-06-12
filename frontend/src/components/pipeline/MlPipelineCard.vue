<script setup>
import { ref, onMounted } from 'vue'
import { getRecommendV11 } from '../../api/ml'
import { fmtPct, fmtPrice, klineUrl, profitClass, normalizeStock } from '../../utils/format'

const props = defineProps({
  state: Object,
})

const top3 = ref([])
const loading = ref(false)



async function fetchTop3() {
  loading.value = true
  try {
    const r = await getRecommendV11()
    // 兼容多种响应格式
    let list = r
    if (r?.data) list = r.data
    else if (r?.推荐股票) list = r.推荐股票
    else if (r?.stocks) list = r.stocks
    else if (r?.recommend) list = r.recommend
    if (!Array.isArray(list)) list = []
    top3.value = list.map(normalizeStock).slice(0, 3)
  } finally {
    loading.value = false
  }
}

onMounted(fetchTop3)
</script>

<template>
  <el-card shadow="never" class="pipeline-card">
    <template #header>
      <div class="card-header">
        <span><el-icon><DataAnalysis /></el-icon> 阶段 2: ML 推理</span>
        <div class="card-header-right">
          <el-tag size="small" type="info" effect="plain">
            {{ state?.model || 'V11.0' }}
          </el-tag>
          <el-tag size="small" v-if="state?.rank_ic" effect="plain" :type="(state.rank_ic || 0) > 0 ? 'success' : 'danger'">
            IC: {{ state.rank_ic }}
          </el-tag>
          <el-button text size="small" @click="fetchTop3">刷新</el-button>
        </div>
      </div>
    </template>
    <el-row :gutter="16">
      <el-col :span="8" v-for="(stock, i) in top3" :key="i">
        <el-card shadow="hover" class="stock-card" :body-style="{ padding: '12px' }">
          <div class="stock-rank">#{{ i + 1 }}</div>
          <div class="stock-name">
            <a :href="klineUrl(stock.ts_code)" target="_blank" rel="noopener">{{ stock.name || stock.ts_code }}</a>
          </div>
          <div class="stock-code">{{ stock.ts_code }}</div>
          <div class="stock-score" :class="profitClass(stock.ml_prob)">
            {{ stock.ml_prob ? (stock.ml_prob * 100).toFixed(1) + '%' : '--' }}
          </div>
        </el-card>
      </el-col>
      <el-col :span="8" v-if="top3.length === 0">
        <div class="empty-hint">暂无推荐数据，点击刷新</div>
      </el-col>
    </el-row>
    <div class="card-footer">
      <el-text size="small" type="info">
        候选池: {{ state?.candidates || 500 }} 只 · 最近预测: {{ state?.last_predict || '--' }}
      </el-text>
    </div>
  </el-card>
</template>

<style scoped>
.stock-card { position: relative; }
.stock-rank { position: absolute; top: 8px; right: 12px; font-size: 24px; font-weight: 800; opacity: 0.15; }
.stock-name { font-size: 16px; font-weight: 600; margin-bottom: 2px; }
.stock-name a { color: inherit; text-decoration: none; }
.stock-name a:hover { color: #409eff; }
.stock-code { font-size: 12px; color: #909399; margin-bottom: 4px; }
.stock-score { font-size: 20px; font-weight: 700; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.card-header-right { display: flex; align-items: center; gap: 8px; }
.card-footer { margin-top: 12px; border-top: 1px solid #f0f0f0; padding-top: 8px; }
.empty-hint { text-align: center; color: #909399; padding: 20px; }
</style>
