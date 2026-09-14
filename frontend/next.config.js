/** @type {import('next').NextConfig} */

// The desktop build exports the app to plain static files, which the Python
// backend then serves at "/" (see backend/app/main.py). That works because the
// app has no server-side rendering at all — no getServerSideProps, no
// getStaticProps, no dynamic routes, no API routes. Gated behind an env flag so
// `npm run dev` and the Docker build keep their normal behaviour.
const isDesktop = process.env.TRADEBOT_DESKTOP === '1'

const nextConfig = {
  pageExtensions: ['ts', 'tsx', 'js', 'jsx', 'md', 'mdx'],
  typescript: {
    ignoreBuildErrors: false,
  },
  compress: true,
  poweredByHeader: false,
  // Tree-shake heavy libs — lucide barrel + recharts/three dedup
  experimental: {
    optimizePackageImports: ['lucide-react', 'recharts', 'three', 'date-fns'],
  },
  async headers() {
    return [
      {
        source: '/_next/static/:path*',
        headers: [{ key: 'Cache-Control', value: 'public, max-age=31536000, immutable' }],
      },
    ];
  },

  ...(isDesktop && {
    output: 'export',
    trailingSlash: true,
  }),
}

module.exports = nextConfig
