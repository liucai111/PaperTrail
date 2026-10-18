import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'VITE_')
  const apiProxyTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8011'
  const buildId = env.VITE_BUILD_ID || 'unmanaged'
  return {
    plugins: [vue(), {
      name: 'papertrail-build-identity',
      transformIndexHtml(html) {
        return html.replace('</head>', `<meta name="papertrail-build" content="${buildId}"></head>`)
      }
    }],
    server: { proxy: { '/api': apiProxyTarget } },
    preview: { proxy: { '/api': apiProxyTarget } }
  }
})
