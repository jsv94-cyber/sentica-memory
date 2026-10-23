"""The context-latency node stays serial, collected, and on its budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.sentica.context_latency_ci import (
    WORKFLOW,
    LatencyCIError,
    check_test_source,
    check_workflow,
    collect_output,
    require_collected,
    require_passed,
)

ROOT = Path(__file__).resolve().parents[2]
LATENCY_SOURCE = ROOT / "tests" / "sentica" / "test_m6_retrieval.py"


def test_context_latency_ci_matches_the_workflow() -> None:
    check_workflow()


def test_dropping_the_serial_step_fails_the_guard() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("      - name: Run context-latency test serially\n")
    end = text.index("\n  test-m1-stack:\n")
    mutated = text[:start] + text[end + 1 :]
    with pytest.raises(LatencyCIError, match="serial"):
        check_workflow(mutated)


def test_removing_the_deselect_fails_the_guard() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    mutated = text.replace(
        " --deselect tests/sentica/test_m6_retrieval.py::"
        + "test_context_latency_on_ten_thousand_conclusions",
        "",
        1,
    )
    with pytest.raises(LatencyCIError, match="parallel"):
        check_workflow(mutated)


def test_skipped_collection_is_rejected() -> None:
    with pytest.raises(LatencyCIError, match="skipped"):
        require_collected("1 skipped in 0.01s\n")


def test_zero_collected_is_rejected() -> None:
    with pytest.raises(LatencyCIError, match="zero"):
        require_collected("0 tests collected in 0.01s\n")


def test_missing_collection_count_is_rejected() -> None:
    with pytest.raises(LatencyCIError, match="zero"):
        require_collected("no tests ran\n")


def test_passed_run_must_report_timing() -> None:
    with pytest.raises(LatencyCIError, match="timing"):
        require_passed("1 passed in 0.20s\n")


def test_skipped_run_is_rejected() -> None:
    with pytest.raises(LatencyCIError, match="skipped"):
        require_passed(
            "1 passed, 1 skipped in 0.20s\ncontext cold 10.0 ms, warm p95 9.0 ms\n"
        )


def test_skip_marker_in_the_latency_test_is_rejected() -> None:
    source = LATENCY_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace(
        "    assert warm_p95 < 0.3\n",
        "    pytest.skip('load')\n    assert warm_p95 < 0.3\n",
        1,
    )
    with pytest.raises(LatencyCIError, match="pytest.skip"):
        check_test_source(mutated)


def test_loosened_budget_is_rejected() -> None:
    source = LATENCY_SOURCE.read_text(encoding="utf-8")
    mutated = source.replace("assert warm_p95 < 0.3", "assert warm_p95 < 0.5", 1)
    with pytest.raises(LatencyCIError, match="300 ms"):
        check_test_source(mutated)


def test_context_latency_collects_exactly_once() -> None:
    text, status = collect_output()
    assert status == 0, text
    require_collected(text)
