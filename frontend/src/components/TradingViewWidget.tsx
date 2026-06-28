import { useEffect, useMemo, useRef, memo } from 'react';
import { normalizeTradingViewStudies } from '@/utils/tradingviewStudies';

export interface TradingViewStudy {
  id: string;
  inputs?: Record<string, any>;
}

interface TradingViewWidgetProps {
  symbol: string;
  exchange?: string;
  timeframe?: string;
  studies?: TradingViewStudy[];
  maximized?: boolean;
  onToggleMaximize?: () => void;
}

// Map internal timeframes to TradingView intervals
function mapTimeframe(tf: string): string {
  const map: Record<string, string> = {
    '1m': '1',
    '3m': '3',
    '5m': '5',
    '15m': '15',
    '30m': '30',
    '1h': '60',
    '2h': '120',
    '4h': '240',
    '1d': 'D',
    '1w': 'W',
    '1M': 'M',
  };
  return map[tf] || '60';
}

// Convert our symbol format (BTC/USDT) + exchange to TradingView symbol
function toTradingViewSymbol(symbol: string, exchange: string): string {
  const clean = symbol.replace('/', '');
  const exchangeMap: Record<string, string> = {
    bitget: 'BITGET',
    binance: 'BINANCE',
    bybit: 'BYBIT',
    okx: 'OKX',
    kucoin: 'KUCOIN',
    coinbase: 'COINBASE',
  };
  const tvExchange = exchangeMap[exchange.toLowerCase()] || exchange.toUpperCase();
  // Always use perpetual futures charts (.P suffix)
  return `${tvExchange}:${clean}.P`;
}

function TradingViewWidget({
  symbol,
  exchange = 'bitget',
  timeframe = '1h',
  studies = [],
  maximized = false,
  onToggleMaximize,
}: TradingViewWidgetProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const scriptRef = useRef<HTMLScriptElement | null>(null);
  const normalizedStudies = useMemo(() => normalizeTradingViewStudies(studies), [studies]);

  useEffect(() => {
    if (!containerRef.current) return;

    // Clear previous widget
    const container = containerRef.current;
    container.innerHTML = '';

    // Build widget container structure
    const widgetDiv = document.createElement('div');
    widgetDiv.className = 'tradingview-widget-container';
    widgetDiv.style.height = '100%';
    widgetDiv.style.width = '100%';

    const innerDiv = document.createElement('div');
    innerDiv.className = 'tradingview-widget-container__widget';
    innerDiv.style.height = '100%';
    innerDiv.style.width = '100%';
    widgetDiv.appendChild(innerDiv);

    container.appendChild(widgetDiv);

    // Format studies for TradingView widget
    const tvStudies = normalizedStudies.map(s => {
      if (s.inputs && Object.keys(s.inputs).length > 0) {
        return { id: s.id, inputs: s.inputs };
      }
      return s.id;
    });

    // Create and inject the widget script
    const script = document.createElement('script');
    script.type = 'text/javascript';
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.async = true;
    script.textContent = JSON.stringify({
      symbol: toTradingViewSymbol(symbol, exchange),
      interval: mapTimeframe(timeframe),
      timezone: 'Africa/Johannesburg',
      theme: 'dark',
      style: '1',
      locale: 'en',
      backgroundColor: '#0a0e27',
      gridColor: 'rgba(26, 30, 58, 0.8)',
      allow_symbol_change: true,
      calendar: false,
      details: false,
      hotlist: false,
      hide_side_toolbar: false,
      hide_top_toolbar: false,
      hide_legend: false,
      hide_volume: false,
      save_image: true,
      withdateranges: true,
      support_host: 'https://www.tradingview.com',
      studies: tvStudies,
      autosize: true,
    });

    widgetDiv.appendChild(script);
    scriptRef.current = script;

    return () => {
      container.innerHTML = '';
      scriptRef.current = null;
    };
  }, [symbol, exchange, timeframe, normalizedStudies]);

  const height = maximized ? 'calc(100vh - 120px)' : '500px';

  return (
    <div className="w-full">
      <div className="mb-2 flex justify-between items-center">
        <div>
          <h3 className="text-lg font-semibold">{symbol}</h3>
          <div className="text-xs text-gray-500">
            {exchange.toUpperCase()} • {timeframe} • TradingView
          </div>
        </div>
        <div className="flex items-center gap-2">
          {onToggleMaximize && (
            <button
              onClick={onToggleMaximize}
              className="p-1.5 rounded hover:bg-gray-700 text-gray-400 hover:text-white transition"
              title={maximized ? 'Exit fullscreen' : 'Maximize chart'}
            >
              {maximized ? (
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
              ) : (
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
              )}
            </button>
          )}
        </div>
      </div>
      <div
        ref={containerRef}
        className="rounded-lg overflow-hidden border border-gray-700"
        style={{ height }}
      />
    </div>
  );
}

export default memo(TradingViewWidget);
