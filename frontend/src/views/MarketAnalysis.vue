<script setup>
import { ref, onMounted } from 'vue'
import { getMarketPremarket } from '../api/market'

const premarket = ref(null)
const loading = ref(false)

async function fetchData() {
  loading.value = true
  try {
    premarket.value = await getMarketPremarket()
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>大盘研判</h2>
      <el-button size="small" @click="fetchData">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
    </div>
    <div v-loading="loading">
      <template v-if="premarket?.data">
        <div v-if="premarket.data.analysis" class="analysis-cards">
          <el-card shadow="never">
            <template #header>市场分析</template>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="市场状态">{{ premarket.data.analysis.status || '--' }}</el-descriptions-item>
              <el-descriptions-item label="仓位建议">{{ premarket.data.analysis.position_ratio || '--' }}%</el-descriptions-item>
              <el-descriptions-item label="大盘研判" :span="2">{{ premarket.data.analysis.advice || '--' }}</el-descriptions-item>
            </el-descriptions>
          </el-card>
        </div>
        <div v-if="premarket.data.indices">
          <h3 style="margin: 16px 0 8px;">主要指数</h3>
          <el-table :data="Object.entries(premarket.data.indices).map(([k, v]) => ({ name: k, ...v }))" stripe v-loading="false">
            <el-table-column prop="name" label="指数" width="120" />
            <el-table-column prop="最新价" label="最新价" width="100" align="right" />
            <el-table-column label="涨跌幅" width="100" align="right">
              <template #default="{ row }">
                <span :class="(row.涨跌幅 || 0) >= 0 ? 'profit' : 'loss'">
                  {{ row.涨跌幅 >= 0 ? '+' : '' }}{{ row.涨跌幅?.toFixed(2) }}%
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="涨跌额" label="涨跌额" width="100" align="right" />
            <el-table-column prop="成交额" label="成交额" min-width="120" align="right" />
          </el-table>
        </div>
      </template>
      <div v-else class="empty-hint">暂无数据，点击刷新</div>
    </div>
  </div>
</template>

<style scoped>
.view { max-width: 1200px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.empty-hint { text-align: center; color: #909399; padding: 40px; }
</style>
