"""API de lectura de los hallazgos."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query

from trace_lens.store import Store

NAME = r"^[\w .:/{}@*,=+-]{1,200}$"
Service = Annotated[str | None, Query(pattern=NAME)]
#: (servicio, endpoint, span_kind, desde, hasta, trazas por muestra) -> resumen de /examine.
Examiner = Callable[[str, str, str, float, float, int], dict[str, Any]]
#: (servicio, desde, hasta) -> deploys y CPU/memoria de ECS del servicio.
Contexter = Callable[[str, float, float], dict[str, Any]]


def create_app(
    store: Store,
    status: Callable[[], dict[str, Any]] | None = None,
    examiner: Examiner | None = None,
    contexter: Contexter | None = None,
) -> FastAPI:
    app = FastAPI(title="trace-lens", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "counts": store.counts(), **(status() if status else {})}

    @app.get("/insights")
    def insights(
        state: Literal["active", "resolved", "all"] = "active",
        service: Service = None,
        hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        since = time.time() - hours * 3600
        items = store.insights(state=state, service=service, since=since, limit=limit)
        return {"total": len(items), "insights": items}

    @app.get("/examine")
    def examine(
        service: Annotated[str, Query(pattern=NAME)],
        endpoint: Annotated[str, Query(pattern=NAME)],
        kind: Literal["server", "client"] = "server",
        minutes: Annotated[int, Query(ge=5, le=7 * 24 * 60)] = 60,
        limit: Annotated[int, Query(ge=10, le=300)] = 100,
    ) -> dict[str, Any]:
        """Con qué datos reales se llama al endpoint y cuáles se concentran en las fallas."""
        if examiner is None:
            raise HTTPException(503, "Tempo no está configurado")
        end = time.time()
        try:
            return examiner(service, endpoint, kind, end - minutes * 60, end, limit)
        except OSError as exc:
            raise HTTPException(502, f"Tempo no respondió: {type(exc).__name__}") from exc

    @app.get("/context")
    def context(
        service: Annotated[str, Query(pattern=NAME)],
        minutes: Annotated[int, Query(ge=5, le=24 * 60)] = 60,
    ) -> dict[str, Any]:
        """Deploys del servicio y su CPU y memoria de ECS en la ventana, para los gráficos."""
        if contexter is None:
            raise HTTPException(503, "stack-lens no está configurado")
        end = time.time()
        return contexter(service, end - minutes * 60, end)

    return app
