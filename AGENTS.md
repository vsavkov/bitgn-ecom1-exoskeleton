# AGENTS.md

This repository contains an agent being developed for the ECOM1 challenge for agentic ecommerce.

Quote from the challenge website:

> You write an agent, connect it to BitGN via API, and solve tasks inside a deterministic simulated commercial environment. BitGN evaluates observable outcomes such as tool calls, state changes, required flags/references, and forbidden actions avoided.

The agent works through an API in a special sandbox. It receives different test tasks that simulate conversations between a customer or employee and the agent.
Each task is scored. The test results are used to build a leaderboard with the best scores.

You should help me develop and improve the agent so it earns the highest possible score.
At the same time, do not tune the agent to specific tasks. Do not rely on regex patches or similar hacks.
Think through architectural improvements and use run results to identify nuances and rules.

**Important**
* `runs/bitgn__ecom1-dev` contains the run history from the tuning period on `BENCH_ID=bitgn/ecom1-dev`.
* `runs/bitgn__ecom1` contains the run history from the current improvement cycle for the active challenge `BENCH_ID=bitgn/ecom1-prod`.

**We are currently improving** for `BENCH_ID=bitgn/ecom1-prod`.
Use commit history and dev run history to understand what changed, when, and why.
In the PROD challenge, the OS can change quite a lot between runs of the same tasks.
Therefore, analyze the history of runs for the same task carefully.
Tasks are not the same between `ecom1-dev` and `ecom1-prod`. `t01` in one benchmark is not the same as `t01` in the other.

## Commands

- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t04"`
- Check linting and typing after any code changes: `make check`
- Run unit tests after any code changes: `make test`

After any Python-code or project-configuration changes, run `make check test`.

Do not run task benchmarks on your own, to avoid spending run limits.

## Tests

- Place test files next to the corresponding source files: `module.py` is covered by `tests/test_module.py`, scripts from `scripts/foo.py` are covered by `tests/scripts/test_foo.py`.
- Cover pure functions and deterministic helpers with unit tests that do not use external APIs, the BitGN runtime, or LangSmith. Use fake/stub objects for runtime adapters.

## Commits

Write commits with detailed explanations of what they include.

## BITGN Architecture

Important observations to keep in mind when improving the agent:

- The challenge consists of `benchmark -> run -> trial`. `get_benchmark` provides descriptions and preview/hint text for tasks, but the concrete instructions inside `start_run/start_trial` can be parameterized differently; do not tune to exact preview text.
- Each trial receives its own runtime URL and an isolated snapshot of the ECOM OS. Actions are scored by observable results: runtime tool calls, state changes, the final `answer`, grounding refs, the correct outcome, and avoidance of forbidden mutations.
- `StartPlayground` exists in the SDK, but the server replies that sandbox mode is no longer supported. Use a normal run/trial to explore the environment; such trials must be closed carefully and should not be treated as productive score runs.
- The runtime looks like a Unix-like filesystem rooted at `/`, for example: `/AGENTS.MD`, `/docs`, `/proc`, `/bin`, `/run/actions`. All paths passed to runtime tools must be absolute.
- Grounding refs matter for scoring.
- Do not hard-code assumptions about folder structure in code; it can change.

## LangSmith Trace Analysis

To analyze an already completed run, use the read-only helper `scripts/langsmith_trace_report.py`; it reads LangSmith traces and does not start BitGN run/trial.

- List recent root traces: `uv run python scripts/langsmith_trace_report.py --limit 80`.
- Analyze several tasks by index: `uv run python scripts/langsmith_trace_report.py --limit 80 --indices 3,6,14-16`.
- Detailed analysis with child LLM/tool spans: `uv run python scripts/langsmith_trace_report.py --limit 80 --indices 38-40 --children --output-limit 3000`.
- Analyze one trace by id: `uv run python scripts/langsmith_trace_report.py --run-id <RUN_ID> --children`.
- Helper indices follow the ordering of root traces by `start_time`; before mapping them to `tXX`, check whether there were earlier single-task runs in the same project.

## Run Reports

Store run reports in `reports/report_run<N>_<YYYYMMDD_HHMMSS>.md`.

- At the top, specify the data source: user-provided score output, LangSmith project, root span/revision range, and the fact that no BitGN runs were started during analysis.
- Add a summary: final score, number of full/zero/partial tasks, and the main groups of problems.
- Add an overview table for all tasks: task, score, detail from grader output, category.
- For failed and partial cases, describe: affected task ids, trace observations, root cause, and a generalizable proposal for improving the agent.
- Do not tune conclusions to specific SKUs, basket IDs, payment IDs, or customer IDs; use them only as evidence in the report, and formulate proposals as general rules for agent behavior.
