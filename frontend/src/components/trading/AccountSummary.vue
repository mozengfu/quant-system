<script setup>
import { computed } from 'vue'
import { fmtMoney } from '../../utils/format'

const props = defineProps({
  balance: Object,
  connected: Boolean,
})

const totalAsset = computed(() => props.balance?.total_asset ?? props.balance?.total_asset ?? 0)
const available = computed(() => props.balance?.available ?? props.balance?.可用金额 ?? 0)
const marketValue = computed(() => props.balance?.market_value ?? props.balance?.股票市值 ?? 0)
</script>

<template>
  <el-row :gutter="12">
    <el-col :span="6">
      <el-card shadow="never" class="summary-card">
        <div class="summary-label">总资产</div>
        <div class="summary-value">¥{{ fmtMoney(totalAsset) }}</div>
      </el-card>
    </el-col>
    <el-col :span="6">
      <el-card shadow="never" class="summary-card">
        <div class="summary-label">可用资金</div>
        <div class="summary-value">{{ fmtMoney(available) }}</div>
      </el-card>
    </el-col>
    <el-col :span="6">
      <el-card shadow="never" class="summary-card">
        <div class="summary-label">股票市值</div>
        <div class="summary-value">{{ fmtMoney(marketValue) }}</div>
      </el-card>
    </el-col>
    <el-col :span="6">
      <el-card shadow="never" class="summary-card">
        <div class="summary-label">连接状态</div>
        <div class="summary-value">
          <el-tag :type="connected ? 'success' : 'danger'" size="small" effect="dark">
            {{ connected ? '已连接' : '未连接' }}
          </el-tag>
        </div>
      </el-card>
    </el-col>
  </el-row>
</template>

<style scoped>
.summary-card { text-align: center; }
.summary-label { font-size: 12px; color: #909399; margin-bottom: 4px; }
.summary-value { font-size: 20px; font-weight: 700; }
</style>
