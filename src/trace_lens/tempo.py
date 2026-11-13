"""Lectura mínima de una traza de Tempo (OTLP JSON): quién llama a quién y dónde nace el error."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SpanLite:
    span_id: str
    parent_id: str | None
    service: str
    name: str
    error: bool
    message: str


def _hex(b64: str | None) -> str | None:
    if not b64:
        return None
    return base64.b64decode(b64).hex()


def parse_trace(body: dict[str, Any]) -> list[SpanLite]:
    trace = body.get("trace", body) if isinstance(body, dict) else {}
    out: list[SpanLite] = []
    for rs in trace.get("resourceSpans") or []:
        attrs = {
            a.get("key"): (a.get("value") or {}) for a in (rs.get("resource") or {}).get("attributes") or []
        }
        service = (attrs.get("service.name") or {}).get("stringValue") or "desconocido"
        for ss in rs.get("scopeSpans") or []:
            for s in ss.get("spans") or []:
                status = s.get("status") or {}
                out.append(
                    SpanLite(
                        span_id=_hex(s.get("spanId")) or "",
                        parent_id=_hex(s.get("parentSpanId")),
                        service=service,
                        name=s.get("name") or "",
                        error=status.get("code") == "STATUS_CODE_ERROR",
                        message=status.get("message") or "",
                    )
                )
    return out


def error_origins(spans: list[SpanLite]) -> list[SpanLite]:
    """Spans con error sin descendientes con error: donde nació (el error sube por la cadena)."""
    children: dict[str, list[SpanLite]] = {}
    for s in spans:
        if s.parent_id and s.parent_id != s.span_id:
            children.setdefault(s.parent_id, []).append(s)
    memo: dict[str, bool] = {}
    visiting: set[str] = set()

    def error_below(span_id: str) -> bool:
        if span_id in memo:
            return memo[span_id]
        if span_id in visiting:
            return False
        visiting.add(span_id)
        result = any(c.error or error_below(c.span_id) for c in children.get(span_id, []))
        visiting.discard(span_id)
        memo[span_id] = result
        return result

    return [s for s in spans if s.error and not error_below(s.span_id)]
