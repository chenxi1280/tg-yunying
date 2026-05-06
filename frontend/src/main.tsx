import React from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import App from './app/App';
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
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
