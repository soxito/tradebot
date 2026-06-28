/** @type {import('next').NextConfig} */
const path = require('path')

const nextConfig = {
  reactStrictMode: false,        // true doubles render work in dev — off saves CPU
  // Disable the dev "static route" indicator — its isrManifest handler crashes
  // under Next 16 Turbopack (TypeError: reading 'components' in handleStaticIndicator).
  devIndicators: false,
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1',
  },
}

module.exports = nextConfig

