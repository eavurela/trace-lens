from trace_lens.detect import Finding
from trace_lens.explain import Cause, Deploy, group_causes, match_deploy, normalize_message, summarize

T0 = 1_794_500_000.0


def finding(kind="error_rate", **kw) -> Finding:
    base = dict(
        kind=kind,
        service="svc-a",
        endpoint="POST /environments/{env_id}/redeploy/{service_id}",
        severity="media",
        value=0.15,
        baseline=0.002,
        new=False,
        threshold=0.05,
    )
    base.update(kw)
    return Finding(**base)


def test_messages_are_normalized_so_the_same_error_groups_together():
    a = normalize_message("The image with imageTag:'develo' does not exist within repository 'ms-assistant'")
    b = normalize_message("The image with imageTag:'main' does not exist within repository 'ms-events'")
    assert a == b
    assert normalize_message("timeout after 3021 ms on 10.0.1.2:27017 id=6f1e2d3c4b5a69788796a5b4") == (
        "timeout after <n> ms on <ip> id=<id>"
    )
    assert len(normalize_message("x" * 500)) <= 160


def test_group_causes_counts_by_origin_and_sorts_by_share():
    origins = [
        ("svc-a", "ECR.DescribeImages", "image 'a' does not exist"),
        ("svc-a", "ECR.DescribeImages", "image 'b' does not exist"),
        ("svc-a", "ECR.DescribeImages", "image 'c' does not exist"),
        ("svc-b", "Mongo.find", "timeout after 3000 ms"),
    ]
    causes = group_causes(origins, top=2)
    assert causes[0] == Cause(
        service="svc-a",
        operation="ECR.DescribeImages",
        message="image <v> does not exist",
        count=3,
        share=0.75,
    )
    assert causes[1].operation == "Mongo.find"
    assert group_causes([]) == []


def test_the_deploy_closest_before_the_start_is_the_suspect():
    deploys = [
        Deploy(at=T0 - 7200, source="lazy", service="svc-a", detail="old"),
        Deploy(at=T0 - 300, source="lazy", service="svc-a", detail="v2"),
        Deploy(at=T0 + 900, source="lazy", service="svc-a", detail="after"),
    ]
    assert match_deploy(deploys, start=T0).detail == "v2"
    # Un deploy de unos segundos después también cuenta (el rollout tarda).
    assert (
        match_deploy([Deploy(at=T0 + 60, source="ecs", service="svc-a", detail="x")], start=T0).detail == "x"
    )
    assert match_deploy([deploys[0]], start=T0) is None


def test_summary_of_an_error_rate_finding_with_cause_and_deploy():
    text = summarize(
        finding(),
        start=T0,
        causes=[Cause("svc-a", "ECR.DescribeImages", "image <v> does not exist", 9, 0.9)],
        deploy=Deploy(at=T0 - 180, source="lazy", service="svc-a", detail="commit abc123"),
        tz="America/Argentina/Buenos_Aires",
    )
    assert text == (
        "POST /environments/{env_id}/redeploy/{service_id} (svc-a) falla en el 15% de los requests desde las "
        "13:13 (lo habitual es 0,2%). "
        "El 90% de los errores nace en ECR.DescribeImages: image <v> does not exist. "
        "Empezó 3 min después de un deploy (lazy: commit abc123)."
    )


def test_summary_of_new_errors_latency_and_traffic_drop():
    tz = "America/Argentina/Buenos_Aires"
    assert summarize(finding(new=True), start=None, causes=[], deploy=None, tz=tz) == (
        "POST /environments/{env_id}/redeploy/{service_id} (svc-a) falla en el 15% de los requests. "
        "Son errores nuevos: no había en las últimas 24 h."
    )
    assert (
        summarize(
            finding("latency", endpoint="GET /x", value=450, baseline=150),
            start=T0,
            causes=[],
            deploy=None,
            tz=tz,
        )
        == "GET /x (svc-a) tarda 450 ms en p95 desde las 13:13 (lo habitual es 150 ms)."
    )
    assert (
        summarize(
            finding("traffic_drop", endpoint="GET /x", value=20, baseline=400),
            start=None,
            causes=[],
            deploy=None,
            tz=tz,
        )
        == "GET /x (svc-a) recibe 95% menos requests que ayer a esta hora (20 contra 400)."
    )


def test_summary_of_a_failing_dependency():
    tz = "America/Argentina/Buenos_Aires"
    f = finding(span_kind="client", endpoint="ECR.DescribeImages", value=0.3, baseline=0.0, new=True)
    assert summarize(f, start=T0, causes=[], deploy=None, tz=tz) == (
        "Las llamadas de svc-a a ECR.DescribeImages fallan en el 30% desde las 13:13. "
        "Son errores nuevos: no había en las últimas 24 h."
    )
    lat = finding("latency", span_kind="client", endpoint="DynamoDB.GetItem", value=900, baseline=100)
    assert summarize(lat, start=None, causes=[], deploy=None, tz=tz) == (
        "Las llamadas de svc-a a DynamoDB.GetItem tardan 900 ms en p95 (lo habitual es 100 ms)."
    )
