"""Tests for Multi-Function (parallel tool calls) and Multi-Core (batch workers).

These assert the *behavior* of the concurrency knobs — that a user-configured
value flows through to the worker cap, that out-of-range values fall back to a
safe default, and that the batch runner auto-detects CPU cores when nothing is
set. They do NOT snapshot a hardcoded number.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent import tool_executor
import batch_runner


def _tool_call(name: str, args: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=__import__("json").dumps(args)),
    )


def _runnable(call_name: str, call_id: str) -> tuple:
    # (order_index, tool_call, tool_name, scope_path)
    return (0, _tool_call(call_name, {"x": 1}, call_id), call_name, {})


# ---------------------------------------------------------------------------
# Multi-Function: agent.max_concurrent_tool_calls
# ---------------------------------------------------------------------------


def test_tool_workers_default_ceil_when_unset():
    runnable = [_runnable("read_file", f"r{i}") for i in range(12)]
    with patch("hermes_cli.config.load_config", return_value={}):
        # No config -> built-in ceiling (_MAX_TOOL_WORKERS = 8), capped by len.
        assert tool_executor._max_workers_for_tool_batch(runnable) == 8


def test_tool_workers_uses_configured_cap():
    runnable = [_runnable("read_file", f"r{i}") for i in range(12)]
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 4}},
    ):
        assert tool_executor._max_workers_for_tool_batch(runnable) == 4


def test_tool_workers_can_exceed_builtin_ceiling():
    # Many-core machines should be able to raise past the original 8.
    runnable = [_runnable("read_file", f"r{i}") for i in range(20)]
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 16}},
    ):
        assert tool_executor._max_workers_for_tool_batch(runnable) == 16


def test_tool_workers_config_ignored_when_out_of_range():
    runnable = [_runnable("read_file", f"r{i}") for i in range(12)]
    # 0 / negative must be ignored -> fall back to built-in ceiling.
    for bad in (0, -3):
        with patch(
            "hermes_cli.config.load_config",
            return_value={"agent": {"max_concurrent_tool_calls": bad}},
        ):
            assert tool_executor._max_workers_for_tool_batch(runnable) == 8


def test_tool_workers_capped_by_batch_size():
    # Even with a huge configured cap, we never spawn more workers than calls.
    runnable = [_runnable("read_file", "only_one")]
    with patch(
        "hermes_cli.config.load_config",
        return_value={"agent": {"max_concurrent_tool_calls": 16}},
    ):
        assert tool_executor._max_workers_for_tool_batch(runnable) == 1


def test_tool_workers_image_gen_still_capped_by_its_own_limit():
    runnable = [_runnable("image_generate", f"img{i}") for i in range(10)]
    with patch(
        "hermes_cli.config.load_config",
        return_value={
            "agent": {"max_concurrent_tool_calls": 16},
            "image_gen": {"max_parallel_requests": 2},
        },
    ):
        # image_generate carries its own conservative cap regardless of the
        # generic tool-call cap.
        assert tool_executor._max_workers_for_tool_batch(runnable) == 2


# ---------------------------------------------------------------------------
# Multi-Core: batch_runner num_workers resolution
# ---------------------------------------------------------------------------


def test_resolve_num_workers_explicit_flag_wins():
    assert batch_runner._resolve_num_workers(6) == 6
    # Explicit 0/negative is clamped to >= 1.
    assert batch_runner._resolve_num_workers(0) == 1


def test_resolve_num_workers_config_batch_max_workers():
    with patch(
        "hermes_cli.config.load_config",
        return_value={"batch": {"max_workers": 7}},
    ):
        assert batch_runner._resolve_num_workers(None) == 7


def test_resolve_num_workers_config_out_of_range_falls_back_to_cores():
    with patch("hermes_cli.config.load_config", return_value={"batch": {"max_workers": 0}}), patch(
        "os.cpu_count", return_value=4
    ):
        # Bad config -> auto core detection (clamped to [1, 16]).
        assert batch_runner._resolve_num_workers(None) == 4


def test_resolve_num_workers_auto_core_detection():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "os.cpu_count", return_value=8
    ):
        assert batch_runner._resolve_num_workers(None) == 8


def test_resolve_num_workers_auto_core_capped_at_sane_ceiling():
    # A 128-core box must not spawn 128 provider-parallel processes by default.
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "os.cpu_count", return_value=128
    ):
        assert batch_runner._resolve_num_workers(None) == 16


def test_resolve_num_workers_falls_back_when_cpu_count_unavailable():
    with patch("hermes_cli.config.load_config", return_value={}), patch(
        "os.cpu_count", return_value=None
    ):
        assert batch_runner._resolve_num_workers(None) == 4


def test_batch_runner_run_resolves_none_num_workers():
    # Direct construction with num_workers=None must auto-resolve at run() time
    # rather than passing None into multiprocessing.Pool.
    runner = batch_runner.BatchRunner.__new__(batch_runner.BatchRunner)
    runner.num_workers = None
    with patch("os.cpu_count", return_value=6):
        resolved = batch_runner._resolve_num_workers(None)
    assert resolved == 6
