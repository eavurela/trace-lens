"""Examinar un endpoint: con qué valores reales se lo llama y cuáles se concentran en las fallas.

La plantilla (`/pipelines/{service_id}/reviews/{run_id}`) dice qué función es; el path concreto
(`/pipelines/ms-participation/reviews/123/content`) dice con qué dato. Muchas veces la función anda y
falla un dato: acá se separa cada parámetro (del path, del query string y algunos atributos del span)
y se compara una muestra de llamadas que fallan contra una que anda.

Nada de I/O en las funciones de análisis: entra una lista de llamadas, sale el resumen.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

NIL = "nil"
MASK = "•••"
#: Claves de query string cuyos valores no se muestran nunca.
_SENSITIVE = re.compile(r"token|pass|secret|key|auth|sig|session|cred|jwt|code|otp|cookie", re.I)
_JWT = re.compile(r"^eyJ[\w-]+\.[\w-]+\.[\w-]+$")
#: Segmentos que en una URL sin plantilla son un dato (ids, números, hashes).
_ID_SEGMENT = re.compile(
    r"^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?=.*\d)[0-9a-f]{16,}|[0-9a-f]{24})$",
    re.I,
)
#: Atributos de span que también son "el dato" de una llamada que no es HTTP.
ATTRIBUTE_PARAMS = (
    "aws.dynamodb.table_names",
    "db.name",
    "db.mongodb.collection",
    "db.sql.table",
    "rpc.method",
    "server.address",
)
URL_ATTRIBUTES = (
    "http.target",
    "http.url",
    "url.full",
    "url.path",
    "url.query",
    "http.route",
    "http.status_code",
    "http.response.status_code",
)
TOP_VALUES = 10
TOP_PATHS = 15
TRACES_PER_VALUE = 3


@dataclass(frozen=True)
class Call:
    """Un span de la muestra: el endpoint llamado con un dato concreto."""

    trace_id: str
    failing: bool
    path: str | None
    query: str
    route: str | None
    status: int | None
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass
class _Tally:
    failing: int = 0
    ok: int = 0
    codes: Counter[str] = field(default_factory=Counter)
    traces: list[str] = field(default_factory=list)

    def add(self, call: Call) -> None:
        if call.failing:
            self.failing += 1
            if len(self.traces) < TRACES_PER_VALUE and call.trace_id not in self.traces:
                self.traces.append(call.trace_id)
        else:
            self.ok += 1
        if call.status is not None:
            self.codes[str(call.status)] += 1


# --- leer un span ---------------------------------------------------------------------------------


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return None if text in ("", NIL) else text


def _status(raw: str | None) -> int | None:
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def route_from_name(span_name: str) -> str | None:
    """`GET /x/{id}` → `/x/{id}`. Un span que se llama solo `GET` no tiene ruta (FastAPI no encontró una)."""
    parts = span_name.split(" ", 1)
    candidate = parts[1] if len(parts) == 2 else parts[0]
    return candidate if candidate.startswith("/") else None


def call_from_span(attrs: dict[str, str], trace_id: str, span_name: str = "") -> Call:
    """Arma una llamada con los atributos que devuelve un ``select()`` de Tempo."""
    a = {k: _clean(v) for k, v in attrs.items()}
    path, query = None, ""
    target = a.get("http.target")
    full = a.get("url.full") or a.get("http.url")
    if target:
        path, _, query = target.partition("?")
    elif a.get("url.path"):
        path, query = a["url.path"], a.get("url.query") or ""
    elif full:
        parsed = urllib.parse.urlsplit(full)
        path, query = parsed.path or "/", parsed.query
    status = _status(a.get("http.status_code") or a.get("http.response.status_code"))
    error = a.get("status") == "error"
    extra = tuple((k, a[k]) for k in ATTRIBUTE_PARAMS if a.get(k))  # type: ignore[misc]
    return Call(
        trace_id=trace_id,
        failing=error or (status is not None and status >= 400),
        path=path,
        query=query,
        route=a.get("http.route") or route_from_name(span_name),
        status=status,
        attributes=extra,
    )


# --- separar el dato ------------------------------------------------------------------------------


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s != ""]


def route_params(route: str, path: str) -> list[tuple[str, str]] | None:
    """Valores de cada ``{param}`` de la plantilla en el path concreto. None si no coinciden."""
    r, p = _segments(route), _segments(path)
    out: list[tuple[str, str]] = []
    for i, seg in enumerate(r):
        if seg.startswith("{") and seg.endswith("}"):
            name, _, conv = seg[1:-1].partition(":")
            if conv == "path":  # {file:path} se come el resto
                out.append((name, "/".join(p[i:])))
                return out
            if i >= len(p):
                return None
            out.append((name, urllib.parse.unquote(p[i])))
        elif i >= len(p) or p[i] != seg:
            return None
    return out if len(p) == len(r) else None


def templatize(path: str) -> tuple[str, list[tuple[str, str]]]:
    """Para una URL sin plantilla (cliente HTTP, ruta inexistente): los segmentos que parecen ids
    pasan a ser parámetros, nombrados por el segmento anterior (`/users/42` → `users/{id}`)."""
    segs = _segments(path)
    params: list[tuple[str, str]] = []
    out = []
    for i, seg in enumerate(segs):
        if _ID_SEGMENT.match(seg):
            name = f"{segs[i - 1]}/{{id}}" if i > 0 else "{id}"
            params.append((name, seg))
            out.append("{id}")
        else:
            out.append(seg)
    return "/" + "/".join(out), params


def mask(key: str, value: str) -> str:
    if _SENSITIVE.search(key) or _JWT.match(value):
        return MASK
    return value


def masked_path(path: str) -> str:
    return "/".join(MASK if _JWT.match(s) else s for s in path.split("/"))


def params_of(call: Call) -> list[tuple[str, str, str]]:
    """(nombre, origen, valor) de cada dato de la llamada. Origen: path, query o atributo."""
    out: list[tuple[str, str, str]] = []
    if call.path:
        found = route_params(call.route, call.path) if call.route else None
        if found is None:
            found = templatize(call.path)[1]
        out += [(name, "path", mask(name, value)) for name, value in found]
    for key, value in urllib.parse.parse_qsl(call.query, keep_blank_values=True):
        out.append((key, "query", mask(key, value)))
    out += [(key, "atributo", value) for key, value in call.attributes]
    return out


# --- resumir --------------------------------------------------------------------------------------


def _share(n: int, total: int) -> float | None:
    return n / total if total else None


def _value_row(value: str, t: _Tally, n_failing: int, n_ok: int) -> dict[str, Any]:
    return {
        "value": value,
        "failing": t.failing,
        "ok": t.ok,
        "failing_share": _share(t.failing, n_failing),
        "ok_share": _share(t.ok, n_ok),
        "codes": dict(t.codes.most_common()),
        "traces": t.traces,
    }


def is_focus(row: dict[str, Any], min_failing: int = 3) -> bool:
    """Un valor "concentra" las fallas si está en la mitad o más de las que fallan y bastante menos
    en las que andan (o no hay muestra que ande para comparar)."""
    fs, oks = row["failing_share"], row["ok_share"]
    if row["failing"] < min_failing or fs is None or fs < 0.5:
        return False
    return oks is None or fs - oks >= 0.3


def examine(calls: list[Call]) -> dict[str, Any]:
    failing = [c for c in calls if c.failing]
    n_failing, n_ok = len(failing), len(calls) - len(failing)

    routes = Counter(c.route or templatize(c.path)[0] for c in calls if c.route or c.path)
    by_param: dict[tuple[str, str], dict[str, _Tally]] = defaultdict(lambda: defaultdict(_Tally))
    by_path: dict[str, _Tally] = defaultdict(_Tally)
    codes: Counter[str] = Counter()
    for c in calls:
        if c.path:
            by_path[
                masked_path(c.path) + (f"?{c.query}" if c.query and not _has_sensitive(c.query) else "")
            ].add(c)
        if c.status is not None and c.failing:
            codes[str(c.status)] += 1
        seen: set[tuple[str, str, str]] = set()
        for p in params_of(c):
            if p not in seen:  # un valor repetido en la misma llamada cuenta una vez
                seen.add(p)
                by_param[(p[0], p[1])][p[2]].add(c)

    params = []
    focus = []
    for (name, source), values in by_param.items():
        rows = [_value_row(v, t, n_failing, n_ok) for v, t in values.items()]
        rows.sort(key=lambda r: (-r["failing"], -(r["failing"] + r["ok"]), r["value"]))
        for r in rows:
            if is_focus(r):
                focus.append({"param": name, "source": source, **r})
        params.append({"name": name, "source": source, "distinct": len(rows), "values": rows[:TOP_VALUES]})
    params.sort(key=lambda p: (-max((v["failing"] for v in p["values"]), default=0), p["name"]))
    focus.sort(key=lambda f: -(f["failing_share"] or 0))

    paths = [{"path": p, **_value_row(p, t, n_failing, n_ok)} for p, t in by_path.items()]
    paths.sort(key=lambda r: (-r["failing"], -(r["failing"] + r["ok"])))
    for row in paths:
        row.pop("value")

    return {
        "route": routes.most_common(1)[0][0] if routes else None,
        "sampled": {"failing": n_failing, "ok": n_ok},
        "codes": dict(codes.most_common()),
        "focus": focus,
        "params": params,
        "paths": paths[:TOP_PATHS],
    }


def _has_sensitive(query: str) -> bool:
    return any(_SENSITIVE.search(k) for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True))


# --- consultas a Tempo ----------------------------------------------------------------------------


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def examine_queries(service: str, endpoint: str, span_kind: str) -> dict[str, str]:
    """TraceQL de la muestra que falla y de la que anda, con los atributos que hacen falta."""
    base = f'resource.service.name="{_label(service)}" && name="{_label(endpoint)}" && kind={span_kind}'
    select = ", ".join(f"span.{a}" for a in (*URL_ATTRIBUTES, *ATTRIBUTE_PARAMS))
    return {
        "failing": f"{{{base} && (status=error || span.http.status_code>=400)}} | select(status, {select})",
        "ok": (
            f"{{{base} && status!=error && (span.http.status_code<400 || span.http.status_code=nil)}}"
            f" | select(status, {select})"
        ),
    }


def calls_from_search(body: dict[str, Any], span_name: str) -> list[Call]:
    """Las llamadas de una respuesta de búsqueda de Tempo (con ``select``)."""
    out: list[Call] = []
    for t in body.get("traces") or []:
        trace_id = str(t.get("traceID", "")).lower().rjust(32, "0")
        sets = t.get("spanSets") or ([t["spanSet"]] if t.get("spanSet") else [])
        seen: set[str] = set()
        for s in sets:
            for span in s.get("spans") or []:
                sid = span.get("spanID", "")
                if sid in seen:
                    continue
                seen.add(sid)
                attrs = {
                    a.get("key", ""): next(iter((a.get("value") or {}).values()), None)
                    for a in span.get("attributes") or []
                }
                out.append(call_from_span(attrs, trace_id, span_name))
    return out
