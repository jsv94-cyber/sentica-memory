"""Fail closed if the context-latency CI split is skipped or removed."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import yaml

NODE_ID = (
    "tests/sentica/test_m6_retrieval.py::"
    "test_context_latency_on_ten_thousand_conclusions"
)
# sha256 of NODE_ID encoded as UTF-8. Change the two together.
NODE_ID_SHA256 = "5e027ecdbd83b9e89f9770eb5b2be9a622c0368a33dffc83a3833a51711cbcfb"
CHECKER_PATH = Path("tests/sentica/context_latency_ci.py")
PARALLEL_STEP = "Run Tests"
SERIAL_STEP = "Run context-latency test serially"
PARALLEL_COMMAND = (
    "uv run pytest -x --ignore=tests/sentica/test_m1_stack.py --deselect " + NODE_ID
)
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "unittest.yml"
TEST_SOURCE = ROOT / "tests" / "sentica" / "test_m6_retrieval.py"
FUNCTION_NAME = "test_context_latency_on_ten_thousand_conclusions"
BUDGET_ASSERTION = "assert warm_p95 < 0.3"
_BANNED_SOURCE = (
    "pytest.skip",
    "pytest.importorskip",
    "importorskip",
    "pytest.xfail",
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "pytest.mark.xfail",
)
_SKIPPED = re.compile(
    r"(?i)\bskipped\b|\bxfailed\b|\bxfail\b|\bxpassed\b|\bimportorskip\b"
)
_COLLECTED = (
    re.compile(r"(?m)^(\d+) tests? collected\b"),
    re.compile(r"(?m)^collected (\d+) items?\b"),
)
_TIMING = re.compile(r"context cold \d+\.\d+ ms, warm p95 \d+\.\d+ ms")
_PASSED = re.compile(r"\b1 passed\b")


class LatencyCIError(RuntimeError):
    """The context-latency CI split is missing or fail-open."""


def check_node_pin() -> None:
    """The node id in this module matches its pinned digest."""
    digest = hashlib.sha256(NODE_ID.encode("utf-8")).hexdigest()
    if digest != NODE_ID_SHA256:
        raise LatencyCIError("context-latency node id digest does not match the pin")


def check_test_source(source: str) -> None:
    """The latency test is real, unskipped, and still on the 300 ms budget."""
    tree = ast.parse(source)
    found = False
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != FUNCTION_NAME:
            continue
        found = True
        segment = ast.get_source_segment(source, node) or ""
        for banned in _BANNED_SOURCE:
            if banned in segment:
                raise LatencyCIError("context-latency test uses " + banned)
        if BUDGET_ASSERTION not in segment:
            raise LatencyCIError("context-latency budget is not 300 ms")
    if not found:
        raise LatencyCIError("context-latency test is missing from its module")


def _collected_count(text: str) -> int | None:
    found: list[int] = []
    for pattern in _COLLECTED:
        found.extend(int(value) for value in pattern.findall(text))
    if not found:
        return None
    return found[-1]


def require_collected(text: str) -> None:
    """Fail if collection skipped the test or did not collect it once."""
    if _SKIPPED.search(text):
        raise LatencyCIError("context-latency test was skipped")
    count = _collected_count(text)
    if count is None or count == 0:
        raise LatencyCIError("context-latency test was collected zero times")
    if count != 1:
        raise LatencyCIError(
            "context-latency test was collected " + str(count) + " times"
        )


def require_passed(text: str) -> None:
    """Fail unless the serial run passed once and reported its timing."""
    if _SKIPPED.search(text):
        raise LatencyCIError("context-latency test was skipped")
    if _PASSED.search(text) is None:
        raise LatencyCIError("context-latency test did not pass")
    if _TIMING.search(text) is None:
        raise LatencyCIError("context-latency timing was not reported")


def _as_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LatencyCIError(label + " is missing")
    return cast(dict[str, Any], value)


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    raw_steps = job.get("steps")
    if not isinstance(raw_steps, list):
        raise LatencyCIError("test-python steps are missing")
    parsed: list[dict[str, Any]] = []
    for step in cast(list[object], raw_steps):
        parsed.append(_as_mapping(step, "test-python step"))
    return parsed


def _run(step: dict[str, Any]) -> str:
    run = step.get("run")
    if not isinstance(run, str):
        return ""
    return run


def _step_index(steps: list[dict[str, Any]], name: str) -> int | None:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    return None


def _check_status(document: dict[str, Any]) -> None:
    jobs = _as_mapping(document.get("jobs"), "workflow jobs")
    status = _as_mapping(jobs.get("test-status"), "test-status")
    needs = status.get("needs")
    if not isinstance(needs, list) or "test-python" not in needs:
        raise LatencyCIError("test-status does not require test-python")
    if status.get("if") != "always()":
        raise LatencyCIError("test-status does not always report")
    script = "\n".join(_run(step) for step in _steps(status))
    needle = 'if [[ "${{ needs.test-python.result }}" != "success" ]]; then'
    if needle not in script or "exit 1" not in script:
        raise LatencyCIError("test-status does not fail the job when test-python fails")


def check_workflow(text: str | None = None) -> None:
    """Fail if either CI step no longer covers the latency node."""
    check_node_pin()
    check_test_source(TEST_SOURCE.read_text(encoding="utf-8"))
    raw = WORKFLOW.read_text(encoding="utf-8") if text is None else text
    loaded: object = yaml.safe_load(raw)
    document = _as_mapping(loaded, "unittest workflow")
    jobs = _as_mapping(document.get("jobs"), "workflow jobs")
    job = _as_mapping(jobs.get("test-python"), "test-python")
    steps = _steps(job)
    parallel_index = _step_index(steps, PARALLEL_STEP)
    serial_index = _step_index(steps, SERIAL_STEP)
    problems: list[str] = []
    if (
        parallel_index is None
        or _run(steps[parallel_index]).strip() != PARALLEL_COMMAND
    ):
        problems.append("missing from the parallel step")
    if serial_index is None or parallel_index is None or serial_index < parallel_index:
        problems.append("missing from the serial step")
    if problems or parallel_index is None or serial_index is None:
        raise LatencyCIError(
            "context-latency test is " + " and ".join(problems)
            if problems
            else "context-latency test is missing from the serial step"
        )
    serial = steps[serial_index]
    if serial.get("continue-on-error") or "if" in serial:
        raise LatencyCIError("serial context-latency step can be skipped")
    serial_run = _run(serial)
    digest = hashlib.sha256((ROOT / CHECKER_PATH).read_bytes()).hexdigest()
    pin = digest + "  " + CHECKER_PATH.as_posix()
    required = (
        "set -euo pipefail",
        "sha256sum -c",
        pin,
        'nodeid="' + NODE_ID + '"',
        'uv run pytest "$nodeid" -n0 --collect-only -q',
        "context_latency_ci.py --require-collected",
        'uv run pytest "$nodeid" -n0 -v --tb=short',
        "context_latency_ci.py --require-passed",
        'exit "$collect_status"',
        'exit "$run_status"',
    )
    if any(item not in serial_run for item in required):
        raise LatencyCIError("context-latency test is missing from the serial step")
    if "|| true" in serial_run or "||true" in serial_run:
        raise LatencyCIError("serial context-latency step fails open")
    parallel_env = steps[parallel_index].get("env")
    if serial.get("env") != parallel_env:
        raise LatencyCIError(
            "serial context-latency step does not use the suite environment"
        )
    _check_status(document)


def collect_output() -> tuple[str, int]:
    """Collect the latency node once, without running it."""
    completed = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            NODE_ID,
            "-n0",
            "--collect-only",
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    text = (completed.stdout or "") + (completed.stderr or "")
    return text, completed.returncode


def main(argv: list[str] | None = None) -> int:
    """Check the workflow, a collection log, or a serial-run log."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args == ["--check-workflow"]:
            check_workflow()
            return 0
        if len(args) == 2 and args[0] == "--require-collected":
            require_collected(Path(args[1]).read_text(encoding="utf-8"))
            return 0
        if len(args) == 2 and args[0] == "--require-passed":
            require_passed(Path(args[1]).read_text(encoding="utf-8"))
            return 0
    except LatencyCIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "usage: context_latency_ci.py --check-workflow | "
        + "--require-collected FILE | --require-passed FILE",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
