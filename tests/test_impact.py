from __future__ import annotations

from trace_lens.explain import Cause, Deploy
from trace_lens.impact import (
    Side,
    Window,
    change_phrase,
    compare,
    judge,
    new_causes,
    summary,
    window_queries,
    windows,
)

NONE = Side()


def test_windows_leave_time_for_the_rollout_and_mark_partial() -> None:
    before, after, partial = windows(10_000, 30, now=10_000 + 300 + 600)
    assert (before.start, before.end) == (10_000 - 1800, 10_000)
    assert (after.start, after.end) == (10_300, 10_900)
    assert partial is True
    assert windows(10_000, 30, now=99_999)[2] is False


def test_window_queries_evaluate_at_the_end_and_yesterday() -> None:
    q = window_queries('demo-"api', Window(1000, 2800))
    assert '[1800s] @ 2800' in q["calls"] and 'service="demo-\\"api"' in q["calls"]
    assert "offset 1d" in window_queries("demo-api", Window(1000, 2800), "1d")["errors"]


def test_judge_new_errors() -> None:
    assert judge(Side(calls=100), Side(calls=100, errors=5), NONE, NONE, 1) == ["new_errors"]


def test_judge_more_errors_unless_it_happens_every_day() -> None:
    before, after = Side(calls=100, errors=1), Side(calls=100, errors=10)
    assert judge(before, after, Side(calls=100, errors=1), Side(calls=100, errors=1), 1) == ["more_errors"]
    # ayer a esta hora subió igual: no es el deploy
    assert judge(before, after, Side(calls=100, errors=1), Side(calls=100, errors=10), 1) == []


def test_judge_fewer_errors_slower_and_4xx() -> None:
    assert judge(Side(calls=100, errors=10), Side(calls=100, errors=1), NONE, NONE, 1) == ["fewer_errors"]
    slow = judge(Side(calls=100, p95_ms=100), Side(calls=100, p95_ms=400), NONE, NONE, 1)
    assert slow == ["slower"]
    assert judge(Side(calls=100), Side(calls=100, http_4xx=30), NONE, NONE, 1) == ["more_4xx"]


def test_judge_stopped_only_if_yesterday_had_traffic() -> None:
    assert judge(Side(calls=100), NONE, NONE, Side(calls=50), 1) == ["stopped"]
    assert judge(Side(calls=100), NONE, NONE, NONE, 1) == []


def _prom(tables: dict[str, dict[str, float]]):
    """Responde según la ventana (antes/después, hoy/ayer) y la métrica pedida."""

    def query(q: str):
        when = ("y" if "offset 1d" in q else "") + ("after" if "@ 5000" in q else "before")
        metric = "p95" if "histogram_quantile" in q else "errors" if "STATUS_CODE_ERROR\"}" in q and "!=" not in q \
            else "http_4xx" if "http_status_code" in q else "calls"
        rows = tables.get(f"{when}:{metric}", {})
        return [{"metric": {"span_name": k, "span_kind": "SPAN_KIND_SERVER"}, "value": [0, str(v)]}
                for k, v in rows.items()]

    return query


def test_compare_ranks_what_got_worse_first() -> None:
    prom = _prom({
        "before:calls": {"GET /a": 100, "GET /b": 100},
        "after:calls": {"GET /a": 100, "GET /b": 100},
        "after:errors": {"GET /b": 8},
    })
    rows = compare(prom, "demo-api", Window(1000, 2000), Window(4000, 5000))
    assert [(r["endpoint"], r["verdicts"]) for r in rows] == [("GET /b", ["new_errors"]), ("GET /a", [])]
    assert change_phrase(rows[0]) == "errores 0,0% → 8,0%"


def test_new_causes_and_summary() -> None:
    old = Cause("demo-api", "db.find", "timeout", 2, 1.0)
    new = Cause("demo-api", "GET /b", "KeyError: 'id'", 3, 1.0)
    assert new_causes([old], [old, new]) == [new]
    row = {"endpoint": "GET /b", "verdicts": ["new_errors"],
           "before": {"error_ratio": 0, "p95_ms": None, "http_4xx_ratio": 0, "calls": 100},
           "after": {"error_ratio": 0.08, "p95_ms": None, "http_4xx_ratio": 0, "calls": 100}}
    deploy = Deploy(at=0, source="lazy", service="demo-api", detail="master", commit="1a2b3c4d5e", author="Ana")
    verdict, text = summary(deploy, [row], [new], partial=False)
    assert verdict == "worse"
    assert text.startswith("Después del deploy (lazy: commit 1a2b3c4d de Ana) empeoró: GET /b: errores 0,0% → 8,0%")
    assert "Error nuevo en GET /b (demo-api): KeyError" in text
    assert summary(None, [], [], partial=True)[0] == "same"
