/**
 * Connection Status Indicator
 * Compact indicator for the top bar
 */
import { useConnectionTest } from '../hooks/useConnectionTest';
import { useStreamState } from '../hooks/useEventStream';

export default function ConnectionStatus() {
  const { isConnected, isLoading, error, retry } = useConnectionTest(true);
  const streamState = useStreamState();

  const statusLabel = isLoading ? 'Connecting...' : isConnected ? 'Connected' : 'Disconnected';

  // Small realtime-stream badge: green "Live" when SSE is connected, amber when
  // reconnecting, gray "Poll" fallback otherwise.
  const streamBadge = (() => {
    if (streamState === 'live') {
      return (
        <span className="flex items-center gap-1 text-emerald-400" title="Realtime stream connected">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
          Live
        </span>
      );
    }
    if (streamState === 'reconnecting' || streamState === 'connecting') {
      return (
        <span className="flex items-center gap-1 text-yellow-400" title="Realtime stream reconnecting">
          <span className="h-1.5 w-1.5 rounded-full bg-yellow-500 animate-pulse" />
          Live
        </span>
      );
    }
    return (
      <span className="flex items-center gap-1 text-gray-500" title="Realtime stream offline — polling">
        <span className="h-1.5 w-1.5 rounded-full bg-gray-500" />
        Poll
      </span>
    );
  })();

  if (!isConnected && !isLoading) {
    return (
      <button
        onClick={retry}
        title={error || 'API connection unavailable'}
        className="flex items-center gap-2 px-2.5 py-1 rounded bg-red-500/20 border border-red-500/40 text-red-400 text-xs hover:bg-red-500/30 transition"
      >
        <div className="h-2 w-2 rounded-full bg-red-500 animate-pulse" />
        <span>{statusLabel}</span>
      </button>
    );
  }

  if (isLoading) {
    return (
      <button
        onClick={retry}
        title={error || 'Checking API connection'}
        className="flex items-center gap-2 px-2.5 py-1 rounded bg-yellow-500/20 border border-yellow-500/40 text-yellow-400 text-xs hover:bg-yellow-500/30 transition"
      >
        <div className="animate-spin rounded-full h-3 w-3 border-2 border-yellow-400 border-t-transparent" />
        <span>{statusLabel}</span>
      </button>
    );
  }

  return (
    <div className="flex items-center gap-2 text-green-400 text-xs" title={error || 'API connected'}>
      <div className="h-2 w-2 rounded-full bg-green-500" />
      <span>{statusLabel}</span>
      <span className="text-gray-600">·</span>
      {streamBadge}
    </div>
  );
}
