import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// The dev server proxies to the Python shell so the browser never needs CORS;
// in production the shell serves `dist/` itself.
export default defineConfig({
  plugins: [react()],
  // '@analytics' 是 SPA 内部自引前缀:商业版 umi 里 '@' 已指向它自己的 src,
  // 共用源码必须用一个不冲突的前缀。
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
      '@analytics': new URL('./src', import.meta.url).pathname,
    },
  },
  server: {
    port: 5273,
    proxy: {
      '/api': 'http://127.0.0.1:9395',
      '/v1': 'http://127.0.0.1:9395',
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          elk: ['elkjs/lib/elk.bundled.js'],
          flow: ['@xyflow/react'],
        },
      },
    },
  },
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
});
