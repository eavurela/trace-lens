from trace_lens.detect import EndpointStats, Thresholds, change_start, detect

TH = Thresholds()


def stats(**kw) -> EndpointStats:
    base = dict(
        service="svc-a",
        endpoint="POST /items",
        calls=200.0,
        errors=0.0,
        p95_ms=100.0,
        base_calls=20_000.0,
        base_errors=20.0,
        base_p95_ms=100.0,
        calls_yesterday=200.0,
    )
    base.update(kw)
    return EndpointStats(**base)


def kinds(findings):
    return sorted(f.kind for f in findings)


def test_normal_endpoint_has_no_findings():
    assert detect([stats(errors=1)], TH) == []


def test_error_rate_far_above_usual_is_a_finding():
    [f] = detect([stats(errors=30)], TH)  # 15% contra 0,1% habitual
    assert f.kind == "error_rate"
    assert f.value == 0.15
    assert f.baseline == 0.001
    assert f.severity == "media"
    assert f.new is False


def test_errors_never_seen_before_are_flagged_as_new():
    [f] = detect([stats(errors=10, base_errors=0)], TH)
    assert f.kind == "error_rate"
    assert f.new is True


def test_low_traffic_or_few_errors_is_noise():
    # 3 errores de 6 requests: 50%, pero no alcanza para decir nada.
    assert detect([stats(calls=6, errors=3, calls_yesterday=6)], TH) == []
    assert detect([stats(calls=200, errors=4)], TH) == []


def test_an_endpoint_that_always_fails_a_bit_more_is_not_news():
    # 6% de error siempre (base 6%): no es un cambio.
    assert detect([stats(errors=12, base_errors=1200)], TH) == []


def test_severity_grows_with_the_error_ratio():
    [high] = detect([stats(errors=80)], TH)
    assert high.severity == "alta"
    [low] = detect([stats(calls=1000, errors=30)], TH)  # 3%
    assert low.severity == "baja"


def test_latency_increase_is_a_finding_only_if_relevant_in_ms_too():
    [f] = detect([stats(p95_ms=450, base_p95_ms=150)], TH)
    assert f.kind == "latency"
    assert f.value == 450 and f.baseline == 150
    # x3 pero de 2 ms a 6 ms: nadie lo nota.
    assert detect([stats(p95_ms=6, base_p95_ms=2)], TH) == []
    # Sin línea de base de latencia, no se compara.
    assert detect([stats(p95_ms=900, base_p95_ms=None)], TH) == []


def test_traffic_drop_against_the_same_window_yesterday():
    [f] = detect([stats(calls=20, calls_yesterday=400)], TH)
    assert f.kind == "traffic_drop"
    assert f.value == 20 and f.baseline == 400
    # Poco tráfico ayer: una caída no significa nada.
    assert detect([stats(calls=2, calls_yesterday=30)], TH) == []
    assert detect([stats(calls=20, calls_yesterday=None)], TH) == []


def test_several_problems_in_one_endpoint_give_several_findings():
    found = detect([stats(errors=40, p95_ms=900, base_p95_ms=100)], TH)
    assert kinds(found) == ["error_rate", "latency"]


def test_change_start_walks_back_while_the_value_stays_high():
    pts = [(0, 0.0), (60, 0.0), (120, 0.2), (180, 0.3), (240, 0.25), (300, 0.3)]
    assert change_start(pts, threshold=0.1) == 120


def test_change_start_tolerates_short_dips():
    pts = [(0, 0.0), (60, 0.0), (120, 0.2), (180, 0.05), (240, 0.3), (300, 0.3)]
    assert change_start(pts, threshold=0.1, max_gap=1) == 120
    assert change_start(pts, threshold=0.1, max_gap=0) == 240


def test_change_start_ignores_missing_points_and_needs_a_recent_high():
    assert change_start([(0, 0.3), (60, None), (120, 0.3)], threshold=0.1) == 0
    assert change_start([(0, 0.3), (60, 0.0)], threshold=0.1) is None
    assert change_start([], threshold=0.1) is None


def test_dependency_calls_are_checked_for_errors_and_latency_but_not_traffic():
    dep = dict(span_kind="client", endpoint="ECR.DescribeImages")
    [err] = detect([stats(errors=60, **dep)], TH)
    assert (err.kind, err.span_kind) == ("error_rate", "client")
    assert err.key == "error_rate|client|svc-a|ECR.DescribeImages"
    [lat] = detect([stats(p95_ms=900, base_p95_ms=100, **dep)], TH)
    assert lat.kind == "latency"
    # Menos llamadas a una dependencia no es un problema en sí.
    assert detect([stats(calls=20, calls_yesterday=400, **dep)], TH) == []
