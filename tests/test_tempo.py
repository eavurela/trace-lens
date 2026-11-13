from trace_lens.tempo import error_origins, parse_trace

ROOT = "mNzumr8RC9s="  # 98dcee9abf110bdb
CHILD = "vFbOfX+/n4g="
GRAND = "diYvFyUP5G0="


def kv(key, value):
    return {"key": key, "value": {"stringValue": value}}


def body(child_status="STATUS_CODE_ERROR", grand_status="STATUS_CODE_ERROR"):
    return {
        "trace": {
            "resourceSpans": [
                {
                    "resource": {"attributes": [kv("service.name", "svc-a")]},
                    "scopeSpans": [
                        {
                            "spans": [
                                {"spanId": ROOT, "name": "POST /x", "status": {"code": "STATUS_CODE_ERROR"}},
                                {
                                    "spanId": CHILD,
                                    "parentSpanId": ROOT,
                                    "name": "deploy",
                                    "status": {"code": child_status, "message": "failed"},
                                },
                            ]
                        }
                    ],
                },
                {
                    "resource": {"attributes": [kv("service.name", "svc-b")]},
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "spanId": GRAND,
                                    "parentSpanId": CHILD,
                                    "name": "ECR.DescribeImages",
                                    "status": {"code": grand_status, "message": "image 'x' not found"},
                                },
                            ]
                        }
                    ],
                },
            ]
        }
    }


def test_parse_trace_reads_ids_services_and_status():
    spans = parse_trace(body())
    assert [(s.service, s.name, s.error) for s in spans] == [
        ("svc-a", "POST /x", True),
        ("svc-a", "deploy", True),
        ("svc-b", "ECR.DescribeImages", True),
    ]
    assert spans[0].span_id == "98dcee9abf110bdb"
    assert spans[1].parent_id == spans[0].span_id
    assert parse_trace({}) == []


def test_the_origin_is_the_deepest_error():
    [origin] = error_origins(parse_trace(body()))
    assert (origin.service, origin.name, origin.message) == (
        "svc-b",
        "ECR.DescribeImages",
        "image 'x' not found",
    )


def test_if_children_are_fine_the_error_starts_higher_up():
    [origin] = error_origins(parse_trace(body(grand_status="STATUS_CODE_UNSET")))
    assert origin.name == "deploy"


def test_a_cycle_does_not_hang():
    b = body()
    b["trace"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["parentSpanId"] = GRAND
    assert len(error_origins(parse_trace(b))) <= 3
