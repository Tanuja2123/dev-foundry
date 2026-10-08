import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Read .env from the repo root (DevFoundry/.env) instead of frontend/.env
  envDir: '..',
  server: {
    port: 5173,
    strictPort: true,
  },
})
