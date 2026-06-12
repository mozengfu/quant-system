<script setup>
import { fmtPrice, fmtPct, profitClass, klineUrl } from '../../utils/format'

defineProps({
  positions: { type: Array, default: () => [] },
  loading: Boolean,
})

const emit = defineEmits(['fill-order', 'refresh'])
</script>

<template>
  <el-card shadow="never">
    <template #header>
      <div class="card-header">
        <span>当前持仓</span>
        <el-button text size="small" @click="emit('refresh')">
          <el-icon><Refresh /></el-icon>
        </el-button>
      </div>
    </template>
    <el-table
      :data="positions"
      size="small"
      stripe
      max-height="400"
      v-loading="loading"
      @row-click="(row) => emit('fill-order', row)"
      style="cursor: pointer;"
    >
      <el-table-column prop="ts_code" label="代码" width="105">
        <template #default="{ row }">
          <a :href="klineUrl(row.ts_code)" target="_blank" rel="noopener">{{ row.ts_code }}</a>
        </template>
      </el-table-column>
      <el-table-column prop="name" label="名称" min-width="90" />
      <el-table-column prop="quantity" label="数量" width="80" align="right" />
      <el-table-column prop="cost_price" label="成本" width="80" align="right" :formatter="(r) => fmtPrice(r.cost_price)" />
      <el-table-column prop="current_price" label="现价" width="80" align="right" :formatter="(r) => fmtPrice(r.current_price)" />
      <el-table-column label="盈亏" width="100" align="right">
        <template #default="{ row }">
          <span :class="profitClass(row.pnl)">
            {{ row.pnl >= 0 ? '+' : '' }}{{ fmtPrice(row.pnl) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="盈亏率" width="80" align="right">
        <template #default="{ row }">
          <span :class="profitClass(row.pnl_pct)">
            {{ fmtPct(row.pnl_pct) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="market_value" label="市值" width="90" align="right" :formatter="(r) => fmtPrice(r.market_value)" />
    </el-table>
    <div v-if="!positions.length && !loading" class="empty-hint">
      暂无持仓数据
    </div>
    <div class="click-hint" v-if="positions.length">点击行自动填入卖出面板</div>
  </el-card>
</template>

<style scoped>
.card-header { display: flex; justify-content: space-between; align-items: center; }
.empty-hint { text-align: center; color: #909399; padding: 20px; font-size: 13px; }
.click-hint { margin-top: 6px; font-size: 11px; color: #c0c4cc; text-align: right; }
:deep(.el-table__row) { cursor: pointer; }
</style>
