import type { AppProps } from 'next/app'
import { useEffect } from 'react'
import '@/styles/globals.css'
import Layout from '@/components/Layout'

// Pages can opt out of the Layout wrapper by setting `PageComponent.noLayout = true`
// (used by full-screen pages like /jarvis-room)
type PageWithLayout = AppProps['Component'] & { noLayout?: boolean }

export default function App({ Component, pageProps }: AppProps) {
  const PageComponent = Component as PageWithLayout

  // Swallow transient axios *network/timeout* rejections so a momentary backend
  // blip (dev-server reload, restart, brief connectivity loss) doesn't surface
  // as a full-screen "Runtime AxiosError: Network Error" overlay. These errors
  // have no server `response` and are always environmental, not logic bugs —
  // real 4xx/5xx responses still propagate normally.
  useEffect(() => {
    const onRejection = (e: PromiseRejectionEvent) => {
      const err = e.reason as
        | { isAxiosError?: boolean; response?: unknown; code?: string; message?: string }
        | undefined
      const isAxios = !!err && (err.isAxiosError === true || 'response' in (err as object))
      const isNetworkOrTimeout =
        isAxios &&
        !err?.response &&
        (err?.code === 'ERR_NETWORK' ||
          err?.code === 'ECONNABORTED' ||
          err?.message === 'Network Error' ||
          (err?.message?.startsWith('timeout of') ?? false))
      if (isNetworkOrTimeout) {
        e.preventDefault() // keep Next's dev overlay from popping for a transient blip
        // eslint-disable-next-line no-console
        console.warn('[api] transient network error suppressed:', err?.message)
      }
    }
    window.addEventListener('unhandledrejection', onRejection)
    return () => window.removeEventListener('unhandledrejection', onRejection)
  }, [])

  if (PageComponent.noLayout) {
    return <Component {...pageProps} />
  }
  return (
    <Layout>
      <Component {...pageProps} />
    </Layout>
  )
}
