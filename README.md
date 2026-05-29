# BitGN ECOM Python Sample

Runnable Python sample for the `bitgn/ecom1-dev` benchmark.

Watch the leaderboard here - [https://bitgn.com/challenge/ecom](https://bitgn.com/challenge/ecom)

ECOM is an ecommerce operations runtime. It exposes a file-shaped workspace plus runtime tools such as `/bin/sql` over the `bitgn.vm.ecom` API.

You will need to provide your own `BITGN_API_KEY` and `OPENAI_API_KEY`, or swap the OpenAI client for a provider of your choice.

## Setup

1. Copy `.env.example` to `.env`
2. Fill in `BITGN_API_KEY` and `OPENAI_API_KEY`
3. Set `MODEL_ID` in `.env` when switching models
4. Run `make sync`
5. Run `make run`

## Commands

- Run the full ECOM benchmark: `uv run python main.py`
- Run a single task: `uv run python main.py t01`
- Run a subset of tasks: `uv run python main.py t01 t04`
- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t04"`
- Generate a local run score heatmap: `make runs-html`

Full benchmark runs save ignored local artifacts to `runs/run_<date>_<time>.json`.
The heatmap command renders those artifacts to ignored `runs.html` in the project root.

Useful environment overrides:

- `BITGN_API_KEY` is required for official ECOM benchmark runs
- `BENCH_ID` or `BENCHMARK_ID` defaults to `bitgn/ecom1-dev`
- `MODEL_ID` can be set in `.env`; this checkout currently uses `gpt-5.4-mini`
- `TRIAL_BATCH_SIZE` controls concurrent trials during `make run`; default is `10`
- `OPENAI_TIMEOUT_SECONDS` caps each OpenAI request; default is `40`
- `OPENAI_MAX_RETRIES` controls OpenAI SDK retries; default is `1`
- Exported shell variables take precedence over values from `.env`
