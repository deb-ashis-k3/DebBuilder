import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Override to point the editor at another API instance: BACKEND_URL=http://localhost:8001 npm run dev
const backend = process.env.BACKEND_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/webhook': backend,
      '/webhook-test': backend,
      '/form': backend,
      '/ws': { target: backend, ws: true },
    },
  },
})
