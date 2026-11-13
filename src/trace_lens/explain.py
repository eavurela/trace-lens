"""Por qué: de dónde salen los errores, qué deploy coincide, y la frase que lo resume."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from trace_lens.detect import Finding


@dataclass(frozen=True)
class Cause:
    """Un origen de error (el span más profundo con error de cada traza), agrupado."""

    service: str
    operation: str
    message: str
    count: int
    share: float


@dataclass(frozen=True)
class Deploy:
    at: float
    source: str
    service: str
    detail: str


_QUOTED = re.compile(r"""'[^']*'|"[^"]*\"""")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{12,}\b", re.I)
_NUM = re.compile(r"\b\d+(?:\.\d+)?\b")
_SPACES = re.compile(r"\s+")
MAX_MESSAGE = 160


def normalize_message(message: str) -> str:
    """Saca lo que cambia entre ocurrencias del mismo error (valores, ids, IPs, números)."""
    text = _QUOTED.sub("<v>", message)
    text = _UUID.sub("<id>", text)
    text = _IP.sub("<ip>", text)
    text = _HEX.sub("<id>", text)
    text = _NUM.sub("<n>", text)
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def group_causes(origins: list[tuple[str, str, str]], top: int = 3) -> list[Cause]:
    """``origins``: (servicio, operación, mensaje) del origen del error de cada traza."""
    if not origins:
        return []
    counts = Counter((svc, op, normalize_message(msg)) for svc, op, msg in origins)
    total = len(origins)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][1]))
    return [Cause(svc, op, msg, n, n / total) for (svc, op, msg), n in ranked[:top]]


def match_deploy(
    deploys: list[Deploy], start: float, before: float = 1800, after: float = 300
) -> Deploy | None:
    """El deploy que más probablemente lo causó: el último antes del inicio (hasta 30 min), o uno que
    estaba en curso (hasta 5 min después: el rollout tarda)."""
    window = [d for d in deploys if start - before <= d.at <= start + after]
    prior = [d for d in window if d.at <= start]
    if prior:
        return max(prior, key=lambda d: d.at)
    return min(window, key=lambda d: d.at) if window else None


def _pct(ratio: float) -> str:
    pct = ratio * 100
    if pct >= 10:
        text = f"{round(pct)}"
    elif pct >= 1:
        text = f"{pct:.1f}".rstrip("0").rstrip(".")
    else:
        text = f"{pct:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"


def _ms(value: float) -> str:
    if value < 1000:
        return f"{round(value)} ms"
    return f"{value / 1000:.1f}".replace(".", ",") + " s"


def _clock(ts: float, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M")


#: Qué suele querer decir cada código, sin jerga.
CODE_MEANING = {
    "400": "pedido mal armado",
    "401": "sin credenciales o con el token vencido",
    "403": "sin permiso",
    "404": "ruta o recurso que no existe",
    "405": "método que la ruta no acepta",
    "409": "conflicto con el estado actual",
    "422": "datos que no pasan la validación",
    "429": "límite de requests",
}


def code_phrase(codes: tuple[tuple[str, int], ...] | list[tuple[str, int]], only_4xx: bool = True) -> str:
    """`Predomina el 404 (80%): ruta o recurso que no existe.` Vacío si no hay un código que domine."""
    pairs = [(c, n) for c, n in codes if not only_4xx or c.startswith("4")]
    total = sum(n for _, n in pairs)
    if not pairs or total <= 0:
        return ""
    code, n = max(pairs, key=lambda p: p[1])
    if n / total < 0.5:
        return ""
    meaning = CODE_MEANING.get(code, "error del servidor" if code.startswith("5") else "")
    head = f"Todos son {code}" if n == total else f"Predomina el {code} ({_pct(n / total)})"
    return f" {head}: {meaning}." if meaning else f" {head}."


def focus_phrase(focus: list[dict[str, Any]] | None) -> str:
    """`Se concentra en service_id=ms-a: 100% de los que fallan y 3% de los que andan.`"""
    if not focus:
        return ""
    f = focus[0]
    text = f" Se concentra en {f['param']}={f['value']}: {_pct(f['failing_share'])} de los que fallan"
    if f.get("ok_share") is not None:
        text += f" y {_pct(f['ok_share'])} de los que andan"
    return text + "."


def summarize(
    f: Finding,
    start: float | None,
    causes: list[Cause],
    deploy: Deploy | None,
    tz: str,
    focus: list[dict[str, Any]] | None = None,
) -> str:
    """La frase del hallazgo, en español y sin jerga: qué, desde cuándo, contra qué, y por qué."""
    dependency = f.span_kind == "client"
    if dependency:
        who = f"Las llamadas de {f.service} a {f.endpoint}"
    elif "/" not in f.endpoint:
        # FastAPI y otros nombran solo con el método los requests a rutas que no tienen.
        who = f"Los {f.endpoint} a rutas que {f.service} no tiene"
    else:
        who = f"{f.endpoint} ({f.service})"
    plural = dependency or "/" not in f.endpoint
    since = f" desde las {_clock(start, tz)}" if start is not None else ""

    if f.kind == "error_rate":
        verb = "fallan en el" if plural else "falla en el"
        what = "" if plural else " de los requests"
        text = f"{who} {verb} {_pct(f.value)}{what}{since}"
        text += "." if f.new else f" (lo habitual es {_pct(f.baseline)})."
        if f.new:
            text += " Son errores nuevos: no había en las últimas 24 h."
        text += code_phrase(f.codes)
    elif f.kind == "http_4xx":
        verb = "reciben 4xx en el" if plural else "responde 4xx al"
        what = "" if plural else " de los requests"
        text = f"{who} {verb} {_pct(f.value)}{what}{since}"
        text += "." if f.new else f" (lo habitual es {_pct(f.baseline)})."
        text += code_phrase(f.codes)
    elif f.kind == "latency":
        verb = "tardan" if dependency else "tarda"
        text = f"{who} {verb} {_ms(f.value)} en p95{since} (lo habitual es {_ms(f.baseline)})."
    else:
        drop = 1 - f.value / f.baseline if f.baseline else 1.0
        text = (
            f"{who} recibe {_pct(drop)} menos requests que ayer a esta hora "
            f"({round(f.value)} contra {round(f.baseline)})."
        )

    text += focus_phrase(focus)
    if causes:
        c = causes[0]
        where = c.operation if c.service == f.service else f"{c.operation} ({c.service})"
        text += f" El {round(c.share * 100)}% de los errores nace en {where}"
        text += f": {c.message}." if c.message else "."
    if deploy is not None and start is not None:
        minutes = round(abs(start - deploy.at) / 60)
        what = f"({deploy.source}: {deploy.detail})"
        if deploy.at <= start:
            text += f" Empezó {minutes} min después de un deploy {what}."
        else:
            text += f" Hubo un deploy {minutes} min después de que empezara {what}."
    return text
