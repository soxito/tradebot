/** @type {import('next').NextConfig} */

// The desktop build exports the app to plain static files, which the Python
// backend then serves at "/" (see backend/app/main.py). That works because the
// app has no server-side rendering at all — no getServerSideProps, no
// getStaticProps, no dynamic routes, no API routes. Gated behind an env flag so
// `npm run dev` and the Docker build keep their normal behaviour.
const isDesktop = process.env.TRADEBOT_DESKTOP === '1'

const nextConfig = {
  pageExtensions: ['ts', 'tsx', 'js', 'jsx', 'md', 'mdx'],
  // Pin Turbopack's workspace root. Without this it walks up past the repo to
  // ~/package-lock.json and roots itself at the home directory; watching all of
  // $HOME makes client-side rebuilds silently miss edits, so the server renders
  // new code while the browser still runs the old bundle — which shows up as a
  // "server rendered HTML didn't match the client" hydration error on every edit.
  turbopack: {
    root: __dirname,
  },
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
    // Dev chunk filenames are not content-hashed, so an immutable cache pins the
    // browser to whatever it saw first and edits never appear. Prod builds only.
    if (process.env.NODE_ENV !== 'production') return [];
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
