from trace_lens.examine import (
    MASK,
    Call,
    call_from_span,
    calls_from_search,
    examine,
    examine_queries,
    params_of,
    route_params,
    templatize,
)

ROUTE = "/pipelines/{service_id}/reviews/{run_id}/content"


def call(path, failing, status=None, trace="t", query="", route=ROUTE, attributes=()):
    return Call(
        trace_id=trace,
        failing=failing,
        path=path,
        query=query,
        route=route,
        status=status if status is not None else (500 if failing else 200),
        attributes=attributes,
    )


def test_route_params_reads_each_placeholder_from_the_concrete_path():
    assert route_params(ROUTE, "/pipelines/ms-participation/reviews/123456/content") == [
        ("service_id", "ms-participation"),
        ("run_id", "123456"),
    ]
    assert route_params(ROUTE, "/pipelines/x/other/1/content") is None
    assert route_params(ROUTE, "/pipelines/x/reviews/1") is None
    assert route_params("/files/{p:path}", "/files/a/b/c.txt") == [("p", "a/b/c.txt")]


def test_templatize_turns_ids_into_named_params():
    template, params = templatize("/users/42/orders/3fa85f64-5717-4562-b3fc-2c963f66afa6/items")
    assert template == "/users/{id}/orders/{id}/items"
    assert params == [("users/{id}", "42"), ("orders/{id}", "3fa85f64-5717-4562-b3fc-2c963f66afa6")]
    assert templatize("/health") == ("/health", [])


def test_call_from_span_prefers_http_target_and_ignores_nil():
    c = call_from_span(
        {
            "http.target": "/pipelines/ms-a/reviews/9/content?page=2",
            "http.url": "http://lazy/pipelines/ms-a/reviews/9/content?page=2",
            "url.path": "nil",
            "http.route": ROUTE,
            "http.status_code": "404",
            "status": "unset",
        },
        "abc",
    )
    assert (c.path, c.query, c.route, c.status, c.failing) == (
        "/pipelines/ms-a/reviews/9/content",
        "page=2",
        ROUTE,
        404,
        True,
    )


def test_call_from_span_without_route_takes_it_from_the_span_name_or_has_none():
    c = call_from_span({"http.url": "https://api.x.com/v1/users/42?q=a", "status": "error"}, "t", "GET")
    assert (c.path, c.query, c.route, c.failing) == ("/v1/users/42", "q=a", None, True)
    assert call_from_span({"http.target": "/a/1"}, "t", "GET /a/{id}").route == "/a/{id}"


def test_sensitive_query_values_are_masked():
    c = call("/pipelines/a/reviews/1/content", False, query="access_token=eyJa.b.c&page=2&api_key=zzz")
    values = {(n, s): v for n, s, v in params_of(c)}
    assert values[("access_token", "query")] == MASK
    assert values[("api_key", "query")] == MASK
    assert values[("page", "query")] == "2"


def test_a_value_that_concentrates_the_failures_is_reported_as_focus():
    calls = [call(f"/pipelines/ms-participation/reviews/{i}/content", True, trace=f"f{i}") for i in range(5)]
    calls += [call(f"/pipelines/ms-ok-{i % 3}/reviews/{100 + i}/content", False) for i in range(20)]
    calls += [call("/pipelines/ms-participation/reviews/999/content", False)]
    out = examine(calls)

    assert out["route"] == ROUTE
    assert out["sampled"] == {"failing": 5, "ok": 21}
    assert out["codes"] == {"500": 5}
    [focus] = out["focus"]
    assert (focus["param"], focus["source"], focus["value"]) == ("service_id", "path", "ms-participation")
    assert focus["failing"] == 5 and focus["failing_share"] == 1.0
    assert round(focus["ok_share"], 3) == round(1 / 21, 3)
    assert focus["traces"] == ["f0", "f1", "f2"]
    # run_id cambia en cada llamada: nunca concentra.
    run_ids = next(p for p in out["params"] if p["name"] == "run_id")
    assert run_ids["distinct"] == 26
    assert len(run_ids["values"]) == 10


def test_no_focus_when_the_value_is_just_as_common_in_calls_that_work():
    calls = [call("/pipelines/ms-a/reviews/1/content", i < 5) for i in range(20)]
    assert examine(calls)["focus"] == []


def test_paths_are_ranked_by_failures_and_carry_their_codes():
    calls = [
        call("/pipelines/a/reviews/1/content", True, status=404),
        call("/pipelines/a/reviews/1/content", True, status=404),
        call("/pipelines/b/reviews/2/content", False),
    ]
    out = examine(calls)
    assert out["paths"][0]["path"] == "/pipelines/a/reviews/1/content"
    assert out["paths"][0]["codes"] == {"404": 2}
    assert out["paths"][1]["ok"] == 1


def test_non_http_calls_are_examined_by_their_attributes():
    calls = [
        call(None, True, status=None, route=None, attributes=(("aws.dynamodb.table_names", "lazy-runs"),)),
        call(None, True, status=None, route=None, attributes=(("aws.dynamodb.table_names", "lazy-runs"),)),
        call(None, True, status=None, route=None, attributes=(("aws.dynamodb.table_names", "lazy-runs"),)),
        call(None, False, status=None, route=None, attributes=(("aws.dynamodb.table_names", "lazy-envs"),)),
    ]
    out = examine(calls)
    assert out["route"] is None
    assert out["focus"][0]["value"] == "lazy-runs"


def test_examine_queries_split_failing_and_working_samples():
    q = examine_queries("svc", "GET /a/{id}", "server")
    assert 'name="GET /a/{id}"' in q["failing"] and "kind=server" in q["failing"]
    assert "(status=error || span.http.status_code>=400)" in q["failing"]
    assert "status!=error" in q["ok"] and "span.http.status_code=nil" in q["ok"]
    assert "span.http.target" in q["ok"]


def test_calls_from_search_reads_tempo_select_attributes():
    body = {
        "traces": [
            {
                "traceID": "abc",
                "spanSets": [
                    {
                        "spans": [
                            {
                                "spanID": "s1",
                                "attributes": [
                                    {"key": "http.target", "value": {"stringValue": "/a/7"}},
                                    {"key": "http.status_code", "value": {"intValue": "404"}},
                                    {"key": "status", "value": {"stringValue": "unset"}},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    [c] = calls_from_search(body, "GET /a/{id}")
    assert c.trace_id == "abc".rjust(32, "0")
    assert (c.path, c.route, c.status, c.failing) == ("/a/7", "/a/{id}", 404, True)
