import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 嵌入构建:自带 React 的 ES 模块,宿主动态 import 后调用 mount()。
// 只产 embed.js/.css —— 宿主 umi 提供路由与页面框架,不需要整页 index.html。
export default defineConfig({
  plugins: [react()],
  // lib 模式不像整页构建那样自动注入 NODE_ENV,而 React 在运行时读它。
  // 不注入的话宿主一加载 embed.js 就是 ReferenceError: process is not defined。
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  resolve: {
    alias: {
      '@': new URL('./src', import.meta.url).pathname,
      '@analytics': new URL('./src', import.meta.url).pathname,
    },
  },
  build: {
    outDir: 'dist-embedded',
    emptyOutDir: true,
    sourcemap: false,
    lib: { entry: 'src/embed.tsx', formats: ['es'], fileName: () => 'embed.js' },
    rollupOptions: { output: { assetFileNames: 'embed[extname]' } },
  },
});
