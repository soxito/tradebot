/**
 * OpenHumanMascot — Animated SVG mascot inspired by OpenHuman's "tiny" character.
 *
 * The character has 6 moods that animate smoothly:
 *   idle      — gentle bob / breathing pulse
 *   thinking  — head tilts, inner ring pulses
 *   listening — antennas light up, ring expands
 *   talking   — mouth animates, glow ripples
 *   dreaming  — slow sine wave drift, star sparkles
 *   surprised — eyes widen, quick bounce
 *
 * Props
 *   mood     — one of the moods above
 *   size     — overall diameter in px (default 120)
 *   onClick  — click callback
 *   className
 */
import React, { useEffect, useRef, useState } from 'react'

export type MascotMood = 'idle' | 'thinking' | 'listening' | 'talking' | 'dreaming' | 'surprised'

interface Props {
  mood?: MascotMood
  size?: number
  onClick?: () => void
  className?: string
  showLabel?: boolean
}

// Colour palette per mood
const PALETTE: Record<MascotMood, { primary: string; secondary: string; glow: string; ring: string }> = {
  idle:      { primary: '#7C3AED', secondary: '#4F46E5', glow: '#A78BFA', ring: '#6D28D9' },
  thinking:  { primary: '#0EA5E9', secondary: '#7C3AED', glow: '#38BDF8', ring: '#0284C7' },
  listening: { primary: '#059669', secondary: '#0EA5E9', glow: '#34D399', ring: '#047857' },
  talking:   { primary: '#D97706', secondary: '#DC2626', glow: '#FBBF24', ring: '#B45309' },
  dreaming:  { primary: '#4338CA', secondary: '#6D28D9', glow: '#818CF8', ring: '#3730A3' },
  surprised: { primary: '#EC4899', secondary: '#F97316', glow: '#F472B6', ring: '#DB2777' },
}

const LABEL: Record<MascotMood, string> = {
  idle: '', thinking: 'Thinking…', listening: 'Listening…',
  talking: 'Speaking…', dreaming: 'Dreaming…', surprised: '!',
}

export default function OpenHumanMascot({
  mood = 'idle',
  size = 120,
  onClick,
  className = '',
  showLabel = false,
}: Props) {
  const pal = PALETTE[mood]
  const scale = size / 120  // base design is 120px
  const [talkFrame, setTalkFrame] = useState(0)
  const [bobY, setBobY] = useState(0)
  const rafRef = useRef<number>(0)
  const t = useRef(0)

  // Continuous animation loop driving the bob & talk mouth
  useEffect(() => {
    const tick = (ts: number) => {
      t.current = ts
      // Bob (all moods have a gentle vertical oscillation)
      const speed = mood === 'dreaming' ? 0.0008 : mood === 'talking' ? 0.003 : 0.0015
      setBobY(Math.sin(ts * speed) * 4)
      // Talk mouth oscillation
      if (mood === 'talking') {
        setTalkFrame(Math.floor((ts / 120) % 4))
      }
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [mood])

  const mouthPaths: Record<number, string> = {
    0: 'M 52 78 Q 60 82 68 78',
    1: 'M 52 78 Q 60 84 68 78',
    2: 'M 52 78 Q 60 86 68 78',
    3: 'M 52 78 Q 60 84 68 78',
  }
  const mouthPath = mood === 'talking' ? mouthPaths[talkFrame] : 'M 52 78 Q 60 83 68 78'

  const eyeScale = mood === 'surprised' ? 1.4 : mood === 'dreaming' ? 0.6 : 1.0
  const eyeY = mood === 'thinking' ? 3 : 0

  return (
    <div
      className={`relative select-none ${className}`}
      style={{ width: size, height: size + (showLabel ? 18 : 0) }}
      onClick={onClick}
    >
      {/* Outer glow ring */}
      <div
        className="absolute inset-0 rounded-full"
        style={{
          background: `radial-gradient(circle, ${pal.glow}22 0%, transparent 70%)`,
          transform: `scale(${1.2 + (mood === 'listening' ? 0.15 : 0)})`,
          transition: 'transform 0.6s ease, background 0.4s ease',
        }}
      />

      {/* SVG character */}
      <svg
        viewBox="0 0 120 120"
        width={size}
        height={size}
        style={{
          display: 'block',
          transform: `translateY(${bobY}px) rotate(${mood === 'thinking' ? -6 : 0}deg)`,
          transition: 'transform 0.5s ease',
          filter: `drop-shadow(0 0 ${8 * scale}px ${pal.glow})`,
        }}
      >
        <defs>
          <radialGradient id="oh-body-grad" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor={pal.glow} stopOpacity="0.9" />
            <stop offset="100%" stopColor={pal.primary} stopOpacity="1" />
          </radialGradient>
          <radialGradient id="oh-eye-grad" cx="40%" cy="30%" r="70%">
            <stop offset="0%" stopColor="#E0F2FE" />
            <stop offset="100%" stopColor="#BAE6FD" />
          </radialGradient>
          <radialGradient id="oh-core-grad" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={pal.glow} stopOpacity="1" />
            <stop offset="100%" stopColor={pal.secondary} stopOpacity="0.6" />
          </radialGradient>
          <filter id="oh-blur">
            <feGaussianBlur stdDeviation="2" />
          </filter>
        </defs>

        {/* Body / torso */}
        <ellipse cx="60" cy="88" rx="24" ry="16" fill={pal.primary} opacity="0.9" />
        {/* Body highlight */}
        <ellipse cx="60" cy="83" rx="16" ry="10" fill={pal.glow} opacity="0.2" />

        {/* Neck */}
        <rect x="54" y="72" width="12" height="12" rx="4" fill={pal.secondary} opacity="0.9" />

        {/* Head shell */}
        <circle cx="60" cy="58" r="28" fill={pal.primary} />
        <circle cx="60" cy="58" r="28" fill="url(#oh-body-grad)" opacity="0.8" />

        {/* Inner face circle */}
        <circle cx="60" cy="58" r="22" fill={pal.secondary} opacity="0.5" />

        {/* Core/heart glow */}
        <circle cx="60" cy="88" r="6" fill="url(#oh-core-grad)" />
        <circle cx="60" cy="88" r="3" fill={pal.glow} opacity="0.9" />

        {/* Antenna */}
        <line x1="60" y1="30" x2="60" y2="20" stroke={pal.glow} strokeWidth="2.5" strokeLinecap="round" />
        <circle
          cx="60" cy="17"
          r="4"
          fill={mood === 'listening' || mood === 'talking' ? pal.glow : pal.secondary}
          style={{ transition: 'fill 0.3s ease' }}
        />
        {/* Antenna sparkle (dreaming) */}
        {mood === 'dreaming' && (
          <>
            <circle cx="72" cy="22" r="2" fill={pal.glow} opacity="0.8" />
            <circle cx="48" cy="22" r="1.5" fill={pal.glow} opacity="0.6" />
            <circle cx="78" cy="30" r="1.5" fill={pal.glow} opacity="0.5" />
          </>
        )}

        {/* Eyes */}
        <g transform={`translate(0,${eyeY})`} style={{ transition: 'transform 0.3s ease' }}>
          {/* Left eye */}
          <ellipse
            cx="50" cy="56"
            rx={5 * eyeScale} ry={6 * eyeScale}
            fill="url(#oh-eye-grad)"
            style={{ transition: 'rx 0.3s ease, ry 0.3s ease' }}
          />
          <ellipse cx="49.5" cy="56" rx={2 * eyeScale} ry={2.5 * eyeScale} fill="#0C4A6E" />
          <circle cx="48.5" cy="54.5" r="1" fill="white" opacity="0.8" />

          {/* Right eye */}
          <ellipse
            cx="70" cy="56"
            rx={5 * eyeScale} ry={6 * eyeScale}
            fill="url(#oh-eye-grad)"
            style={{ transition: 'rx 0.3s ease, ry 0.3s ease' }}
          />
          <ellipse cx="69.5" cy="56" rx={2 * eyeScale} ry={2.5 * eyeScale} fill="#0C4A6E" />
          <circle cx="68.5" cy="54.5" r="1" fill="white" opacity="0.8" />

          {/* Dreaming: closed eyes */}
          {mood === 'dreaming' && (
            <>
              <path d="M 45 56 Q 50 59 55 56" stroke={pal.glow} strokeWidth="2" fill="none" strokeLinecap="round" />
              <path d="M 65 56 Q 70 59 75 56" stroke={pal.glow} strokeWidth="2" fill="none" strokeLinecap="round" />
              {/* Hide the open eyes */}
              <ellipse cx="50" cy="56" rx="6" ry="7" fill={pal.primary} />
              <ellipse cx="70" cy="56" rx="6" ry="7" fill={pal.primary} />
            </>
          )}
        </g>

        {/* Cheeks (thinking / surprised) */}
        {(mood === 'thinking' || mood === 'surprised') && (
          <>
            <circle cx="43" cy="65" r="5" fill="#EC4899" opacity="0.3" />
            <circle cx="77" cy="65" r="5" fill="#EC4899" opacity="0.3" />
          </>
        )}

        {/* Mouth */}
        <path
          d={mouthPath}
          stroke={pal.glow}
          strokeWidth="2.5"
          fill="none"
          strokeLinecap="round"
          style={{ transition: 'd 0.05s linear' }}
        />

        {/* Circuit patterns on head */}
        <g opacity="0.2" stroke={pal.glow} strokeWidth="0.8" fill="none">
          <path d="M 36 50 L 40 50 L 40 46 L 44 46" />
          <path d="M 84 50 L 80 50 L 80 46 L 76 46" />
          <circle cx="40" cy="50" r="1.5" fill={pal.glow} />
          <circle cx="80" cy="50" r="1.5" fill={pal.glow} />
        </g>

        {/* Thinking thought bubbles */}
        {mood === 'thinking' && (
          <g opacity="0.8">
            <circle cx="82" cy="38" r="3" fill={pal.glow} />
            <circle cx="88" cy="30" r="4.5" fill={pal.glow} />
            <circle cx="93" cy="22" r="3" fill={pal.glow} />
          </g>
        )}

        {/* Ring indicator (listening) */}
        {mood === 'listening' && (
          <circle cx="60" cy="58" r="32" stroke={pal.glow} strokeWidth="1.5" fill="none" opacity="0.5"
            strokeDasharray="6 3"
          />
        )}

        {/* Surprised exclamation */}
        {mood === 'surprised' && (
          <text x="84" y="40" fontSize="14" fontWeight="bold" fill={pal.glow}>!</text>
        )}
      </svg>

      {/* Mood label */}
      {showLabel && LABEL[mood] && (
        <div
          className="text-center text-xs font-medium mt-1"
          style={{ color: pal.glow, fontSize: 11 }}
        >
          {LABEL[mood]}
        </div>
      )}
    </div>
  )
}
