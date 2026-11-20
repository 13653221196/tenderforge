import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 新版资料页单独构建到 web/vue，旧页面与后端接口都不动。
export default defineConfig({
  base: '/vue/',
  plugins: [vue()],
  build: {
    outDir: '../web/vue',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        home: fileURLToPath(new URL('./home.html', import.meta.url)),
        materials: fileURLToPath(new URL('./materials.html', import.meta.url)),
        write: fileURLToPath(new URL('./write.html', import.meta.url)),
        check: fileURLToPath(new URL('./check.html', import.meta.url)),
        requirements: fileURLToPath(new URL('./requirements.html', import.meta.url)),
        kb: fileURLToPath(new URL('./kb.html', import.meta.url)),
        reqmatrix: fileURLToPath(new URL('./reqmatrix.html', import.meta.url)),
        files: fileURLToPath(new URL('./files.html', import.meta.url)),
        settings: fileURLToPath(new URL('./settings.html', import.meta.url)),
        runtime: fileURLToPath(new URL('./runtime.html', import.meta.url)),
      },
    },
  },
})
