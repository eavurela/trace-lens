import base64

from trace_lens.config import Settings
from trace_lens.engine import Engine, stats_queries
from trace_lens.explain import Deploy
from trace_lens.store import Store

NOW = 1_794_500_000.0
SVC, EP = "svc-a", "POST /items"


def b64(hex_id: str) -> str:
    return base64.b64encode(bytes.fromhex(hex_id)).decode()


def sample(value, **labels):
    metric = {"service": SVC, "span_name": EP, "span_kind": "SPAN_KIND_SERVER", **labels}
    return {"metric": metric, "value": [NOW, str(value)]}


class FakeProm:
    def __init__(self, failing: bool, http_4xx: int = 0):
        self.failing = failing
        self.http_4xx = http_4xx
        self.q = stats_queries(600)

    def query(self, query):
        errors = 40 if self.failing else 0
        table = {
            self.q["http_4xx"]: [sample(self.http_4xx)] if self.http_4xx else [],
            self.q["base_http_4xx"]: [sample(10)],
            self.q["codes"]: (
                [
                    sample(self.http_4xx * 0.9, http_status_code="404"),
                    sample(self.http_4xx * 0.1, http_status_code="401"),
                ]
                if self.http_4xx
                else []
            ),
            self.q["calls"]: [sample(200)],
            self.q["errors"]: [sample(errors)] if errors else [],
            self.q["p95"]: [sample(0.1)],
            self.q["base_calls"]: [sample(20000)],
            self.q["base_errors"]: [sample(20)],
            self.q["base_p95"]: [sample(0.1)],
            self.q["calls_yesterday"]: [sample(200)],
        }
        return table[query]

    def query_range(self, query, start, end, step):
        # El error sube a las NOW-600 y sigue alto.
        values = [[t, "0.2" if t >= NOW - 600 else "0"] for t in range(int(start), int(end) + 1, step)]
        return [{"metric": {}, "values": values}]


def span_hit(trace, query, status_code, status="unset"):
    attrs = [
        {"key": "http.target", "value": {"stringValue": f"/items?{query}"}},
        {"key": "http.status_code", "value": {"intValue": str(status_code)}},
        {"key": "status", "value": {"stringValue": status}},
    ]
    return {"traceID": trace, "spanSets": [{"spans": [{"spanID": trace, "attributes": attrs}]}]}


class FakeTempo:
    def __init__(self):
        self.searches = 0

    def search_body(self, query, start, end, limit):
        # Las que fallan son todas del tenant "acme"; las que andan, de otros.
        if "status=error ||" in query:
            return {"traces": [span_hit(f"f{i}", "tenant=acme", 404) for i in range(6)]}
        return {"traces": [span_hit(f"o{i}", f"tenant=t{i % 4}", 200) for i in range(20)]}

    def search(self, query, start, end, limit):
        self.searches += 1
        assert "status=error" in query and f'name="{EP}"' in query and "kind=server" in query
        return ["aa" * 16, "bb" * 16]

    def trace(self, trace_id):
        return {
            "trace": {
                "resourceSpans": [
                    {
                        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": SVC}}]},
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "spanId": b64("01" * 8),
                                        "name": EP,
                                        "status": {"code": "STATUS_CODE_ERROR"},
                                    },
                                    {
                                        "spanId": b64("02" * 8),
                                        "parentSpanId": b64("01" * 8),
                                        "name": "Mongo.find",
                                        "status": {
                                            "code": "STATUS_CODE_ERROR",
                                            "message": f"timeout {trace_id[:2]}",
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                ]
            }
        }


def deploys(service, since, until):
    return [Deploy(at=NOW - 900, source="lazy", service=service, detail="commit abc123")]


def engine(store, prom, tempo, now):
    return Engine(store, prom, tempo, deploys, Settings(prometheus_url="x", tempo_url="y"), now=lambda: now)


def test_a_new_problem_becomes_an_explained_insight():
    store = Store(":memory:")
    tempo = FakeTempo()
    stats = engine(store, FakeProm(failing=True), tempo, NOW).run_once()
    assert stats["active"] == 1

    [ins] = store.insights(state="active")
    assert ins["kind"] == "error_rate"
    assert ins["service"] == SVC and ins["endpoint"] == EP
    assert ins["severity"] == "alta"
    assert ins["started_at"] == NOW - 600
    assert ins["causes"][0]["operation"] == "Mongo.find"
    # Dos trazas con mensajes distintos ("timeout aa", "timeout bb"): dos causas, mitad y mitad.
    assert [(c["count"], c["share"]) for c in ins["causes"]] == [(1, 0.5), (1, 0.5)]
    assert ins["deploy"]["detail"] == "commit abc123"
    assert "falla en el 20% de los requests" in ins["summary"]
    assert "Empezó 5 min después de un deploy" in ins["summary"]
    assert ins["search"] == {
        "service": SVC,
        "endpoint": EP,
        "errorsOnly": True,
        "status": "",
        "since": NOW - 600,
    }


def test_the_same_problem_is_updated_not_duplicated_and_not_re_explained_right_away():
    store = Store(":memory:")
    tempo = FakeTempo()
    engine(store, FakeProm(failing=True), tempo, NOW).run_once()
    engine(store, FakeProm(failing=True), tempo, NOW + 120).run_once()
    assert len(store.insights(state="all")) == 1
    assert tempo.searches == 1
    [ins] = store.insights(state="active")
    assert ins["last_seen"] == NOW + 120


def test_when_it_goes_back_to_normal_it_is_resolved():
    store = Store(":memory:")
    engine(store, FakeProm(failing=True), FakeTempo(), NOW).run_once()
    # Una corrida sin el problema no alcanza (puede ser un bache); la segunda lo cierra.
    engine(store, FakeProm(failing=False), FakeTempo(), NOW + 120).run_once()
    assert len(store.insights(state="active")) == 1
    engine(store, FakeProm(failing=False), FakeTempo(), NOW + 400).run_once()
    assert store.insights(state="active") == []
    [ins] = store.insights(state="resolved")
    assert ins["resolved_at"] == NOW + 400


def test_a_failing_tempo_still_leaves_the_insight_without_causes():
    class BrokenTempo(FakeTempo):
        def search(self, *a):
            raise OSError("tempo caído")

    store = Store(":memory:")
    engine(store, FakeProm(failing=True), BrokenTempo(), NOW).run_once()
    [ins] = store.insights(state="active")
    assert ins["causes"] == []
    assert "falla en el 20%" in ins["summary"]


def test_4xx_that_are_not_errors_become_their_own_insight_with_the_code_and_the_data():
    store = Store(":memory:")
    engine(store, FakeProm(failing=False, http_4xx=100), FakeTempo(), NOW).run_once()
    [ins] = store.insights(state="active")
    assert ins["kind"] == "http_4xx"
    assert ins["severity"] == "alta"
    assert ins["codes"] == {"404": 90, "401": 10}
    assert ins["causes"] == []
    assert ins["focus"][0]["param"] == "tenant" and ins["focus"][0]["value"] == "acme"
    assert "responde 4xx al 50% de los requests" in ins["summary"]
    assert "Predomina el 404 (90%): ruta o recurso que no existe." in ins["summary"]
    assert "Se concentra en tenant=acme: 100% de los que fallan y 0% de los que andan." in ins["summary"]
    assert ins["search"]["status"] == "4xx"


def test_error_insights_also_say_where_the_failures_concentrate():
    store = Store(":memory:")
    engine(store, FakeProm(failing=True), FakeTempo(), NOW).run_once()
    [ins] = store.insights(state="active")
    assert "Se concentra en tenant=acme" in ins["summary"]
