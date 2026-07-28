/**
 * variantScene — the renderer harness that drives a `robotVariants` build.
 *
 * Canvas-agnostic in exactly the same way as `robotScene`: it touches no DOM,
 * so it runs against a normal <canvas> on the main thread OR an OffscreenCanvas
 * inside a Web Worker. The caller resolves devicePixelRatio, the graphics tier
 * and `prefers-reduced-motion` (none of which a worker can read) and passes
 * them in; live state, voice amplitude and pointer position are read each frame
 * through the `getState` getter.
 *
 * ── Post-processing ─────────────────────────────────────────────────────────
 * The bloom + chromatic-aberration chain is hand-rolled rather than assembled
 * from EffectComposer, for one reason: the global avatar floats over the page
 * on a transparent canvas, and the stock bloom composite destroys that alpha —
 * you get a black box around the robot. Here the final composite writes
 * `alpha = sceneAlpha + bloomLuma`, so the glow spills *past* the silhouette
 * onto the page instead of dragging a background with it.
 *
 * The whole chain is skipped on the low tier and under reduced motion, in which
 * case the scene renders straight to the canvas.
 */
import * as THREE from 'three'
import type { RobotState } from './robotScene'
import {
  VARIANTS,
  buildEnvScene,
  qualityForTier,
  type VariantBuild,
  type VariantFrame,
  type VariantId,
} from './robotVariants'

export type VariantMode = 'robot' | 'orb'

/** The worker-safe subset of the perf profile this harness needs (plain data). */
export interface VariantGfx {
  antialias: boolean
  /** devicePixelRatio ceiling from the device tier. Also hard-capped at 2. */
  dprCap: number
  fpsTarget: number
  tier: 'low' | 'medium' | 'high' | 'ultra'
}

export interface VariantSceneOptions {
  canvas: HTMLCanvasElement | OffscreenCanvas
  mode: VariantMode
  variant: VariantId
  width: number
  height: number
  /** Resolved on the main thread (window.devicePixelRatio) — workers can't read it. */
  dpr: number
  gfx: VariantGfx
  /** Resolved from prefers-reduced-motion on the main thread. */
  reducedMotion: boolean
  getState: () => {
    state: RobotState
    energy: number
    /** Pointer in NDC, −1..1. */
    px: number
    py: number
  }
}

export interface VariantSceneHandle {
  setSize(width: number, height: number): void
  setVariant(variant: VariantId): void
  setHidden(hidden: boolean): void
  /** Rough draw-call count of the live build, for the perf readout. */
  drawCalls(): number
  dispose(): void
}

/** devicePixelRatio is never allowed above this, whatever the display reports. */
const MAX_DPR = 2

// ── Fullscreen-quad shaders for the post chain ───────────────────────────────
const QUAD_VERT = /* glsl */`
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4( position.xy, 0.0, 1.0 );
}
`

/** Bright-pass: keep only what is above the bloom threshold, alpha-weighted. */
const BRIGHT_FRAG = /* glsl */`
precision mediump float;
varying vec2 vUv;
uniform sampler2D tScene;
uniform float uThreshold;
void main() {
  vec4 c = texture2D( tScene, vUv );
  float l = dot( c.rgb, vec3( 0.2126, 0.7152, 0.0722 ) );
  float k = max( 0.0, l - uThreshold ) / max( 0.0001, l );
  gl_FragColor = vec4( c.rgb * k, 1.0 );
}
`

/** Separable 9-tap gaussian; `uDir` selects the axis. */
const BLUR_FRAG = /* glsl */`
precision mediump float;
varying vec2 vUv;
uniform sampler2D tSrc;
uniform vec2 uDir;
void main() {
  vec4 sum = texture2D( tSrc, vUv ) * 0.227027;
  vec2 o1 = uDir * 1.3846153846;
  vec2 o2 = uDir * 3.2307692308;
  sum += ( texture2D( tSrc, vUv + o1 ) + texture2D( tSrc, vUv - o1 ) ) * 0.3162162162;
  sum += ( texture2D( tSrc, vUv + o2 ) + texture2D( tSrc, vUv - o2 ) ) * 0.0702702703;
  gl_FragColor = sum;
}
`

/**
 * Composite: chromatic aberration on the scene sample (radial, so the centre
 * stays sharp and only the edges split), plus additive bloom. Alpha is carried
 * through from the scene and *raised* by the bloom so glow reads over the page.
 */
const COMPOSITE_FRAG = /* glsl */`
precision mediump float;
varying vec2 vUv;
uniform sampler2D tScene;
uniform sampler2D tBloom;
uniform float uAberration;
uniform float uBloomStrength;
void main() {
  vec2 d = vUv - 0.5;
  float r2 = dot( d, d );
  vec2 off = d * uAberration * ( 0.35 + r2 * 3.0 );

  vec4 cr = texture2D( tScene, vUv + off );
  vec4 cg = texture2D( tScene, vUv );
  vec4 cb = texture2D( tScene, vUv - off );

  vec3 scene = vec3( cr.r, cg.g, cb.b );
  float alpha = max( cg.a, max( cr.a, cb.a ) );

  vec3 bloom = texture2D( tBloom, vUv ).rgb * uBloomStrength;
  float bloomLuma = dot( bloom, vec3( 0.2126, 0.7152, 0.0722 ) );

  // Values leave here premultiplied: the scene target already holds everything
  // composited over a zero background, so the browser's own premultiplied
  // canvas compositing turns the bloom skirt into glow over the page.
  gl_FragColor = vec4( scene + bloom, clamp( alpha + bloomLuma, 0.0, 1.0 ) );
}
`

interface PostChain {
  sceneTarget: THREE.WebGLRenderTarget
  bloomA: THREE.WebGLRenderTarget
  bloomB: THREE.WebGLRenderTarget
  bright: THREE.ShaderMaterial
  blur: THREE.ShaderMaterial
  composite: THREE.ShaderMaterial
  quad: THREE.Mesh
  quadScene: THREE.Scene
  quadCamera: THREE.OrthographicCamera
  dispose(): void
}

function buildPostChain(width: number, height: number): PostChain {
  const rtOpts: THREE.RenderTargetOptions = {
    minFilter: THREE.LinearFilter,
    magFilter: THREE.LinearFilter,
    format: THREE.RGBAFormat,
    type: THREE.HalfFloatType,
    depthBuffer: true,
    stencilBuffer: false,
  }
  const sceneTarget = new THREE.WebGLRenderTarget(width, height, rtOpts)
  const half = { ...rtOpts, depthBuffer: false }
  const bw = Math.max(1, Math.floor(width / 2))
  const bh = Math.max(1, Math.floor(height / 2))
  const bloomA = new THREE.WebGLRenderTarget(bw, bh, half)
  const bloomB = new THREE.WebGLRenderTarget(bw, bh, half)

  const bright = new THREE.ShaderMaterial({
    vertexShader: QUAD_VERT,
    fragmentShader: BRIGHT_FRAG,
    uniforms: { tScene: { value: null }, uThreshold: { value: 0.5 } },
    depthTest: false, depthWrite: false,
  })
  const blur = new THREE.ShaderMaterial({
    vertexShader: QUAD_VERT,
    fragmentShader: BLUR_FRAG,
    uniforms: { tSrc: { value: null }, uDir: { value: new THREE.Vector2() } },
    depthTest: false, depthWrite: false,
  })
  const composite = new THREE.ShaderMaterial({
    vertexShader: QUAD_VERT,
    fragmentShader: COMPOSITE_FRAG,
    uniforms: {
      tScene: { value: null },
      tBloom: { value: null },
      uAberration: { value: 0.003 },
      uBloomStrength: { value: 1 },
    },
    // The composite is the final word on every channel — blending it would
    // multiply the colour by its own alpha and dim the glow.
    blending: THREE.NoBlending,
    depthTest: false, depthWrite: false,
  })

  const quadGeo = new THREE.PlaneGeometry(2, 2)
  const quad = new THREE.Mesh(quadGeo, bright)
  quad.frustumCulled = false
  const quadScene = new THREE.Scene()
  quadScene.add(quad)
  const quadCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1)

  return {
    sceneTarget, bloomA, bloomB, bright, blur, composite, quad, quadScene, quadCamera,
    dispose() {
      sceneTarget.dispose(); bloomA.dispose(); bloomB.dispose()
      bright.dispose(); blur.dispose(); composite.dispose()
      quadGeo.dispose()
    },
  }
}

/**
 * Build a variant scene and start its render loop against `canvas`.
 * Returns a handle for resize / variant swap / dispose, or null if WebGL is
 * unavailable (the caller then falls back to whatever it had before).
 */
export function createVariantScene(opts: VariantSceneOptions): VariantSceneHandle | null {
  const { canvas, mode, width: w0, height: h0, dpr, gfx, reducedMotion, getState } = opts

  let renderer: THREE.WebGLRenderer
  try {
    renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: gfx.antialias && gfx.tier !== 'low',
      alpha: true,
      powerPreference: 'high-performance',
    })
  } catch {
    return null
  }

  const quality = qualityForTier(gfx.tier)
  // Post-processing is the first thing to go on weak hardware, and reduced
  // motion means "no shimmer" as much as "no movement".
  const usePost = gfx.tier !== 'low' && !reducedMotion

  let width = Math.max(1, w0)
  let height = Math.max(1, h0)
  const pixelRatio = Math.min(dpr || 1, gfx.dprCap, MAX_DPR)

  renderer.setPixelRatio(pixelRatio)
  renderer.setSize(width, height, false)
  renderer.setClearColor(0x000000, 0)
  renderer.outputColorSpace = THREE.SRGBColorSpace
  renderer.toneMapping = THREE.ACESFilmicToneMapping
  renderer.toneMappingExposure = 1.15

  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(35, width / height, 0.1, 100)

  // ── Lighting ───────────────────────────────────────────────────────────────
  // The IBL below does most of the work; these are shaping lights on top of it.
  const ambient = new THREE.AmbientLight(0xffffff, 0.35)
  scene.add(ambient)
  const keyLight = new THREE.DirectionalLight(0xffffff, 1.5)
  keyLight.position.set(3, 5, 4)
  scene.add(keyLight)
  const rimLight = new THREE.PointLight(0xffffff, 12, 22, 2)
  rimLight.position.set(-3.5, 2, 2.5)
  scene.add(rimLight)
  const fillLight = new THREE.PointLight(0xffffff, 6, 18, 2)
  fillLight.position.set(2.5, -1.5, 3)
  scene.add(fillLight)

  // ── Environment (PMREM IBL) ────────────────────────────────────────────────
  let envTarget: THREE.WebGLRenderTarget | null = null
  const pmrem = new THREE.PMREMGenerator(renderer)

  // ── Backdrop (orb mode only) ───────────────────────────────────────────────
  // Transmissive glass needs something behind it to refract; on a transparent
  // canvas there is nothing, and the orb reads as flat plastic.
  let backdrop: THREE.Mesh | null = null
  let backdropMat: THREE.ShaderMaterial | null = null
  let backdropGeo: THREE.SphereGeometry | null = null

  let build: VariantBuild | null = null
  let variantId: VariantId = opts.variant
  let post: PostChain | null = usePost ? buildPostChain(
    Math.max(1, Math.floor(width * pixelRatio)),
    Math.max(1, Math.floor(height * pixelRatio)),
  ) : null

  function applyEnvironment(id: VariantId) {
    const env = buildEnvScene(VARIANTS[id].palette)
    const next = pmrem.fromScene(env.scene, 0.04)
    env.dispose()
    envTarget?.dispose()
    envTarget = next
    scene.environment = next.texture
  }

  function buildBackdrop(id: VariantId) {
    if (mode !== 'orb') return
    const pal = VARIANTS[id].palette
    if (!backdrop) {
      backdropGeo = new THREE.SphereGeometry(30, 32, 20)
      backdropMat = new THREE.ShaderMaterial({
        side: THREE.BackSide,
        depthWrite: false,
        uniforms: {
          uTop: { value: new THREE.Color(0x000000) },
          uBottom: { value: new THREE.Color(0x000000) },
        },
        vertexShader: /* glsl */`
          varying vec3 vDir;
          void main() {
            vDir = normalize( position );
            gl_Position = projectionMatrix * modelViewMatrix * vec4( position, 1.0 );
          }
        `,
        fragmentShader: /* glsl */`
          precision mediump float;
          varying vec3 vDir;
          uniform vec3 uTop;
          uniform vec3 uBottom;
          void main() {
            float h = vDir.y * 0.5 + 0.5;
            vec3 c = mix( uBottom, uTop, smoothstep( 0.0, 1.0, h ) );
            gl_FragColor = vec4( c, 1.0 );
          }
        `,
      })
      backdrop = new THREE.Mesh(backdropGeo, backdropMat)
      backdrop.frustumCulled = false
      scene.add(backdrop)
    }
    if (backdropMat) {
      backdropMat.uniforms.uTop.value = new THREE.Color(pal.deep).multiplyScalar(1.4)
      backdropMat.uniforms.uBottom.value = new THREE.Color(0x02030a)
    }
  }

  function mountVariant(id: VariantId) {
    build?.dispose()
    if (build) scene.remove(build.root)
    variantId = id
    const variant = VARIANTS[id]
    build = mode === 'robot' ? variant.buildRobot(quality) : variant.buildOrb(quality)
    scene.add(build.root)

    // Framing.
    camera.fov = build.camera.fov
    camera.position.set(0, build.camera.y, build.camera.z)
    camera.lookAt(0, build.camera.targetY, 0)
    camera.aspect = width / Math.max(1, height)
    camera.updateProjectionMatrix()

    // Tint the shaping lights with the palette so the robot and the orb of one
    // variant are lit by the same fixtures.
    const pal = variant.palette
    rimLight.color.setHex(pal.key)
    fillLight.color.setHex(pal.accent)
    ambient.color.setHex(pal.rim)

    if (post) {
      post.bright.uniforms.uThreshold.value = build.bloom.threshold
      post.composite.uniforms.uAberration.value = build.aberration
      post.composite.uniforms.uBloomStrength.value = build.bloom.strength
    }

    applyEnvironment(id)
    buildBackdrop(id)
  }

  mountVariant(opts.variant)

  // ── Frame loop ─────────────────────────────────────────────────────────────
  let raf = 0
  let hidden = false
  let lastRender = 0
  let disposed = false
  const frameMs = 1000 / Math.max(20, gfx.fpsTarget)
  const clock = new THREE.Clock()

  // Shared blink cadence. Closing is faster than opening, as a real eyelid is,
  // and the gap is randomised so it never reads as a loop.
  let blinkClock = 0
  let nextBlink = 1.8 + Math.random() * 3
  const CLOSE_S = 0.055
  const OPEN_S = 0.11

  const frame: VariantFrame = {
    t: 0, dt: 0, energy: 0, state: 'idle', px: 0, py: 0,
    blink: 0, reduced: reducedMotion, cameraWorld: new THREE.Vector3(),
  }

  const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

  function renderPost(chain: PostChain) {
    const prevAutoClear = renderer.autoClear

    // 1. Scene → offscreen, alpha preserved.
    renderer.setRenderTarget(chain.sceneTarget)
    renderer.clear(true, true, true)
    renderer.render(scene, camera)

    // 2. Bright pass → half-res.
    chain.quad.material = chain.bright
    chain.bright.uniforms.tScene.value = chain.sceneTarget.texture
    renderer.setRenderTarget(chain.bloomA)
    renderer.clear(true, false, false)
    renderer.render(chain.quadScene, chain.quadCamera)

    // 3. Separable blur, two ping-pong passes for a wider skirt.
    chain.quad.material = chain.blur
    const texelX = 1 / chain.bloomA.width
    const texelY = 1 / chain.bloomA.height
    for (let pass = 0; pass < 2; pass++) {
      const spread = 1 + pass * 2

      chain.blur.uniforms.tSrc.value = chain.bloomA.texture
      chain.blur.uniforms.uDir.value.set(texelX * spread, 0)
      renderer.setRenderTarget(chain.bloomB)
      renderer.clear(true, false, false)
      renderer.render(chain.quadScene, chain.quadCamera)

      chain.blur.uniforms.tSrc.value = chain.bloomB.texture
      chain.blur.uniforms.uDir.value.set(0, texelY * spread)
      renderer.setRenderTarget(chain.bloomA)
      renderer.clear(true, false, false)
      renderer.render(chain.quadScene, chain.quadCamera)
    }

    // 4. Composite to the canvas.
    chain.quad.material = chain.composite
    chain.composite.uniforms.tScene.value = chain.sceneTarget.texture
    chain.composite.uniforms.tBloom.value = chain.bloomA.texture
    renderer.setRenderTarget(null)
    renderer.autoClear = true
    renderer.clear(true, true, true)
    renderer.render(chain.quadScene, chain.quadCamera)
    renderer.autoClear = prevAutoClear
  }

  const animate = () => {
    if (disposed) return
    raf = requestAnimationFrame(animate)
    if (hidden) return
    const ts = now()
    if (ts - lastRender < frameMs) return
    lastRender = ts

    const dt = Math.min(0.1, clock.getDelta())
    const t = clock.getElapsedTime()
    const live = getState()

    // Blink cycle.
    if (reducedMotion) {
      frame.blink = 0
    } else {
      blinkClock += dt
      const into = blinkClock - nextBlink
      let closed = 0
      if (into >= 0) {
        closed = into < CLOSE_S
          ? into / CLOSE_S
          : Math.max(0, 1 - (into - CLOSE_S) / OPEN_S)
      }
      if (into > CLOSE_S + OPEN_S) {
        blinkClock = 0
        const idleish = live.state === 'idle' || live.state === 'walking'
        nextBlink = idleish ? 1.6 + Math.random() * 3.4 : 4 + Math.random() * 5
      }
      frame.blink = closed
    }

    frame.t = t
    frame.dt = dt
    frame.state = live.state
    frame.energy = Math.min(1, Math.max(0, live.energy))
    frame.px = live.px
    frame.py = live.py
    frame.cameraWorld.copy(camera.position)

    build?.update(frame)

    if (post) renderPost(post)
    else {
      renderer.setRenderTarget(null)
      renderer.render(scene, camera)
    }
  }

  animate()

  return {
    setSize(nextW: number, nextH: number) {
      width = Math.max(1, nextW)
      height = Math.max(1, nextH)
      renderer.setSize(width, height, false)
      camera.aspect = width / height
      camera.updateProjectionMatrix()
      if (post) {
        const pw = Math.max(1, Math.floor(width * pixelRatio))
        const ph = Math.max(1, Math.floor(height * pixelRatio))
        post.sceneTarget.setSize(pw, ph)
        post.bloomA.setSize(Math.max(1, pw >> 1), Math.max(1, ph >> 1))
        post.bloomB.setSize(Math.max(1, pw >> 1), Math.max(1, ph >> 1))
      }
    },
    setVariant(next: VariantId) {
      if (next === variantId) return
      mountVariant(next)
    },
    setHidden(next: boolean) {
      if (next === hidden) return
      hidden = next
      if (hidden) {
        // Clear to fully transparent so nothing — glow included — is left
        // painted on the canvas while the scene is parked.
        renderer.setRenderTarget(null)
        renderer.clear(true, true, true)
      } else {
        lastRender = 0
      }
    },
    drawCalls() {
      return build?.drawCalls ?? 0
    },
    dispose() {
      disposed = true
      cancelAnimationFrame(raf)
      build?.dispose()
      if (build) scene.remove(build.root)
      build = null
      post?.dispose()
      post = null
      envTarget?.dispose()
      envTarget = null
      pmrem.dispose()
      backdropGeo?.dispose()
      backdropMat?.dispose()
      backdrop = null
      scene.clear()
      // No forceContextLoss here: the page keeps other WebGL surfaces alive and
      // a deliberately lost context still surfaces as a context-loss warning.
      renderer.dispose()
    },
  }
}

/** Map a device perf profile onto the harness's gfx knobs. */
export function gfxFromProfile(p: {
  antialias: boolean
  robotDprCap?: number
  dprCap: number
  fpsTarget: number
  tier: string
}, which: VariantMode): VariantGfx {
  const tier = (['low', 'medium', 'high', 'ultra'] as const).includes(p.tier as never)
    ? (p.tier as VariantGfx['tier'])
    : 'high'
  return {
    antialias: p.antialias,
    dprCap: which === 'robot' ? (p.robotDprCap ?? p.dprCap) : p.dprCap,
    fpsTarget: p.fpsTarget,
    tier,
  }
}

/** True when the user has asked the OS to reduce motion. Main thread only. */
export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}
