<script setup>
import { reactive, computed, ref } from 'vue'
import { useTradingStore } from '../../stores/trading'
import { ElMessage } from 'element-plus'

const emit = defineEmits(['order-placed'])
const store = useTradingStore()

const form = reactive({
  code: '',
  name: '',
  price: '',
  amount: '',
  side: 'BUY',
  orderType: 'limit',
})

const title = computed(() => form.side === 'BUY' ? '买入' : '卖出')
const submitBtnType = computed(() => form.side === 'BUY' ? 'danger' : 'success')
const submitLoading = ref(false)

async function submitOrder() {
  if (!form.code || !form.amount) {
    ElMessage.warning('请填写股票代码和数量')
    return
  }
  const code = form.code.toUpperCase().trim()
  const price = form.orderType === 'market' ? 0 : parseFloat(form.price)
  const amount = parseInt(form.amount)

  if (!amount || amount <= 0) {
    ElMessage.warning('数量必须大于 0')
    return
  }

  submitLoading.value = true
  try {
    if (form.orderType === 'market') {
      // 市价单走 /trading/market-order 端点（QMT 对手价立即成交）
      if (form.side === 'BUY') {
        await store.marketBuy(code, price, amount, form.name)
      } else {
        await store.marketSell(code, price, amount, form.name)
      }
    } else {
      if (form.side === 'BUY') {
        await store.buy(code, price, amount, form.name)
      } else {
        await store.sell(code, price, amount, form.name)
      }
    }
    ElMessage.success(`${title.value}单已提交`)
    form.code = ''
    form.name = ''
    form.price = ''
    form.amount = ''
    emit('order-placed')
  } catch (e) {
    ElMessage.error(`${title.value}失败: ${e.message}`)
  } finally {
    submitLoading.value = false
  }
}

function fillFromPosition(row) {
  form.code = row.ts_code?.replace(/\.(SH|SZ)$/, '') || ''
  form.name = row.name || row.stock_name || ''
  form.price = row.current_price || ''
  form.side = 'SELL'
}

defineExpose({ fillFromPosition })
</script>


<template>
  <el-card shadow="never">
    <template #header>
      <span>下单面板</span>
    </template>
    <el-form label-position="top" size="small">
      <el-form-item label="方向">
        <el-radio-group v-model="form.side">
          <el-radio-button value="BUY">买入</el-radio-button>
          <el-radio-button value="SELL">卖出</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="股票代码">
        <el-input v-model="form.code" placeholder="如 000559" @keyup.enter="submitOrder" />
      </el-form-item>
      <el-form-item label="股票名称" v-if="form.name">
        <el-input v-model="form.name" disabled />
      </el-form-item>
      <el-form-item label="订单类型">
        <el-radio-group v-model="form.orderType">
          <el-radio-button value="limit">限价单</el-radio-button>
          <el-radio-button value="market">市价单</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="价格" v-if="form.orderType === 'limit'">
        <el-input v-model.number="form.price" type="number" step="0.01" placeholder="输入价格" />
      </el-form-item>
      <el-form-item label="数量（股）">
        <el-input v-model.number="form.amount" type="number" step="100" placeholder="100的整数倍" />
      </el-form-item>
      <el-form-item>
        <el-button
          :type="submitBtnType"
          :loading="submitLoading"
          @click="submitOrder"
          style="width: 100%;"
          size="default"
        >
          {{ title }}
        </el-button>
      </el-form-item>
    </el-form>
    <div class="balance-hint" v-if="store.balance">
      可用资金: ¥{{ store.balance?.available?.toFixed(2) || '--' }}
    </div>
  </el-card>
</template>

<style scoped>
.balance-hint {
  margin-top: 8px;
  font-size: 12px;
  color: #909399;
  text-align: center;
}
</style>
