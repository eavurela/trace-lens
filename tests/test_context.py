from __future__ import annotations

import io
import json
import urllib.error

import pytest

from trace_lens import clients, context
from trace_lens.explain import Deploy


def _fake_get(responses):
    def get(url, params=None, headers=None, timeout=30):
        for key, value in responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(url)

    return get


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


def test_context_with_ecs_series(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"cluster": "demo", "period_seconds": 60,
            "CPUUtilization": {"series": [[100, 5.0]]}, "MemoryUtilization": {"series": [[100, 40.0]]}}
    monkeypatch.setattr(context, "get_json", _fake_get({"/metrics": body}))
    deploys = [Deploy(at=200.0, source="ecs", service="demo-api", detail="demo-api:3 completed"),
               Deploy(at=150.0, source="jenkins_deploy", service="demo-api", detail="dp-demo")]
    out = context.service_context(lambda s, a, b: deploys, "http://lens", "demo-api", 0, 3600)
    assert [d["at"] for d in out["deploys"]] == [150.0, 200.0]
    assert out["ecs"]["available"] and out["ecs"]["cpu"] == [[100, 5.0]]


def test_context_not_ecs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context, "get_json", _fake_get({"/metrics": _http(404)}))
    out = context.service_context(lambda s, a, b: [], "http://lens", "demo-api", 0, 3600)
    assert out["ecs"] == {"available": False, "reason": "no es un servicio ECS"}


def test_context_window_capped_at_a_day() -> None:
    seen = {}

    def deploys(service, start, end):
        seen["window"] = end - start
        return []

    context.service_context(deploys, "", "demo-api", 0, 3 * 86400)
    assert seen["window"] == context.MAX_SECONDS


def test_deploys_include_ecs_rollouts(monkeypatch: pytest.MonkeyPatch) -> None:
    ecs = {"deploys": [{"deployment": "ecs-svc/1", "started": None, "finished": "1970-01-01T00:10:00+00:00",
                        "state": "completed", "task_definition": "demo-api:7"}]}
    monkeypatch.setattr(clients, "get_json", _fake_get({"/changes": {"changes": []}, "/deploys": ecs}))
    found = clients.Deploys(stack_lens_url="http://lens")("demo-api", 0, 3600)
    assert [(d.at, d.source, d.detail, d.status) for d in found] == [
        (600.0, "ecs", "demo-api:7", "completed")
    ]


def test_deploys_ignore_non_ecs_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clients, "get_json", _fake_get({"/changes": {"changes": []}, "/deploys": _http(404)}))
    assert clients.Deploys(stack_lens_url="http://lens")("demo-api", 0, 3600) == []


def test_context_endpoint() -> None:
    from fastapi.testclient import TestClient

    from trace_lens.api import create_app
    from trace_lens.store import Store

    app = create_app(Store(":memory:"), contexter=lambda s, a, b: {"service": s, "window": round(b - a)})
    body = TestClient(app).get("/context", params={"service": "demo-api", "minutes": 15}).json()
    assert body == {"service": "demo-api", "window": 900}
    assert json.dumps(body)


def test_lazy_run_as_deploy_with_commit_and_tickets() -> None:
    run = {"run_id": "r1", "run_type": "deploy", "deploy_env": "prod", "branch": "master",
           "commit": "1a2b3c4d5e6f", "commit_short": "1a2b3c4", "commit_message": "fix: login\n\ndetalle",
           "commit_author": "Ana Prueba", "jira_refs": ["PROJ-12"], "verdict": "success",
           "updated_at": "1970-01-01T00:10:00Z"}
    d = clients.lazy_deploy("demo-api", run, "prod")
    assert (d.at, d.commit, d.author) == (600.0, "1a2b3c4d5e6f", "Ana Prueba")
    assert (d.tickets, d.status, d.message) == (("PROJ-12",), "success", "fix: login\n\ndetalle")
    assert clients.lazy_deploy("demo-api", {**run, "run_type": "analysis"}, "prod") is None
    assert clients.lazy_deploy("demo-api", {**run, "deploy_env": "develop"}, "prod") is None


def test_deploy_label_uses_what_is_known() -> None:
    from trace_lens.explain import deploy_label

    full = Deploy(at=0, source="lazy", service="s", detail="master", commit="1a2b3c4d5e", author="Ana",
                  tickets=("PROJ-12",))
    assert deploy_label(full) == "lazy: commit 1a2b3c4d de Ana, PROJ-12"
    assert deploy_label(Deploy(at=0, source="ecs", service="s", detail="demo-api:7")) == "ecs: demo-api:7"


def test_context_deploys_carry_the_details() -> None:
    d = Deploy(at=1.0, source="lazy", service="s", detail="master", commit="abc", tickets=("PROJ-1",))
    out = context.service_context(lambda s, a, b: [d], "", "s", 0, 10)
    assert out["deploys"][0]["commit"] == "abc" and out["deploys"][0]["tickets"] == ["PROJ-1"]
