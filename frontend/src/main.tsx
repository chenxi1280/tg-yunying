import React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';

import App from './app/App';
import 'antd/dist/reset.css';
import './styles/index.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      refetchOnWindowFocus: false,
    },
  },
});

const rootElement = document.getElementById('root')! as HTMLElement & { tgOpsRoot?: Root };
const root = rootElement.tgOpsRoot ?? createRoot(rootElement);
rootElement.tgOpsRoot = root;
root.render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: '#2563eb',
          colorSuccess: '#16a34a',
          colorWarning: '#d97706',
          colorError: '#dc2626',
          colorInfo: '#0f766e',
          borderRadius: 8,
          fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        },
        components: {
          Card: { borderRadiusLG: 8 },
          Table: { headerBg: '#f8fafc', rowHoverBg: '#f8fafc', cellPaddingBlock: 10, cellPaddingInline: 12 },
          Modal: { borderRadiusLG: 8 },
          Tag: { borderRadiusSM: 999 },
        },
      }}
      modal={{ mask: { blur: false } }}
    >
      <AntApp>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
