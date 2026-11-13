"""Configuración por variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass

from trace_lens.detect import Thresholds


@dataclass(frozen=True)
class Settings:
    prometheus_url: str
    tempo_url: str
    #: Host para Tempo detrás de un ALB que enruta por nombre (si se le pega por IP).
    tempo_host_header: str = ""
    stack_lens_url: str = ""
    lazy_url: str = ""
    lazy_token: str = ""
    db_path: str = "/data/trace-lens.db"
    tz: str = "America/Argentina/Buenos_Aires"
    poll_seconds: int = 120
    window_seconds: int = 600
    #: Cuánto hacia atrás se busca el inicio de un cambio.
    lookback_seconds: int = 3 * 3600
    #: Cada cuánto se vuelven a buscar causas y deploys de un hallazgo abierto.
    reexplain_seconds: int = 900
    #: Trazas con error que se leen para encontrar las causas.
    sample_traces: int = 10
    #: Trazas por muestra (las que fallan y las que andan) para ver en qué dato se concentran las fallas.
    examine_traces: int = 50
    #: Un hallazgo se cierra si no aparece durante este tiempo.
    resolve_after_seconds: int = 300
    retention_days: int = 30
    thresholds: Thresholds = Thresholds()


def settings_from_env() -> Settings:
    def env(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    prometheus = env("PROMETHEUS_URL")
    tempo = env("TEMPO_URL")
    if not prometheus or not tempo:
        raise SystemExit("PROMETHEUS_URL y TEMPO_URL son obligatorias")
    return Settings(
        prometheus_url=prometheus.rstrip("/"),
        tempo_url=tempo.rstrip("/"),
        tempo_host_header=env("TEMPO_HOST_HEADER"),
        stack_lens_url=env("STACK_LENS_URL").rstrip("/"),
        lazy_url=env("LAZY_URL").rstrip("/"),
        lazy_token=env("LAZY_TOKEN"),
        db_path=env("TRACE_LENS_DB", "/data/trace-lens.db"),
        tz=env("TZ_NAME", "America/Argentina/Buenos_Aires"),
        poll_seconds=int(env("POLL_SECONDS", "120")),
        window_seconds=int(env("WINDOW_SECONDS", "600")),
    )
