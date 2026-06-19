# BitGN ECOM1 agent — Exoskeleton

An AI agent for the [BitGN ECOM1](https://bitgn.com/challenge/ecom) agentic-commerce challenge: a hundred tasks inside a simulated e-commerce operating system (catalog, carts, checkout, payments, fraud, refunds, support).

The agent runs a small model (`gpt-5.4-mini`, `gpt-5.4-nano`) inside a deterministic engineering harness — **Exoskeleton**: the model dispatches, while deterministic code computes the heavy domain logic, grounds the evidence, holds the exact answer format, and guards the security boundaries. The guiding principle — **the model proposes, the code disposes.**

**Results** — agent `@dev_salikhov ecom1 gpt-5.4-mini`:

- **1st** — Live PROD leaderboard (at the time of writing)
- **1st** — Hall of Fame: Speed
- **10th** — Hall of Fame: Ultimate
- **18th** — Hall of Fame: Accuracy

**Architecture write-up:**
- [ARCHITECTURE.md](articles/ARCHITECTURE.md) (english)
- [ARCHITECTURE_RU.md](articles/ARCHITECTURE_RU.md) (russian)

**Open-model research** — can open-weight models replace `gpt-5.4-mini` in this agent? Ten families benchmarked on quality, time, and cost:
- [OPEN_MODELS_RESEARCH.md](articles/OPEN_MODELS_RESEARCH.md) (english)
- [OPEN_MODELS_RESEARCH_RU.md](articles/OPEN_MODELS_RESEARCH_RU.md) (russian)

**Author:** [@dev_salikhov on Telegram](https://t.me/dev_salikhov)

Leaderboard: [https://bitgn.com/challenge/ecom](https://bitgn.com/challenge/ecom)

## Setup

1. Copy `.env.example` to `.env` and fill
2. Run `make sync`
3. Run `make run`

## Commands

- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t04"`
- Generate a local run score heatmap: `make runs-html`

Full benchmark runs save ignored local artifacts to
`runs/<bench-id-with-__-for-slashes>/run_<date>_<time>.json`.
`make run` regenerates the ignored benchmark heatmap at
`runs/<bench-id-with-__-for-slashes>.html` after the benchmark completes. The heatmap
command can also render those artifacts manually.

Useful environment overrides:

- `BITGN_API_KEY` is required for official ECOM benchmark runs
- `BENCH_ID` or `BENCHMARK_ID` defaults to `bitgn/ecom1-dev`
- `MODEL_ID` can be set in `.env`; this checkout currently uses `gpt-5.4-mini`
- `HELPER_MODEL` controls helper agents such as the answer formatter and catalog parser; default is `gpt-5.4-nano`
- `HELPER_REASONING_EFFORT` controls helper-agent reasoning; default is `low`
- `AGENT_MAX_STEPS` caps response/tool iterations per trial; default is `75`
- `AGENT_RUNTIME_TIMEOUT_MS` caps each runtime filesystem/tool call; default is `300` ms, set `0` to disable
- `AGENT_AUTO_HELP_TIMEOUT_MS` caps auto-discovered `<command> --help` calls; default is `300` ms
- `TRIAL_BATCH_SIZE` controls concurrent trials during `make run`; default is `10`
- `OPENAI_TIMEOUT_SECONDS` caps each OpenAI request; default is `40`
- `OPENAI_MAX_RETRIES` controls OpenAI SDK retries; default is `1`
- Exported shell variables take precedence over values from `.env`
