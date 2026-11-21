import { defineConfig } from 'vite'
import tailwind from '@tailwindcss/vite'

export default defineConfig({
  plugins: [tailwind()],
  envPrefix: 'VITE_PUBLIC_',
  server: {
    proxy: { '/api': { target: 'http://127.0.0.1:8000', proxyTimeout: 130000, timeout: 135000 } },
  },
})
