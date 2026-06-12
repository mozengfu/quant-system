<script setup>
import { ref, onMounted } from 'vue'
import api from '../api'
import { ElMessage, ElMessageBox } from 'element-plus'

const user = ref('')
const pendingUsers = ref([])
const activeUsers = ref({})
const loading = ref(false)
const authError = ref(false)
const activeTab = ref('users')

async function checkAuth() {
  try {
    const r = await api.get('/auth/me')
    user.value = r?.user || ''
  } catch {
    authError.value = true
  }
}

async function fetchUsers() {
  loading.value = true
  try {
    const [pending, active] = await Promise.allSettled([
      api.get('/admin/pending'),
      api.get('/admin/users'),
    ])
    if (pending.status === 'fulfilled') pendingUsers.value = Object.entries(pending.value?.pending || {})
    if (active.status === 'fulfilled') activeUsers.value = active.value?.users || {}
  } catch (e) {
    if (e.message?.includes('未登录')) authError.value = true
  } finally {
    loading.value = false
  }
}

async function approveUser(username) {
  try {
    const { value: days } = await ElMessageBox.prompt(
      `审核通过 ${username}，设置有效期（天）`, '审核通过',
      { inputValue: '365', inputValidator: v => /^\d+$/.test(v) || '请输入数字' }
    )
    const r = await api.post('/admin/approve', { username, duration: parseInt(days) })
    ElMessage.success(r.message || '已通过')
    fetchUsers()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('操作失败')
  }
}

async function rejectUser(username) {
  try {
    await ElMessageBox.confirm(`确认拒绝 ${username}？`, '拒绝用户')
    await api.post('/admin/reject', { username })
    ElMessage.success('已拒绝')
    fetchUsers()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error('操作失败')
  }
}

onMounted(() => {
  checkAuth()
  fetchUsers()
})
</script>

<template>
  <div class="view">
    <div class="view-header">
      <h2>管理后台</h2>
      <div class="header-actions">
        <el-tag v-if="user" type="primary" size="small">{{ user }}</el-tag>
        <el-button size="small" @click="fetchUsers"><el-icon><Refresh /></el-icon></el-button>
      </div>
    </div>

    <el-alert v-if="authError" title="未登录或无权限" type="warning" description="需要管理员账号登录" show-icon :closable="false">
      <template #footer>
        <el-button type="primary" size="small" @click="$router.push('/login')">登录</el-button>
      </template>
    </el-alert>

    <template v-else>
      <el-card shadow="never" style="margin-bottom: 16px;">
        <template #header>待审核用户 ({{ pendingUsers.length }})</template>
        <el-table v-if="pendingUsers.length" :data="pendingUsers" v-loading="loading" stripe>
          <el-table-column label="用户名" width="150">
            <template #default="{ row }">{{ row[0] }}</template>
          </el-table-column>
          <el-table-column label="邮箱" min-width="200">
            <template #default="{ row }">{{ row[1]?.email || '-' }}</template>
          </el-table-column>
          <el-table-column label="注册时间" width="180">
            <template #default="{ row }">{{ row[1]?.register_time || '-' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="200">
            <template #default="{ row }">
              <el-button type="success" size="small" @click="approveUser(row[0])">通过</el-button>
              <el-button type="danger" size="small" @click="rejectUser(row[0])">拒绝</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-else class="empty-hint">暂无待审核用户</div>
      </el-card>

      <el-card shadow="never">
        <template #header>已激活用户 ({{ Object.keys(activeUsers).length }})</template>
        <el-table v-if="Object.keys(activeUsers).length" :data="Object.entries(activeUsers)" stripe>
          <el-table-column label="用户名" width="150">
            <template #default="{ row }">{{ row[0] }}</template>
          </el-table-column>
          <el-table-column label="邮箱" min-width="200">
            <template #default="{ row }">{{ row[1]?.email || '-' }}</template>
          </el-table-column>
          <el-table-column label="注册时间" width="180">
            <template #default="{ row }">{{ row[1]?.register_time || '-' }}</template>
          </el-table-column>
          <el-table-column label="过期时间" width="120">
            <template #default="{ row }">{{ row[1]?.expire_date || '-' }}</template>
          </el-table-column>
        </el-table>
        <div v-else class="empty-hint">暂无激活用户</div>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.view { max-width: 1200px; }
.view-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.view-header h2 { margin: 0; font-size: 20px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
.empty-hint { text-align: center; color: #909399; padding: 30px; font-size: 13px; }
</style>
