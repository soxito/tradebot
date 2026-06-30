import type { AppProps } from 'next/app'
import '@/styles/globals.css'
import Layout from '@/components/Layout'

// Pages can opt out of the Layout wrapper by setting `PageComponent.noLayout = true`
// (used by full-screen pages like /jarvis-room)
type PageWithLayout = AppProps['Component'] & { noLayout?: boolean }

export default function App({ Component, pageProps }: AppProps) {
  const PageComponent = Component as PageWithLayout
  if (PageComponent.noLayout) {
    return <Component {...pageProps} />
  }
  return (
    <Layout>
      <Component {...pageProps} />
    </Layout>
  )
}
