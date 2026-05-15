import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
var __dirname = dirname(fileURLToPath(import.meta.url));
export default defineConfig({
    plugins: [react()],
    build: {
        chunkSizeWarningLimit: 600,
        rollupOptions: {
            output: {
                manualChunks: function (id) {
                    if (!id.includes('/node_modules/'))
                        return undefined;
                    if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id))
                        return 'vendor-react';
                    if (id.includes('/node_modules/lucide-react/') || id.includes('/node_modules/@ant-design/icons'))
                        return 'vendor-icons';
                    if (id.includes('/node_modules/@ant-design/cssinjs/'))
                        return 'vendor-antd-style';
                    return undefined;
                },
            },
        },
    },
    resolve: {
        alias: {
            '@': resolve(__dirname, 'src'),
        },
    },
    server: {
        host: '127.0.0.1',
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
            '/media': {
                target: 'http://127.0.0.1:8000',
                changeOrigin: true,
            },
        },
    },
});
