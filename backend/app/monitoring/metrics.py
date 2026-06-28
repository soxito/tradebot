"""Prometheus metrics helpers."""
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


REQUEST_COUNT = Counter(
    "tradebot_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "tradebot_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)

SIGNALS_CREATED = Counter(
    "tradebot_signals_created_total",
    "Signals created",
    ["source", "action"],
)

TRADES_EXECUTED = Counter(
    "tradebot_trades_executed_total",
    "Trades executed or planned",
    ["exchange", "side", "mode"],
)

ALERTS_SENT = Counter(
    "tradebot_alerts_sent_total",
    "Alert delivery attempts",
    ["channel", "status"],
)

SCHEDULER_CYCLES = Counter(
    "tradebot_scheduler_cycles_total",
    "Scheduler cycle results",
    ["cycle", "status"],
)

SNIPER_PUMP_INTAKE = Counter(
    "tradebot_sniper_pump_intake_total",
    "Sniper cycle pump-intake totals by bucket",
    ["bucket"],
)

APP_INFO = Gauge(
    "tradebot_app_info",
    "Static app info",
    ["version", "environment"],
)


def normalize_path(path: str) -> str:
    if not path:
        return "unknown"
    if path.startswith("/api/v1/signals/") and path.count("/") > 4:
        return "/api/v1/signals/{signal_id}"
    return path


def record_request(method: str, path: str, status: int, duration_seconds: float) -> None:
    path_label = normalize_path(path)
    REQUEST_COUNT.labels(method=method, path=path_label, status=str(status)).inc()
    REQUEST_LATENCY.labels(method=method, path=path_label).observe(duration_seconds)


def record_signal_created(source: str, action: str) -> None:
    SIGNALS_CREATED.labels(source=source or "unknown", action=(action or "unknown").lower()).inc()


def record_trade_execution(exchange: str, side: str, mode: str) -> None:
    TRADES_EXECUTED.labels(
        exchange=exchange or "unknown",
        side=(side or "unknown").lower(),
        mode=(mode or "unknown").lower(),
    ).inc()


def record_alert(channel: str, status: str) -> None:
    ALERTS_SENT.labels(channel=channel or "unknown", status=status or "unknown").inc()


def record_scheduler_cycle(cycle: str, status: str) -> None:
    SCHEDULER_CYCLES.labels(cycle=cycle or "unknown", status=status or "unknown").inc()


def record_sniper_pump_intake(intake: dict | None) -> None:
    if not intake:
        return

    new_count = len(intake.get("new", [])) if isinstance(intake.get("new"), list) else 0
    existing_count = len(intake.get("existing", [])) if isinstance(intake.get("existing"), list) else 0

    try:
        total_pumped = max(int(intake.get("total_pumped", 0) or 0), 0)
    except (TypeError, ValueError):
        total_pumped = 0

    try:
        filtered_out = max(int(intake.get("filtered_out", 0) or 0), 0)
    except (TypeError, ValueError):
        filtered_out = 0

    if new_count:
        SNIPER_PUMP_INTAKE.labels(bucket="new").inc(new_count)
    if existing_count:
        SNIPER_PUMP_INTAKE.labels(bucket="existing").inc(existing_count)
    if total_pumped:
        SNIPER_PUMP_INTAKE.labels(bucket="total_pumped").inc(total_pumped)
    if filtered_out:
        SNIPER_PUMP_INTAKE.labels(bucket="filtered_out").inc(filtered_out)


def metrics_payload() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
