<template>
  <el-table :data="positions" stripe style="width: 100%" size="small" empty-text="暂无持仓">
    <el-table-column prop="code" label="代码" width="80" />
    <el-table-column prop="stock_name" label="名称" width="100" />
    <el-table-column label="策略" width="60">
      <template #default="{ row }">
        <el-tag :type="row.strategy === 'scanner' ? 'warning' : 'primary'" size="small" effect="plain">
          {{ row.strategy === 'scanner' ? '扫描' : 'ML' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column label="成本" width="90" align="right">
      <template #default="{ row }">{{ row.cost_price ? round(row.cost_price) : '-' }}</template>
    </el-table-column>
    <el-table-column label="现价" width="90" align="right">
      <template #default="{ row }">{{ row.current_price ? round(row.current_price, 3) : '-' }}</template>
    </el-table-column>
    <el-table-column label="盈亏" width="100" align="right">
      <template #default="{ row }">
        <span :class="row.profit >= 0 ? 'profit' : 'loss'">
          {{ row.profit >= 0 ? '+' : '' }}{{ row.profit ? row.profit.toFixed(2) : '0.00' }}
        </span>
      </template>
    </el-table-column>
    <el-table-column label="盈亏%" width="80" align="right">
      <template #default="{ row }">
        <span :class="row.pnl_pct >= 0 ? 'profit' : 'loss'">
          {{ row.pnl_pct >= 0 ? '+' : '' }}{{ (row.pnl_pct || 0).toFixed(1) }}%
        </span>
      </template>
    </el-table-column>
    <el-table-column prop="shares" label="持仓" width="70" align="right" />
    <el-table-column label="持有" width="60" align="center">
      <template #default="{ row }">
        <span v-if="row.days_held === 0" style="color:#e6a23c">T+1</span>
        <span v-else>{{ row.days_held || '-' }}天</span>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup>
defineProps({
  positions: { type: Array, default: () => [] },
})

function round(v, d = 2) {
  return Number(v).toFixed(d)
}
</script>
