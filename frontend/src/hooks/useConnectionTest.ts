/**
 * Connection Test Hook
 * Automatically tests API connectivity and provides status
 */
import { useEffect, useState, useCallback } from 'react';
import { apiClient } from '../services/api';

interface ConnectionStatus {
  isConnected: boolean;
  isLoading: boolean;
  error: string | null;
  lastChecked: Date | null;
  serverVersion: string | null;
}

export function useConnectionTest(autoRetry: boolean = true) {
  const [status, setStatus] = useState<ConnectionStatus>({
    isConnected: false,
    isLoading: true,
    error: null,
    lastChecked: null,
    serverVersion: null,
  });

  const testConnection = useCallback(async () => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 4000);

    try {
      // Show loading only on the very first probe; keep retries stable in UI.
      setStatus(prev => ({ ...prev, isLoading: prev.lastChecked === null, error: null }));

      const response = await Promise.race([
        apiClient.status({ signal: controller.signal }),
        new Promise<never>((_, reject) => {
          setTimeout(() => reject(new Error('Connection check timed out')), 4000);
        }),
      ]);
      
      setStatus({
        isConnected: true,
        isLoading: false,
        error: null,
        lastChecked: new Date(),
        serverVersion: response.data?.version || null,
      });
      
      return true;
    } catch (error: any) {
      const errorMessage =
        error?.code === 'ERR_CANCELED'
          ? 'Connection check timed out'
          : error?.message || 'Failed to connect to API';
      
      setStatus({
        isConnected: false,
        isLoading: false,
        error: errorMessage,
        lastChecked: new Date(),
        serverVersion: null,
      });
      
      return false;
    } finally {
      clearTimeout(timeoutId);
    }
  }, []);

  useEffect(() => {
    let running = false;
    let mounted = true;

    const runTest = async () => {
      if (running || !mounted) return;
      running = true;
      try {
        await testConnection();
      } finally {
        running = false;
      }
    };

    // Initial connection test
    runTest();

    if (autoRetry) {
      // Retry every 10 seconds if disconnected
      const interval = setInterval(() => {
        if (!status.isConnected) {
          runTest();
        }
      }, 10000);

      return () => {
        mounted = false;
        clearInterval(interval);
      };
    }

    return () => {
      mounted = false;
    };
  }, [testConnection, autoRetry, status.isConnected]);

  return {
    ...status,
    testConnection,
    retry: testConnection,
  };
}
