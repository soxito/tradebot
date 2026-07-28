import '@testing-library/jest-dom'
import { vi } from 'vitest'

// Mock Next.js router
vi.mock('next/router', () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    prefetch: vi.fn(),
    pathname: '/',
    query: {},
    asPath: '/',
  }),
}))

// Mock localStorage
const localStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
}
Object.defineProperty(window, 'localStorage', { value: localStorageMock })

// Mock sessionStorage
const sessionStorageMock = {
  getItem: vi.fn(),
  setItem: vi.fn(),
  removeItem: vi.fn(),
  clear: vi.fn(),
}
Object.defineProperty(window, 'sessionStorage', { value: sessionStorageMock })

// Mock matchMedia
Object.defineProperty(window, 'matchMedia', {
  value: vi.fn().mockImplementation(query => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})

// Mock ResizeObserver
global.ResizeObserver = vi.fn().mockImplementation(() => ({
  observe: vi.fn(),
  unobserve: vi.fn(),
  disconnect: vi.fn(),
}))

// Mock SpeechRecognition
const mockSpeechRecognition = vi.fn().mockImplementation(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  abort: vi.fn(),
  onresult: null,
  onerror: null,
  onend: null,
  onstart: null,
  continuous: true,
  interimResults: true,
  lang: 'en-US',
}))
Object.defineProperty(window, 'SpeechRecognition', { value: mockSpeechRecognition })
Object.defineProperty(window, 'webkitSpeechRecognition', { value: mockSpeechRecognition })

// Mock AudioContext.
// configurable/writable so a test needing a *controllable* graph — the voice
// turn machine drives barge-in off an AnalyserNode it reads every frame — can
// substitute its own class via vi.stubGlobal instead of hitting a sealed property.
Object.defineProperty(window, 'AudioContext', {
  configurable: true,
  writable: true,
  value: vi.fn().mockImplementation(() => ({
    createMediaStreamSource: vi.fn(),
    createMediaStreamDestination: vi.fn(),
    createMediaElementSource: vi.fn(),
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) },
    close: vi.fn().mockResolvedValue(undefined),
    sampleRate: 44100,
    state: 'running',
  })),
})

// Mock OffscreenCanvas
// Only the members the code under test touches are stubbed, so the object is
// cast rather than implementing the full OffscreenCanvas surface.
HTMLCanvasElement.prototype.transferControlToOffscreen = vi.fn(() => ({
  getContext: vi.fn(),
  width: 0,
  height: 0,
}) as unknown as OffscreenCanvas)

// Mock THREE
vi.mock('three', () => ({
  WebGLRenderer: vi.fn(() => ({
    setSize: vi.fn(),
    setPixelRatio: vi.fn(),
    shadowMap: { enabled: false, type: null },
    dispose: vi.fn(),
    render: vi.fn(),
  })),
  Scene: vi.fn(() => ({
    add: vi.fn(),
    traverse: vi.fn(),
  })),
  PerspectiveCamera: vi.fn(() => ({
    position: { set: vi.fn() },
    lookAt: vi.fn(),
  })),
  AmbientLight: vi.fn(),
  DirectionalLight: vi.fn(() => ({
    position: { set: vi.fn() },
    castShadow: false,
  })),
  PointLight: vi.fn(() => ({
    position: { set: vi.fn() },
  })),
  MeshStandardMaterial: vi.fn(() => ({
    dispose: vi.fn(),
    clone: vi.fn(() => ({ dispose: vi.fn() })),
  })),
  MeshBasicMaterial: vi.fn(() => ({
    dispose: vi.fn(),
    clone: vi.fn(() => ({ dispose: vi.fn() })),
  })),
  Mesh: vi.fn(() => ({
    position: { set: vi.fn(), x: 0, y: 0, z: 0 },
    rotation: { x: 0, y: 0, z: 0 },
    scale: { set: vi.fn(), setScalar: vi.fn(), x: 1, y: 1, z: 1 },
    geometry: { dispose: vi.fn() },
    material: { dispose: vi.fn() },
    clone: vi.fn(() => ({ position: { set: vi.fn() }, rotation: { x: 0, y: 0, z: 0 }, scale: { set: vi.fn() } })),
  })),
  Group: vi.fn(() => ({
    position: { set: vi.fn(), x: 0, y: 0, z: 0 },
    rotation: { x: 0, y: 0, z: 0 },
    add: vi.fn(),
    children: [],
    clone: vi.fn(() => ({ position: { set: vi.fn() }, rotation: { x: 0, y: 0, z: 0 }, add: vi.fn() })),
  })),
  BoxGeometry: vi.fn(),
  CylinderGeometry: vi.fn(),
  SphereGeometry: vi.fn(),
  TorusGeometry: vi.fn(),
  CircleGeometry: vi.fn(),
  RingGeometry: vi.fn(),
  Clock: vi.fn(() => ({
    getElapsedTime: vi.fn(() => 0),
    getDelta: vi.fn(() => 0.016),
  })),
  MathUtils: {
    lerp: (a: number, b: number, k: number) => a + (b - a) * k,
  },
  PCFSoftShadowMap: 1,
  DoubleSide: 2,
}))

// Mock lucide-react icons
vi.mock('lucide-react', () => {
  const icons = [
    'LayoutDashboard', 'LineChart', 'Layers', 'Radio', 'Brain', 'History', 'Settings',
    'Menu', 'X', 'TrendingUp', 'AlertTriangle', 'Zap', 'Bot', 'BookOpen', 'Skull',
    'Rocket', 'Crosshair', 'CandlestickChart', 'BarChart2', 'Cpu', 'Monitor', 'Rewind',
    'Copy', 'Sparkles', 'MessageCircle', 'MessageSquareText', 'Network', 'Workflow',
    'AudioWaveform', 'Atom', 'Puzzle', 'Telescope', 'Globe', 'FlaskConical', 'Smartphone',
    'ChevronDown', 'TrendingUp', 'TrendingDown', 'Ear', 'Mic', 'MicOff', 'Volume2', 'VolumeX',
    'Send', 'Minimize2', 'Bell', 'Trash2', 'Play', 'Settings', 'ArrowRight', 'ArrowLeft',
    'Home', 'Search', 'Filter', 'MoreHorizontal', 'MoreVertical', 'Check', 'X', 'Plus',
    'Minus', 'Edit', 'Trash', 'Eye', 'EyeOff', 'Download', 'Upload', 'RefreshCw',
    'RotateCcw', 'Save', 'Share', 'Link', 'ExternalLink', 'Mail', 'Phone', 'MapPin',
    'User', 'Users', 'Lock', 'Unlock', 'Key', 'Shield', 'AlertCircle', 'Info',
    'HelpCircle', 'Menu', 'Grid', 'List', 'Sliders', 'ToggleLeft', 'ToggleRight',
    'Sun', 'Moon', 'Cloud', 'CloudRain', 'CloudSnow', 'CloudLightning', 'Wind',
    'Droplet', 'Flame', 'Waves', 'Mountain', 'Tree', 'Flower', 'Heart', 'Star',
    'Award', 'Trophy', 'Medal', 'Crown', 'Gem', 'Diamond', 'Sparkles', 'Zap',
    'Bolt', 'Battery', 'Plug', 'Cpu', 'HardDrive', 'MemoryStick', 'Server', 'Database',
    'Network', 'Wifi', 'Bluetooth', 'Usb', 'Hdmi', 'Headphones', 'Mic', 'Camera',
    'Video', 'Music', 'Film', 'Image', 'File', 'Folder', 'Archive', 'Trash2',
    'Edit', 'Edit2', 'Edit3', 'Type', 'Code', 'Terminal', 'Package', 'Box',
    'Truck', 'Ship', 'Plane', 'Car', 'Bike', 'Bus', 'Train', 'Anchor',
    'Compass', 'Map', 'Navigation', 'Target', 'Crosshair', 'Scope', 'Binoculars',
    'Telescope', 'Microscope', 'FlaskConical', 'TestTube', 'Beaker', 'Pipette',
    'Dna', 'Atom', 'Molecule', 'Cells', 'Bacteria', 'Virus', 'Pill', 'Syringe',
    'Bandage', 'Stethoscope', 'Thermometer', 'HeartPulse', 'Activity', 'Pulse',
    'Monitor', 'Smartphone', 'Tablet', 'Laptop', 'Desktop', 'Server', 'Printer',
    'Keyboard', 'Mouse', 'Monitor', 'Speaker', 'Headphones', 'Camera', 'Video',
    'Music', 'Film', 'Image', 'File', 'Folder', 'Archive', 'Trash2', 'Edit',
    'Edit2', 'Edit3', 'Type', 'Code', 'Terminal', 'Package', 'Box', 'Truck',
    'Ship', 'Plane', 'Car', 'Bike', 'Bus', 'Train', 'Anchor', 'Compass', 'Map',
    'Navigation', 'Target', 'Crosshair', 'Scope', 'Binoculars', 'Telescope',
    'Microscope', 'FlaskConical', 'TestTube', 'Beaker', 'Pipette', 'Dna', 'Atom',
    'Molecule', 'Cells', 'Bacteria', 'Virus', 'Pill', 'Syringe', 'Bandage',
    'Stethoscope', 'Thermometer', 'HeartPulse', 'Activity', 'Pulse'
  ]
  
  const mockIcons: Record<string, React.FC<{ className?: string }>> = {}
  icons.forEach(name => {
    mockIcons[name] = ({ className }) => <div className={className} data-testid={name.toLowerCase()} />
  })
  
  return mockIcons
})