import { useRouter } from 'next/router';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { useTradeStore } from '@/store/useTradeStore';
import ConnectionStatus from '@/components/ConnectionStatus';
import { useRealtimeNotifications } from '@/services/notifications';

// PAUL JARVIS — lazy-loaded so it never blocks the main bundle, no SSR needed
const PaulChat = dynamic(() => import('@/components/PaulChat'), { ssr: false })
// Extension install prompt — detects the browser & offers the voice extension
const ExtensionInstallPrompt = dynamic(() => import('@/components/ExtensionInstallPrompt'), { ssr: false })
import {
  LayoutDashboard,
  LineChart,
  Layers,
  Radio,
  Brain,
  History,
  Settings,
  Menu,
  X,
  TrendingUp,
  AlertTriangle,
  Zap,
  Bot,
  BookOpen,
  Skull,
  Rocket,
  Crosshair,
  CandlestickChart,
  BarChart2,
  Cpu,
  Monitor,
  Rewind,
  Copy,
  Sparkles,
  MessageCircle,
  MessageSquareText,
  Network,
  Workflow,
  AudioWaveform,
  Atom,
  Puzzle,
  Telescope,
} from 'lucide-react';

const navItems = [
  { href: '/', label: 'Dashboard', icon: LayoutDashboard },
  { href: '/trading', label: 'Trading', icon: LineChart },
  { href: '/futures', label: 'Futures', icon: Layers },
  { href: '/signals', label: 'Signals', icon: Radio },
  { href: '/telegram-signals', label: 'Telegram Signals', icon: MessageSquareText },
  { href: '/trending', label: 'Trending', icon: TrendingUp },
  { href: '/strategies', label: 'Strategies', icon: Zap },
  { href: '/sentiment', label: 'Sentiment', icon: Brain },
  { href: '/market-cap', label: 'Market Cap', icon: BarChart2 },
  { href: '/pump-monitor', label: 'Pump Monitor', icon: Rocket },
  { href: '/rug-pulled', label: 'Rug Pulled', icon: Skull },
  { href: '/sniper-signals', label: 'Sniper Signals', icon: Crosshair },
  { href: '/smart-money-concepts', label: 'Smart Money Concepts', icon: CandlestickChart },
  { href: '/kronos-forecast', label: 'Kronos Forecast', icon: Telescope },
  { href: '/delistings', label: 'Delistings', icon: AlertTriangle },
  { href: '/agents', label: 'AI Agents', icon: Bot },
  { href: '/custom-agents', label: 'Custom Agents', icon: Cpu },
  { href: '/agent-paul', label: 'Agent Paul', icon: Workflow },
  { href: '/intelligence', label: 'Intelligence', icon: Network },
  { href: '/vault', label: 'Vault', icon: BookOpen },
  { href: '/insights', label: 'Insights', icon: BookOpen },
  { href: '/mt5-live', label: 'MT5 Live', icon: Monitor },
  { href: '/mt5-replay', label: 'MT5 Replay', icon: Rewind },
  { href: '/mt5-copy-sim', label: 'MT5 Copy Sim', icon: Copy },
  { href: '/ai-analysis', label: 'AI Analyst', icon: Sparkles },
  { href: '/telegram', label: 'Telegram', icon: MessageCircle },
  { href: '/ai-agents-admin', label: 'AI Profiles', icon: Bot },
  { href: '/history', label: 'Trade History', icon: History },
  { href: '/binary-engine', label: 'Binary Engine', icon: AudioWaveform },
  { href: '/jarvis-room', label: 'JARVIS Room', icon: Atom },
  { href: '/settings', label: 'Settings', icon: Settings },
];

export default function Layout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { sidebarOpen, toggleSidebar } = useTradeStore();

  // Surface realtime SSE events as desktop notifications (no-op unless opted in).
  useRealtimeNotifications();

  return (
    <div className="min-h-screen flex">
      {/* Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex flex-col bg-gray-900/95 border-r border-gray-700/50 backdrop-blur-sm transition-all duration-300 ${
          sidebarOpen ? 'w-56' : 'w-16'
        }`}
      >
        {/* Logo */}
        <div className="flex items-center gap-3 px-4 h-14 border-b border-gray-700/50 shrink-0">
          <TrendingUp className="w-6 h-6 text-tradebot-accent shrink-0" />
          {sidebarOpen && (
            <span className="font-bold text-lg text-white whitespace-nowrap">
              TradeBot
            </span>
          )}
        </div>

        {/* Nav Links */}
        <nav className="flex-1 py-3 overflow-y-auto">
          {navItems.map((item) => {
            const isActive =
              item.href === '/'
                ? router.pathname === '/'
                : router.pathname === item.href || router.pathname.startsWith(`${item.href}/`);
            const Icon = item.icon;

            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center gap-3 mx-2 px-3 py-2.5 rounded-lg text-sm transition-colors ${
                  isActive
                    ? 'bg-tradebot-accent/15 text-tradebot-accent font-semibold'
                    : 'text-gray-400 hover:text-white hover:bg-gray-800/60'
                }`}
                title={sidebarOpen ? undefined : item.label}
              >
                <Icon className="w-5 h-5 shrink-0" />
                {sidebarOpen && <span className="whitespace-nowrap">{item.label}</span>}
              </Link>
            );
          })}

          {/* Extension — opens the JARVIS voice-extension install guide on demand
              (works even after the auto-prompt was dismissed). */}
          <button
            type="button"
            onClick={() => window.dispatchEvent(new CustomEvent('jarvis-open-extension-install'))}
            className="w-[calc(100%-1rem)] flex items-center gap-3 mx-2 px-3 py-2.5 rounded-lg text-sm transition-colors text-gray-400 hover:text-white hover:bg-gray-800/60"
            title={sidebarOpen ? undefined : 'Voice Extension'}
          >
            <Puzzle className="w-5 h-5 shrink-0" />
            {sidebarOpen && <span className="whitespace-nowrap">Voice Extension</span>}
          </button>
        </nav>

        {/* Collapse Button */}
        <button
          onClick={toggleSidebar}
          className="flex items-center justify-center h-12 border-t border-gray-700/50 text-gray-400 hover:text-white transition"
          aria-label={sidebarOpen ? 'Collapse sidebar' : 'Expand sidebar'}
        >
          {sidebarOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
        </button>
      </aside>

      {/* Main Content */}
      <div
        className={`flex-1 flex flex-col transition-all duration-300 ${
          sidebarOpen ? 'ml-56' : 'ml-16'
        }`}
      >
        {/* Top Bar */}
        <header className="sticky top-0 z-30 h-14 flex items-center justify-between px-6 bg-gray-900/80 border-b border-gray-700/50 backdrop-blur-sm">
          <div className="flex items-center gap-4">
            {/* Mobile toggle */}
            <button
              onClick={toggleSidebar}
              className="lg:hidden text-gray-400 hover:text-white"
            >
              <Menu className="w-5 h-5" />
            </button>
            <h1 className="text-sm font-medium text-gray-300 capitalize">
              {navItems.find(
                (n) =>
                  n.href === '/'
                    ? router.pathname === '/'
                    : router.pathname === n.href || router.pathname.startsWith(`${n.href}/`)
              )?.label || 'TradeBot'}
            </h1>
          </div>
          <ConnectionStatus />
        </header>

        {/* Page Content */}
        <main className="flex-1 p-4 md:p-6 overflow-auto">{children}</main>
      </div>

      {/* PAUL JARVIS — global floating assistant (available on every page) */}
      <PaulChat />

      {/* Browser extension install prompt (auto-hides when extension connects) */}
      <ExtensionInstallPrompt />
    </div>
  );
}
