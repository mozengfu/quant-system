import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  withCredentials: true,
  timeout: 30000,
})

// 自动添加 CSRF 头
api.interceptors.request.use((config) => {
  if (config.method && ['post', 'put', 'delete'].includes(config.method)) {
    config.headers['X-CSRF-Protection'] = '1'
  }
  return config
})

// 统一错误处理
api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const msg = err.response?.data?.detail || err.response?.data?.error || err.message || '请求失败'
    console.error('[API Error]', msg)
    return Promise.reject(new Error(msg))
  },
)

export default api
