/** @type {import('next').NextConfig} */
const path = require('path')

const nextConfig = {
  reactStrictMode: false,        // true doubles render work in dev — off saves CPU
  // Disable the dev "static route" indicator — its isrManifest handler crashes
  // under Next 16 Turbopack (TypeError: reading 'components' in handleStaticIndicator).
  devIndicators: false,

  // ── Performance optimisations ──────────────────────────────────────────────
  // Enable gzip/br compression on the built-in Next.js server (prod).
  compress: true,

  // Tree-shake heavy icon/chart packages at the import level so only the icons
  // actually used are bundled.  Cuts initial JS parse time significantly.
  experimental: {
    optimizePackageImports: [
      'lucide-react',   // ~1500 icons; we import <30 — save ~400 kB parsed
      'recharts',       // only a few chart components used
      'date-fns',       // only format/formatDistance used
    ],
  },

  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1',
  },
}

module.exports = nextConfig

