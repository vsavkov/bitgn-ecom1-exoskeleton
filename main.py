import os
import textwrap

from bitgn.harness_connect import HarnessServiceClientSync
from bitgn.harness_pb2 import (
    EndTrialRequest,
    EvalPolicy,
    GetBenchmarkRequest,
    StartRunRequest,
    StartTrialRequest,
    StatusRequest,
    SubmitRunRequest,
    TRIAL_STATE_DONE,
)
from connectrpc.errors import ConnectError
from langsmith import Client as LangSmithClient

from agent import run_agent
from config import load_dotenv


load_dotenv()

BITGN_URL = (
    os.getenv("BITGN_HOST")
    or os.getenv("BENCHMARK_HOST")
    or "https://api.bitgn.com"
)
BITGN_API_KEY = os.getenv("BITGN_API_KEY") or ""
BENCH_ID = os.getenv("BENCH_ID") or os.getenv("BENCHMARK_ID") or "bitgn/ecom1-dev"
MODEL_ID = os.getenv("MODEL_ID") or "gpt-4.1-2025-04-14"

CLI_RED = "\x1B[31m"
CLI_GREEN = "\x1B[32m"
CLI_CLR = "\x1B[0m"
CLI_BLUE = "\x1B[34m"


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


AGENT_DEBUG = _env_flag("AGENT_DEBUG")


def _color(text: str, color: str) -> str:
    return f"{color}{text}{CLI_CLR}"


def _flush_langsmith() -> None:
    if not _env_flag("LANGSMITH_TRACING"):
        return
    if not os.getenv("LANGSMITH_API_KEY"):
        return

    try:
        LangSmithClient().flush(timeout=10)
    except Exception as exc:
        print(f"{CLI_RED}LangSmith flush failed: {exc}{CLI_CLR}")


def main() -> None:
    task_filter = os.sys.argv[1:]

    try:
        client = HarnessServiceClientSync(BITGN_URL)
        print("Connecting to BitGN", client.status(StatusRequest()))
        res = client.get_benchmark(GetBenchmarkRequest(benchmark_id=BENCH_ID))
        print(
            f"{EvalPolicy.Name(res.policy)} benchmark: {res.benchmark_id} "
            f"with {len(res.tasks)} tasks.\n{_color(res.description, CLI_GREEN)}"
        )
        print(_color(f"Model: {MODEL_ID}", CLI_BLUE))

        run = client.start_run(
            StartRunRequest(
                name=f"@dev_salikhov ecom1 {MODEL_ID}",
                benchmark_id=BENCH_ID,
                api_key=BITGN_API_KEY,
            )
        )

        try:
            for trial_id in run.trial_ids:
                t = client.start_trial(
                    StartTrialRequest(trial_id=trial_id),
                )
                if task_filter and t.task_id not in task_filter:
                    continue

                print(f"{'=' * 30} Starting task: {t.task_id} {'=' * 30}")
                print(f"{_color(t.instruction, CLI_BLUE)}\n{'-' * 80}")
                try:
                    run_agent(MODEL_ID, t.harness_url, t.instruction)
                except Exception as exc:
                    print(_color(str(exc), CLI_RED))

                client.end_trial(EndTrialRequest(trial_id=t.trial_id))
        finally:
            _flush_langsmith()
            print(f"\n{_color('>>>> Submitting run... <<<<', CLI_GREEN)}")
            result = client.submit_run(SubmitRunRequest(run_id=run.run_id, force=True))

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
