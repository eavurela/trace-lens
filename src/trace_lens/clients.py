"""Clientes HTTP (solo stdlib): Prometheus, Tempo y las fuentes de deploys (Lazy y stack-lens)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from trace_lens.explain import Deploy

log = logging.getLogger("trace_lens.clients")
USER_AGENT = "trace-lens/0.1"


def get_json(
    url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 30
) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - URLs de la config
        return json.loads(resp.read().decode("utf-8"))


class PromClient:
    def __init__(self, base_url: str) -> None:
        self._base = base_url

    def query(self, query: str) -> list[dict[str, Any]]:
        body = get_json(f"{self._base}/api/v1/query", {"query": query})
        return body.get("data", {}).get("result", [])

    def query_range(self, query: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        body = get_json(
            f"{self._base}/api/v1/query_range",
            {"query": query, "start": int(start), "end": int(end), "step": step},
        )
        return body.get("data", {}).get("result", [])


class TempoClient:
    def __init__(self, base_url: str, host_header: str = "") -> None:
        self._base = base_url
        self._headers = {"Host": host_header} if host_header else {}

    def search(self, query: str, start: float, end: float, limit: int) -> list[str]:
        body = get_json(
            f"{self._base}/api/search",
            {"q": query, "start": int(start), "end": int(end), "limit": limit},
            self._headers,
        )
        return [t["traceID"].lower().rjust(32, "0") for t in body.get("traces") or []]

    def search_body(self, query: str, start: float, end: float, limit: int) -> dict[str, Any]:
        """La respuesta entera de la búsqueda (con los atributos de ``select``), hasta 5 spans por traza."""
        return get_json(
            f"{self._base}/api/search",
            {"q": query, "start": int(start), "end": int(end), "limit": limit, "spss": 5},
            self._headers,
        )

    def trace(self, trace_id: str) -> dict[str, Any]:
        return get_json(f"{self._base}/api/v2/traces/{trace_id}", headers=self._headers)


def _ts(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Deploys:
    """Deploys de un servicio en una ventana: corridas de Lazy y builds/deploys de stack-lens
    (Jenkins, CodeBuild, CodeDeploy). Cada fuente es opcional; si falla, se sigue con las otras."""

    def __init__(
        self, lazy_url: str = "", lazy_token: str = "", stack_lens_url: str = "", lazy_env: str = "prod"
    ):
        self._lazy_url = lazy_url
        self._lazy_token = lazy_token
        self._stack_lens = stack_lens_url
        self._lazy_env = lazy_env

    def __call__(self, service: str, since: float, until: float) -> list[Deploy]:
        found: list[Deploy] = []
        if self._lazy_url and self._lazy_token:
            try:
                found += self._lazy(service)
            except Exception:  # noqa: BLE001
                log.warning("Lazy no respondió para %s", service, exc_info=True)
        if self._stack_lens:
            try:
                found += self._changes(service)
            except Exception:  # noqa: BLE001
                log.warning("stack-lens no respondió para %s", service, exc_info=True)
            try:
                found += self._ecs(service, since, until)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:  # 404: no es un servicio ECS
                    log.warning("stack-lens no respondió los deploys ECS de %s: %s", service, exc)
            except Exception:  # noqa: BLE001
                log.warning("stack-lens no respondió los deploys ECS de %s", service, exc_info=True)
        return [d for d in found if since <= d.at <= until]

    def _lazy(self, service: str) -> list[Deploy]:
        body = get_json(
            f"{self._lazy_url}/pipelines/{urllib.parse.quote(service, safe='')}/runs",
            headers={"Authorization": f"Bearer {self._lazy_token}"},
        )
        runs = body if isinstance(body, list) else body.get("runs") or body.get("items") or []
        return [d for d in (lazy_deploy(service, r, self._lazy_env) for r in runs) if d is not None]

    def _ecs(self, service: str, since: float, until: float) -> list[Deploy]:
        """Deploys de ECS (stack-lens acepta hasta 24 h): el fin del rollout o, si sigue, su inicio."""
        since = max(since, until - 24 * 3600)
        body = get_json(
            f"{self._stack_lens}/ecs/{urllib.parse.quote(service, safe='')}/deploys",
            {"start": _iso(since), "end": _iso(until)},
        )
        out = []
        for d in body.get("deploys") or []:
            at = _ts(d.get("finished")) or _ts(d.get("started"))
            if at is None:
                continue
            detail = d.get("task_definition") or str(d.get("deployment"))
            status = d.get("state") or ""
            out.append(Deploy(at=at, source="ecs", service=service, detail=detail, status=status))
        return out

    def _changes(self, service: str) -> list[Deploy]:
        body = get_json(f"{self._stack_lens}/changes", {"service": service, "hours": 24})
        out = []
        for c in body.get("changes") or []:
            at = _ts(c.get("completed")) or _ts(c.get("started"))
            if at is None:
                continue
            what = c.get("what") or "deploy"
            name = c.get("application") or c.get("job") or c.get("project") or c.get("name") or ""
            status = str(c.get("result") or c.get("status") or "").lower()
            commit = c.get("commit") or ""
            deploy = Deploy(
                at=at, source=what, service=service, detail=str(name), commit=commit, status=status
            )
            out.append(deploy)
        return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Corridas de Lazy que no despliegan: análisis y revisiones de PR.
NOT_DEPLOY = {"analysis", "pr_review"}


def lazy_deploy(service: str, run: dict[str, Any], env: str) -> Deploy | None:
    """Una corrida de Lazy como deploy, con commit, mensaje, autor y tickets de Jira."""
    if run.get("run_type") in NOT_DEPLOY or (run.get("deploy_env") and run["deploy_env"] != env):
        return None
    at = _ts(run.get("updated_at")) or _ts(run.get("created_at"))
    if at is None:
        return None
    tickets = run.get("jira_refs") or []
    if isinstance(tickets, str):
        tickets = [t for t in tickets.replace(",", " ").split() if t]
    return Deploy(
        at=at,
        source="lazy",
        service=service,
        detail=str(run.get("branch") or run.get("run_id") or ""),
        commit=str(run.get("commit") or run.get("commit_short") or ""),
        message=str(run.get("commit_message") or "")[:500],
        author=str(run.get("commit_author") or ""),
        tickets=tuple(str(t) for t in tickets[:10]),
        status=str(run.get("verdict") or run.get("status") or "").lower(),
    )
