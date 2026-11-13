"""Contexto de un servicio para los gráficos: sus deploys y la CPU y memoria de ECS en la misma ventana.

trace-lens no recalcula nada: los deploys salen de las fuentes que ya usa (Lazy, stack-lens /changes y
/ecs/{svc}/deploys) y la CPU y memoria de stack-lens /ecs/{svc}/metrics (CloudWatch). Si el servicio no es
ECS, ``ecs`` viene vacío con el motivo.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from trace_lens.clients import get_json
from trace_lens.explain import Deploy

log = logging.getLogger("trace_lens.context")
#: Ventana máxima que acepta stack-lens para las series y los deploys de ECS.
MAX_SECONDS = 24 * 3600

DeploySource = Callable[[str, float, float], list[Deploy]]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ecs_series(stack_lens_url: str, service: str, start: float, end: float) -> dict[str, Any]:
    """CPU y memoria (% de lo reservado) como [[epoch, valor], ...], o el motivo de que no haya."""
    if not stack_lens_url:
        return {"available": False, "reason": "stack-lens no está configurado"}
    try:
        body = get_json(
            f"{stack_lens_url}/ecs/{urllib.parse.quote(service, safe='')}/metrics",
            {"start": iso(start), "end": iso(end), "series": "true"},
            timeout=60,
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"available": False, "reason": "no es un servicio ECS"}
        return {"available": False, "reason": f"stack-lens respondió {exc.code}"}
    except OSError as exc:
        log.warning("stack-lens no respondió las métricas de %s: %s", service, exc)
        return {"available": False, "reason": "stack-lens no respondió"}
    return {
        "available": True,
        "cluster": body.get("cluster"),
        "period_seconds": body.get("period_seconds"),
        "cpu": (body.get("CPUUtilization") or {}).get("series") or [],
        "memory": (body.get("MemoryUtilization") or {}).get("series") or [],
    }


def service_context(
    deploys: DeploySource, stack_lens_url: str, service: str, start: float, end: float
) -> dict[str, Any]:
    start = max(start, end - MAX_SECONDS)
    found = sorted(deploys(service, start, end), key=lambda d: d.at)
    return {
        "service": service,
        "from": iso(start),
        "to": iso(end),
        "deploys": [{**asdict(d), "tickets": list(d.tickets)} for d in found],
        "ecs": ecs_series(stack_lens_url, service, start, end),
    }
