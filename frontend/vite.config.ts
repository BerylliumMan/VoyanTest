/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import svgrPlugin from '@arco-plugins/vite-plugin-svgr';
import vitePluginForArco from '@arco-plugins/vite-react';
import path from 'path';
import setting from './src/settings.json';

// https://vitejs.dev/config/
export default defineConfig({
  resolve: {
    alias: [{ find: '@', replacement: path.resolve(__dirname, 'src') }],
  },
  plugins: [
    react(),
    svgrPlugin({
      svgrOptions: {},
    }),
    vitePluginForArco({
      theme: '@arco-themes/react-arco-pro',
      modifyVars: {
        'arcoblue-6': setting.themeColor,
      },
    }),
  ],
  css: {
    preprocessorOptions: {
      less: {
        javascriptEnabled: true,
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8002',
      '/health': 'http://localhost:8002',
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 038 US6：第三方依赖单独分包（长缓存；首屏只加载必需 vendor）
        manualChunks: function (id: string) {
          if (!id.includes('node_modules')) return undefined;
          if (id.includes('@arco-design')) return 'arco';
          if (id.includes('/react') || id.includes('scheduler') || id.includes('react-router')) {
            return 'react-vendor';
          }
          if (id.includes('lodash') || id.includes('axios')) return 'vendor-utils';
          // 其余第三方库不强行归并：仅被某个页面使用的重库（如图表）随页面异步 chunk 加载
          return undefined;
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
  },
});
