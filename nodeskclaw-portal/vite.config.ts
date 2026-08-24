import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// 把本机回环地址追加到 NO_PROXY，避免开发机上的 Clash/V2Ray 等本地代理
// （HTTP_PROXY/HTTPS_PROXY 指向 127.0.0.1:7897 之类）把 Vite -> backend 的
// localhost 请求 RST 掉，导致 /api/* 出现 ECONNRESET。
const LOCAL_BYPASS = ['localhost', '127.0.0.1', '::1']
const existingNoProxy = (process.env.NO_PROXY ?? process.env.no_proxy ?? '')
  .split(',')
  .map((entry) => entry.trim())
  .filter(Boolean)
const mergedNoProxy = Array.from(new Set([...existingNoProxy, ...LOCAL_BYPASS])).join(',')
process.env.NO_PROXY = mergedNoProxy
process.env.no_proxy = mergedNoProxy

// API 代理目标：
// - 默认（本地开发）：vite 跑在宿主机上，backend 端口映射到宿主 localhost:4510，
//   直接用 localhost 即可（宿主机上 host.docker.internal 不可解析）。
// - DEPLOY_ENV=server（vite 跑在 compose 网络内的场景）：走服务名。
// - 容器内跑 vite dev 的特殊场景：显式设 API_PROXY_TARGET=http://host.docker.internal:4510。
const apiTarget =
  process.env.API_PROXY_TARGET ||
  (process.env.DEPLOY_ENV === 'server' ? 'http://nodeskclaw-backend:8000' : 'http://localhost:4510')

const projectRoot = path.resolve(__dirname, '..')
const eePortalDir = path.resolve(projectRoot, 'ee/frontend/portal')
const hasEE = fs.existsSync(eePortalDir)

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  define: {
    __APP_VERSION__: JSON.stringify(process.env.VITE_APP_VERSION || 'dev'),
  },
  resolve: {
    alias: [
      ...(hasEE
        ? [{ find: '@/router/ee-stub', replacement: path.resolve(eePortalDir, 'routes') }]
        : []),
      { find: '@', replacement: fileURLToPath(new URL('./src', import.meta.url)) },
    ],
    dedupe: hasEE
      ? ['vue', 'vue-router', 'vue-i18n', 'pinia', 'lucide-vue-next', '@vueuse/core']
      : [],
  },
  server: {
    host: true,
    port: 4517,
    fs: {
      allow: ['.', ...(hasEE ? [eePortalDir] : [])],
    },
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          three: ['three', 'troika-three-text'],
          d3: ['d3-zoom', 'd3-selection'],
        },
      },
    },
  },
})
