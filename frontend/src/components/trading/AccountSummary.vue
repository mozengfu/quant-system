<template>
  <div class="account-summary">
    <div class="account-row">
      <div class="account-item">
        <span class="label">总资产</span>
        <span class="value main">{{ fmt(balance.总资产 || 0) }}</span>
      </div>
      <div class="account-item">
        <span class="label">可用资金</span>
        <span class="value">{{ fmt(balance.可用金额 || 0) }}</span>
      </div>
      <div class="account-item">
        <span class="label">持仓市值</span>
        <span class="value">{{ fmt(balance.股票市值 || 0) }}</span>
      </div>
      <div class="account-item">
        <span class="label">持仓数量</span>
        <span class="value">{{ positionCount }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
defineProps({
  balance: { type: Object, default: () => ({}) },
  positionCount: { type: Number, default: 0 },
})

function fmt(v) {
  return '¥' + Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}
</script>

<style scoped>
.account-summary {
  width: 100%;
}
.account-row {
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
}
.account-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 100px;
}
.label {
  font-size: 12px;
  color: #909399;
}
.value {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}
.value.main {
  color: #409eff;
}
</style>
