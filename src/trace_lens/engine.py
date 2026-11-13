"""Una corrida: pedir los números a Prometheus, detectar, explicar lo nuevo y guardar.

Prometheus hace las cuentas (sumas de la ventana y de la línea de base); acá solo se comparan.
Tempo se consulta solo para hallazgos de error nuevos (o cada ``reexplain_seconds``), y con pocas
trazas: es para encontrar el origen, no para recorrer todo.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any, Protocol

from trace_lens.config import Settings
from trace_lens.detect import EndpointStats, Finding, change_start, detect
from trace_lens.examine import calls_from_search, examine, examine_queries
from trace_lens.explain import Cause, Deploy, group_causes, match_deploy, summarize
from trace_lens.impact import MIN_AFTER_SECONDS, change_phrase, compare, windows
from trace_lens.store import Store
from trace_lens.tempo import error_origins, parse_trace

log = logging.getLogger("trace_lens.engine")

SERVER = 'span_kind="SPAN_KIND_SERVER"'
#: Requests recibidos y llamadas hechas (dependencias).
KINDS = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CLIENT"'
SPAN_KIND = {"SPAN_KIND_SERVER": "server", "SPAN_KIND_CLIENT": "client"}
_PROM_KIND = {v: k for k, v in SPAN_KIND.items()}
ERROR = 'status_code="STATUS_CODE_ERROR"'
#: 4xx que no son error OTel (en el servidor nunca lo son; en el cliente ya cuentan como error).
HTTP_4XX = 'http_status_code=~"4..", status_code!="STATUS_CODE_ERROR"'
BASE = "24h"


class Prom(Protocol):
    def query(self, query: str) -> list[dict[str, Any]]: ...
    def query_range(self, query: str, start: float, end: float, step: int) -> list[dict[str, Any]]: ...


class Tempo(Protocol):
    def search(self, query: str, start: float, end: float, limit: int) -> list[str]: ...
    def trace(self, trace_id: str) -> dict[str, Any]: ...
    def search_body(self, query: str, start: float, end: float, limit: int) -> dict[str, Any]: ...


DeploySource = Callable[[str, float, float], list[Deploy]]


def label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def stats_queries(window_seconds: int) -> dict[str, str]:
    """Sumas por endpoint (y por dependencia) de la ventana actual, de las 24 h anteriores y de la
    misma ventana ayer."""
    w = f"{window_seconds}s"
    by = "service, span_name, span_kind"
    calls = "traces_spanmetrics_calls_total"
    bucket = "traces_spanmetrics_latency_bucket"
    return {
        "calls": f"sum by ({by}) (increase({calls}{{{KINDS}}}[{w}]))",
        "errors": f"sum by ({by}) (increase({calls}{{{KINDS}, {ERROR}}}[{w}]))",
        "p95": f"histogram_quantile(0.95, sum by ({by}, le) (rate({bucket}{{{KINDS}}}[{w}])))",
        "base_calls": f"sum by ({by}) (increase({calls}{{{KINDS}}}[{BASE}] offset {w}))",
        "base_errors": f"sum by ({by}) (increase({calls}{{{KINDS}, {ERROR}}}[{BASE}] offset {w}))",
        "base_p95": (
            f"histogram_quantile(0.95, sum by ({by}, le) (rate({bucket}{{{KINDS}}}[{BASE}] offset {w})))"
        ),
        "calls_yesterday": f"sum by ({by}) (increase({calls}{{{SERVER}}}[{w}] offset 1d))",
        "http_4xx": f"sum by ({by}) (increase({calls}{{{KINDS}, {HTTP_4XX}}}[{w}]))",
        "base_http_4xx": f"sum by ({by}) (increase({calls}{{{KINDS}, {HTTP_4XX}}}[{BASE}] offset {w}))",
        "codes": (
            f'sum by ({by}, http_status_code) (increase({calls}{{{KINDS}, http_status_code=~"[45].."}}[{w}]))'
        ),
    }


def _num(raw: Any) -> float | None:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


Key = tuple[str, str, str]


def _key(metric: dict[str, str]) -> Key:
    return (
        metric.get("service", ""),
        metric.get("span_name", ""),
        metric.get("span_kind", "SPAN_KIND_SERVER"),
    )


def collect_codes(samples: list[dict[str, Any]]) -> dict[Key, tuple[tuple[str, int], ...]]:
    """Códigos 4xx/5xx por endpoint, de más a menos frecuente (se descartan los que no llegan a 1)."""
    found: dict[Key, dict[str, int]] = {}
    for s in samples:
        n = _num((s.get("value") or [0, None])[1])
        code = s["metric"].get("http_status_code")
        if code and n is not None and round(n) > 0:
            found.setdefault(_key(s["metric"]), {})[code] = round(n)
    return {k: tuple(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))) for k, v in found.items()}


def collect_stats(results: dict[str, list[dict[str, Any]]]) -> list[EndpointStats]:
    tables: dict[str, dict[tuple[str, str, str], float | None]] = {}
    for name, samples in results.items():
        if name == "codes":
            continue
        tables[name] = {
            (
                s["metric"].get("service", ""),
                s["metric"].get("span_name", ""),
                s["metric"].get("span_kind", "SPAN_KIND_SERVER"),
            ): _num((s.get("value") or [0, None])[1])
            for s in samples
        }
    keys = set(tables["calls"]) | set(tables["calls_yesterday"])
    out = []
    for key in sorted(keys):
        if key[2] not in SPAN_KIND:
            continue

        def get(name: str, k: tuple[str, str, str] = key) -> float | None:
            return tables[name].get(k)

        p95, base_p95 = get("p95"), get("base_p95")
        out.append(
            EndpointStats(
                service=key[0],
                endpoint=key[1],
                calls=get("calls") or 0.0,
                errors=get("errors") or 0.0,
                p95_ms=p95 * 1000 if p95 is not None else None,
                base_calls=get("base_calls") or 0.0,
                base_errors=get("base_errors") or 0.0,
                base_p95_ms=base_p95 * 1000 if base_p95 is not None else None,
                calls_yesterday=get("calls_yesterday"),
                span_kind=SPAN_KIND[key[2]],  # type: ignore[arg-type]
                http_4xx=(tables.get("http_4xx") or {}).get(key) or 0.0,
                base_http_4xx=(tables.get("base_http_4xx") or {}).get(key) or 0.0,
            )
        )
    return out


def _series_query(f: Finding, window_seconds: int) -> str:
    kind = "SPAN_KIND_CLIENT" if f.span_kind == "client" else "SPAN_KIND_SERVER"
    sel = f'span_kind="{kind}", service="{label(f.service)}", span_name="{label(f.endpoint)}"'
    calls = "traces_spanmetrics_calls_total"
    if f.kind == "error_rate":
        return f"sum(rate({calls}{{{sel}, {ERROR}}}[5m])) / sum(rate({calls}{{{sel}}}[5m]))"
    if f.kind == "http_4xx":
        return f"sum(rate({calls}{{{sel}, {HTTP_4XX}}}[5m])) / sum(rate({calls}{{{sel}}}[5m]))"
    if f.kind == "latency":
        bucket = "traces_spanmetrics_latency_bucket"
        return f"histogram_quantile(0.95, sum by (le) (rate({bucket}{{{sel}}}[5m]))) * 1000"
    return f"sum(increase({calls}{{{sel}}}[{window_seconds}s]))"


def run_examine(
    tempo: Tempo, service: str, endpoint: str, span_kind: str, start: float, end: float, limit: int
) -> dict[str, Any]:
    """Una muestra de llamadas que fallan y otra de las que andan, y en qué datos se diferencian."""
    queries = examine_queries(service, endpoint, span_kind)
    calls = []
    for name in ("failing", "ok"):
        calls += calls_from_search(tempo.search_body(queries[name], start, end, limit), endpoint)
    return {
        "service": service,
        "endpoint": endpoint,
        "span_kind": span_kind,
        "start": start,
        "end": end,
        **examine(calls),
    }


def _stored_deploy(raw: dict[str, Any] | None) -> Deploy | None:
    """El deploy guardado en la base (JSON: los tickets vuelven como lista)."""
    if not raw:
        return None
    return Deploy(**{**raw, "tickets": tuple(raw.get("tickets") or ())})


class Engine:
    def __init__(
        self,
        store: Store,
        prom: Prom,
        tempo: Tempo,
        deploys: DeploySource,
        settings: Settings,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._prom = prom
        self._tempo = tempo
        self._deploys = deploys
        self._s = settings
        self._now = now

    def run_once(self) -> dict[str, int]:
        now = self._now()
        queries = stats_queries(self._s.window_seconds)
        results = {name: self._prom.query(q) for name, q in queries.items()}
        codes = collect_codes(results.get("codes") or [])
        findings = [
            replace(f, codes=codes.get((f.service, f.endpoint, _PROM_KIND[f.span_kind]), ()))
            for f in detect(collect_stats(results), self._s.thresholds)
        ]

        seen: set[str] = set()
        for f in findings:
            seen.add(f.key)
            self._save(f, now)
        resolved = self._store.resolve_stale(seen, now, self._s.resolve_after_seconds)
        return {
            "findings": len(findings),
            "resolved": resolved,
            "active": self._store.counts().get("active", 0),
        }

    # --- explicar ---------------------------------------------------------------------------------

    def _save(self, f: Finding, now: float) -> None:
        prev = self._store.active(f.key)
        fresh = prev is None or now - (prev.get("explained_at") or 0) >= self._s.reexplain_seconds
        if fresh:
            start = self._start(f, now)
            since = start or now - self._s.window_seconds
            causes = self._causes(f, since, now)
            focus = self._focus(f, since, now)
            deploy = self._deploy(f, start)
            deploy_change = self._deploy_change(f, deploy, start, now)
            explained_at = now
        else:
            start = prev["started_at"]
            causes = [Cause(**c) for c in prev["causes"]]
            focus = prev.get("focus") or []
            deploy = _stored_deploy(prev["deploy"])
            deploy_change = prev.get("deploy_change") or ""
            explained_at = prev["explained_at"]

        self._store.save(
            {
                "key": f.key,
                "kind": f.kind,
                "span_kind": f.span_kind,
                "service": f.service,
                "endpoint": f.endpoint,
                "severity": f.severity,
                "value": f.value,
                "baseline": f.baseline,
                "new": int(f.new),
                "started_at": start,
                "detected_at": now,
                "last_seen": now,
                "explained_at": explained_at,
                "causes": [asdict(c) for c in causes],
                "deploy": asdict(deploy) if deploy else None,
                "codes": dict(f.codes),
                "focus": focus,
                "deploy_change": deploy_change,
                "summary": summarize(f, start, causes, deploy, self._s.tz, focus)
                + (f" Después del deploy: {deploy_change}." if deploy_change else ""),
            }
        )

    def _start(self, f: Finding, now: float) -> float | None:
        try:
            series = self._prom.query_range(
                _series_query(f, self._s.window_seconds), now - self._s.lookback_seconds, now, 60
            )
        except Exception:  # noqa: BLE001 - sin inicio el hallazgo igual sirve
            log.warning("no se pudo buscar el inicio de %s", f.key, exc_info=True)
            return None
        points = [(float(t), _num(v)) for t, v in (series[0]["values"] if series else [])]
        return change_start(points, f.threshold, below=f.kind == "traffic_drop")

    def _causes(self, f: Finding, start: float, now: float) -> list[Cause]:
        if f.kind != "error_rate":
            return []
        query = (
            f'{{resource.service.name="{label(f.service)}" && name="{label(f.endpoint)}" '
            f"&& kind={f.span_kind} && status=error}}"
        )
        try:
            ids = self._tempo.search(query, start, now, self._s.sample_traces)
            origins = []
            for trace_id in ids[: self._s.sample_traces]:
                for o in error_origins(parse_trace(self._tempo.trace(trace_id))):
                    origins.append((o.service, o.name, o.message))
        except Exception:  # noqa: BLE001 - Tempo caído no tapa el hallazgo
            log.warning("no se pudieron leer las trazas de %s", f.key, exc_info=True)
            return []
        return group_causes(origins)

    def _focus(self, f: Finding, start: float, now: float) -> list[dict[str, Any]]:
        """Si las fallas se concentran en un dato (un service_id, una tabla, una URL)."""
        if f.kind not in ("error_rate", "http_4xx"):
            return []
        try:
            result = run_examine(
                self._tempo, f.service, f.endpoint, f.span_kind, start, now, self._s.examine_traces
            )
        except Exception:  # noqa: BLE001
            log.warning("no se pudo examinar %s", f.key, exc_info=True)
            return []
        # Lo justo para la frase y la tarjeta; el detalle completo lo pide el front con /examine.
        keep = ("param", "source", "value", "failing", "ok", "failing_share", "ok_share", "codes")
        return [{k: x[k] for k in keep} for x in result["focus"][:3]]

    def _deploy_change(self, f: Finding, deploy: Deploy | None, start: float | None, now: float) -> str:
        """Qué cambió en este endpoint entre antes y después del deploy que coincide (si lo hay)."""
        if deploy is None or start is None or deploy.at > start:
            return ""
        before, after, _ = windows(deploy.at, 30, now)
        if after.end - after.start < MIN_AFTER_SECONDS:
            return ""
        try:
            rows = compare(self._prom.query, f.service, before, after)
        except Exception:  # noqa: BLE001 - sin la comparación el hallazgo igual sirve
            log.warning("no se pudo comparar el deploy de %s", f.key, exc_info=True)
            return ""
        row = next((r for r in rows if r["endpoint"] == f.endpoint and r["kind"] == f.span_kind), None)
        return change_phrase(row) if row and row["verdicts"] else ""

    def _deploy(self, f: Finding, start: float | None) -> Deploy | None:
        if start is None:
            return None
        try:
            deploys = self._deploys(f.service, start - 3600, start + 600)
        except Exception:  # noqa: BLE001
            log.warning("no se pudieron leer los deploys de %s", f.service, exc_info=True)
            return None
        return match_deploy(deploys, start)
