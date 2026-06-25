<template>
  <el-table :data="trades" stripe style="width: 100%" size="small" empty-text="暂无交易记录">
    <el-table-column label="时间" width="140">
      <template #default="{ row }">{{ row.created_at || row.trade_time || '-' }}</template>
    </el-table-column>
    <el-table-column prop="ts_code" label="代码" width="80" />
    <el-table-column prop="stock_name" label="名称" width="100" />
    <el-table-column label="方向" width="60">
      <template #default="{ row }">
        <el-tag :type="(row.trade_type || row.signal_type || '').includes('卖') ? 'danger' : 'success'" size="small" effect="plain">
          {{ (row.trade_type || row.signal_type || '').includes('卖') ? '卖出' : '买入' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column prop="price" label="价格" width="80" align="right">
      <template #default="{ row }">{{ row.price ? Number(row.price).toFixed(2) : '-' }}</template>
    </el-table-column>
    <el-table-column prop="shares" label="数量" width="70" align="right" />
    <el-table-column label="盈亏" width="100" align="right">
      <template #default="{ row }">
        <span v-if="row.profit != null" :class="row.profit >= 0 ? 'profit' : 'loss'">
          {{ row.profit >= 0 ? '+' : '' }}{{ Number(row.profit).toFixed(2) }}
        </span>
        <span v-else class="neutral">-</span>
      </template>
    </el-table-column>
  </el-table>
</template>

<script setup>
defineProps({
  trades: { type: Array, default: () => [] },
})
</script>
