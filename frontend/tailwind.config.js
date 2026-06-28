/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      colors: {
        'tradebot-dark': '#0a0e27',
        'tradebot-accent': '#00d4ff',
        'bullish': '#00ff88',
        'bearish': '#ff4444',
      },
    },
  },
  plugins: [],
}
