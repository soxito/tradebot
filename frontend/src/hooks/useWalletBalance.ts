/**
 * Wallet Balance Hook
 * Fetches and tracks wallet balances from configured exchanges.
 * Uses native Bitget SDK for Bitget (both spot and futures), ccxt for other exchanges.
 */
import { useEffect, useState, useCallback } from 'react';
import { apiClient } from '../services/api';

interface Balance {
  currency: string;
  free: number;
  used: number;
  total: number;
}

interface ExchangeBalance {
  exchange: string;
  balances: Balance[];
  totalUSD: number;
  futuresUSD?: number; // Bitget-specific futures balance
  status: 'loading' | 'success' | 'error';
  error?: string;
}

interface WalletBalanceData {
  exchanges: ExchangeBalance[];
  totalBalance: number;
  isLoading: boolean;
  lastUpdated: Date | null;
}

interface ExchangeInitStatus {
  initialized?: boolean;
}

export function useWalletBalance(autoRefresh: boolean = true, refreshInterval: number = 30000) {
  const [data, setData] = useState<WalletBalanceData>({
    exchanges: [],
    totalBalance: 0,
    isLoading: true,
    lastUpdated: null,
  });

  const fetchBalances = useCallback(async () => {
    setData(prev => ({ ...prev, isLoading: true }));

    try {
      // Get list of configured exchanges
      const statusResponse = await apiClient.getExchangesStatus();
      const exchangeStatus = (statusResponse.data?.exchanges ?? {}) as Record<string, ExchangeInitStatus>;

      // Fetch balances for each initialized exchange
      const balancePromises: Promise<ExchangeBalance>[] = Object.entries(exchangeStatus)
        .filter(([_, status]) => Boolean(status?.initialized))
        .map(async ([exchangeName]): Promise<ExchangeBalance> => {
          try {
            // Use native SDK endpoint for Bitget, standard ccxt for others
            if (exchangeName === 'bitget') {
              return await fetchBitgetBalance();
            }

            const balanceResponse = await apiClient.getBalance(exchangeName);
            const balanceData = (balanceResponse.data?.balance ?? {}) as Record<
              string,
              { free?: number; used?: number; total?: number }
            >;

            // Convert balance object to array format
            const balances: Balance[] = Object.entries(balanceData)
              .filter(([_, data]) => typeof data === 'object' && data !== null && (data.total ?? 0) > 0)
              .map(([currency, data]) => ({
                currency,
                free: Number(data.free ?? 0),
                used: Number(data.used ?? 0),
                total: Number(data.total ?? 0),
              }));

            const totalUSD = balances.reduce((sum, bal) => {
              if (['USDT', 'USD', 'BUSD', 'USDC'].includes(bal.currency)) {
                return sum + bal.total;
              }
              return sum;
            }, 0);

            return {
              exchange: exchangeName,
              balances,
              totalUSD,
              futuresUSD: 0,
              status: 'success' as const,
            };
          } catch (error: unknown) {
            const errorMsg =
              (error as { response?: { data?: { detail?: string } }; message?: string })?.response?.data?.detail ||
              (error as { message?: string })?.message ||
              'Failed to fetch balance';
            const isPermissionError = errorMsg.includes('permissions') || errorMsg.includes('Incorrect permissions');
            
            return {
              exchange: exchangeName,
              balances: [],
              totalUSD: 0,
              futuresUSD: 0,
              status: 'error' as const,
              error: isPermissionError 
                ? `API key needs "spot order read" permissions. Update in ${exchangeName} dashboard.`
                : errorMsg,
            };
          }
        });

      const exchangeBalances = await Promise.all(balancePromises);
      const totalBalance = exchangeBalances.reduce((sum, ex) => sum + ex.totalUSD + (ex.futuresUSD || 0), 0);

      setData({
        exchanges: exchangeBalances,
        totalBalance,
        isLoading: false,
        lastUpdated: new Date(),
      });
    } catch (error: any) {
      console.error('Failed to fetch wallet balances:', error);
      setData(prev => ({
        ...prev,
        isLoading: false,
        lastUpdated: new Date(),
      }));
    }
  }, []);

  useEffect(() => {
    fetchBalances();

    if (autoRefresh) {
      const interval = setInterval(fetchBalances, refreshInterval);
      return () => clearInterval(interval);
    }
  }, [fetchBalances, autoRefresh, refreshInterval]);

  return {
    ...data,
    refresh: fetchBalances,
  };
}

/**
 * Fetch Bitget balance using the native v2 SDK endpoint.
 * Returns detailed asset data with proper available/frozen/locked breakdown.
 * Also fetches futures balance.
 */
async function fetchBitgetBalance(): Promise<ExchangeBalance> {
  try {
    // Fetch both spot and futures balances in parallel
    const [spotResponse, futuresResponse] = await Promise.all([
      apiClient.getBitgetAssets(),
      apiClient.getBitgetFuturesBalance('USDT-FUTURES'),
    ]);

    const assets: any[] = spotResponse.data.assets || [];

    const balances: Balance[] = assets
      .filter((a: any) => {
        const total = parseFloat(a.available || '0') + parseFloat(a.frozen || '0') + parseFloat(a.locked || '0');
        return total > 0;
      })
      .map((a: any) => ({
        currency: a.coin,
        free: parseFloat(a.available || '0'),
        used: parseFloat(a.frozen || '0') + parseFloat(a.locked || '0'),
        total: parseFloat(a.available || '0') + parseFloat(a.frozen || '0') + parseFloat(a.locked || '0'),
      }));

    const totalUSD = balances.reduce((sum, bal) => {
      if (['USDT', 'USD', 'BUSD', 'USDC'].includes(bal.currency)) {
        return sum + bal.total;
      }
      return sum;
    }, 0);

    // Calculate futures balance
    const futuresBalance: any[] = futuresResponse.data.balance || [];
    const futuresUSD = futuresBalance.reduce((sum, b) => {
      return sum + parseFloat(b.usdtEquity || '0');
    }, 0);

    return {
      exchange: 'bitget',
      balances,
      totalUSD,
      futuresUSD,
      status: 'success',
    };
  } catch (error: any) {
    const errorMsg = error.response?.data?.detail || error.message || 'Failed to fetch Bitget balance';
    return {
      exchange: 'bitget',
      balances: [],
      totalUSD: 0,
      futuresUSD: 0,
      status: 'error',
      error: errorMsg,
    };
  }
}
