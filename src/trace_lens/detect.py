"""Qué cambió: endpoints con más errores, más latencia o menos tráfico que lo habitual.

Entra lo que ya calculó Prometheus (sumas de la ventana actual y de la línea de base) y sale una
lista de hallazgos. Nada de I/O: se testea con números.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: http_4xx: respuestas 4xx que no son error en OTel (en el servidor un 4xx es culpa del que llama).
Kind = Literal["error_rate", "http_4xx", "latency", "traffic_drop"]
Severity = Literal["alta", "media", "baja"]
#: server = requests que recibe el servicio; client = llamadas que hace (AWS, Mongo, HTTP).
SpanKind = Literal["server", "client"]


@dataclass(frozen=True)
class EndpointStats:
    """Un endpoint (span de servidor) en la ventana actual y en su línea de base."""

    service: str
    endpoint: str
    calls: float
    errors: float
    p95_ms: float | None
    #: Línea de base: las 24 h anteriores a la ventana (sin pisarla).
    base_calls: float
    base_errors: float
    base_p95_ms: float | None
    #: La misma ventana, ayer (para el tráfico, que depende de la hora).
    calls_yesterday: float | None
    span_kind: SpanKind = "server"
    #: Respuestas 4xx que no se marcaron como error (ventana actual y línea de base).
    http_4xx: float = 0.0
    base_http_4xx: float = 0.0


@dataclass(frozen=True)
class Thresholds:
    min_calls: int = 20
    min_errors: int = 5
    #: Error mínimo para hablar de un problema, y cuánto tiene que superar a lo habitual.
    min_error_ratio: float = 0.02
    error_factor: float = 3.0
    error_margin: float = 0.02
    latency_factor: float = 1.5
    latency_margin_ms: float = 100.0
    #: Caída de tráfico: solo si ayer hubo bastante, y si cayó más que esto.
    drop_min_calls: int = 100
    drop_ratio: float = 0.7
    #: 4xx: un endpoint que siempre responde algún 404 no es noticia; uno que empieza a hacerlo, sí.
    min_4xx: int = 10
    min_4xx_ratio: float = 0.05
    http_4xx_factor: float = 3.0
    http_4xx_margin: float = 0.05


@dataclass(frozen=True)
class Finding:
    kind: Kind
    service: str
    endpoint: str
    severity: Severity
    #: Valor actual: fracción de error, p95 en ms, o requests en la ventana.
    value: float
    baseline: float
    #: Errores que no aparecían en la línea de base.
    new: bool
    #: Desde qué valor se considera "alto" (para buscar cuándo empezó).
    threshold: float
    span_kind: SpanKind = "server"
    #: Códigos HTTP 4xx/5xx de la ventana, de más a menos frecuente (los pone el motor).
    codes: tuple[tuple[str, int], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.span_kind}|{self.service}|{self.endpoint}"


def _error_finding(s: EndpointStats, th: Thresholds) -> Finding | None:
    if s.calls < th.min_calls or s.errors < th.min_errors:
        return None
    ratio = s.errors / s.calls
    base = s.base_errors / s.base_calls if s.base_calls > 0 else 0.0
    if ratio < th.min_error_ratio or ratio < base * th.error_factor or ratio - base < th.error_margin:
        return None
    severity: Severity = "alta" if ratio >= 0.2 else "media" if ratio >= 0.05 else "baja"
    return Finding(
        kind="error_rate",
        service=s.service,
        endpoint=s.endpoint,
        severity=severity,
        value=ratio,
        baseline=base,
        new=s.base_errors == 0,
        threshold=max(th.min_error_ratio / 2, base + (ratio - base) / 2),
        span_kind=s.span_kind,
    )


def _http_4xx_finding(s: EndpointStats, th: Thresholds) -> Finding | None:
    if s.calls < th.min_calls or s.http_4xx < th.min_4xx:
        return None
    ratio = s.http_4xx / s.calls
    base = s.base_http_4xx / s.base_calls if s.base_calls > 0 else 0.0
    if ratio < th.min_4xx_ratio or ratio < base * th.http_4xx_factor or ratio - base < th.http_4xx_margin:
        return None
    severity: Severity = "alta" if ratio >= 0.5 else "media" if ratio >= 0.2 else "baja"
    return Finding(
        kind="http_4xx",
        service=s.service,
        endpoint=s.endpoint,
        severity=severity,
        value=ratio,
        baseline=base,
        new=s.base_http_4xx == 0,
        threshold=max(th.min_4xx_ratio / 2, base + (ratio - base) / 2),
        span_kind=s.span_kind,
    )


def _latency_finding(s: EndpointStats, th: Thresholds) -> Finding | None:
    if s.calls < th.min_calls or s.p95_ms is None or not s.base_p95_ms:
        return None
    if s.p95_ms < s.base_p95_ms * th.latency_factor or s.p95_ms - s.base_p95_ms < th.latency_margin_ms:
        return None
    factor = s.p95_ms / s.base_p95_ms
    severity: Severity = "alta" if factor >= 3 else "media" if factor >= 2 else "baja"
    return Finding(
        kind="latency",
        service=s.service,
        endpoint=s.endpoint,
        severity=severity,
        value=s.p95_ms,
        baseline=s.base_p95_ms,
        new=False,
        threshold=s.base_p95_ms + (s.p95_ms - s.base_p95_ms) / 2,
        span_kind=s.span_kind,
    )


def _drop_finding(s: EndpointStats, th: Thresholds) -> Finding | None:
    # Solo requests recibidos: que un servicio llame menos a una dependencia no es un problema en sí.
    if s.span_kind != "server" or s.calls_yesterday is None or s.calls_yesterday < th.drop_min_calls:
        return None
    if s.calls > s.calls_yesterday * (1 - th.drop_ratio):
        return None
    drop = 1 - s.calls / s.calls_yesterday
    return Finding(
        kind="traffic_drop",
        service=s.service,
        endpoint=s.endpoint,
        severity="alta" if drop >= 0.9 else "media",
        value=s.calls,
        baseline=s.calls_yesterday,
        new=False,
        threshold=s.calls_yesterday * (1 - th.drop_ratio),
    )


def detect(endpoints: list[EndpointStats], th: Thresholds) -> list[Finding]:
    found: list[Finding] = []
    for s in endpoints:
        for check in (_error_finding, _http_4xx_finding, _latency_finding, _drop_finding):
            f = check(s, th)
            if f is not None:
                found.append(f)
    return found


def change_start(
    points: list[tuple[float, float | None]], threshold: float, max_gap: int = 2, below: bool = False
) -> float | None:
    """Desde cuándo el valor está alto (o bajo, con ``below``), recorriendo hacia atrás desde el último
    punto. Tolera ``max_gap`` puntos seguidos fuera del umbral (un minuto bueno en medio de un
    incidente no lo corta). None si el último punto ya está normal."""

    def out(v: float) -> bool:
        return v <= threshold if below else v >= threshold

    pts = [(t, v) for t, v in points if v is not None]
    if not pts or not out(pts[-1][1]):
        return None
    start = pts[-1][0]
    misses = 0
    for t, v in reversed(pts):
        if out(v):
            start, misses = t, 0
        else:
            misses += 1
            if misses > max_gap:
                break
    return start
