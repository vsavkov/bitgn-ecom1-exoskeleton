import json
import os
import sys
import threading
import textwrap
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    EvalPolicy,
    GetBenchmarkRequest,
    RunState,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
    TRIAL_STATE_DONE,
    TrialState,
)
from connectrpc.errors import ConnectError
from langsmith import Client as LangSmithClient

from agent import run_agent
from config import (
    CLI_BLUE,
    CLI_CLR,
    CLI_GREEN,
    CLI_RED,
    PROJECT_ROOT,
    env_flag,
    env_int,
    load_dotenv,
)


load_dotenv()

BITGN_URL = (
    os.getenv("BITGN_HOST")
    or os.getenv("BENCHMARK_HOST")
    or "https://api.bitgn.com"
)
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or os.getenv("BENCHMARK_ID") or "bitgn/ecom1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "gpt-4.1-2025-04-14"

RUNS_DIR = PROJECT_ROOT / "runs"
DEFAULT_TRIAL_BATCH_SIZE = 10
PRINT_LOCK = threading.Lock()


def _color(text: str, color: str) -> str:
    return f"{color}{text}{CLI_CLR}"


def _print_locked(message: str) -> None:
    with PRINT_LOCK:
        print(message)


def _chunks[T](items: list[T], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _format_task_report(output: dict) -> str:
    task_id = output.get("task_id") or "unknown"
    instruction = output.get("instruction") or ""
    lines = [
        f"{'=' * 30} Task: {task_id} {'=' * 30}",
        _color(instruction, CLI_BLUE),
        "-" * 80,
    ]

    formatter_output = output.get("formatter_output") or []
    lines.extend(formatter_output)

    completion_output = output.get("completion_output")
    if completion_output:
        lines.append(completion_output)

    error = output.get("error")
    if error:
        lines.append(_color(f"ERROR: {error}", CLI_RED))

    end_trial_error = output.get("end_trial_error")
    if end_trial_error:
        lines.append(_color(f"END TRIAL ERROR: {end_trial_error}", CLI_RED))

    if output.get("skipped"):
        lines.append("Skipped by task filter.")

    return "\n".join(lines)


def _flush_langsmith() -> None:
    if not env_flag("LANGSMITH_TRACING"):
        return
    if not os.getenv("LANGSMITH_API_KEY"):
        return

    try:
        LangSmithClient().flush(timeout=10)
    except Exception as exc:
        print(f"{CLI_RED}LangSmith flush failed: {exc}{CLI_CLR}")


def _enum_name(enum_type, value: int) -> str:
    try:
        return enum_type.Name(value)
    except ValueError:
        return str(value)


def _run_artifact_path(started_at: datetime) -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    base = RUNS_DIR / f"run_{started_at:%Y%m%d_%H%M%S}.json"
    if not base.exists():
        return base

    for index in range(2, 100):
        candidate = RUNS_DIR / f"run_{started_at:%Y%m%d_%H%M%S}_{index:02d}.json"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not choose a free run artifact path for {base}")


def _write_run_artifact(result, started_at: datetime, trial_outputs: dict[str, dict]) -> Path:
    finished_at = datetime.now().astimezone()
    test_cases = []

    for trial in result.trials:
        score_detail = list(trial.score_detail)
        agent_output = trial_outputs.get(trial.trial_id) or {}
        langsmith_trace_id = (
            agent_output.get("langsmith_trace_id") or agent_output.get("langsmith_run_id")
        )
        grader_comment = ""
        if trial.score != 1:
            grader_comment = "\n".join(score_detail).strip() or trial.error

        test_cases.append(
            {
                "task_id": trial.task_id,
                "task_text": agent_output.get("instruction") or "",
                "trial_id": trial.trial_id,
                "trace_id": langsmith_trace_id,
                "langsmith_trace_id": langsmith_trace_id,
                "langsmith_run_id": agent_output.get("langsmith_run_id"),
                "score": trial.score if trial.score_available else None,
                "score_available": bool(trial.score_available),
                "state": _enum_name(TrialState, trial.state),
                "score_detail": score_detail,
                "grader_comment": grader_comment,
                "error": trial.error,
            }
        )

    payload = {
        "schema_version": 2,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "benchmark_id": BENCH_ID,
        "model_id": MODEL_ID,
        "langsmith_project": os.getenv("LANGSMITH_PROJECT") or "",
        "bitgn_run_id": result.run_id,
        "run_state": _enum_name(RunState, result.state),
        "score": result.score if result.score_available else None,
        "score_available": bool(result.score_available),
        "test_cases": test_cases,
    }

    path = _run_artifact_path(started_at)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return path


def _run_trial(trial_id: str, task_filter: set[str], debug: bool) -> tuple[str, dict]:
    client = HarnessServiceClientSync(BITGN_URL)
    trial = None
    output: dict = {}
    should_end = False

    try:
        trial = client.start_trial(StartTrialRequest(trial_id=trial_id))
        output = {"task_id": trial.task_id, "instruction": trial.instruction}
        if task_filter and trial.task_id not in task_filter:
            output["skipped"] = True
            return trial.trial_id, output

        should_end = True
        if debug:
            _print_locked(
                f"{'=' * 30} Starting task: {trial.task_id} {'=' * 30}\n"
                f"{_color(trial.instruction, CLI_BLUE)}\n{'-' * 80}"
            )

        try:
            output.update(
                run_agent(
                    MODEL_ID,
                    trial.harness_url,
                    trial.instruction,
                    print_completion=debug,
                )
            )
        except Exception as exc:
            output["error"] = str(exc)
            if debug:
                _print_locked(_color(f"{trial.task_id}: {exc}", CLI_RED))

        return trial.trial_id, output
    except Exception as exc:
        key = trial.trial_id if trial else trial_id
        output = {"error": str(exc)}
        if debug:
            _print_locked(_color(f"{key}: {exc}", CLI_RED))
        return key, output
    finally:
        if should_end and trial is not None:
            try:
                client.end_trial(EndTrialRequest(trial_id=trial.trial_id))
            except Exception as exc:
                output["end_trial_error"] = str(exc)
                if debug:
                    _print_locked(
                        _color(f"{trial.task_id}: failed to end trial: {exc}", CLI_RED)
                    )


def main() -> None:
    started_at = datetime.now().astimezone()
    task_filter = sys.argv[1:]
    task_filter_set = set(task_filter)
    full_run = not task_filter
    trial_batch_size = env_int("TRIAL_BATCH_SIZE", DEFAULT_TRIAL_BATCH_SIZE, minimum=1)
    debug = env_flag("AGENT_DEBUG")
    trial_outputs: dict[str, dict] = {}

    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{_color(res.description, CLI_GREEN)}"
        )
        print(_color(f"Model: {MODEL_ID}", CLI_BLUE))
        print(_color(f"Trial batch size: {trial_batch_size}", CLI_BLUE))

        run = client.start_run(
            StartRunRequest(
                name=f"@dev_salikhov ecom1 {MODEL_ID}",
                benchmark_id=BENCH_ID,
                api_key=BITGN_API_KEY,
            )
        )

        try:
            benchmark_task_ids = [task.task_id for task in res.tasks]
            can_filter_before_start = len(benchmark_task_ids) == len(run.trial_ids)
            trial_plan = [
                (
                    trial_id,
                    benchmark_task_ids[index]
                    if index < len(benchmark_task_ids)
                    else trial_id,
                )
                for index, trial_id in enumerate(run.trial_ids)
            ]
            if task_filter_set and can_filter_before_start:
                trial_plan = [
                    (trial_id, task_id)
                    for trial_id, task_id in trial_plan
                    if task_id in task_filter_set
                ]

            for batch in _chunks(trial_plan, trial_batch_size):
                if not debug:
                    running = ", ".join(task_id for _, task_id in batch)
                    _print_locked(_color(f"Running {running}", CLI_BLUE))

                with ThreadPoolExecutor(max_workers=trial_batch_size) as executor:
                    futures = {
                        executor.submit(_run_trial, trial_id, task_filter_set, debug): trial_id
                        for trial_id, _ in batch
                    }
                    for future in as_completed(futures):
                        trial_id = futures[future]
                        try:
                            output_trial_id, output = future.result()
                        except Exception as exc:
                            output_trial_id = trial_id
                            output = {"error": str(exc)}
                            if debug:
                                _print_locked(_color(f"{trial_id}: {exc}", CLI_RED))
                        trial_outputs[output_trial_id] = output
                        if not debug and not output.get("skipped"):
                            _print_locked(_format_task_report(output))
        finally:
            _flush_langsmith()
            print(f"\n{_color('>>>> Submitting run... <<<<', CLI_GREEN)}")
            result = client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))
            if full_run:
                try:
                    artifact_path = _write_run_artifact(result, started_at, trial_outputs)
                    print(_color(f"Run artifact: {artifact_path}", CLI_BLUE))
                except Exception as exc:
                    print(f"{CLI_RED}Failed to write run artifact: {exc}{CLI_CLR}")

            if result.score_available:
                print(f"FINAL SCORE: {result.score:0.2f}")
                incomplete = 0
                for t in result.trials:
                    if t.state != TRIAL_STATE_DONE:
                        incomplete += 1
                        continue

                    style = CLI_GREEN if t.score == 1 else CLI_RED
                    explain = "\n" + textwrap.indent(
                        "\n".join(t.score_detail),
                        "  ",
                    ) + "\n"
                    print(
                        f"- {t.task_id}: {_color(f'Score: {t.score:0.2f}', style)}"
                        f"{explain}".strip("\n ")
                    )

                if incomplete > 0:
                    print(_color(f"incomplete trials: {incomplete}", CLI_RED))
            else:
                print(
                    _color(
                        "\nScore is not available. Results are sealed and "
                        "will be revealed later\n",
                        CLI_RED,
                    )
                )

    except ConnectError as exc:
        print(f"{exc.code}: {exc.message}")
    except KeyboardInterrupt:
        print(_color("Interrupted", CLI_RED))


if __name__ == "__main__":
    main()
