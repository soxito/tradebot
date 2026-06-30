import Head from 'next/head'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiClient } from '@/services/api'
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  LogIn,
  LogOut,
  MessageCircle,
  Plus,
  Power,
  RefreshCw,
  Save,
  Trash2,
} from 'lucide-react'

type SourceKind = 'signals' | 'news'
type Provider = 'auto' | 'telethon' | 'bot_api' | 'telegram_mcp'
type MethodsTestMode = 'binding' | 'invoke_readonly'
type AuthStep = 'idle' | 'enter_phone' | 'enter_code' | 'enter_2fa' | 'done'

interface TelegramAuthStatus {
  authenticated: boolean
  provider: string
  phone_number: string | null
  username: string | null
  first_name: string | null
}

interface TelegramProviderStatus {
  name: string
  available: boolean
  reason?: string | null
}

interface TelegramStatusResponse {
  plugin: string
  version: string
  providers: TelegramProviderStatus[]
  channels_total: number
  channels_enabled: number
  messages_total: number
}

interface TelegramChannelSource {
  id: number
  user_id: number
  title: string
  channel_handle: string
  channel_id: string | null
  source_kind: SourceKind
  provider: string
  is_enabled: boolean
  poll_interval_seconds: number
  include_keywords: string[] | null
  exclude_keywords: string[] | null
  language_hint: string | null
  last_message_id: string | null
  last_polled_at: string | null
  last_error: string | null
  created_at: string
  updated_at: string
}

interface TelegramDiscoveredChannel {
  title: string
  channel_handle: string
  channel_id: string | null
  provider: string
}

interface TelegramSubscribedChannelsResponse {
  provider: string
  total_subscribed: number
  channels: TelegramDiscoveredChannel[]
}

interface TelegramMethodsTestSummary {
  total_methods: number
  tested_methods: number
  passed: number
  failed: number
  unsupported: number
}

interface TelegramMethodsTestResponse {
  source_url: string
  provider: string
  mode: MethodsTestMode
  readonly_allowlist: string[]
  summary: TelegramMethodsTestSummary
}

interface ApiDetailsForm {
  apiId: string
  apiHash: string
  botToken: string
  mcpChatId: string
  mcpServerUrl: string
}

const API_DETAILS_STORAGE_KEY = 'tradebot.telegram.apiDetails.v1'

function toErrorMessage(error: unknown): string {
  if (typeof error === 'string') return error
  if (error && typeof error === 'object') {
    const errObj = error as {
      message?: string
      response?: { data?: { detail?: string } }
    }
    return errObj.response?.data?.detail || errObj.message || 'Request failed'
  }
  return 'Request failed'
}

function parseKeywordList(raw: string): string[] {
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function normalizeChannelHandle(raw: string): string {
  const value = raw.trim().toLowerCase()
  if (/^-?\d+$/.test(value)) {
    return value
  }
  return value.startsWith('@') ? value : `@${value}`
}

function isKnownProvider(value: string): value is Provider {
  return value === 'auto' || value === 'telethon' || value === 'bot_api' || value === 'telegram_mcp'
}

export default function TelegramPage() {
  const [status, setStatus] = useState<TelegramStatusResponse | null>(null)
  const [channels, setChannels] = useState<TelegramChannelSource[]>([])
  const [discoveredChannels, setDiscoveredChannels] = useState<TelegramDiscoveredChannel[]>([])
  const [discoveredTotal, setDiscoveredTotal] = useState(0)
  const [discoveredKinds, setDiscoveredKinds] = useState<Record<string, SourceKind>>({})
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [polling, setPolling] = useState(false)
  const [discovering, setDiscovering] = useState(false)
  const [savingChannel, setSavingChannel] = useState(false)
  const [savingDiscoveredHandle, setSavingDiscoveredHandle] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const autoDiscoveryTriggeredRef = useRef(false)

  // ── Account authentication state ──────────────────────────────────────
  const [authStatus, setAuthStatus] = useState<TelegramAuthStatus | null>(null)
  const [authStep, setAuthStep] = useState<AuthStep>('idle')
  const [authPhone, setAuthPhone] = useState('')
  const [authCodeHash, setAuthCodeHash] = useState('')
  const [authCode, setAuthCode] = useState('')
  const [auth2faPassword, setAuth2faPassword] = useState('')
  const [authLoading, setAuthLoading] = useState(false)
  const [authError, setAuthError] = useState<string | null>(null)
  const [authMessage, setAuthMessage] = useState<string | null>(null)

  // Credentials form state
  const [credForm, setCredForm] = useState({
    apiId: '',
    apiHash: '',
    phoneNumber: '',
    botToken: '',
    mcpChatId: '',
  })
  const [savingCreds, setSavingCreds] = useState(false)
  const [credsMessage, setCredsMessage] = useState<string | null>(null)
  const [testingConnection, setTestingConnection] = useState(false)
  const [testResults, setTestResults] = useState<Array<{ provider: string; ok: boolean; message: string }> | null>(null)
  const [testingMethods, setTestingMethods] = useState(false)
  const [methodsSummary, setMethodsSummary] = useState<TelegramMethodsTestResponse | null>(null)
  const [savedSecrets, setSavedSecrets] = useState({ apiHash: false, botToken: false })

  const [title, setTitle] = useState('')
  const [channelHandle, setChannelHandle] = useState('')
  const [sourceKind, setSourceKind] = useState<SourceKind>('signals')
  const [provider, setProvider] = useState<Provider>('auto')
  const [pollInterval, setPollInterval] = useState('300')
  const [includeKeywords, setIncludeKeywords] = useState('')
  const [excludeKeywords, setExcludeKeywords] = useState('')
  const [languageHint, setLanguageHint] = useState('')

  const [apiDetails, setApiDetails] = useState<ApiDetailsForm>({
    apiId: '',
    apiHash: '',
    botToken: '',
    mcpChatId: '',
    mcpServerUrl: 'https://telegram-mcp.furkankucuk.net',
  })

  const providerHints = useMemo(
    () => ({
      auto: 'Auto-selects the first available provider.',
      telethon: 'Requires TELEGRAM_API_ID and TELEGRAM_API_HASH on backend.',
      bot_api: 'Requires TELEGRAM_BOT_TOKEN on backend.',
      telegram_mcp: 'Requires TELEGRAM_MCP_CHAT_ID and TELEGRAM_MCP_SERVER_URL on backend.',
    }),
    []
  )

  const configuredChannelKeys = useMemo(() => {
    const keys = new Set<string>()
    channels.forEach((channel) => {
      keys.add(`${normalizeChannelHandle(channel.channel_handle)}::${channel.source_kind}`)
    })
    return keys
  }, [channels])

  const hasAvailableProvider = useMemo(
    () => Boolean(status?.providers.some((entry) => entry.available)),
    [status]
  )

  const loadData = useCallback(async () => {
    setError(null)
    try {
      const [statusRes, channelsRes, settingsRes, authRes] = await Promise.all([
        apiClient.telegram.getStatus(),
        apiClient.telegram.getChannels({ user_id: '0' }),
        apiClient.telegram.getSettings().catch(() => null),
        apiClient.telegram.getAuthStatus().catch(() => null),
      ])
      setStatus(statusRes.data)
      setChannels(channelsRes.data)
      if (authRes?.data) {
        setAuthStatus(authRes.data as TelegramAuthStatus)
      }
      if (settingsRes?.data) {
        const s = settingsRes.data
        setCredForm((prev) => ({
          apiId: s.api_id ? String(s.api_id) : prev.apiId,
          apiHash: prev.apiHash, // never pre-fill secret
          phoneNumber: s.phone_number ?? prev.phoneNumber,
          botToken: prev.botToken, // never pre-fill secret
          mcpChatId: s.mcp_chat_id ?? prev.mcpChatId,
        }))
        setSavedSecrets({ apiHash: !!s.api_hash_set, botToken: !!s.bot_token_set })
      }
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    }
  }, [])

  // ── Auth handlers ─────────────────────────────────────────────────────
  const handleSendCode = useCallback(async () => {
    if (!authPhone.trim()) {
      setAuthError('Phone number is required.')
      return
    }
    setAuthLoading(true)
    setAuthError(null)
    setAuthMessage(null)
    try {
      const res = await apiClient.telegram.startAuth({ phone_number: authPhone.trim() })
      setAuthCodeHash(res.data.phone_code_hash)
      setAuthStep('enter_code')
      setAuthMessage(res.data.message)
    } catch (e: unknown) {
      setAuthError(toErrorMessage(e))
    } finally {
      setAuthLoading(false)
    }
  }, [authPhone])

  const handleVerifyCode = useCallback(async () => {
    if (!authCode.trim()) {
      setAuthError('Verification code is required.')
      return
    }
    setAuthLoading(true)
    setAuthError(null)
    try {
      const res = await apiClient.telegram.completeAuth({
        phone_number: authPhone.trim(),
        phone_code_hash: authCodeHash,
        code: authCode.trim(),
        password: authStep === 'enter_2fa' ? auth2faPassword.trim() || null : null,
      })
      const data = res.data as {
        success: boolean
        requires_2fa: boolean
        message: string
        account?: { phone?: string; username?: string; first_name?: string } | null
      }
      if (data.requires_2fa && authStep !== 'enter_2fa') {
        setAuthStep('enter_2fa')
        setAuthMessage('Two-factor authentication required. Enter your 2FA password.')
      } else if (data.success) {
        setAuthStep('done')
        setAuthMessage(data.message)
        setAuthCode('')
        setAuth2faPassword('')
        // Refresh auth status and trigger channel discovery
        const authRes = await apiClient.telegram.getAuthStatus().catch(() => null)
        if (authRes?.data) setAuthStatus(authRes.data as TelegramAuthStatus)
        autoDiscoveryTriggeredRef.current = false
        await loadData()
      } else {
        setAuthError(data.message || 'Verification failed.')
      }
    } catch (e: unknown) {
      setAuthError(toErrorMessage(e))
    } finally {
      setAuthLoading(false)
    }
  }, [authPhone, authCodeHash, authCode, auth2faPassword, authStep, loadData])

  const handleDisconnect = useCallback(async () => {
    setAuthLoading(true)
    setAuthError(null)
    try {
      await apiClient.telegram.disconnectAuth()
      setAuthStatus(null)
      setAuthStep('idle')
      setAuthMessage('Account disconnected.')
      setDiscoveredChannels([])
      setDiscoveredTotal(0)
      autoDiscoveryTriggeredRef.current = false
      await loadData()
    } catch (e: unknown) {
      setAuthError(toErrorMessage(e))
    } finally {
      setAuthLoading(false)
    }
  }, [loadData])

  const saveCredentials = useCallback(async () => {
    setSavingCreds(true)
    setCredsMessage(null)
    try {
      await apiClient.telegram.updateSettings({
        api_id: credForm.apiId ? Number(credForm.apiId) : null,
        api_hash: credForm.apiHash || null,
        phone_number: credForm.phoneNumber || null,
        bot_token: credForm.botToken || null,
        mcp_chat_id: credForm.mcpChatId || null,
      })
      setCredsMessage('Credentials saved. Refreshing provider status...')
      // Reset auto-discovery so it re-triggers after credentials change
      autoDiscoveryTriggeredRef.current = false
      await loadData()
    } catch (e: unknown) {
      setCredsMessage(`Error: ${toErrorMessage(e)}`)
    } finally {
      setSavingCreds(false)
    }
  }, [credForm, loadData])

  const testConnection = useCallback(async () => {
    setTestingConnection(true)
    setTestResults(null)
    try {
      const res = await apiClient.telegram.testConnection()
      setTestResults(res.data.results ?? [])
    } catch (e: unknown) {
      setTestResults([{ provider: 'error', ok: false, message: toErrorMessage(e) }])
    } finally {
      setTestingConnection(false)
    }
  }, [])

  const discoverSubscribedChannels = useCallback(
    async (options?: { quiet?: boolean }) => {
      if (!hasAvailableProvider) {
        setDiscoveredChannels([])
        setDiscoveredTotal(0)
        return
      }

      setDiscovering(true)
      if (!options?.quiet) {
        setError(null)
      }

      try {
        // Always use backend auto-selection so registry fallback can prefer
        // the first provider that returns subscribed channels.
        const preferredProvider: Provider = 'auto'
        const response = await apiClient.telegram.discoverSubscribedChannels({
          provider: preferredProvider,
          limit: 500,
        })
        const payload = (response.data ?? {
          provider: preferredProvider,
          total_subscribed: 0,
          channels: [],
        }) as TelegramSubscribedChannelsResponse
        const rows = payload.channels ?? []
        setDiscoveredChannels(rows)
        setDiscoveredTotal(payload.total_subscribed ?? rows.length)
        setDiscoveredKinds((prev) => {
          const next = { ...prev }
          rows.forEach((channel: TelegramDiscoveredChannel) => {
            const key = normalizeChannelHandle(channel.channel_handle)
            if (!next[key]) {
              next[key] = 'signals'
            }
          })
          return next
        })
        if (!options?.quiet) {
          const total = payload.total_subscribed ?? rows.length
          setMessage(`Discovered ${total} subscribed Telegram channel${total === 1 ? '' : 's'}.`)
        }
      } catch (e: unknown) {
        if (!options?.quiet) {
          setError(toErrorMessage(e))
        }
        setDiscoveredTotal(0)
      } finally {
        setDiscovering(false)
      }
    },
    [hasAvailableProvider]
  )

  const testCoreMethods = useCallback(async (mode: MethodsTestMode = 'binding') => {
    setTestingMethods(true)
    setMethodsSummary(null)
    try {
      const res = await apiClient.telegram.testMethods({ provider: 'auto', mode })
      setMethodsSummary(res.data as TelegramMethodsTestResponse)
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setTestingMethods(false)
    }
  }, [])

  useEffect(() => {
    const stored = typeof window !== 'undefined' ? window.localStorage.getItem(API_DETAILS_STORAGE_KEY) : null
    if (!stored) return
    try {
      const parsed = JSON.parse(stored) as ApiDetailsForm
      setApiDetails((prev) => ({ ...prev, ...parsed }))
    } catch {
      // Ignore invalid local storage payloads.
    }
  }, [])

  useEffect(() => {
    const initialize = async () => {
      setLoading(true)
      await loadData()
      setLoading(false)
    }
    initialize()
  }, [loadData])

  useEffect(() => {
    if (!hasAvailableProvider || autoDiscoveryTriggeredRef.current) {
      return
    }
    autoDiscoveryTriggeredRef.current = true
    void discoverSubscribedChannels({ quiet: true })
  }, [discoverSubscribedChannels, hasAvailableProvider])

  const handleRefresh = async () => {
    setRefreshing(true)
    await loadData()
    await discoverSubscribedChannels({ quiet: true })
    setRefreshing(false)
  }

  const handlePoll = async () => {
    setPolling(true)
    setMessage(null)
    setError(null)
    try {
      const result = await apiClient.telegram.poll({ user_id: '0', limit_per_channel: 50 })
      setMessage(`Poll finished: ${result.data.messages_saved} saved from ${result.data.channels_scanned} channels.`)
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setPolling(false)
    }
  }

  const handleSaveApiDetails = async () => {
    if (typeof window === 'undefined') return

    window.localStorage.setItem(API_DETAILS_STORAGE_KEY, JSON.stringify(apiDetails))

    const parsedApiId = apiDetails.apiId.trim()
    const normalizedApiId = parsedApiId ? Number(parsedApiId) : null

    if (parsedApiId && (normalizedApiId === null || !Number.isFinite(normalizedApiId) || normalizedApiId <= 0)) {
      setError('API ID must be a positive number.')
      return
    }

    try {
      await apiClient.telegram.updateSettings({
        api_id: normalizedApiId,
        api_hash: apiDetails.apiHash.trim() || null,
        bot_token: apiDetails.botToken.trim() || null,
        mcp_chat_id: apiDetails.mcpChatId.trim() || null,
      })

      setMessage('Telegram API details saved in this browser and synced to backend plugin settings.')
      setError(null)
      autoDiscoveryTriggeredRef.current = false
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    }
  }

  const handleCreateChannel = async () => {
    if (!channelHandle.trim()) {
      setError('Channel handle is required.')
      return
    }

    const interval = Number.parseInt(pollInterval, 10)
    const payload = {
      user_id: '0',
      title: title.trim() || undefined,
      channel_handle: channelHandle.trim(),
      source_kind: sourceKind,
      provider,
      poll_interval_seconds: Number.isNaN(interval) ? 300 : Math.max(60, Math.min(interval, 3600)),
      include_keywords: parseKeywordList(includeKeywords),
      exclude_keywords: parseKeywordList(excludeKeywords),
      language_hint: languageHint.trim() || undefined,
      verify_on_create: true,
    }

    setSavingChannel(true)
    setMessage(null)
    setError(null)
    try {
      await apiClient.telegram.createChannel(payload)
      setTitle('')
      setChannelHandle('')
      setIncludeKeywords('')
      setExcludeKeywords('')
      setLanguageHint('')
      setMessage('Channel saved successfully.')
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setSavingChannel(false)
    }
  }

  const handleToggleChannel = async (channel: TelegramChannelSource) => {
    setError(null)
    try {
      await apiClient.telegram.updateChannel(channel.id, { is_enabled: !channel.is_enabled }, '0')
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    }
  }

  const handleDeleteChannel = async (channel: TelegramChannelSource) => {
    setError(null)
    try {
      await apiClient.telegram.deleteChannel(channel.id, '0')
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    }
  }

  const handleAddDiscoveredChannel = async (channel: TelegramDiscoveredChannel) => {
    const handleKey = normalizeChannelHandle(channel.channel_handle)
    const chosenKind = discoveredKinds[handleKey] ?? 'signals'
    const dedupeKey = `${handleKey}::${chosenKind}`

    if (configuredChannelKeys.has(dedupeKey)) {
      setMessage('This channel is already configured for the selected source type.')
      return
    }

    setSavingDiscoveredHandle(handleKey)
    setError(null)
    setMessage(null)
    try {
      await apiClient.telegram.createChannel({
        user_id: '0',
        title: channel.title,
        channel_handle: channel.channel_handle,
        source_kind: chosenKind,
        provider: isKnownProvider(channel.provider) ? channel.provider : 'auto',
        poll_interval_seconds: 300,
        verify_on_create: false,
      })
      setMessage(`${channel.title || channel.channel_handle} added to ${chosenKind}.`)
      await loadData()
    } catch (e: unknown) {
      setError(toErrorMessage(e))
    } finally {
      setSavingDiscoveredHandle(null)
    }
  }

  return (
    <>
      <Head>
        <title>Telegram Settings - TradeBot</title>
      </Head>

      <div className="space-y-6">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <h1 className="text-3xl font-bold text-white flex items-center gap-2">
              <MessageCircle className="w-7 h-7 text-cyan-400" />
              Telegram
            </h1>
            <p className="text-sm text-gray-400 mt-1">
              Configure Telegram providers, channels, and polling for signal/news ingestion.
            </p>
          </div>

          <div className="flex gap-2">
            <button
              onClick={handleRefresh}
              disabled={refreshing || loading}
              className="px-3 py-2 rounded-md bg-gray-700 hover:bg-gray-600 text-white text-sm disabled:opacity-60"
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                Refresh
              </span>
            </button>
            <button
              onClick={handlePoll}
              disabled={polling || loading}
              className="px-3 py-2 rounded-md bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60"
            >
              <span className="inline-flex items-center gap-2">
                <RefreshCw className={`w-4 h-4 ${polling ? 'animate-spin' : ''}`} />
                Poll Now
              </span>
            </button>
          </div>
        </div>

        {message && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-300 flex items-start gap-2">
            <CheckCircle2 className="w-4 h-4 mt-0.5" />
            <span>{message}</span>
          </div>
        )}

        {error && (
          <div className="rounded-md border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-300 flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 mt-0.5" />
            <span>{error}</span>
          </div>
        )}

        {/* ── Connect Telegram Account ────────────────────────────── */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <div className="flex items-center justify-between gap-3 mb-3">
            <div>
              <h2 className="font-semibold text-white flex items-center gap-2">
                <LogIn className="w-4 h-4 text-cyan-400" />
                Connect Telegram Account
              </h2>
              <p className="text-xs text-gray-400 mt-0.5">
                Authenticate with your personal Telegram account (Telethon) to list all your channels and groups.
                Requires API ID + API Hash from{' '}
                <a href="https://my.telegram.org" target="_blank" rel="noopener noreferrer" className="text-cyan-400 underline">
                  my.telegram.org
                </a>.
              </p>
            </div>
            {authStatus?.authenticated && (
              <button
                onClick={handleDisconnect}
                disabled={authLoading}
                className="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded bg-red-600/30 border border-red-500/40 text-red-300 hover:bg-red-600/50 text-xs disabled:opacity-60"
              >
                <LogOut className="w-3.5 h-3.5" />
                Disconnect
              </button>
            )}
          </div>

          {authError && (
            <div className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300 mb-3 flex items-start gap-2">
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              {authError}
            </div>
          )}
          {authMessage && (
            <div className="rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-300 mb-3 flex items-start gap-2">
              <CheckCircle2 className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              {authMessage}
            </div>
          )}

          {authStatus?.authenticated ? (
            // ── Already connected ───────────────────────────────────
            <div className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 flex items-center gap-3">
              <CheckCircle2 className="w-5 h-5 text-emerald-400 shrink-0" />
              <div>
                <div className="text-sm font-medium text-emerald-200">
                  Connected
                  {authStatus.first_name ? ` as ${authStatus.first_name}` : ''}
                  {authStatus.username ? ` (@${authStatus.username})` : ''}
                </div>
                {authStatus.phone_number && (
                  <div className="text-xs text-emerald-300/70">{authStatus.phone_number}</div>
                )}
                <div className="text-xs text-emerald-300/60 mt-0.5">
                  Channels are loading automatically. Click <strong>Discover</strong> below to refresh.
                </div>
              </div>
            </div>
          ) : (
            // ── Auth flow ───────────────────────────────────────────
            <div className="space-y-3">
              {(authStep === 'idle' || authStep === 'enter_phone') && (
                <div className="flex items-end gap-2">
                  <div className="flex-1">
                    <label className="block text-xs font-medium text-gray-300 mb-1">
                      Phone Number <span className="text-gray-500">(with country code)</span>
                    </label>
                    <input
                      type="tel"
                      placeholder="+27844942767"
                      value={authPhone}
                      onChange={(e) => setAuthPhone(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && handleSendCode()}
                      className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
                    />
                  </div>
                  <button
                    onClick={handleSendCode}
                    disabled={authLoading || !authPhone.trim()}
                    className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60 whitespace-nowrap"
                  >
                    {authLoading ? 'Sending...' : 'Send Code'}
                  </button>
                </div>
              )}

              {(authStep === 'enter_code' || authStep === 'enter_2fa') && (
                <div className="space-y-2">
                  <div className="text-xs text-gray-400">
                    Code sent to <span className="text-white">{authPhone}</span>.
                    <button
                      onClick={() => { setAuthStep('enter_phone'); setAuthCode(''); setAuthError(null); setAuthMessage(null); }}
                      className="ml-2 text-cyan-400 underline"
                    >
                      Change number
                    </button>
                  </div>
                  <div className="flex items-end gap-2">
                    <div className="flex-1">
                      <label className="block text-xs font-medium text-gray-300 mb-1">
                        Verification Code
                      </label>
                      <input
                        type="text"
                        inputMode="numeric"
                        placeholder="12345"
                        value={authCode}
                        onChange={(e) => setAuthCode(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && !auth2faPassword && handleVerifyCode()}
                        className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
                        autoFocus
                      />
                    </div>
                    {authStep !== 'enter_2fa' && (
                      <button
                        onClick={handleVerifyCode}
                        disabled={authLoading || !authCode.trim()}
                        className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60 whitespace-nowrap"
                      >
                        {authLoading ? 'Verifying...' : 'Verify'}
                      </button>
                    )}
                  </div>

                  {authStep === 'enter_2fa' && (
                    <div className="flex items-end gap-2">
                      <div className="flex-1">
                        <label className="block text-xs font-medium text-gray-300 mb-1">
                          2FA Password
                        </label>
                        <input
                          type="password"
                          placeholder="Your Telegram 2FA password"
                          value={auth2faPassword}
                          onChange={(e) => setAuth2faPassword(e.target.value)}
                          onKeyDown={(e) => e.key === 'Enter' && handleVerifyCode()}
                          className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
                          autoFocus
                        />
                      </div>
                      <button
                        onClick={handleVerifyCode}
                        disabled={authLoading || !auth2faPassword.trim()}
                        className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60 whitespace-nowrap"
                      >
                        {authLoading ? 'Verifying...' : 'Verify 2FA'}
                      </button>
                    </div>
                  )}
                </div>
              )}

              {authStep === 'idle' && (
                <p className="text-xs text-gray-500">
                  Make sure you have entered your <strong className="text-gray-300">API ID</strong> and <strong className="text-gray-300">API Hash</strong> in the <em>API Details</em> section below before connecting.
                </p>
              )}
            </div>
          )}
        </div>

        {/* ── API Details ─────────────────────────────────────────── */}
        <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
          <h2 className="font-semibold text-white mb-1">API Details</h2>
          <p className="text-xs text-gray-400 mb-4">
            Enter Telegram API details here for convenience. They are stored locally in this browser and should also be set in backend environment variables.
          </p>

          {credsMessage && (
            <div className={`rounded border px-3 py-2 text-xs mb-3 ${credsMessage.startsWith('Error') ? 'border-red-500/40 text-red-300 bg-red-500/10' : 'border-green-500/40 text-green-300 bg-green-500/10'}`}>
              {credsMessage}
            </div>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {/* MCP Chat ID — most important for most users */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1">
                Chat ID <span className="text-cyan-400 text-[10px]">(MCP provider)</span>
              </label>
              <input
                type="text"
                placeholder="e.g. 1241928883"
                value={credForm.mcpChatId}
                onChange={(e) => setCredForm((p) => ({ ...p, mcpChatId: e.target.value }))}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
              <p className="text-[11px] text-gray-500 mt-1">Your Telegram numeric user/chat ID. Enables channel discovery via MCP.</p>
            </div>

            {/* Phone number */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1">
                Phone Number <span className="text-gray-500 text-[10px]">(Telethon)</span>
              </label>
              <input
                type="text"
                placeholder="e.g. +27844942767"
                value={credForm.phoneNumber}
                onChange={(e) => setCredForm((p) => ({ ...p, phoneNumber: e.target.value }))}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
            </div>

            {/* API ID */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1">
                API ID <span className="text-gray-500 text-[10px]">(Telethon — my.telegram.org)</span>
              </label>
              <input
                type="text"
                placeholder="e.g. 12345678"
                value={credForm.apiId}
                onChange={(e) => setCredForm((p) => ({ ...p, apiId: e.target.value }))}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
            </div>

            {/* API Hash */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1">
                API Hash <span className="text-gray-500 text-[10px]">(Telethon)</span>
                {savedSecrets.apiHash && !credForm.apiHash && (
                  <span className="ml-2 text-green-400 text-[10px]">● already set</span>
                )}
              </label>
              <input
                type="password"
                placeholder={savedSecrets.apiHash ? 'Leave blank to keep existing value' : 'Enter API hash'}
                value={credForm.apiHash}
                onChange={(e) => setCredForm((p) => ({ ...p, apiHash: e.target.value }))}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
            </div>

            {/* Bot Token */}
            <div>
              <label className="block text-xs font-medium text-gray-300 mb-1">
                Bot Token <span className="text-gray-500 text-[10px]">(Bot API — @BotFather)</span>
                {savedSecrets.botToken && !credForm.botToken && (
                  <span className="ml-2 text-green-400 text-[10px]">● already set</span>
                )}
              </label>
              <input
                type="password"
                placeholder={savedSecrets.botToken ? 'Leave blank to keep existing value' : 'Enter bot token'}
                value={credForm.botToken}
                onChange={(e) => setCredForm((p) => ({ ...p, botToken: e.target.value }))}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-sm text-white placeholder:text-gray-500"
              />
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-start gap-3">
            <button
              onClick={saveCredentials}
              disabled={savingCreds}
              className="px-4 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60"
            >
              {savingCreds ? 'Saving...' : 'Save API Details'}
            </button>
            <button
              onClick={testConnection}
              disabled={testingConnection}
              className="px-4 py-2 rounded bg-gray-600 hover:bg-gray-500 text-white text-sm disabled:opacity-60"
            >
              {testingConnection ? 'Testing...' : 'Test Connection'}
            </button>
            <button
              onClick={() => testCoreMethods('binding')}
              disabled={testingMethods}
              className="px-4 py-2 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-sm disabled:opacity-60"
            >
              {testingMethods ? 'Testing Core Methods...' : 'Test Core Methods (Binding)'}
            </button>
            <button
              onClick={() => testCoreMethods('invoke_readonly')}
              disabled={testingMethods}
              className="px-4 py-2 rounded bg-violet-600 hover:bg-violet-500 text-white text-sm disabled:opacity-60"
            >
              {testingMethods ? 'Testing Core Methods...' : 'Test Core Methods (Invoke Read-only)'}
            </button>
          </div>

          {testResults !== null && (
            <div className="mt-3 space-y-1.5">
              {testResults.map((r) => (
                <div
                  key={r.provider}
                  className={`rounded border px-3 py-2 text-xs flex items-start gap-2 ${
                    r.ok
                      ? 'border-green-500/40 bg-green-500/10 text-green-300'
                      : 'border-gray-600/40 bg-gray-800/30 text-gray-400'
                  }`}
                >
                  <span className="font-bold shrink-0">{r.provider}:</span>
                  <span>{r.message}</span>
                </div>
              ))}
            </div>
          )}

          {methodsSummary !== null && (
            <div className="mt-3 rounded border border-indigo-500/40 bg-indigo-500/10 p-3 text-xs text-indigo-100 space-y-1">
              <div className="font-semibold text-indigo-200">
                Telegram Core Methods ({methodsSummary.provider})
              </div>
              <div>Mode: {methodsSummary.mode}</div>
              <div>
                Total: {methodsSummary.summary.total_methods} | Tested: {methodsSummary.summary.tested_methods}
              </div>
              <div>
                Supported: {methodsSummary.summary.passed} | Unsupported: {methodsSummary.summary.unsupported} | Failed: {methodsSummary.summary.failed}
              </div>
              {methodsSummary.mode === 'invoke_readonly' && (
                <div>
                  Read-only allowlist size: {methodsSummary.readonly_allowlist?.length ?? 0}
                </div>
              )}
              <div className="text-indigo-300/90 break-all">Source: {methodsSummary.source_url}</div>
            </div>
          )}
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5 xl:col-span-2">
            <h2 className="font-semibold text-white mb-3">Channel Sources</h2>
            <p className="text-xs text-gray-400 mb-4">
              Manage which Telegram channels are ingested for signals or news.
            </p>

            <div className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 p-3 mb-4">
              <div className="flex items-center justify-between gap-2 mb-2">
                <div className="text-sm font-medium text-cyan-100">
                  Subscribed Channels (Auto-discovered)
                  <span className="ml-2 text-cyan-300/90 text-xs">Total: {discoveredTotal}</span>
                </div>
                <button
                  onClick={() => discoverSubscribedChannels()}
                  disabled={discovering || !hasAvailableProvider}
                  className="px-2.5 py-1.5 rounded text-xs bg-cyan-600 hover:bg-cyan-500 text-white disabled:opacity-60"
                >
                  <span className="inline-flex items-center gap-1">
                    <RefreshCw className={`w-3.5 h-3.5 ${discovering ? 'animate-spin' : ''}`} />
                    {discovering ? 'Discovering...' : 'Discover'}
                  </span>
                </button>
              </div>

              {!hasAvailableProvider ? (
                <div className="text-xs text-gray-300">
                  No Telegram provider is currently available. Configure backend credentials first.
                </div>
              ) : discoveredChannels.length === 0 ? (
                <div className="text-xs text-gray-300">
                  No subscribed channels discovered yet. Click Discover to refresh.
                </div>
              ) : (
                <div className="space-y-2">
                  {discoveredChannels.map((channel) => {
                    const handleKey = normalizeChannelHandle(channel.channel_handle)
                    const chosenKind = discoveredKinds[handleKey] ?? 'signals'
                    const isAlreadyAdded = configuredChannelKeys.has(`${handleKey}::${chosenKind}`)
                    return (
                      <div
                        key={`${channel.provider}:${channel.channel_handle}:${channel.channel_id || 'none'}`}
                        className="rounded border border-cyan-500/20 bg-gray-900/40 p-2"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-sm text-white font-medium">{channel.title || channel.channel_handle}</div>
                            <div className="text-xs text-gray-400 mt-0.5">
                              {channel.channel_handle} • {channel.provider}
                            </div>
                          </div>

                          <div className="flex items-center gap-2">
                            <select
                              value={chosenKind}
                              onChange={(e) =>
                                setDiscoveredKinds((prev) => ({
                                  ...prev,
                                  [handleKey]: e.target.value as SourceKind,
                                }))
                              }
                              className="rounded bg-gray-900 border border-gray-700 px-2 py-1 text-xs text-white"
                            >
                              <option value="signals">signals</option>
                              <option value="news">news</option>
                            </select>
                            <button
                              onClick={() => handleAddDiscoveredChannel(channel)}
                              disabled={isAlreadyAdded || savingDiscoveredHandle === handleKey}
                              className="px-2.5 py-1 rounded text-xs bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-60"
                            >
                              {isAlreadyAdded
                                ? 'Added'
                                : savingDiscoveredHandle === handleKey
                                  ? 'Adding...'
                                  : 'Add'}
                            </button>
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            {loading ? (
              <div className="text-sm text-gray-400">Loading channels...</div>
            ) : channels.length === 0 ? (
              <div className="text-sm text-gray-400">No Telegram channels configured yet.</div>
            ) : (
              <div className="space-y-3">
                {channels.map((channel) => (
                  <div
                    key={channel.id}
                    className="rounded-lg border border-gray-700/70 bg-gray-900/40 p-3"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="text-sm font-medium text-white">
                          {channel.title || channel.channel_handle}
                        </div>
                        <div className="text-xs text-gray-400 mt-0.5">
                          {channel.channel_handle} • {channel.source_kind} • {channel.provider}
                        </div>
                      </div>

                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => handleToggleChannel(channel)}
                          className={`px-2.5 py-1.5 rounded text-xs ${
                            channel.is_enabled
                              ? 'bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30'
                              : 'bg-gray-700 text-gray-200 hover:bg-gray-600'
                          }`}
                        >
                          <span className="inline-flex items-center gap-1">
                            <Power className="w-3.5 h-3.5" />
                            {channel.is_enabled ? 'Enabled' : 'Disabled'}
                          </span>
                        </button>
                        <button
                          onClick={() => handleDeleteChannel(channel)}
                          className="px-2.5 py-1.5 rounded text-xs bg-red-500/20 text-red-300 hover:bg-red-500/30"
                        >
                          <span className="inline-flex items-center gap-1">
                            <Trash2 className="w-3.5 h-3.5" />
                            Delete
                          </span>
                        </button>
                      </div>
                    </div>

                    <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
                      <div className="text-gray-400">
                        Poll interval: <span className="text-gray-200">{channel.poll_interval_seconds}s</span>
                      </div>
                      <div className="text-gray-400">
                        Last poll: <span className="text-gray-200">{channel.last_polled_at || 'Never'}</span>
                      </div>
                    </div>

                    {channel.last_error && (
                      <div className="mt-2 rounded border border-red-500/30 bg-red-500/10 px-2.5 py-2 text-xs text-red-300">
                        Last error: {channel.last_error}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <h2 className="font-semibold text-white mb-3">Plugin Status</h2>

            {status ? (
              <div className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-2 text-xs text-gray-300">
                  <div className="rounded bg-gray-900/50 border border-gray-700 p-2">
                    <div className="text-gray-400">Channels</div>
                    <div className="text-white font-medium">{status.channels_total}</div>
                  </div>
                  <div className="rounded bg-gray-900/50 border border-gray-700 p-2">
                    <div className="text-gray-400">Enabled</div>
                    <div className="text-white font-medium">{status.channels_enabled}</div>
                  </div>
                </div>

                <div className="text-xs text-gray-400">Provider readiness</div>
                <div className="space-y-2">
                  {status.providers.map((entry) => (
                    <div
                      key={entry.name}
                      className="rounded border border-gray-700/70 bg-gray-900/40 p-2"
                    >
                      <div className="flex items-center justify-between text-sm">
                        <span className="text-gray-200">{entry.name}</span>
                        <span
                          className={`text-xs px-2 py-0.5 rounded ${
                            entry.available
                              ? 'bg-emerald-500/20 text-emerald-300'
                              : 'bg-gray-700 text-gray-300'
                          }`}
                        >
                          {entry.available ? 'Available' : 'Unavailable'}
                        </span>
                      </div>
                      {!entry.available && entry.reason && (
                        <div className="text-xs text-amber-300 mt-1">{entry.reason}</div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ) : (
              <div className="text-sm text-gray-400">Status unavailable.</div>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <h2 className="font-semibold text-white mb-3">Add Channel</h2>
            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1">Title (optional)</label>
                <input
                  type="text"
                  value={title}
                  onChange={(e) => setTitle(e.target.value)}
                  placeholder="Example: Alpha Signals"
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Channel handle or id</label>
                <input
                  type="text"
                  value={channelHandle}
                  onChange={(e) => setChannelHandle(e.target.value)}
                  placeholder="@channel or numeric id"
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Source kind</label>
                  <select
                    value={sourceKind}
                    onChange={(e) => setSourceKind(e.target.value as SourceKind)}
                    className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                  >
                    <option value="signals">Signals</option>
                    <option value="news">News</option>
                  </select>
                </div>
                <div>
                  <label className="text-xs text-gray-400 block mb-1">Provider</label>
                  <select
                    value={provider}
                    onChange={(e) => setProvider(e.target.value as Provider)}
                    className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                  >
                    <option value="auto">auto</option>
                    <option value="telethon">telethon</option>
                    <option value="bot_api">bot_api</option>
                    <option value="telegram_mcp">telegram_mcp</option>
                  </select>
                </div>
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Poll interval (seconds)</label>
                <input
                  type="number"
                  min={60}
                  max={3600}
                  value={pollInterval}
                  onChange={(e) => setPollInterval(e.target.value)}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Include keywords (comma separated)</label>
                <input
                  type="text"
                  value={includeKeywords}
                  onChange={(e) => setIncludeKeywords(e.target.value)}
                  placeholder="breakout, long, buy"
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Exclude keywords (comma separated)</label>
                <input
                  type="text"
                  value={excludeKeywords}
                  onChange={(e) => setExcludeKeywords(e.target.value)}
                  placeholder="spam, ad"
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Language hint (optional)</label>
                <input
                  type="text"
                  value={languageHint}
                  onChange={(e) => setLanguageHint(e.target.value)}
                  placeholder="en"
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div className="rounded border border-cyan-500/30 bg-cyan-500/10 px-3 py-2 text-xs text-cyan-200">
                {providerHints[provider]}
              </div>

              <button
                onClick={handleCreateChannel}
                disabled={savingChannel}
                className="w-full rounded bg-cyan-600 hover:bg-cyan-500 text-white px-4 py-2 text-sm disabled:opacity-60"
              >
                <span className="inline-flex items-center gap-2">
                  <Plus className="w-4 h-4" />
                  {savingChannel ? 'Saving...' : 'Add Channel'}
                </span>
              </button>
            </div>
          </div>

          <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
            <h2 className="font-semibold text-white mb-3">API Details</h2>
            <p className="text-xs text-gray-400 mb-3">
              Enter Telegram API details here for convenience. They are stored locally in this browser and also synced to
              backend plugin settings when you save.
            </p>

            <div className="space-y-3">
              <div>
                <label className="text-xs text-gray-400 block mb-1">API ID</label>
                <input
                  type="text"
                  value={apiDetails.apiId}
                  onChange={(e) => setApiDetails((prev) => ({ ...prev, apiId: e.target.value }))}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">API Hash</label>
                <input
                  type="text"
                  value={apiDetails.apiHash}
                  onChange={(e) => setApiDetails((prev) => ({ ...prev, apiHash: e.target.value }))}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">Bot Token</label>
                <input
                  type="text"
                  value={apiDetails.botToken}
                  onChange={(e) => setApiDetails((prev) => ({ ...prev, botToken: e.target.value }))}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">MCP Chat ID</label>
                <input
                  type="text"
                  value={apiDetails.mcpChatId}
                  onChange={(e) => setApiDetails((prev) => ({ ...prev, mcpChatId: e.target.value }))}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <div>
                <label className="text-xs text-gray-400 block mb-1">MCP Server URL</label>
                <input
                  type="text"
                  value={apiDetails.mcpServerUrl}
                  onChange={(e) => setApiDetails((prev) => ({ ...prev, mcpServerUrl: e.target.value }))}
                  className="w-full rounded bg-gray-900 border border-gray-700 px-3 py-2 text-sm text-white"
                />
              </div>

              <button
                onClick={handleSaveApiDetails}
                className="w-full rounded bg-gray-700 hover:bg-gray-600 text-white px-4 py-2 text-sm"
              >
                <span className="inline-flex items-center gap-2">
                  <Save className="w-4 h-4" />
                  Save API Details (Browser + Backend)
                </span>
              </button>

              <div className="rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200 flex items-start gap-2">
                <Info className="w-4 h-4 mt-0.5 shrink-0" />
                <div>
                  <div className="font-medium mb-1">Backend environment variables</div>
                  <div>TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN</div>
                  <div>TELEGRAM_MCP_CHAT_ID, TELEGRAM_MCP_SERVER_URL</div>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* ── Bot Control ─────────────────────────────────────────── */}
        <BotControlPanel onMessage={setMessage} onError={setError} />
      </div>
    </>
  )
}

// ── Bot Control Panel ─────────────────────────────────────────────────────────

function BotControlPanel({
  onMessage,
  onError,
}: {
  onMessage: (msg: string) => void
  onError: (err: string) => void
}) {
  const [botInfo, setBotInfo] = useState<{
    ok: boolean; bot_id?: number; username?: string; first_name?: string; error?: string
  } | null>(null)
  const [botConfig, setBotConfig] = useState<{
    token_set: boolean; webhook_url: string | null; polling_enabled: boolean;
    allowed_chat_ids: string[]; ai_fallback_enabled: boolean; last_update_id: number | null
  } | null>(null)
  const [webhookInfo, setWebhookInfo] = useState<{
    ok: boolean; url?: string | null; pending_update_count?: number;
    last_error_message?: string | null; error?: string | null
  } | null>(null)

  const [loadingInfo, setLoadingInfo] = useState(false)
  const [webhookUrl, setWebhookUrl] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [settingWebhook, setSettingWebhook] = useState(false)
  const [deletingWebhook, setDeletingWebhook] = useState(false)
  const [testChatId, setTestChatId] = useState('')
  const [testText, setTestText] = useState('✅ Jarvis TradeBot test message — connection OK!')
  const [sendingTest, setSendingTest] = useState(false)
  const [pollingEnabled, setPollingEnabled] = useState(false)
  const [togglingPolling, setTogglingPolling] = useState(false)
  const [syncingCommands, setSyncingCommands] = useState(false)
  const [allowedChatIdsInput, setAllowedChatIdsInput] = useState('')
  const [savingConfig, setSavingConfig] = useState(false)

  const loadBotInfo = useCallback(async () => {
    setLoadingInfo(true)
    try {
      const [infoRes, configRes, webhookRes] = await Promise.all([
        apiClient.telegram.bot.getInfo().catch(() => null),
        apiClient.telegram.bot.getConfig().catch(() => null),
        apiClient.telegram.bot.getWebhook().catch(() => null),
      ])
      if (infoRes?.data) setBotInfo(infoRes.data)
      if (configRes?.data) {
        setBotConfig(configRes.data)
        setPollingEnabled(configRes.data.polling_enabled)
        setAllowedChatIdsInput((configRes.data.allowed_chat_ids || []).join(', '))
      }
      if (webhookRes?.data) {
        setWebhookInfo(webhookRes.data)
        if (webhookRes.data.url) setWebhookUrl(webhookRes.data.url)
      }
    } catch (e) {
      onError('Failed to load bot info')
    } finally {
      setLoadingInfo(false)
    }
  }, [onError])

  useEffect(() => { loadBotInfo() }, [loadBotInfo])

  const handleSetWebhook = async () => {
    if (!webhookUrl.trim()) { onError('Webhook URL is required'); return }
    setSettingWebhook(true)
    try {
      await apiClient.telegram.bot.setWebhook(webhookUrl.trim(), webhookSecret.trim() || undefined)
      onMessage(`Webhook set: ${webhookUrl}`)
      await loadBotInfo()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to set webhook')
    } finally { setSettingWebhook(false) }
  }

  const handleDeleteWebhook = async () => {
    setDeletingWebhook(true)
    try {
      await apiClient.telegram.bot.deleteWebhook()
      onMessage('Webhook deleted.')
      await loadBotInfo()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to delete webhook')
    } finally { setDeletingWebhook(false) }
  }

  const handleTestMessage = async () => {
    if (!testChatId.trim()) { onError('Chat ID is required'); return }
    setSendingTest(true)
    try {
      await apiClient.telegram.bot.testMessage(testChatId.trim(), testText)
      onMessage(`Test message sent to ${testChatId}`)
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to send test message')
    } finally { setSendingTest(false) }
  }

  const handleTogglePolling = async () => {
    setTogglingPolling(true)
    try {
      const res = await apiClient.telegram.bot.setPolling(!pollingEnabled)
      setPollingEnabled(res.data.polling_enabled)
      onMessage(res.data.polling_enabled ? '🤖 Bot polling started.' : '⏹ Bot polling stopped.')
      await loadBotInfo()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to toggle polling')
    } finally { setTogglingPolling(false) }
  }

  const handleSyncCommands = async () => {
    setSyncingCommands(true)
    try {
      const res = await apiClient.telegram.bot.setCommands()
      onMessage(`✅ ${res.data.commands_set} commands synced to Telegram.`)
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to sync commands')
    } finally { setSyncingCommands(false) }
  }

  const handleSaveConfig = async () => {
    setSavingConfig(true)
    try {
      const chatIds = allowedChatIdsInput.split(',').map(s => s.trim()).filter(Boolean)
      await apiClient.telegram.bot.updateConfig({ allowed_chat_ids: chatIds })
      onMessage('Bot config saved.')
      await loadBotInfo()
    } catch (e: any) {
      onError(e?.response?.data?.detail || 'Failed to save config')
    } finally { setSavingConfig(false) }
  }

  return (
    <div className="bg-gray-800/30 border border-gray-700 rounded-lg p-5">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="font-semibold text-white flex items-center gap-2">
            🤖 Bot Control
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Create, test, and manage your Telegram bot. Execute Jarvis commands from any Telegram chat.
          </p>
        </div>
        <button
          onClick={loadBotInfo}
          disabled={loadingInfo}
          className="px-3 py-1.5 rounded bg-gray-700 hover:bg-gray-600 text-white text-xs"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${loadingInfo ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {/* Bot Identity */}
      <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-4 mb-4">
        <h3 className="text-sm font-medium text-gray-200 mb-2">Bot Identity</h3>
        {botInfo ? (
          botInfo.ok ? (
            <div className="flex items-center gap-3">
              <span className="text-2xl">🤖</span>
              <div>
                <div className="text-white font-medium">@{botInfo.username}</div>
                <div className="text-xs text-gray-400">{botInfo.first_name} · ID: {botInfo.bot_id}</div>
                <div className="text-xs text-emerald-400 mt-0.5">● Token valid</div>
              </div>
            </div>
          ) : (
            <div className="text-xs text-red-400">❌ {botInfo.error || 'Bot token invalid or not set'}</div>
          )
        ) : (
          <div className="text-xs text-gray-400">
            {loadingInfo ? 'Loading...' : 'Bot token not configured. Set it in the API Details section above.'}
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        {/* Webhook / Polling */}
        <div className="space-y-4">
          <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-4">
            <h3 className="text-sm font-medium text-gray-200 mb-3">Connection Mode</h3>

            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="text-sm text-white">Polling Mode</div>
                <div className="text-xs text-gray-400">
                  {pollingEnabled ? '🟢 Active (localhost-friendly)' : '⚫ Disabled'}
                </div>
              </div>
              <button
                onClick={handleTogglePolling}
                disabled={togglingPolling}
                className={`px-4 py-1.5 rounded text-sm text-white disabled:opacity-60 ${
                  pollingEnabled ? 'bg-red-600 hover:bg-red-500' : 'bg-emerald-600 hover:bg-emerald-500'
                }`}
              >
                {togglingPolling ? '...' : pollingEnabled ? 'Stop' : 'Start Polling'}
              </button>
            </div>

            <div className="border-t border-gray-700 pt-3 space-y-2">
              <div className="text-xs text-gray-300 font-medium">Webhook (requires public HTTPS URL)</div>

              {webhookInfo?.url && (
                <div className="rounded border border-cyan-500/30 bg-cyan-500/10 px-2.5 py-2 text-xs text-cyan-200 break-all">
                  Active: {webhookInfo.url}
                  {webhookInfo.pending_update_count !== undefined && (
                    <span className="ml-2 text-cyan-300">({webhookInfo.pending_update_count} pending)</span>
                  )}
                  {webhookInfo.last_error_message && (
                    <div className="text-red-300 mt-1">Last error: {webhookInfo.last_error_message}</div>
                  )}
                </div>
              )}

              <input
                type="url"
                placeholder="https://yourdomain.com/api/v1/plugins/telegram/bot/receive"
                value={webhookUrl}
                onChange={e => setWebhookUrl(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-xs text-white placeholder:text-gray-500"
              />
              <input
                type="text"
                placeholder="Secret token (optional, recommended)"
                value={webhookSecret}
                onChange={e => setWebhookSecret(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-xs text-white placeholder:text-gray-500"
              />
              <div className="flex gap-2">
                <button
                  onClick={handleSetWebhook}
                  disabled={settingWebhook || !webhookUrl}
                  className="flex-1 px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-xs disabled:opacity-60"
                >
                  {settingWebhook ? 'Setting...' : 'Set Webhook'}
                </button>
                {webhookInfo?.url && (
                  <button
                    onClick={handleDeleteWebhook}
                    disabled={deletingWebhook}
                    className="px-3 py-1.5 rounded bg-red-600/30 border border-red-500/40 text-red-300 text-xs disabled:opacity-60"
                  >
                    {deletingWebhook ? '...' : 'Delete'}
                  </button>
                )}
              </div>
              {!botConfig?.token_set && (
                <div className="text-xs text-amber-300">
                  ⚠️ No bot token set. Add one in API Details → Bot Token above.
                </div>
              )}
            </div>
          </div>

          {/* Security */}
          <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-4">
            <h3 className="text-sm font-medium text-gray-200 mb-2">Security</h3>
            <div className="space-y-2">
              <div>
                <label className="text-xs text-gray-400 block mb-1">
                  Allowed Chat IDs <span className="text-gray-500">(comma-separated, empty = allow all)</span>
                </label>
                <input
                  type="text"
                  placeholder="e.g. 123456789, -100987654321"
                  value={allowedChatIdsInput}
                  onChange={e => setAllowedChatIdsInput(e.target.value)}
                  className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-xs text-white placeholder:text-gray-500"
                />
                <p className="text-[10px] text-gray-500 mt-1">
                  Only messages from these chats will be processed. Leave empty to accept from anyone (dev only).
                </p>
              </div>
              <button
                onClick={handleSaveConfig}
                disabled={savingConfig}
                className="w-full px-3 py-1.5 rounded bg-gray-600 hover:bg-gray-500 text-white text-xs disabled:opacity-60"
              >
                {savingConfig ? 'Saving...' : 'Save Security Config'}
              </button>
            </div>
          </div>
        </div>

        {/* Test Console + Commands */}
        <div className="space-y-4">
          <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-4">
            <h3 className="text-sm font-medium text-gray-200 mb-3">Test Console</h3>
            <p className="text-xs text-gray-400 mb-3">
              Send a message through your bot to verify the full connection works.
            </p>
            <div className="space-y-2">
              <input
                type="text"
                placeholder="Chat ID (e.g. 123456789)"
                value={testChatId}
                onChange={e => setTestChatId(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-xs text-white placeholder:text-gray-500"
              />
              <textarea
                rows={3}
                value={testText}
                onChange={e => setTestText(e.target.value)}
                className="w-full rounded bg-gray-900 border border-gray-600 px-3 py-2 text-xs text-white resize-none"
              />
              <button
                onClick={handleTestMessage}
                disabled={sendingTest || !testChatId}
                className="w-full px-3 py-2 rounded bg-cyan-600 hover:bg-cyan-500 text-white text-sm disabled:opacity-60"
              >
                {sendingTest ? 'Sending...' : '📨 Send Test Message'}
              </button>
            </div>
          </div>

          <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-4">
            <h3 className="text-sm font-medium text-gray-200 mb-2">Registered Commands</h3>
            <p className="text-xs text-gray-400 mb-3">
              Push the Jarvis command list to Telegram so it appears in the / menu.
            </p>
            <div className="space-y-1 mb-3 text-xs text-gray-300 font-mono">
              {[
                '/start — Get started',
                '/help — List all commands',
                '/status — App & exchange health',
                '/positions — Open futures positions',
                '/portfolio — Total PnL & equity',
                '/signals — Latest channel signals',
                '/sniper — Sniper auto-trade status',
                '/monitor start|stop — Signal monitor',
                '/close BTCUSDT — Close a position',
                '/tp 0.025 BTCUSDT — Set take-profit',
                '/sl 0.020 BTCUSDT — Set stop-loss',
                '/jarvis <command> — Free-form command',
              ].map(cmd => (
                <div key={cmd} className="text-gray-400">{cmd}</div>
              ))}
            </div>
            <button
              onClick={handleSyncCommands}
              disabled={syncingCommands}
              className="w-full px-3 py-2 rounded bg-indigo-600 hover:bg-indigo-500 text-white text-sm disabled:opacity-60"
            >
              {syncingCommands ? 'Syncing...' : '↑ Sync Commands to Telegram'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
