<script setup>
import { ref, onMounted } from 'vue'
import { getRecommendV11 } from '../api/ml'
import { fmtPct, fmtPrice, klineUrl, profitClass, normalizeStock } from '../utils/format'

const stocks = ref([])
const loading = ref(false)



async function fetchData() {
  loading.value = true
  try {
    const r = await getRecommendV11(true)
    let list = r
    if (r?.data) list = r.data
    else if (r?.推荐股票) list = r.推荐股票
    else if (r?.stocks) list = r.stocks
    if (!Array.isArray(list)) list = []
    stocks.value = list.map(normalizeStock)
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>ML 选股推荐</h2>
      <el-button size="small" @click="fetchData">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
    </div>
    <el-table :data="stocks" v-loading="loading" stripe>
      <el-table-column type="index" label="#" width="50" />
      <el-table-column prop="ts_code" label="代码" width="110">
        <template #default="{ row }">
          <a :href="klineUrl(row.ts_code)" target="_blank" rel="noopener">{{ row.ts_code }}</a>
        </template>
      </el-table-column>
      <el-table-column prop="name" label="名称" width="110" />
      <el-table-column prop="ml_prob" label="ML 概率" width="100" align="right">
        <template #default="{ row }">
          <span :class="profitClass(row.ml_prob)">{{ row.ml_prob ? (row.ml_prob * 100).toFixed(1) + '%' : '--' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="price" label="价格" width="90" align="right" :formatter="(r) => fmtPrice(r.price)" />
      <el-table-column prop="pct_chg" label="涨幅" width="80" align="right" :formatter="(r) => fmtPct(r.pct_chg)" />
      <el-table-column prop="reason" label="理由" min-width="180" show-overflow-tooltip />
    </el-table>
    <div v-if="!stocks.length && !loading" class="empty-hint">暂无推荐数据</div>
  </div>
</template>

<style scoped>
.view { max-width: 1200px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.empty-hint { text-align: center; color: #909399; padding: 40px; }
</style>
