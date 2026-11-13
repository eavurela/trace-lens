"""Antes y después de un deploy: qué cambió en cada endpoint y dependencia, y qué errores son nuevos.

Prometheus hace las sumas de cada ventana (con ``@`` se evalúa en el momento exacto, así sirve también para
deploys pasados); acá solo se comparan. Para no culpar al deploy de lo que pasa todos los días a esa hora, a
cada cambio se le resta el de ayer en las mismas ventanas. Las trazas: una muestra con error de antes y de
después; los orígenes que aparecen solo después son los "errores nuevos" del deploy.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from trace_lens.explain import Cause, Deploy, deploy_label, group_causes
from trace_lens.tempo import error_origins, parse_trace

#: Minutos que se deja asentar el rollout antes de medir el "después".
SETTLE_SECONDS = 300
MIN_AFTER_SECONDS = 120
SAMPLE_TRACES = 10

#: Umbrales: un cambio tiene que ser grande y con volumen para contar.
MIN_ERRORS = 3
MIN_CALLS = 20
ERROR_RATIO_DELTA = 0.01
LATENCY_FACTOR = 1.5
LATENCY_DELTA_MS = 50.0
HTTP_4XX_DELTA = 0.05

BAD = {"new_errors", "more_errors", "slower", "more_4xx", "stopped"}
GOOD = {"fewer_errors", "faster"}


@dataclass(frozen=True)
class Window:
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return max(self.end - self.start, 1.0)


@dataclass(frozen=True)
class Side:
    calls: float = 0.0
    errors: float = 0.0
    http_4xx: float = 0.0
    p95_ms: float | None = None

    @property
    def error_ratio(self) -> float:
        return self.errors / self.calls if self.calls else 0.0

    @property
    def http_4xx_ratio(self) -> float:
        return self.http_4xx / self.calls if self.calls else 0.0


def windows(at: float, minutes: int, now: float) -> tuple[Window, Window, bool]:
    """(antes, después, después incompleto)."""
    span = minutes * 60
    before = Window(at - span, at)
    after_start = at + SETTLE_SECONDS
    after_end = min(after_start + span, now)
    return before, Window(after_start, after_end), after_end < after_start + span


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def window_queries(service: str, w: Window, offset: str = "") -> dict[str, str]:
    """Sumas por endpoint/dependencia del servicio en la ventana (``offset`` = "1d" para ayer)."""
    sel = f'service="{_label(service)}", span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CLIENT"'
    by = "span_name, span_kind"
    rng = f"[{int(w.seconds)}s] @ {int(w.end)}" + (f" offset {offset}" if offset else "")
    calls = "traces_spanmetrics_calls_total"
    bucket = "traces_spanmetrics_latency_bucket"
    err = 'status_code="STATUS_CODE_ERROR"'
    h4xx = 'http_status_code=~"4..", status_code!="STATUS_CODE_ERROR"'
    return {
        "calls": f"sum by ({by}) (increase({calls}{{{sel}}}{rng}))",
        "errors": f"sum by ({by}) (increase({calls}{{{sel}, {err}}}{rng}))",
        "http_4xx": f"sum by ({by}) (increase({calls}{{{sel}, {h4xx}}}{rng}))",
        "p95": f"histogram_quantile(0.95, sum by ({by}, le) (rate({bucket}{{{sel}}}{rng})))",
    }


def _sides(query: Callable[[str], list[dict[str, Any]]], queries: dict[str, str]) -> dict[tuple[str, str], Side]:
    tables: dict[str, dict[tuple[str, str], float]] = {}
    for name, q in queries.items():
        rows = {}
        for s in query(q):
            try:
                value = float(s["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if value == value:  # NaN de histogram_quantile sin datos
                rows[(s["metric"].get("span_name", ""), s["metric"].get("span_kind", ""))] = value
        tables[name] = rows
    keys = set(tables["calls"]) | set(tables["errors"])
    return {
        k: Side(
            calls=tables["calls"].get(k, 0.0),
            errors=tables["errors"].get(k, 0.0),
            http_4xx=tables["http_4xx"].get(k, 0.0),
            p95_ms=tables["p95"][k] * 1000 if k in tables["p95"] else None,
        )
        for k in keys
    }


def judge(before: Side, after: Side, yesterday_before: Side, yesterday_after: Side, scale: float) -> list[str]:
    """Qué cambió. ``scale`` lleva las cuentas de "antes" a la duración de "después" (si está incompleto)."""
    found = []
    usual = yesterday_after.error_ratio - yesterday_before.error_ratio
    delta = after.error_ratio - before.error_ratio
    if after.errors >= MIN_ERRORS and before.errors == 0 and yesterday_after.errors == 0:
        found.append("new_errors")
    elif (
        after.errors >= MIN_ERRORS
        and delta >= ERROR_RATIO_DELTA
        and after.error_ratio >= 2 * before.error_ratio
        and delta - max(usual, 0) >= ERROR_RATIO_DELTA
    ):
        found.append("more_errors")
    elif (
        before.errors >= MIN_ERRORS
        and before.error_ratio >= ERROR_RATIO_DELTA
        and after.error_ratio <= before.error_ratio / 2
    ):
        found.append("fewer_errors")
    if after.calls >= MIN_CALLS and before.calls >= MIN_CALLS and before.p95_ms and after.p95_ms:
        if after.p95_ms >= before.p95_ms * LATENCY_FACTOR and after.p95_ms - before.p95_ms >= LATENCY_DELTA_MS:
            found.append("slower")
        elif before.p95_ms >= after.p95_ms * LATENCY_FACTOR and before.p95_ms - after.p95_ms >= LATENCY_DELTA_MS:
            found.append("faster")
    d4 = after.http_4xx_ratio - before.http_4xx_ratio
    usual4 = yesterday_after.http_4xx_ratio - yesterday_before.http_4xx_ratio
    doubled_4xx = after.http_4xx_ratio >= 2 * before.http_4xx_ratio
    if after.http_4xx >= MIN_ERRORS and d4 - max(usual4, 0) >= HTTP_4XX_DELTA and doubled_4xx:
        found.append("more_4xx")
    if before.calls * scale >= MIN_CALLS and after.calls == 0 and yesterday_after.calls > 0:
        found.append("stopped")
    return found


def _side_dict(s: Side, seconds: float) -> dict[str, Any]:
    return {
        "calls": round(s.calls),
        "rate": round(s.calls / seconds, 3),
        "errors": round(s.errors),
        "error_ratio": round(s.error_ratio, 4),
        "http_4xx_ratio": round(s.http_4xx_ratio, 4),
        "p95_ms": round(s.p95_ms, 1) if s.p95_ms is not None else None,
    }


def compare(
    query: Callable[[str], list[dict[str, Any]]], service: str, before: Window, after: Window
) -> list[dict[str, Any]]:
    b = _sides(query, window_queries(service, before))
    a = _sides(query, window_queries(service, after))
    yb = _sides(query, window_queries(service, before, "1d"))
    ya = _sides(query, window_queries(service, after, "1d"))
    scale = after.seconds / before.seconds
    rows = []
    for key in sorted(set(b) | set(a)):
        name, kind = key
        if kind not in ("SPAN_KIND_SERVER", "SPAN_KIND_CLIENT"):
            continue
        sb, sa = b.get(key, Side()), a.get(key, Side())
        verdicts = judge(sb, sa, yb.get(key, Side()), ya.get(key, Side()), scale)
        rows.append({
            "endpoint": name,
            "kind": "server" if kind == "SPAN_KIND_SERVER" else "client",
            "before": _side_dict(sb, before.seconds),
            "after": _side_dict(sa, after.seconds),
            "yesterday_error_ratio": [
                round(yb.get(key, Side()).error_ratio, 4),
                round(ya.get(key, Side()).error_ratio, 4),
            ],
            "verdicts": verdicts,
        })
    rank = {"new_errors": 0, "more_errors": 1, "stopped": 2, "slower": 3, "more_4xx": 4}
    rows.sort(key=lambda r: (min((rank.get(v, 9) for v in r["verdicts"]), default=10), -r["after"]["errors"]))
    return rows


def error_causes(tempo: Any, service: str, w: Window, limit: int = SAMPLE_TRACES) -> list[Cause]:
    query = f'{{resource.service.name="{_label(service)}" && status=error}}'
    origins = []
    for trace_id in tempo.search(query, w.start, w.end, limit)[:limit]:
        for o in error_origins(parse_trace(tempo.trace(trace_id))):
            origins.append((o.service, o.name, o.message))
    return group_causes(origins)


def new_causes(before: list[Cause], after: list[Cause]) -> list[Cause]:
    """Orígenes de error que aparecen después y no antes (mismo servicio, operación y mensaje)."""
    seen = {(c.service, c.operation, c.message) for c in before}
    return [c for c in after if (c.service, c.operation, c.message) not in seen]


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".", ",")


def change_phrase(row: dict[str, Any]) -> str:
    """`errores 0,1% → 4,5%; p95 120 → 900 ms` con lo que haya cambiado."""
    b, a = row["before"], row["after"]
    parts = []
    if {"new_errors", "more_errors", "fewer_errors"} & set(row["verdicts"]):
        parts.append(f"errores {_pct(b['error_ratio'])} → {_pct(a['error_ratio'])}")
    if {"slower", "faster"} & set(row["verdicts"]):
        parts.append(f"p95 {round(b['p95_ms'] or 0)} → {round(a['p95_ms'] or 0)} ms")
    if "more_4xx" in row["verdicts"]:
        parts.append(f"4xx {_pct(b['http_4xx_ratio'])} → {_pct(a['http_4xx_ratio'])}")
    if "stopped" in row["verdicts"]:
        parts.append(f"dejó de recibir tráfico ({b['calls']} llamadas antes)")
    return "; ".join(parts)


def summary(deploy: Deploy | None, rows: list[dict[str, Any]], fresh: list[Cause], partial: bool) -> tuple[str, str]:
    bad = [r for r in rows if BAD & set(r["verdicts"])]
    good = [r for r in rows if GOOD & set(r["verdicts"]) and not BAD & set(r["verdicts"])]
    verdict = "worse" if bad or fresh else "better" if good else "same"
    who = f"Después del deploy ({deploy_label(deploy)})" if deploy else "Después del deploy"
    if verdict == "same":
        text = f"{who} no cambió nada que se note en errores, latencia ni tráfico."
    else:
        items = [f"{r['endpoint']}: {change_phrase(r)}" for r in (bad or good)[:3]]
        text = f"{who} {'empeoró' if verdict == 'worse' else 'mejoró'}: " + " · ".join(items) if items else who
        if fresh:
            c = fresh[0]
            text += f". Error nuevo en {c.operation} ({c.service})" + (f": {c.message}" if c.message else "")
        text += "."
    if partial:
        text += " Todavía no pasó la ventana entera después del deploy: los números pueden cambiar."
    return verdict, text


def deploy_impact(
    prom_query: Callable[[str], list[dict[str, Any]]],
    tempo: Any,
    service: str,
    at: float,
    deploy: Deploy | None,
    minutes: int = 30,
    now: float | None = None,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    before, after, partial = windows(at, minutes, now)
    if after.end - after.start < MIN_AFTER_SECONDS:
        return {"service": service, "at": at, "deploy": asdict(deploy) if deploy else None, "ready": False,
                "summary": "El deploy es muy reciente: todavía no hay suficiente 'después' para comparar."}
    rows = compare(prom_query, service, before, after)
    try:
        causes_before = error_causes(tempo, service, before)
        causes_after = error_causes(tempo, service, after)
    except Exception:  # noqa: BLE001 - sin Tempo igual sirve la comparación de métricas
        causes_before, causes_after = [], []
    fresh = new_causes(causes_before, causes_after)
    verdict, text = summary(deploy, rows, fresh, partial)
    return {
        "service": service,
        "at": at,
        "deploy": {**asdict(deploy), "tickets": list(deploy.tickets)} if deploy else None,
        "ready": True,
        "before": [before.start, before.end],
        "after": [after.start, after.end],
        "partial": partial,
        "verdict": verdict,
        "summary": text,
        "changes": [r for r in rows if r["verdicts"]],
        "unchanged": len([r for r in rows if not r["verdicts"]]),
        "new_errors": [asdict(c) for c in fresh],
        "errors_after": [asdict(c) for c in causes_after],
    }
