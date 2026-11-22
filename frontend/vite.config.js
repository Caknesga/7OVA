import tailwind from '@tailwindcss/vite'
import { defineConfig, loadEnv } from 'vite'

function registerReadOnlyApi(server) {
  server.middlewares.use('/api', (request, response, next) => {
    if (request.method === 'GET' || request.method === 'HEAD') return next()
    response.writeHead(405, { Allow: 'GET, HEAD' })
    response.end()
  })
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'WORKFLOW_BACKEND_')
  const proxy = {
    '/api': { target: env.WORKFLOW_BACKEND_URL || 'http://127.0.0.1:8000' },
  }
  return {
    envPrefix: 'VITE_PUBLIC_',
    plugins: [tailwind(), {
      name: 'read-only-workflow-api',
      configureServer: registerReadOnlyApi,
      configurePreviewServer: registerReadOnlyApi,
    }],
    server: { proxy },
    preview: { proxy },
  }
})
