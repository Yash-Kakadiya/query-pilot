import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: {
    proxy: {
      // Development-only proxy targeting QueryPilot local-agent daemon.
      // Note: This Vite proxy is strictly for local development (npm run dev)
      // and does not solve production HTTPS-to-loopback deployment by itself
      // (e.g. browser Mixed Content restrictions). In production, the client connects
      // via the agent-hosted app (http://127.0.0.1:8001/app/) or a packaged wrapper.
      // changeOrigin: true rewrites the Host header to 127.0.0.1:8001, satisfying HostOriginValidationMiddleware.
      '/agent': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
      '/api/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
