<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import { ElMessage } from 'element-plus'

const router = useRouter()
const username = ref('')
const password = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function doLogin() {
  if (!username.value || !password.value) {
    errorMsg.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  errorMsg.value = ''
  try {
    const r = await api.post('/auth/login', {
      username: username.value,
      password: password.value,
    })
    if (r.success) {
      ElMessage.success('登录成功')
      // 跳回之前页面或默认到管线工作台
      const redirect = router.currentRoute.value.query.redirect || '/'
      router.push(redirect)
    } else {
      errorMsg.value = r.detail || r.error || '登录失败'
    }
  } catch (e) {
    errorMsg.value = e.message || '登录失败，请检查用户名密码'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-wrapper">
    <div class="login-card">
      <h2 class="login-title">智量</h2>
      <p class="login-subtitle">量化交易系统</p>
      <el-form @submit.prevent="doLogin" label-position="top">
        <el-form-item label="用户名">
          <el-input v-model="username" placeholder="请输入用户名" @keyup.enter="doLogin" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="password" type="password" placeholder="请输入密码" show-password @keyup.enter="doLogin" />
        </el-form-item>
        <el-alert v-if="errorMsg" :title="errorMsg" type="error" show-icon :closable="false" style="margin-bottom: 16px;" />
        <el-button type="primary" :loading="loading" style="width: 100%" @click="doLogin">
          登 录
        </el-button>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-wrapper {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
}
.login-card {
  width: 380px;
  padding: 40px 32px;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
.login-title {
  text-align: center;
  font-size: 28px;
  font-weight: 800;
  margin-bottom: 4px;
  background: linear-gradient(135deg, #2563eb, #6366f1);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.login-subtitle {
  text-align: center;
  color: #909399;
  font-size: 13px;
  margin-bottom: 32px;
}

@media (max-width: 420px) {
  .login-card {
    width: 90%;
    padding: 32px 20px;
    border-radius: 8px;
  }
}
</style>
