"""Punto de entrada: el detector corre cada ``poll_seconds`` en un hilo y la API sirve los hallazgos."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import uvicorn

from trace_lens.api import create_app
from trace_lens.clients import Deploys, PromClient, TempoClient
from trace_lens.config import Settings, settings_from_env
from trace_lens.engine import Engine, run_examine
from trace_lens.store import Store

log = logging.getLogger("trace_lens")
DAY = 86400


class Runner:
    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._s = settings
        self.tempo = TempoClient(settings.tempo_url, settings.tempo_host_header)
        self._engine = Engine(
            store,
            PromClient(settings.prometheus_url),
            self.tempo,
            Deploys(settings.lazy_url, settings.lazy_token, settings.stack_lens_url),
            settings,
        )
        self._state: dict[str, Any] = {"last_run": None, "last_error": None, "last_stats": None}
        self._last_prune = 0.0

    def status(self) -> dict[str, Any]:
        return {
            "detector": dict(self._state),
            "sources": {
                "lazy": bool(self._s.lazy_url and self._s.lazy_token),
                "stack_lens": bool(self._s.stack_lens_url),
            },
        }

    def start(self) -> None:
        threading.Thread(target=self._loop, name="detector", daemon=True).start()

    def _loop(self) -> None:
        while True:
            self._tick()
            time.sleep(self._s.poll_seconds)

    def _tick(self) -> None:
        try:
            stats = self._engine.run_once()
            self._state.update(last_stats=stats, last_error=None)
            if time.time() - self._last_prune > DAY:
                self._store.prune(before=time.time() - self._s.retention_days * DAY)
                self._last_prune = time.time()
        except Exception as exc:  # noqa: BLE001 - el loop no muere; el error queda en /health
            log.exception("falló la detección")
            self._state["last_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._state["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings = settings_from_env()
    store = Store(settings.db_path)
    runner = Runner(store, settings)
    runner.start()
    uvicorn.run(
        create_app(
            store,
            runner.status,
            lambda svc, ep, kind, start, end, limit: run_examine(
                runner.tempo, svc, ep, kind, start, end, limit
            ),
        ),
        host=os.getenv("TRACE_LENS_HOST", "0.0.0.0"),
        port=int(os.getenv("TRACE_LENS_PORT", "7500")),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
