interface MT5AccountBadgeProps {
  type: string // 'live' | 'demo' | 'prop'
  size?: 'sm' | 'md'
}

export default function MT5AccountBadge({ type, size = 'sm' }: MT5AccountBadgeProps) {
  const pad = size === 'md' ? 'px-3 py-1 text-sm' : 'px-2 py-0.5 text-xs'

  if (type === 'live') {
    return (
      <span className={`${pad} rounded-full font-bold tracking-wider bg-red-900/40 text-red-400 border border-red-700/50`}>
        LIVE
      </span>
    )
  }
  if (type === 'prop') {
    return (
      <span className={`${pad} rounded-full font-bold tracking-wider bg-yellow-900/40 text-yellow-400 border border-yellow-700/50`}>
        PROP
      </span>
    )
  }
  // demo (default)
  return (
    <span className={`${pad} rounded-full font-bold tracking-wider bg-green-900/40 text-green-400 border border-green-700/50`}>
      DEMO
    </span>
  )
}
