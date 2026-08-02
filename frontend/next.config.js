/** @type {import('next').NextConfig} */

// The desktop build exports the app to plain static files, which the Python
// backend then serves at "/" (see backend/app/main.py). That works because the
// app has no server-side rendering at all — no getServerSideProps, no
// getStaticProps, no dynamic routes, no API routes. Gated behind an env flag so
// `npm run dev` and the Docker build keep their normal behaviour.
const isDesktop = process.env.TRADEBOT_DESKTOP === '1'

const nextConfig = {
  pageExtensions: ['ts', 'tsx', 'js', 'jsx', 'md', 'mdx'],
  // Ignore whatsapp pages and test files
  ignoreBuildErrors: true,
  // TypeScript configuration
  typescript: {
    ignoreBuildErrors: false,
  },

  ...(isDesktop && {
    output: 'export',
    // Emits `out/<route>/index.html` per page, so a deep link resolves to a real
    // file rather than needing a server-side rewrite.
    trailingSlash: true,
  }),
}

module.exports = nextConfig
