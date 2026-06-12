<script setup>
import { ref, computed, onMounted } from 'vue'
import { useMarketStore } from '../../stores/market'
import { useTradingStore } from '../../stores/trading'
import api from '../../api'

const props = defineProps({
  isMobile: Boolean,
})
const emit = defineEmits(['toggle-sidebar'])

const market = useMarketStore()
const trading = useTradingStore()
const user = ref('')
const checkedAuth = ref(false)

const stateTagType = computed(() => {
  const s = market.state?.state
  if (s === '恐慌' || s === '恐慌清仓') return 'danger'
  if (s === '阻断') return 'warning'
  if (s === '逆市') return 'warning'
  if (s === '偏弱') return 'info'
  return 'success'
})

const shIndex = computed(() => market.premarket?.indices?.['上证指数'])

async function checkAuth() {
  try {
    const r = await api.get('/auth/me')
    if (r?.user) user.value = r.user
  } catch {
    user.value = ''
  } finally {
    checkedAuth.value = true
  }
}

onMounted(() => {
  market.fetchState()
  trading.checkStatus()
  checkAuth()
})
</script>

<template>
  <el-header class="app-header">
    <div class="header-left">
      <el-button v-if="isMobile" class="hamburger-btn" text @click="emit('toggle-sidebar')">
        <el-icon size="20"><Operation /></el-icon>
      </el-button>
      <span class="logo">智量</span>
      <span v-if="!isMobile" class="logo-sub">量化交易系统</span>
    </div>
    <div v-if="!isMobile" class="header-center">
      <template v-if="shIndex">
        <span class="index-value" :class="shIndex.涨跌幅 >= 0 ? 'up' : 'down'">
          上证 {{ shIndex.最新价?.toFixed(2) }}
          <span class="change">{{ shIndex.涨跌幅 >= 0 ? '+' : '' }}{{ shIndex.涨跌幅?.toFixed(2) }}%</span>
        </span>
      </template>
      <el-tag v-if="market.state?.state" :type="stateTagType" size="small" effect="dark">
        {{ market.state.state }}
      </el-tag>
      <span v-if="market.state?.position_ratio != null" class="position-hint">
        仓位建议: {{ market.state.position_ratio }}%
      </span>
    </div>
    <div class="header-right">
      <template v-if="checkedAuth">
        <el-button v-if="!user" text size="small" @click="$router.push('/login')">
          {{ isMobile ? '' : '登录' }}
          <el-icon v-if="isMobile"><User /></el-icon>
        </el-button>
        <span v-else class="user-name">{{ user }}</span>
      </template>
      <el-tag v-if="!isMobile" :type="trading.connected ? 'success' : 'info'" size="small" effect="plain">
        QMT: {{ trading.connected ? '已连接' : '未连接' }}
      </el-tag>
    </div>
  </el-header>
</template>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 0 20px;
  height: 48px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.hamburger-btn {
  margin-left: -8px;
  font-size: 18px;
}
.logo {
  font-size: 18px;
  font-weight: 800;
  background: linear-gradient(135deg, #2563eb, #6366f1);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.logo-sub {
  font-size: 12px;
  color: #909399;
}
.header-center {
  display: flex;
  align-items: center;
  gap: 16px;
}
.index-value { font-size: 14px; font-weight: 600; }
.index-value.up { color: #ef4444; }
.index-value.down { color: #10b981; }
.change { font-weight: 400; margin-left: 4px; }
.position-hint { font-size: 12px; color: #909399; }
.header-right { display: flex; align-items: center; gap: 12px; }
.user-name { font-size: 13px; color: #409eff; font-weight: 500; }

@media (max-width: 767px) {
  .app-header {
    padding: 0 12px;
  }
}
</style>
