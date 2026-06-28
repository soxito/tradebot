import { create } from 'zustand';

interface TradeStore {
  selectedSymbol: string;
  selectedExchange: string;
  selectedTimeframe: string;
  sidebarOpen: boolean;
  tradingMode: 'sim' | 'live';
  setSelectedSymbol: (symbol: string) => void;
  setSelectedExchange: (exchange: string) => void;
  setSelectedTimeframe: (timeframe: string) => void;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;
  setTradingMode: (mode: 'sim' | 'live') => void;
}

export const useTradeStore = create<TradeStore>((set) => ({
  selectedSymbol: 'BTC/USDT',
  selectedExchange: 'bitget',
  selectedTimeframe: '1h',
  sidebarOpen: true,
  tradingMode: 'sim',
  setSelectedSymbol: (symbol) => set({ selectedSymbol: symbol }),
  setSelectedExchange: (exchange) => set({ selectedExchange: exchange }),
  setSelectedTimeframe: (timeframe) => set({ selectedTimeframe: timeframe }),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  toggleSidebar: () => set((state) => ({ sidebarOpen: !state.sidebarOpen })),
  setTradingMode: (mode) => set({ tradingMode: mode }),
}));
