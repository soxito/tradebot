/**
 * Jarvis Room — Dedicated 3D immersive page where JARVIS lives.
 *
 * Features:
 *  • Realistic dark holographic control room rendered with Three.js
 *  • Jarvis robot walks around the room analyzing market data
 *  • Holographic market panels floating in the room (BTC, ETH, Gold prices)
 *  • Wake-word "Jarvis" detection — robot responds with voice + chat answer
 *  • Animated portal/hole: appears when Jarvis is summoned, hides when idle-vanished
 *  • Single listener gate: this page's mic is disabled when extension robot is active
 */
import { useEffect, useRef, useState, useCallback } from 'react'
import dynamic from 'next/dynamic'
import Head from 'next/head'
import * as THREE from 'three'
import { useJarvisSpeak } from '@/hooks/useJarvisSpeak'
import type { RobotState, AvatarStyle } from '@/components/JarvisRobot'

const JarvisRobot = dynamic(() => import('@/components/JarvisRobot'), { ssr: false })

// ── Market data mock (animates in the room) ────────────────────────────────
const MARKETS = [
  { label: 'BTC/USD', base: 67_420, volatility: 250, color: 0xf59e0b },
  { label: 'ETH/USD', base: 3_540,  volatility: 30,  color: 0x06b6d4 },
  { label: 'GOLD',    base: 2_380,  volatility: 8,   color: 0xfbbf24 },
  { label: 'SOL/USD', base: 178,    volatility: 4,   color: 0x8b5cf6 },
]

// ── Three.js room scene ────────────────────────────────────────────────────
// Rebuilt to match the reference concept art: a neon cyberpunk AI lab where
// JARVIS works a wall of holographic screens (neural-net + market charts),
// a companion bot tends a side console, and a robot dog stands guard — all lit
// in cyan + magenta neon with a city skyline glowing through the back window.
function useRoomScene(canvasRef: React.RefObject<HTMLCanvasElement | null>) {
  const rafRef = useRef<number>(0)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const panelMeshes = useRef<Array<{ mesh: THREE.Mesh; matText: THREE.MeshStandardMaterial; label: string; base: number; vol: number }>>([])
  const timeRef = useRef(0)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    let renderer: THREE.WebGLRenderer
    try {
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false })
    } catch { return }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.shadowMap.enabled = true
    renderer.shadowMap.type = THREE.PCFSoftShadowMap
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.05

    const scene = new THREE.Scene()
    sceneRef.current = scene
    scene.background = new THREE.Color(0x050316)
    scene.fog = new THREE.FogExp2(0x0a0524, 0.028)

    const W = canvas.parentElement?.clientWidth || window.innerWidth
    const H = canvas.parentElement?.clientHeight || window.innerHeight
    renderer.setSize(W, H)

    const camera = new THREE.PerspectiveCamera(55, W / H, 0.1, 400)
    camera.position.set(0, 3.4, 14)
    camera.lookAt(0, 2.4, -2)

    // Palette: cyan + magenta/purple neon
    const CYAN = 0x22d3ee
    const CYAN_DEEP = 0x06b6d4
    const MAGENTA = 0xd946ef
    const PURPLE = 0x8b5cf6
    const ORANGE = 0xf59e0b

    // ── Lighting ────────────────────────────────────────────────────────────
    scene.add(new THREE.AmbientLight(0x161033, 0.9))

    const ceiling = new THREE.PointLight(0x2a1a55, 3, 40)
    ceiling.position.set(0, 9, 0)
    scene.add(ceiling)

    const cyanSpot = new THREE.SpotLight(CYAN, 9, 32, Math.PI / 5, 0.45)
    cyanSpot.position.set(-8, 8, 6)
    cyanSpot.castShadow = true
    cyanSpot.target.position.set(-2, 1, 0)
    scene.add(cyanSpot)
    scene.add(cyanSpot.target)

    const magentaSpot = new THREE.SpotLight(MAGENTA, 8, 30, Math.PI / 5, 0.5)
    magentaSpot.position.set(8, 8, 4)
    magentaSpot.target.position.set(3, 1, -4)
    scene.add(magentaSpot)
    scene.add(magentaSpot.target)

    const floorGlow = new THREE.PointLight(CYAN, 2.0, 16)
    floorGlow.position.set(0, 0.3, 2)
    scene.add(floorGlow)

    const magentaFloorGlow = new THREE.PointLight(MAGENTA, 1.4, 14)
    magentaFloorGlow.position.set(5, 0.3, -2)
    scene.add(magentaFloorGlow)

    // ── Floor (reflective dark metal) ─────────────────────────────────────────
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(60, 60),
      new THREE.MeshStandardMaterial({ color: 0x07041a, metalness: 0.96, roughness: 0.18 }),
    )
    floor.rotation.x = -Math.PI / 2
    floor.receiveShadow = true
    scene.add(floor)

    const gridHelper = new THREE.GridHelper(60, 60, CYAN, 0x1a0e3a)
    gridHelper.position.y = 0.01
    ;(gridHelper.material as THREE.Material).opacity = 0.35
    ;(gridHelper.material as THREE.Material).transparent = true
    scene.add(gridHelper)

    // Raised central platform JARVIS stands on
    const platform = new THREE.Mesh(
      new THREE.BoxGeometry(7, 0.35, 6),
      new THREE.MeshStandardMaterial({ color: 0x0c0826, metalness: 0.92, roughness: 0.25 }),
    )
    platform.position.set(0, 0.18, 2)
    platform.receiveShadow = true
    platform.castShadow = true
    scene.add(platform)
    const platformEdge = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(7, 0.35, 6)),
      new THREE.LineBasicMaterial({ color: CYAN }),
    )
    platformEdge.position.copy(platform.position)
    scene.add(platformEdge)

    // ── Walls ───────────────────────────────────────────────────────────────
    const wallMat = new THREE.MeshStandardMaterial({ color: 0x0a0622, metalness: 0.65, roughness: 0.6 })
    const leftWall = new THREE.Mesh(new THREE.PlaneGeometry(36, 20), wallMat)
    leftWall.rotation.y = Math.PI / 2
    leftWall.position.set(-15, 9, -2)
    scene.add(leftWall)
    const rightWall = new THREE.Mesh(new THREE.PlaneGeometry(36, 20), wallMat)
    rightWall.rotation.y = -Math.PI / 2
    rightWall.position.set(15, 9, -2)
    scene.add(rightWall)
    const ceilingMesh = new THREE.Mesh(
      new THREE.PlaneGeometry(40, 36),
      new THREE.MeshStandardMaterial({ color: 0x070420, metalness: 0.8, roughness: 0.5 }),
    )
    ceilingMesh.rotation.x = Math.PI / 2
    ceilingMesh.position.set(0, 16, -4)
    scene.add(ceilingMesh)

    // Window mullions framing the back skyline
    const mullionMat = new THREE.MeshStandardMaterial({ color: 0x0a0826, metalness: 0.85, roughness: 0.4 })
    const backFrame = new THREE.Mesh(new THREE.PlaneGeometry(40, 4), mullionMat)
    backFrame.position.set(0, 16.5, -16)
    scene.add(backFrame)
    for (let i = -3; i <= 3; i++) {
      const col = new THREE.Mesh(new THREE.BoxGeometry(0.25, 18, 0.25), mullionMat)
      col.position.set(i * 5.5, 9, -16)
      scene.add(col)
    }

    // ── City skyline through the back window ──────────────────────────────────
    const skyline = new THREE.Group()
    const skylineColors = [CYAN, PURPLE, MAGENTA, CYAN_DEEP]
    for (let i = 0; i < 46; i++) {
      const w = 1 + Math.random() * 2.2
      const h = 4 + Math.random() * 14
      const c = skylineColors[i % skylineColors.length]
      const tower = new THREE.Mesh(
        new THREE.BoxGeometry(w, h, w),
        new THREE.MeshStandardMaterial({
          color: 0x0a0a1f,
          emissive: c,
          emissiveIntensity: 0.18 + Math.random() * 0.22,
          metalness: 0.7,
          roughness: 0.5,
        }),
      )
      tower.position.set(-26 + (i * 1.15) % 52, h / 2, -18 - Math.random() * 10)
      skyline.add(tower)
    }
    scene.add(skyline)

    // ── Big holographic screen wall on the RIGHT (JARVIS works these) ─────────
    // Neural-network node graph — the centrepiece JARVIS reaches toward.
    const neuralGroup = new THREE.Group()
    neuralGroup.position.set(6.2, 4.6, -6)
    neuralGroup.rotation.y = -0.5
    const layers = [3, 5, 5, 4]
    const nodePositions: THREE.Vector3[][] = []
    const nodeMeshes: THREE.Mesh[] = []
    layers.forEach((count, li) => {
      const col: THREE.Vector3[] = []
      for (let n = 0; n < count; n++) {
        const x = (li - (layers.length - 1) / 2) * 1.25
        const y = (n - (count - 1) / 2) * 0.85
        const isOutput = li === layers.length - 1
        const node = new THREE.Mesh(
          new THREE.SphereGeometry(0.13, 16, 16),
          new THREE.MeshStandardMaterial({
            color: isOutput ? ORANGE : CYAN,
            emissive: isOutput ? ORANGE : CYAN,
            emissiveIntensity: 1.6,
          }),
        )
        node.position.set(x, y, 0)
        neuralGroup.add(node)
        nodeMeshes.push(node)
        col.push(new THREE.Vector3(x, y, 0))
      }
      nodePositions.push(col)
    })
    // Connect adjacent layers
    for (let li = 0; li < nodePositions.length - 1; li++) {
      nodePositions[li].forEach((a) => {
        nodePositions[li + 1].forEach((b) => {
          const geo = new THREE.BufferGeometry().setFromPoints([a, b])
          const line = new THREE.Line(
            geo,
            new THREE.LineBasicMaterial({ color: CYAN, transparent: true, opacity: 0.28 }),
          )
          neuralGroup.add(line)
        })
      })
    }
    scene.add(neuralGroup)
    const neuralGlow = new THREE.PointLight(CYAN, 2.2, 12)
    neuralGlow.position.set(6.2, 4.6, -4.5)
    scene.add(neuralGlow)

    // Holographic screen panels (market charts) around the room
    MARKETS.forEach((market, i) => {
      // Two on the right wall (high), two on the left wall — framing the room
      const layout = [
        { x: 10.5, y: 7,   z: -7,  ry: -0.7 },
        { x: 11,   y: 3.6, z: -3,  ry: -0.85 },
        { x: -10.5, y: 7,  z: -7,  ry: 0.7 },
        { x: -11,  y: 3.6, z: -3,  ry: 0.85 },
      ][i]
      const panelGeo = new THREE.PlaneGeometry(3.8, 2.4)
      const panelMat = new THREE.MeshStandardMaterial({
        color: market.color,
        emissive: market.color,
        emissiveIntensity: 0.18,
        transparent: true,
        opacity: 0.16,
        side: THREE.DoubleSide,
      })
      const panel = new THREE.Mesh(panelGeo, panelMat)
      panel.position.set(layout.x, layout.y, layout.z)
      panel.rotation.y = layout.ry
      scene.add(panel)

      const frame = new THREE.LineSegments(
        new THREE.EdgesGeometry(panelGeo),
        new THREE.LineBasicMaterial({ color: market.color }),
      )
      frame.position.copy(panel.position)
      frame.rotation.copy(panel.rotation)
      scene.add(frame)

      // Mini line-chart drawn inside the panel
      const pts: THREE.Vector3[] = []
      for (let s = 0; s <= 24; s++) {
        pts.push(new THREE.Vector3(-1.6 + (s / 24) * 3.2, Math.sin(s * 0.6 + i) * 0.45 + Math.random() * 0.12, 0.02))
      }
      const chart = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(pts),
        new THREE.LineBasicMaterial({ color: market.color }),
      )
      chart.position.copy(panel.position)
      chart.rotation.copy(panel.rotation)
      scene.add(chart)

      const panelLight = new THREE.PointLight(market.color, 1.0, 7)
      panelLight.position.set(layout.x * 0.85, layout.y, layout.z + 0.5)
      scene.add(panelLight)

      panelMeshes.current.push({ mesh: panel, matText: panelMat, label: market.label, base: market.base, vol: market.volatility })
    })

    // ── Companion bot's console desk (LEFT) ───────────────────────────────────
    const deskMat = new THREE.MeshStandardMaterial({ color: 0x0c0826, metalness: 0.88, roughness: 0.3 })
    const desk = new THREE.Mesh(new THREE.BoxGeometry(6, 0.2, 2.4), deskMat)
    desk.position.set(-8.5, 1.1, 0.5)
    desk.rotation.y = 0.5
    desk.castShadow = true
    desk.receiveShadow = true
    scene.add(desk)
    const deskEdge = new THREE.Mesh(
      new THREE.BoxGeometry(6, 0.05, 0.05),
      new THREE.MeshStandardMaterial({ color: MAGENTA, emissive: MAGENTA, emissiveIntensity: 3 }),
    )
    deskEdge.position.set(-8.5, 1.22, 0.5)
    deskEdge.rotation.y = 0.5
    scene.add(deskEdge)
    // Console screens on the desk
    ;[-1.3, 0.2, 1.6].forEach((dx, k) => {
      const sc = new THREE.Mesh(
        new THREE.PlaneGeometry(1.5, 1.0),
        new THREE.MeshStandardMaterial({
          color: k === 1 ? ORANGE : CYAN,
          emissive: k === 1 ? ORANGE : CYAN,
          emissiveIntensity: 0.4,
          transparent: true,
          opacity: 0.45,
          side: THREE.DoubleSide,
        }),
      )
      sc.position.set(-8.5 + dx * Math.cos(0.5), 2.2, 0.5 + dx * Math.sin(0.5))
      sc.rotation.y = 0.5
      sc.rotation.x = -0.15
      scene.add(sc)
    })

    // ── Robot dog (LEFT foreground) ────────────────────────────────────────────
    const dog = new THREE.Group()
    const dogMetal = new THREE.MeshStandardMaterial({ color: 0x3a2f5c, metalness: 0.92, roughness: 0.28, emissive: PURPLE, emissiveIntensity: 0.12 })
    const dogGlow = new THREE.MeshStandardMaterial({ color: CYAN, emissive: CYAN, emissiveIntensity: 2.2 })
    const dogBody = new THREE.Mesh(new THREE.BoxGeometry(1.5, 0.55, 0.6), dogMetal)
    dogBody.position.y = 0.95
    dogBody.castShadow = true
    dog.add(dogBody)
    // Head
    const dogHead = new THREE.Mesh(new THREE.BoxGeometry(0.55, 0.5, 0.5), dogMetal)
    dogHead.position.set(0.95, 1.15, 0)
    dog.add(dogHead)
    const dogSnout = new THREE.Mesh(new THREE.BoxGeometry(0.35, 0.28, 0.4), dogMetal)
    dogSnout.position.set(1.28, 1.05, 0)
    dog.add(dogSnout)
    // Glowing eyes
    ;[-0.13, 0.13].forEach((ez) => {
      const eye = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 12), dogGlow)
      eye.position.set(1.18, 1.2, ez)
      dog.add(eye)
    })
    // Legs
    ;[[-0.55, 0.22], [0.55, 0.22], [-0.55, -0.22], [0.55, -0.22]].forEach(([lx, lz]) => {
      const upper = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.5, 0.16), dogMetal)
      upper.position.set(lx, 0.6, lz)
      dog.add(upper)
      const lower = new THREE.Mesh(new THREE.BoxGeometry(0.13, 0.5, 0.13), dogMetal)
      lower.position.set(lx, 0.25, lz + 0.08)
      dog.add(lower)
    })
    // Tail antenna
    const tail = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.7, 8), dogMetal)
    tail.position.set(-0.85, 1.25, 0)
    tail.rotation.z = Math.PI / 4
    dog.add(tail)
    const tailTip = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 12), dogGlow)
    tailTip.position.set(-1.1, 1.5, 0)
    dog.add(tailTip)
    dog.position.set(-6.5, 0, 4)
    dog.rotation.y = -0.6
    dog.scale.setScalar(1.35)
    scene.add(dog)
    const dogLight = new THREE.PointLight(CYAN, 3, 11)
    dogLight.position.set(-6, 2.4, 6)
    scene.add(dogLight)
    const dogRim = new THREE.PointLight(MAGENTA, 1.8, 10)
    dogRim.position.set(-9, 1.8, 5)
    scene.add(dogRim)

    // ── Holographic JARVIS core orb (floats above the right screens) ──────────
    const orbMat = new THREE.MeshStandardMaterial({
      color: CYAN, emissive: CYAN, emissiveIntensity: 1.8,
      transparent: true, opacity: 0.7,
    })
    const orb = new THREE.Mesh(new THREE.SphereGeometry(0.4, 32, 32), orbMat)
    orb.position.set(6.2, 8.2, -6)
    scene.add(orb)
    const ringColors = [CYAN, MAGENTA, ORANGE]
    const rings: THREE.Mesh[] = []
    ringColors.forEach((c, i) => {
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(0.7 + i * 0.28, 0.018, 12, 80),
        new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 1.5 }),
      )
      ring.rotation.x = (i * Math.PI) / 3
      ring.position.copy(orb.position)
      scene.add(ring)
      rings.push(ring)
    })

    // ── Draping cables from the ceiling ───────────────────────────────────────
    const cableColors = [MAGENTA, CYAN, PURPLE, MAGENTA, CYAN]
    cableColors.forEach((c, i) => {
      const curve = new THREE.CatmullRomCurve3([
        new THREE.Vector3(-12 + i * 6, 15.5, -13),
        new THREE.Vector3(-12 + i * 6 + (Math.random() - 0.5) * 4, 9 - Math.random() * 3, -10 + (Math.random() - 0.5) * 3),
        new THREE.Vector3(-12 + i * 6 + (Math.random() - 0.5) * 6, 5 - Math.random() * 2, -7),
        new THREE.Vector3(-10 + i * 6, 1.5, -5),
      ])
      const tube = new THREE.Mesh(
        new THREE.TubeGeometry(curve, 40, 0.05, 8, false),
        new THREE.MeshStandardMaterial({ color: 0x120a26, emissive: c, emissiveIntensity: 0.25, metalness: 0.6, roughness: 0.5 }),
      )
      scene.add(tube)
    })

    // ── Neon wall strips (vertical magenta + horizontal cyan accents) ─────────
    ;[
      { x: -14.8, y: 9, z: -6, h: 12, c: MAGENTA, ry: Math.PI / 2 },
      { x: -14.8, y: 9, z: 4,  h: 12, c: CYAN,    ry: Math.PI / 2 },
      { x: 14.8,  y: 9, z: -6, h: 12, c: CYAN,    ry: -Math.PI / 2 },
      { x: 14.8,  y: 9, z: 4,  h: 12, c: MAGENTA, ry: -Math.PI / 2 },
    ].forEach((s) => {
      const strip = new THREE.Mesh(
        new THREE.BoxGeometry(0.12, s.h, 0.12),
        new THREE.MeshStandardMaterial({ color: s.c, emissive: s.c, emissiveIntensity: 2.5 }),
      )
      strip.position.set(s.x, s.y, s.z)
      scene.add(strip)
    })

    // ── Particle field (floating data points) ────────────────────────────────
    const particleCount = 320
    const positions = new Float32Array(particleCount * 3)
    for (let i = 0; i < particleCount; i++) {
      positions[i * 3]     = (Math.random() - 0.5) * 34
      positions[i * 3 + 1] = Math.random() * 13
      positions[i * 3 + 2] = (Math.random() - 0.5) * 24
    }
    const particleGeo = new THREE.BufferGeometry()
    particleGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    const particles = new THREE.Points(
      particleGeo,
      new THREE.PointsMaterial({ color: CYAN, size: 0.045, transparent: true, opacity: 0.55 }),
    )
    scene.add(particles)

    // ── Animation loop ────────────────────────────────────────────────────────
    const animate = () => {
      rafRef.current = requestAnimationFrame(animate)
      timeRef.current += 0.016
      const t = timeRef.current

      orbMat.emissiveIntensity = 1.5 + Math.sin(t * 2) * 0.5
      orb.scale.setScalar(1 + Math.sin(t * 1.5) * 0.05)

      rings.forEach((ring, i) => {
        ring.rotation.x += 0.006 * (i % 2 === 0 ? 1 : -1)
        ring.rotation.y += 0.008 * (i === 1 ? -1 : 1)
        ring.rotation.z += 0.004
      })

      // Neural net pulse — nodes flicker like activity
      neuralGroup.rotation.y = -0.5 + Math.sin(t * 0.25) * 0.08
      nodeMeshes.forEach((node, i) => {
        const m = node.material as THREE.MeshStandardMaterial
        m.emissiveIntensity = 1.2 + Math.sin(t * 3 + i * 0.7) * 0.7
      })

      panelMeshes.current.forEach((p, i) => {
        p.matText.opacity = 0.14 + Math.sin(t * 0.8 + i) * 0.05
        p.matText.emissiveIntensity = 0.14 + Math.sin(t * 1.2 + i) * 0.06
      })

      // Robot dog idle: gentle head bob + tail-tip glow
      dog.position.y = Math.sin(t * 0.8) * 0.04
      ;(tailTip.material as THREE.MeshStandardMaterial).emissiveIntensity = 1.2 + Math.sin(t * 4) * 0.6

      const pos = particles.geometry.attributes.position as THREE.BufferAttribute
      for (let i = 0; i < particleCount; i++) {
        pos.array[i * 3 + 1] += 0.0035
        if (pos.array[i * 3 + 1] > 13) pos.array[i * 3 + 1] = 0
      }
      pos.needsUpdate = true

      camera.position.x = Math.sin(t * 0.08) * 0.6
      camera.position.y = 3.4 + Math.sin(t * 0.06) * 0.18
      camera.lookAt(0, 2.4, -2)

      renderer.render(scene, camera)
    }
    animate()

    const onResize = () => {
      const w = canvas.parentElement?.clientWidth || window.innerWidth
      const h = canvas.parentElement?.clientHeight || window.innerHeight
      renderer.setSize(w, h)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    window.addEventListener('resize', onResize)

    return () => {
      cancelAnimationFrame(rafRef.current)
      window.removeEventListener('resize', onResize)
      renderer.dispose()
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
}

// ── Wake-word listener (browser SpeechRecognition) ─────────────────────────
function useWakeWord(onWake: (transcript: string) => void, disabled: boolean) {
  const recognRef = useRef<any>(null)
  const activeRef = useRef(false)

  useEffect(() => {
    if (disabled) {
      recognRef.current?.stop()
      return
    }
    if (typeof window === 'undefined') return
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SR) return

    const r = new SR()
    recognRef.current = r
    r.continuous = true
    r.interimResults = true
    r.lang = 'en-US'

    r.onresult = (e: any) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript.toLowerCase().trim()
        if (/\bjarvis\b/.test(t)) {
          onWake(t)
        }
      }
    }

    r.onerror = () => {
      // Restart on error after a short delay
      setTimeout(() => { if (!disabled && activeRef.current) { try { r.start() } catch { /* noop */ } } }, 1500)
    }
    r.onend = () => {
      // Auto-restart
      setTimeout(() => { if (!disabled && activeRef.current) { try { r.start() } catch { /* noop */ } } }, 500)
    }

    activeRef.current = true
    try { r.start() } catch { /* noop */ }

    return () => {
      activeRef.current = false
      try { r.stop() } catch { /* noop */ }
    }
  }, [disabled, onWake])
}

// ── Main page component ────────────────────────────────────────────────────
export default function JarvisRoom() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  useRoomScene(canvasRef)

  const speakAsJarvis = useJarvisSpeak()

  // Robot animation state
  const [robotState, setRobotState] = useState<RobotState>('idle')
  const [avatarStyle] = useState<AvatarStyle>('cyan')
  const [robotEnergy, setRobotEnergy] = useState(0)

  // Client-only ticker for the live market HUD (avoids SSR hydration mismatch
  // from Date.now()/Math.random rendered on the server vs. the client).
  const [mounted, setMounted] = useState(false)
  const [, setTick] = useState(0)
  useEffect(() => {
    setMounted(true)
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  // Chat panel
  const [chatOpen, setChatOpen] = useState(false)
  const [question, setQuestion] = useState('')
  const [messages, setMessages] = useState<{ role: 'user' | 'jarvis'; text: string }[]>([
    { role: 'jarvis', text: 'Online. All market systems nominal. Say "Jarvis" or type a question, Sir.' },
  ])
  const chatEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Portal/hole state
  const [portalVisible, setPortalVisible] = useState(false)
  const [robotVisible, setRobotVisible] = useState(true)
  const idleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Extension robot lock — when ext robot is active, this page's mic goes silent
  const [extRobotActive, setExtRobotActive] = useState(false)
  useEffect(() => {
    const onLock = (e: Event) => {
      const ev = e as CustomEvent
      setExtRobotActive(!!ev.detail?.locked)
    }
    const onMsg = (e: MessageEvent) => {
      if (e.data?.__jarvisPage !== true) return
      if (e.data.type === 'robot-mode') setExtRobotActive(!!e.data.active)
    }
    window.addEventListener('jarvis-robot-lock', onLock)
    window.addEventListener('message', onMsg)
    return () => {
      window.removeEventListener('jarvis-robot-lock', onLock)
      window.removeEventListener('message', onMsg)
    }
  }, [])

  // Auto-scroll chat
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Vanish idle logic (60s)
  const resetIdleTimer = useCallback(() => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    idleTimerRef.current = setTimeout(() => {
      setRobotState('idle')
      setRobotVisible(false)
      setPortalVisible(true)
    }, 60_000)
  }, [])

  useEffect(() => {
    resetIdleTimer()
    return () => { if (idleTimerRef.current) clearTimeout(idleTimerRef.current) }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Summon Jarvis from portal
  const summonJarvis = useCallback((transcript?: string) => {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current)
    setPortalVisible(false)
    setRobotVisible(true)
    setRobotState('listening')
    setChatOpen(true)
    resetIdleTimer()

    const greeting = transcript
      ? `Detected: "${transcript}". I'm here, Sir. How can I assist?`
      : `Online, Sir. All systems ready.`
    setMessages(prev => [...prev, { role: 'jarvis', text: greeting }])
    speakAsJarvis(greeting)
    setTimeout(() => setRobotState('idle'), 3000)
  }, [speakAsJarvis, resetIdleTimer])

  // Wake-word handler
  const handleWake = useCallback((transcript: string) => {
    summonJarvis(transcript)
  }, [summonJarvis])

  // Wake-word detection (disabled when extension robot is active)
  useWakeWord(handleWake, extRobotActive)

  // Also listen for wake events from extension
  useEffect(() => {
    const onExtWake = () => summonJarvis()
    window.addEventListener('jarvis-wake', onExtWake)
    window.addEventListener('jarvis-ext-wake', onExtWake)
    return () => {
      window.removeEventListener('jarvis-wake', onExtWake)
      window.removeEventListener('jarvis-ext-wake', onExtWake)
    }
  }, [summonJarvis])

  // Send a chat message to the AI
  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return
    const userMsg = { role: 'user' as const, text: text.trim() }
    setMessages(prev => [...prev, userMsg])
    setQuestion('')
    setRobotState('thinking')
    resetIdleTimer()

    try {
      const res = await fetch('http://localhost:8000/api/v1/paul/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text.trim(), history: [] }),
        signal: AbortSignal.timeout(30000),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const reply = data.response || data.message || data.answer || 'Analysis complete, Sir.'
      setMessages(prev => [...prev, { role: 'jarvis', text: reply }])
      setRobotState('talking')
      setRobotEnergy(0.7)
      speakAsJarvis(reply)
      setTimeout(() => { setRobotState('idle'); setRobotEnergy(0) }, 3500)
    } catch {
      const fallback = 'Systems processing. Backend connection required for full analysis, Sir.'
      setMessages(prev => [...prev, { role: 'jarvis', text: fallback }])
      setRobotState('talking')
      speakAsJarvis(fallback)
      setTimeout(() => { setRobotState('idle'); setRobotEnergy(0) }, 2500)
    }
  }, [speakAsJarvis, resetIdleTimer])

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') sendMessage(question)
  }, [question, sendMessage])

  return (
    <>
      <Head>
        <title>JARVIS Room — TradeBot</title>
      </Head>

      {/* Full-bleed 3D room canvas */}
      <div style={{ position: 'fixed', inset: 0, zIndex: 0 }}>
        <canvas ref={canvasRef} style={{ width: '100%', height: '100%', display: 'block' }} />
      </div>

      {/* ── Holographic screen labels JARVIS is working (right side) ───────── */}
      <div style={{
        position: 'fixed',
        top: '40%',
        right: '3%',
        zIndex: 50,
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
        pointerEvents: 'none',
        fontFamily: 'monospace',
        textAlign: 'right',
      }}>
        <div style={{ animation: 'jarvis-holo-flicker 4s ease-in-out infinite' }}>
          <div style={{ fontSize: 16, fontWeight: 800, color: '#22d3ee', letterSpacing: '0.1em', textShadow: '0 0 14px rgba(34,211,238,0.9)' }}>
            UNIT-A12 // ADVANCED COGNITION
          </div>
          <div style={{ fontSize: 9, color: '#67e8f9', letterSpacing: '0.16em', marginTop: 4, opacity: 0.8 }}>
            NEURAL NET RPT // DATA STREAM ACTIVE // ALGO PROCESS
          </div>
        </div>
        <div style={{ animation: 'jarvis-holo-flicker 5.5s ease-in-out infinite' }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: '#f59e0b', letterSpacing: '0.12em', textShadow: '0 0 12px rgba(245,158,11,0.8)' }}>
            AI PROCESSING UNIT_07
          </div>
          <div style={{ fontSize: 9, color: '#fcd34d', letterSpacing: '0.18em', marginTop: 3, opacity: 0.7 }}>
            CORE TEMP NOMINAL // THROUGHPUT 99.4%
          </div>
        </div>
      </div>

      {/* Project label (top-left of the holo wall) */}
      <div style={{
        position: 'fixed',
        top: '12%',
        left: '30%',
        zIndex: 50,
        pointerEvents: 'none',
        fontFamily: 'monospace',
        fontSize: 13,
        fontWeight: 800,
        color: '#d946ef',
        letterSpacing: '0.16em',
        textShadow: '0 0 14px rgba(217,70,239,0.85)',
        animation: 'jarvis-holo-flicker 6s ease-in-out infinite',
      }}>
        PROJECT: COGNIZANCE
      </div>

      {/* ── Companion bot tending the side console (left) ──────────────────── */}
      <div style={{
        position: 'fixed',
        left: '7%',
        bottom: 120,
        zIndex: 120,
        pointerEvents: 'none',
        filter: 'drop-shadow(0 0 22px rgba(217,70,239,0.55)) drop-shadow(0 10px 30px rgba(0,0,0,0.8))',
        animation: 'jarvis-companion-float 5s ease-in-out infinite',
      }}>
        <JarvisRobot
          state="thinking"
          energy={0.4}
          avatarStyle="purple"
          size={150}
        />
        <div style={{
          position: 'absolute',
          bottom: 6,
          left: '50%',
          transform: 'translateX(-50%)',
          background: 'rgba(19,16,31,.92)',
          border: '1px solid rgba(217,70,239,.45)',
          borderRadius: 10,
          padding: '3px 10px',
          fontSize: 9,
          fontWeight: 700,
          color: '#e9d5ff',
          whiteSpace: 'nowrap',
          letterSpacing: '0.1em',
          fontFamily: 'monospace',
        }}>
          UNIT-B07 · ASSIST
        </div>
      </div>

      {/* ── Portal / hole ──────────────────────────────────────────────────── */}
      <div
        style={{
          position: 'fixed',
          left: '50%',
          bottom: 80,
          width: 160,
          height: 60,
          zIndex: 100,
          borderRadius: '50%',
          background: 'radial-gradient(ellipse at center, #000 20%, #06b6d4 65%, transparent 100%)',
          boxShadow: '0 0 40px 16px #06b6d4, 0 0 80px 30px rgba(6,182,212,0.4)',
          opacity: portalVisible ? 1 : 0,
          transform: `translateX(-50%) scaleY(${portalVisible ? 1 : 0})`,
          transition: 'opacity 0.6s, transform 0.6s',
          pointerEvents: portalVisible ? 'auto' : 'none',
          cursor: 'pointer',
          animation: portalVisible ? 'jarvis-portal-pulse 1.5s ease-in-out infinite alternate' : undefined,
        }}
        onClick={() => summonJarvis()}
        title="Click or say 'Jarvis' to summon"
      />
      {portalVisible && (
        <div style={{
          position: 'fixed',
          left: '50%',
          bottom: 56,
          transform: 'translateX(-50%)',
          color: '#06b6d4',
          fontSize: 11,
          fontFamily: 'monospace',
          letterSpacing: '0.1em',
          textShadow: '0 0 8px #06b6d4',
          zIndex: 101,
          pointerEvents: 'none',
          animation: 'jarvis-portal-text-fade 2s ease-in-out infinite',
        }}>
          SAY &quot;JARVIS&quot; TO SUMMON
        </div>
      )}

      {/* ── Jarvis robot in the room (works the holo-screens on its right) ── */}
      {robotVisible && (
        <div
          style={{
            position: 'fixed',
            left: '42%',
            bottom: 90,
            transform: 'translateX(-50%)',
            zIndex: 200,
            animation: robotVisible ? 'jarvis-room-emerge 0.7s ease-out' : undefined,
            filter: 'drop-shadow(0 0 36px rgba(34,211,238,0.65)) drop-shadow(0 12px 40px rgba(0,0,0,0.85))',
            cursor: 'pointer',
          }}
          onClick={() => setChatOpen(o => !o)}
          title="Click to open chat"
        >
          {/* Holographic data beam — JARVIS reaching into the screen wall */}
          <div style={{
            position: 'absolute',
            top: '34%',
            left: '78%',
            width: 220,
            height: 4,
            background: 'linear-gradient(90deg, rgba(34,211,238,0.95), rgba(34,211,238,0))',
            boxShadow: '0 0 12px rgba(34,211,238,0.8)',
            transformOrigin: 'left center',
            transform: 'rotate(-8deg)',
            pointerEvents: 'none',
            animation: 'jarvis-beam-pulse 1.8s ease-in-out infinite',
          }} />
          {/* UNIT-A12 designation badge */}
          <div style={{
            position: 'absolute',
            top: -14,
            left: '50%',
            transform: 'translateX(-50%)',
            background: 'rgba(5,3,22,.9)',
            border: '1px solid rgba(34,211,238,.5)',
            borderRadius: 10,
            padding: '3px 12px',
            fontSize: 10,
            fontWeight: 800,
            color: '#67e8f9',
            whiteSpace: 'nowrap',
            letterSpacing: '0.14em',
            fontFamily: 'monospace',
            textShadow: '0 0 8px rgba(34,211,238,0.8)',
            boxShadow: '0 0 14px rgba(34,211,238,0.3)',
          }}>
            UNIT-A12 · JARVIS
          </div>
          <JarvisRobot
            state={robotState === 'idle' ? 'walking' : robotState}
            energy={robotEnergy}
            avatarStyle={avatarStyle}
            size={300}
          />
          {robotState === 'listening' && (
            <div style={{
              position: 'absolute',
              bottom: -12,
              left: '50%',
              transform: 'translateX(-50%)',
              background: 'rgba(8,14,26,.92)',
              border: '1px solid rgba(6,182,212,.4)',
              borderRadius: 12,
              padding: '4px 14px',
              fontSize: 11,
              fontWeight: 700,
              color: '#86efac',
              whiteSpace: 'nowrap',
              letterSpacing: '0.06em',
              fontFamily: 'monospace',
              boxShadow: '0 0 12px rgba(6,182,212,0.3)',
            }}>
              LISTENING…
            </div>
          )}
          {robotState === 'thinking' && (
            <div style={{
              position: 'absolute',
              bottom: -12,
              left: '50%',
              transform: 'translateX(-50%)',
              background: 'rgba(8,14,26,.92)',
              border: '1px solid rgba(139,92,246,.4)',
              borderRadius: 12,
              padding: '4px 14px',
              fontSize: 11,
              fontWeight: 700,
              color: '#c4b5fd',
              whiteSpace: 'nowrap',
              letterSpacing: '0.06em',
              fontFamily: 'monospace',
            }}>
              ANALYZING MARKETS…
            </div>
          )}
          {robotState === 'talking' && (
            <div style={{
              position: 'absolute',
              bottom: -12,
              left: '50%',
              transform: 'translateX(-50%)',
              background: 'rgba(8,14,26,.92)',
              border: '1px solid rgba(245,158,11,.4)',
              borderRadius: 12,
              padding: '4px 14px',
              fontSize: 11,
              fontWeight: 700,
              color: '#fbbf24',
              whiteSpace: 'nowrap',
              letterSpacing: '0.06em',
              fontFamily: 'monospace',
            }}>
              RESPONDING…
            </div>
          )}
        </div>
      )}

      {/* ── HUD: market data overlay ────────────────────────────────────── */}
      <div style={{
        position: 'fixed',
        top: 20,
        right: 20,
        zIndex: 300,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        pointerEvents: 'none',
      }}>
        {MARKETS.map((m, i) => {
          // Use the live clock only after mount; SSR + first client render use a
          // fixed t=0 so the markup matches and React doesn't throw a hydration error.
          const clock = mounted ? Date.now() : 0
          const price = m.base + Math.sin(clock * 0.0003 + i) * m.volatility
          const up = Math.sin(clock * 0.0002 + i) > 0
          const pct = Math.abs(Math.sin(clock * 0.0004 + i) * 2.4)
          return (
          <div key={m.label} style={{
            background: 'rgba(4,14,28,0.85)',
            border: `1px solid rgba(${i === 0 ? '245,158,11' : i === 1 ? '6,182,212' : i === 2 ? '251,191,36' : '139,92,246'},.3)`,
            borderRadius: 8,
            padding: '6px 14px',
            fontSize: 11,
            fontFamily: 'monospace',
            color: '#e2e8f0',
            letterSpacing: '0.06em',
            backdropFilter: 'blur(8px)',
            minWidth: 140,
          }}>
            <div style={{ color: i === 0 ? '#f59e0b' : i === 1 ? '#06b6d4' : i === 2 ? '#fbbf24' : '#8b5cf6', fontWeight: 700, fontSize: 9, letterSpacing: '0.1em', marginBottom: 2 }}>
              {m.label}
            </div>
            <div style={{ fontSize: 14, fontWeight: 700 }}>
              ${price.toLocaleString('en-US', { maximumFractionDigits: 2 })}
            </div>
            <div style={{ fontSize: 9, color: up ? '#4ade80' : '#f87171', marginTop: 1 }}>
              {up ? '▲' : '▼'} {pct.toFixed(2)}%
            </div>
          </div>
          )
        })}
      </div>

      {/* ── Status HUD top-left ──────────────────────────────────────────── */}
      <div style={{
        position: 'fixed',
        top: 20,
        left: 20,
        zIndex: 300,
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        pointerEvents: 'none',
      }}>
        <div style={{
          background: 'rgba(4,14,28,0.85)',
          border: '1px solid rgba(6,182,212,.2)',
          borderRadius: 8,
          padding: '8px 16px',
          fontFamily: 'monospace',
          backdropFilter: 'blur(8px)',
        }}>
          <div style={{ fontSize: 10, color: '#64748b', letterSpacing: '0.1em', marginBottom: 4 }}>JARVIS CONTROL ROOM</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{
              width: 8, height: 8, borderRadius: '50%',
              background: extRobotActive ? '#f59e0b' : '#22c55e',
              boxShadow: `0 0 6px ${extRobotActive ? '#f59e0b' : '#22c55e'}`,
              animation: 'jarvis-status-pulse 1.2s ease-in-out infinite alternate',
            }} />
            <div style={{ fontSize: 11, color: extRobotActive ? '#fbbf24' : '#86efac', fontWeight: 700, letterSpacing: '0.06em' }}>
              {extRobotActive ? 'EXT ROBOT ACTIVE' : 'SYSTEMS ONLINE'}
            </div>
          </div>
          {!extRobotActive && (
            <div style={{ fontSize: 9, color: '#475569', marginTop: 2 }}>
              {portalVisible ? 'Say "Jarvis" to summon' : 'Listening for wake word…'}
            </div>
          )}
        </div>
        <button
          onClick={() => summonJarvis()}
          style={{
            background: 'rgba(6,182,212,0.12)',
            border: '1px solid rgba(6,182,212,.3)',
            borderRadius: 8,
            padding: '6px 14px',
            color: '#06b6d4',
            fontSize: 11,
            fontFamily: 'monospace',
            letterSpacing: '0.08em',
            cursor: 'pointer',
            pointerEvents: 'auto',
            transition: 'background 0.2s',
          }}
          onMouseEnter={e => (e.currentTarget.style.background = 'rgba(6,182,212,0.22)')}
          onMouseLeave={e => (e.currentTarget.style.background = 'rgba(6,182,212,0.12)')}
        >
          ▶ SUMMON JARVIS
        </button>
      </div>

      {/* ── Chat panel ────────────────────────────────────────────────────── */}
      {chatOpen && !extRobotActive && (
        <div style={{
          position: 'fixed',
          bottom: 20,
          left: '50%',
          transform: 'translateX(-50%)',
          width: 440,
          maxHeight: 400,
          zIndex: 400,
          background: 'rgba(4,14,28,0.96)',
          border: '1px solid rgba(6,182,212,.25)',
          borderRadius: 16,
          backdropFilter: 'blur(16px)',
          boxShadow: '0 0 40px rgba(6,182,212,0.15), 0 24px 60px rgba(0,0,0,0.7)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          animation: 'jarvis-chat-slide 0.3s ease-out',
        }}>
          {/* Chat header */}
          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '12px 16px',
            borderBottom: '1px solid rgba(6,182,212,.12)',
            background: 'rgba(6,182,212,0.05)',
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: '#22c55e', boxShadow: '0 0 6px #22c55e', animation: 'jarvis-status-pulse 1.2s ease-in-out infinite alternate' }} />
              <span style={{ fontFamily: 'monospace', fontSize: 12, fontWeight: 700, color: '#06b6d4', letterSpacing: '0.1em' }}>
                JARVIS · CONTROL ROOM
              </span>
            </div>
            <button
              onClick={() => setChatOpen(false)}
              style={{ background: 'none', border: 'none', color: '#475569', cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: '2px 6px' }}
            >
              ✕
            </button>
          </div>

          {/* Messages */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: 10 }}>
            {messages.map((msg, i) => (
              <div key={i} style={{
                display: 'flex',
                justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
              }}>
                <div style={{
                  maxWidth: '80%',
                  padding: '8px 12px',
                  borderRadius: msg.role === 'user' ? '12px 12px 2px 12px' : '12px 12px 12px 2px',
                  background: msg.role === 'user'
                    ? 'rgba(6,182,212,0.18)'
                    : 'rgba(139,92,246,0.12)',
                  border: msg.role === 'user'
                    ? '1px solid rgba(6,182,212,.25)'
                    : '1px solid rgba(139,92,246,.2)',
                  fontSize: 12,
                  color: msg.role === 'user' ? '#bae6fd' : '#ddd6fe',
                  fontFamily: msg.role === 'jarvis' ? 'monospace' : 'inherit',
                  lineHeight: 1.5,
                }}>
                  {msg.role === 'jarvis' && (
                    <div style={{ fontSize: 9, color: '#8b5cf6', fontWeight: 700, letterSpacing: '0.1em', marginBottom: 4 }}>
                      ◈ JARVIS
                    </div>
                  )}
                  {msg.text}
                </div>
              </div>
            ))}
            <div ref={chatEndRef} />
          </div>

          {/* Input */}
          <div style={{
            padding: '10px 12px',
            borderTop: '1px solid rgba(6,182,212,.12)',
            display: 'flex',
            gap: 8,
          }}>
            <input
              ref={inputRef}
              value={question}
              onChange={e => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Ask Jarvis about the market…"
              style={{
                flex: 1,
                background: 'rgba(6,182,212,0.07)',
                border: '1px solid rgba(6,182,212,.2)',
                borderRadius: 8,
                padding: '8px 12px',
                color: '#e2e8f0',
                fontSize: 12,
                fontFamily: 'inherit',
                outline: 'none',
              }}
            />
            <button
              onClick={() => sendMessage(question)}
              disabled={!question.trim()}
              style={{
                background: question.trim() ? 'rgba(6,182,212,0.2)' : 'rgba(6,182,212,0.05)',
                border: '1px solid rgba(6,182,212,.3)',
                borderRadius: 8,
                padding: '8px 16px',
                color: '#06b6d4',
                fontSize: 12,
                cursor: question.trim() ? 'pointer' : 'not-allowed',
                fontFamily: 'monospace',
                transition: 'background 0.15s',
              }}
            >
              ▶ SEND
            </button>
          </div>
        </div>
      )}

      {/* ── CSS keyframes ────────────────────────────────────────────────── */}
      <style>{`
        @keyframes jarvis-portal-pulse {
          from { box-shadow: 0 0 40px 16px #06b6d4, 0 0 80px 30px rgba(6,182,212,0.4); transform: translateX(-50%) scaleX(1) scaleY(1); }
          to   { box-shadow: 0 0 60px 24px #22d3ee, 0 0 100px 40px rgba(34,211,238,0.5); transform: translateX(-50%) scaleX(1.1) scaleY(1.2); }
        }
        @keyframes jarvis-portal-text-fade {
          0%,100% { opacity: 0.5; }
          50%      { opacity: 1; }
        }
        @keyframes jarvis-room-emerge {
          from { transform: translateX(-50%) scaleY(0.1) translateY(80px); opacity: 0; }
          to   { transform: translateX(-50%) scaleY(1) translateY(0); opacity: 1; }
        }
        @keyframes jarvis-chat-slide {
          from { opacity: 0; transform: translateX(-50%) translateY(20px); }
          to   { opacity: 1; transform: translateX(-50%) translateY(0); }
        }
        @keyframes jarvis-status-pulse {
          from { opacity: 0.7; }
          to   { opacity: 1; }
        }
        @keyframes jarvis-holo-flicker {
          0%, 100% { opacity: 0.92; }
          47%      { opacity: 0.92; }
          48%      { opacity: 0.55; }
          49%      { opacity: 0.95; }
          73%      { opacity: 0.7; }
          74%      { opacity: 0.95; }
        }
        @keyframes jarvis-companion-float {
          0%, 100% { transform: translateY(0); }
          50%      { transform: translateY(-8px); }
        }
        @keyframes jarvis-beam-pulse {
          0%, 100% { opacity: 0.35; transform: rotate(-8deg) scaleX(0.95); }
          50%      { opacity: 1;    transform: rotate(-8deg) scaleX(1.05); }
        }
      `}</style>
    </>
  )
}

// Disable Layout wrapper so this page is full-screen
JarvisRoom.noLayout = true
