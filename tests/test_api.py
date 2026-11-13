from fastapi.testclient import TestClient

from trace_lens.api import create_app
from trace_lens.store import Store


def insight(key="error_rate|svc-a|GET /x", service="svc-a"):
    kind, svc, ep = key.split("|")
    return {
        "key": key,
        "kind": kind,
        "service": svc,
        "endpoint": ep,
        "severity": "media",
        "value": 0.1,
        "baseline": 0.0,
        "new": 1,
        "started_at": None,
        "detected_at": 9e9,
        "last_seen": 9e9,
        "explained_at": 9e9,
        "causes": [],
        "deploy": None,
        "summary": "texto",
    }


def test_insights_filter_by_service_and_reject_weird_names():
    store = Store(":memory:")
    store.save(insight())
    store.save(insight(key="latency|svc-b|GET /y"))
    client = TestClient(create_app(store, lambda: {"detector": {"last_error": None}}))
    assert client.get("/insights").json()["total"] == 2
    body = client.get("/insights", params={"service": "svc-b"}).json()
    assert [i["service"] for i in body["insights"]] == ["svc-b"]
    assert body["insights"][0]["search"]["errorsOnly"] is False
    assert client.get("/insights", params={"service": 'a"b'}).status_code == 422
    assert client.get("/health").json()["counts"] == {"active": 2}


def test_examine_validates_and_passes_the_window_to_the_examiner():
    calls = []

    def examiner(svc, ep, kind, start, end, limit):
        calls.append((svc, ep, kind, round(end - start), limit))
        return {"route": "/a/{id}", "focus": []}

    client = TestClient(create_app(Store(":memory:"), examiner=examiner))
    r = client.get("/examine", params={"service": "svc-a", "endpoint": "GET /a/{id}", "minutes": 30})
    assert r.status_code == 200 and r.json()["route"] == "/a/{id}"
    assert calls == [("svc-a", "GET /a/{id}", "server", 1800, 100)]
    assert (
        client.get("/examine", params={"service": "svc-a", "endpoint": "x", "kind": "internal"}).status_code
        == 422
    )
    assert client.get("/examine", params={"service": 'a"b', "endpoint": "x"}).status_code == 422


def test_examine_without_tempo_says_so():
    client = TestClient(create_app(Store(":memory:")))
    assert client.get("/examine", params={"service": "s", "endpoint": "e"}).status_code == 503
