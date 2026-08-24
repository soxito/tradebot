/**
 * AI provider roster — the connected accounts the agents actually think with.
 *
 * Several rows may share a `provider_key`: that is how more than one key for the
 * same provider is held, and the router load-balances across all of them. The
 * API never returns a key, only `api_key_set`, so nothing here can leak one.
 */
import { useCallback, useEffect, useState } from 'react'

import { api } from '@/services/api'

const BASE = '/plugins/ai-analyst/ai'

export interface ModelInfo {
  label?: string
  context?: number
  params?: string
  speed?: number
  strengths?: string[]
  best_for?: string
  cost?: string
  notes?: string
}

export interface AiProvider {
  id: number
  provider_key: string
  label: string
  type: string
  api_key_set: boolean
  /** First five and last four characters only — never the whole key. */
  api_key_preview: string | null
  base_url: string | null
  default_model: string | null
  models: string[]
  model_info: Record<string, ModelInfo>
  enabled: boolean
  priority: number
  free_tier: boolean
  status: string
  last_error: string | null
  last_tested_at: string | null
  last_model_used: string | null
  total_calls: number
  total_errors: number
  daily_limit: number | null
  monthly_limit: number | null
  daily_calls: number
  monthly_calls: number
}

export interface ProviderPreset {
  key: string
  label: string
  type: string
  base_url: string
  default_model: string
  models: string[]
  model_info?: Record<string, ModelInfo>
  free_tier: boolean
  daily_limit: number | null
  monthly_limit: number | null
  signup_url?: string
  notes?: string
  editable_endpoint?: boolean
}

export interface RouterSettings {
  strategy: 'priority' | 'round_robin' | 'least_used'
  agents_use_providers: boolean
  agent_token_mode: 'telegram_only' | 'always'
  per_agent_max_tokens: number
  reserve_pct: number
  headroom_enabled: boolean
  graphify_enabled: boolean
}

export interface NewProvider {
  provider_key: string
  label?: string
  api_key: string
  base_url?: string
  default_model?: string
  priority?: number
  daily_limit?: number | null
  monthly_limit?: number | null
}

export function useAiProviders() {
  const [providers, setProviders] = useState<AiProvider[]>([])
  const [presets, setPresets] = useState<ProviderPreset[]>([])
  const [settings, setSettings] = useState<RouterSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState<number | 'all' | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [p, pr, s] = await Promise.all([
        api.get<AiProvider[]>(`${BASE}/providers`),
        api.get<ProviderPreset[]>(`${BASE}/providers/presets`),
        api.get<RouterSettings>(`${BASE}/router-settings`),
      ])
      setProviders(p.data)
      setPresets(pr.data)
      setSettings(s.data)
    } catch (e: any) {
      setError(e?.response?.data?.detail ?? 'Could not reach the backend. Is it running?')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const addProvider = useCallback(async (body: NewProvider) => {
    const { data } = await api.post<AiProvider>(`${BASE}/providers`, body)
    setProviders((prev) => [...prev, data])
    return data
  }, [])

  const updateProvider = useCallback(async (id: number, patch: Partial<AiProvider> & { api_key?: string }) => {
    const { data } = await api.put<AiProvider>(`${BASE}/providers/${id}`, patch)
    setProviders((prev) => prev.map((p) => (p.id === id ? data : p)))
    return data
  }, [])

  const deleteProvider = useCallback(async (id: number) => {
    await api.delete(`${BASE}/providers/${id}`)
    setProviders((prev) => prev.filter((p) => p.id !== id))
  }, [])

  /** Test one key. The row is refreshed so status/last_error reflect the result. */
  const testProvider = useCallback(async (id: number) => {
    setTesting(id)
    try {
      await api.post(`${BASE}/providers/${id}/test`)
    } finally {
      setTesting(null)
      // The test writes status server-side; re-read rather than guess.
      const { data } = await api.get<AiProvider[]>(`${BASE}/providers`)
      setProviders(data)
    }
  }, [])

  const testAll = useCallback(async () => {
    setTesting('all')
    try {
      await api.post(`${BASE}/providers/test-all`)
    } finally {
      setTesting(null)
      const { data } = await api.get<AiProvider[]>(`${BASE}/providers`)
      setProviders(data)
    }
  }, [])

  const saveSettings = useCallback(async (patch: Partial<RouterSettings>) => {
    const { data } = await api.put<RouterSettings>(`${BASE}/router-settings`, patch)
    setSettings(data)
    return data
  }, [])

  return {
    providers, presets, settings, loading, error, testing,
    reload: load, addProvider, updateProvider, deleteProvider,
    testProvider, testAll, saveSettings,
  }
}
