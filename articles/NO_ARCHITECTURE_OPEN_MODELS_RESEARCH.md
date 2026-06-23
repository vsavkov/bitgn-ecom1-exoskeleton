# Open Models on ECOM1 — No-Architecture Run

*A companion to [OPEN_MODELS_RESEARCH.md](https://github.com/muxx/bitgn-ecom1-exoskeleton/blob/main/articles/OPEN_MODELS_RESEARCH.md)
(the "Exoskeleton" study), measured on the same `bitgn/ecom1-prod` benchmark — but with a
**single-model, no-exoskeleton solver**.*

## Context

Can an open-weight model run a real agentic ecommerce-ops workload well enough to replace a
proprietary frontier model? The Exoskeleton study answered this with a multi-model architecture
(a strong "main" model paired with a "helper" model, plus elaborate scaffolding). This run asks
the same question with the opposite setup: **one model drives the entire agent loop**, backed only
by deterministic helper *tools* and per-task routing — no second model, no exoskeleton. The point
is to isolate **what the model itself contributes** when the architecture is minimal.

## TL;DR

> - **deepseek-v4-pro ≈ 89.6 / 100** — the best open model tested, within ~5 pts of gpt-5.5 and
>   effectively at the original 90 target, for **$0.46/run** (~22× cheaper than gpt-5.5's $10.04).
> - **The two things that decide an open model's score are *reasoning* and *completion discipline*** —
>   not the architecture. Reasoning lifts judgment (Qwen3-Thinking ≫ Instruct); reliably calling the
>   finish tool separates a usable model (GLM, DeepSeek: 99–100% completion) from a stalled one
>   (gpt-oss: 70%).
> - **A local model now reaches 83 and beats cloud.** **Gemma-4-31B (dense, thinking) = 83.3** on a
>   single DGX Spark — **above gpt-5.4-mini (71.8) and deepseek-v4-flash (77.1)**, behind only
>   deepseek-pro and gpt-5.5. The "local ceiling" jumped from ~68 (GLM/Gemma-A4B) to ~83. Cost: it's
>   slow (~316 s/task, ~130 min/run, concurrency ≤ 4) but $0 and the data never leaves the box.
> - **"Thinking" models can ship with reasoning off.** Gemma-4 scored 49.9 looking non-reasoning, 67.0
>   (A4B) / 83.3 (31B) with `enable_thinking` turned on server-side — always check the default. (And a
>   contiguous 25-task probe scored the 31B at 64%, ~19 pts low — **confirm with full runs, not subsets**.)
> - **No open model needs an exoskeleton to be useful here.** A single capable model + deterministic
>   tools gets DeepSeek-pro to ~90. The residual gap to gpt-5.5 is model class, not scaffolding.

## The agent and the benchmark

**ECOM1 / `bitgn/ecom1-prod`** is a 100-task agentic benchmark over an ecommerce-ops filesystem
(catalogue lookup, inventory/availability reasoning, policy-source authority, basket checkout,
discount/refund authorization, 3DS payment recovery, archived-payment fraud review, dispatch-wave
planning, and security/injection handling). Each task is graded deterministically on **outcome
token + grounding refs + value**; any wrong component scores 0 (mostly binary, a few partial-credit
like dispatch). The set **re-seeds every run**, so single-run aggregates carry ±5–7 noise.

**The solver (no exoskeleton).** One model runs a native tool-calling loop (pi SDK): the workspace
file tools plus a handful of **deterministic helpers** (`catalog_find`, `availability_check`,
`fraud_scan`, `dispatch_plan`, `basket_resolve`) that compute load-bearing answers + grounding refs,
and a `report_completion` tool to finish. Per-task routing narrows the toolset. A set of
**deterministic, model-agnostic aids** (citation reconstruction from helper outputs, policy-doc
augmentation, injection-deny guidance, a no-completion re-prompt/salvage) is gated to local open
models only — the cloud baselines run untouched. There is **no main/helper model split** — the same
model does everything.

## Methodology

- **Measured per model:** score (avg/max/min over N runs), completion rate, BitGN per-task time
  (`elapsedMs`), and $/run where the model is paid-API.
- **Single-model, not strong/strong pairing.** Unlike the Exoskeleton's main+helper design, every
  task — planning, tool use, grounding, finishing — is the one model. This is the *floor*
  architecture: it measures the model, not an orchestration.
- **Runs & validity.** A valid run = a complete 100-task execution. Run counts vary (1–10) by how
  much a model was studied; multi-run means are used where available, and the **±5–7 re-seed noise**
  is treated as real (e.g. GLM's headline 67.3 ± 0.6 is a 4-run mean, not a lucky draw).
- **Solver evolved during the campaign** (a caveat the Exoskeleton's fixed harness doesn't have):
  earlier models (Qwen3 Instruct/Thinking, gpt-oss) ran on fewer deterministic aids than the later
  ones (GLM, DeepSeek). The aids are worth a few points, so the *earliest* models are mildly
  understated — but the ranking gaps are far larger than that.
- **Provider/compat.** Local models served via vLLM on a DGX Spark (NVFP4/MXFP4/FP8); cloud models
  via their native endpoints (`openai-codex`, DeepSeek). Tool-calling = native function calling
  (`--enable-auto-tool-choice`); reasoning models reason natively. Provider pinned per model id for
  reproducibility. Serving recipes in `README-local-models.md`.

## Key results

**Leaderboard** (open models ranked by avg score; baselines for reference):

| Class | Model | Score avg | max | min | Completion | Time/task | Cost/run |
|---|---|---:|---:|---:|---:|---:|---:|
| baseline (cloud) | **gpt-5.5** (low) | 90.8¹ | — | — | 98% | 25 s | **$10.04** |
| **open (cloud)** | **deepseek-v4-pro** | **89.6** | 90.0 | 89.3 | 99% | 28 s | **$0.46** |
| **open (local)** | **Gemma-4-31B-IT** (thinking) | **83.3** | 84.9 | 80.5 | **100%** | 316 s | **$0 API²** |
| open (cloud) | **deepseek-v4-flash** | **77.1** | 79.1 | 75.1 | 95% | 26 s | **$0.17** |
| baseline (cloud) | gpt-5.4-mini (xhigh) | 71.8 | 79.3 | 67.7 | 92% | 69 s | $3.64 |
| **open (local)** | **GLM-4.5-Air** | **67.7** | 69.2 | 66.0 | **100%** | 520 s | $0 API² |
| **open (local)** | **Gemma-4-26B-A4B** (thinking) | **67.0** | 70.1 | 65.4 | 96% | 233 s | $0 API² |
| open (local) | gpt-oss-120b | 52.8 | — | — | 70% | 149 s | $0 API² |
| open (local) | Qwen3-Next-80B-A3B-Thinking | 51.5 | 52.4 | 50.6 | 95% | 443 s | $0 API² |
| open (local) | Qwen3-Next-80B-A3B-Instruct | 40.6 | 43.1 | 36.7 | 87% | 210 s | $0 API² |

¹ gpt-5.5's measured cost-probe run was at `low` effort (90.8); at higher effort the reference is
~94.8. ² Local = free per run on owned hardware, but ~40–110 min wall-clock/run (bandwidth-bound).

![ECOM1/prod score leaderboard — deepseek-v4-pro 89.6 leads the open models, GLM-4.5-Air 67.7 the local ones](images/leaderboard.svg)

**Takeaways.**
- **One open model reaches frontier-adjacent quality without an architecture.** deepseek-v4-pro
  (89.6) sits between gpt-5.4-mini and gpt-5.5, completes 99%, and its residual is the solver's own
  structural ceilings (dispatch, citation) — not a capability gap a helper model would close.
- **A clean ladder by class — but local now reaches higher than expected:** small/MoE local (Qwen3,
  gpt-oss, GLM, Gemma-A4B) tops out ~52–68; **dense Gemma-4-31B local jumps to ~83**, into mid-cloud
  territory (above gpt-5.4-mini 72 and deepseek-flash 77); large cloud (deepseek-pro, gpt-5.5) ~90–95.
  Architecture didn't move a model between rungs; **active-parameter count did** — the dense 31B's
  ~31B-active vs the MoE locals' 3–12B is the whole story (paid for in wall-clock, not dollars).
- **Completion discipline is a hard gate**, separate from intelligence: gpt-oss is *capable*
  (76% pass-rate *when it completes*) but only completes 70% of tasks, so it scores like the weak
  Qwen3-Thinking. GLM and DeepSeek complete 99–100% and convert their capability into score.

**Decision matrix.**

| Scenario | Pick | Why |
|---|---|---|
| Max quality | gpt-5.5 (94.8) | Highest, if the ~$10/run is fine. |
| **Best quality / dollar** | **deepseek-v4-pro** | ~90 at $0.46/run — ~22× cheaper than gpt-5.5 for ~5 fewer points. |
| Cheap + fast cloud | deepseek-v4-flash | 77, ~26 s/task, $0.17/run. |
| **Local — best quality** | **Gemma-4-31B-IT** (thinking) | **83.3**, $0 API, data stays on the box — beats gpt-5.4-mini & deepseek-flash. Slow: ~316 s/task, ~130 min/run, concurrency ≤ 4. |
| **Local — best speed** | **Gemma-4-26B-A4B** (thinking) or **GLM-4.5-Air** | ~67, $0 API, ~3× the 31B's throughput. Enable `enable_thinking` for the A4B. |

![Quality vs cost per run — deepseek-v4-pro reaches ~90 at $0.46 while gpt-5.4-mini costs $3.64 for only 71.8; local models are free but lower](images/quality-vs-cost.svg)

## Cross-cutting error classes

Inverting the per-model view — *where does each failure class show up* (● frequent ≳5/run,
○ occasional 1–5/run, blank ≲1):

| Error class | Instruct | Thinking | gpt-oss | GLM-Air | Gemma | Gemma31 | ds-flash | ds-pro | 5.4-mini | gpt-5.5 |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Completion failure (no `report_completion`) | ● | ○ | ●● | | ○ | | ○ | ○ | ○ | ○ |
| Citation / grounding (missing·extra·wrong ref) | ● | ● | ● | ● | ●● | ● | ● | ○ | ● | ○ |
| Security under-denial (obeys injection) | ● | ○ | | ○ | ○ | ○ | | | | |
| Arithmetic / value (wrong count·amount·date) | ● | ○ | | ○ | ○ | ○ | | ○ | ● | ○ |
| Outcome judgment (OK vs clarify vs unsupported) | ● | ○ | ○ | ○ | ○ | ○ | | ○ | | ○ |
| Dispatch sub-optimal (shared solver ceiling) | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Fraud detection | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |

![Cross-cutting error matrix — avg failures per run by class and model; gpt-oss completion 30 and gpt-5.4-mini arithmetic 14.7 stand out, deepseek-v4-pro and gpt-5.5 columns are clean](images/error-matrix.svg)

Reading it: **citation is everyone's tax** — the one error class no model escapes (which record is
load-bearing is genuine judgment). **Completion failure is gpt-oss-specific** (a serving/harmony
stall, not capability). **Security under-denial is a *small-model* problem** (Instruct/Thinking
obey injections; gpt-5.4-mini and the DeepSeeks reason through them). **Arithmetic is gpt-5.4-mini's
signature weakness** (14.7/run — it even miscomputed "yesterday's date"). **Dispatch is a shared
*solver* ceiling** — every model, including gpt-5.5, loses ~0.85/run there because `dispatch_plan`'s
min-cost routing isn't profit-optimal; that's architecture, not model.

## Economics

| Model | Cost/run | Driver | Score |
|---|---:|---|---:|
| gpt-5.5 (low) | **$10.04** | input ($7.07) — tiny output at low effort | 90.8 |
| gpt-5.4-mini (xhigh) | **$3.64** | **output ($2.35)** — xhigh = 9.5× the reasoning tokens | 71.8 |
| deepseek-v4-pro | **$0.46** | output (reasoning) at $0.87/M | 89.6 |
| deepseek-v4-flash | **$0.17** | $0.28/M output | 77.1 |
| local (Qwen3 / gpt-oss / GLM) | **$0 API** | owned DGX Spark; ~40–110 min/run | 40–68 |

Why "cheap tokens ≠ cheap runs," and the inverse:
- **gpt-5.5 at `low` is input-bound**: 1.4M uncached input @ $5/M dominates; prompt-caching (2.65M
  cache-reads @ $0.5/M) saves ~$12/run, so the *bill is feeding context*, not generating.
- **gpt-5.4-mini at `xhigh` flips to output-bound**: the high effort emits ~521K reasoning tokens
  (9.5× gpt-5.5-low's 55K) — effort spends in the *output* column.
- **DeepSeek's pricing is ~1/11 (input) to ~1/34 (output) of gpt-5.5's**, with cache-hits near-free
  ($0.003625/M for pro), so even with heavy reasoning a **pro run costs $0.46 and a flash run $0.17** —
  ~22× and ~59× cheaper than gpt-5.5 respectively. (Measured separately; the cost probe in this
  solver couldn't re-run them — the API key was revoked.)
- **Local models are $0 per run but not free**: ~273 GB/s memory bandwidth makes them 40–110 min/run
  (vs minutes for cloud at concurrency 32). The cost moves from $ to wall-clock + hardware.

## Conclusions and recommendations

1. **You do not need an exoskeleton to get a usable open model on ECOM1.** A single model + a few
   deterministic helper tools puts deepseek-v4-pro at ~90 and GLM-4.5-Air at ~68. The architecture's
   job here is to *not get in the model's way* (reliable tool-calling, deterministic grounding) — not
   to compensate for the model.
2. **Pick by class, not by tuning — and "local" reaches higher than we thought.** The rungs map to
   model class (active-param count), but the local ceiling is **not** ~68: the dense Gemma-4-31B hits
   **~83**, above two cloud models. Tuning still doesn't move a model between rungs; *more active
   params* does — paid for in wall-clock on the Spark, not dollars.
3. **deepseek-v4-pro is the value pick** — ~90 at a fraction of gpt-5.5's cost — if cloud + a
   non-OpenAI provider are acceptable. **For local/data-residency, Gemma-4-31B (~83) is the new top
   pick** if you can afford ~130 min/run; **GLM-4.5-Air or Gemma-4-A4B (~67)** when you need ~3× the
   throughput. **gpt-5.5 only if you need the last ~5 points.**
4. **Watch completion discipline before capability** when screening a new model: gpt-oss is the
   cautionary tale — strong per-task, but a serving-level stall capped it 20 points below its ability.

---

## Family-by-family breakdown

### deepseek-v4-pro — best open model (≈89.6)
- **What it is.** DeepSeek V4 "pro", reasoning model, OpenAI-compatible API; reasons natively (no
  effort knob). Pricing ~$0.435/M in (cache-miss), $0.87/M out, cache-hit near-free.
- **Numbers.** 2 runs: 89.3 / **90.0** (avg 89.6), 99% completion, ~28 s/task, **$0.46/run**.
- **What it does well.** Everything the small models fail: security (0.8 under-denials/run), arithmetic
  (wrong-value ~1/run), completion (99%). It converts capability into score with no crutches.
- **Where it stumbles.** Only the solver's *own* ceilings. e.g. dispatch `wave-UJhcZNTa` → score 0.81,
  all 10 packages delivered but EUR 424 margin vs the optimum — it submitted `dispatch_plan`'s plan
  faithfully; the loss is the helper's min-cost≠max-profit routing, not the model.
- **Verdict.** Production-viable as a gpt-5.5 stand-in at **~1/22 the cost** ($0.46 vs $10.04); the remaining gap is the
  benchmark's structural ceilings, not the model. *(Notably, the Exoskeleton study scored DeepSeek-V4-Pro
  at 0.615 — this single-model solver gets 0.896 from the same model, a large delta worth flagging.)*

### deepseek-v4-flash — fast & cheap (≈77.1)
- **What it is.** DeepSeek V4 "flash", reasoning, ~$0.14/M in / $0.28/M out.
- **Numbers.** 2 runs: 75.1 / 79.1 (avg 77.1), 95% completion, ~26 s/task (fastest measured), **$0.17/run**.
- **What it does well.** Clears the entire local field; strong security/arithmetic like its pro sibling.
- **Where it stumbles.** Citation precision (10.5/run) — e.g. "need code: bare stihl hsa 50… sku only"
  → answered `PT-HDG-STI-HSA50-BODY` (correct SKU) but **over-cited** sibling variants
  (`…-AK10.json` …), an extra-reference failure. A few not-completions (5.5/run).
- **Verdict.** The cost/speed sweet spot below pro; loses ~12 pts to pro mostly on citation discipline.

### GLM-4.5-Air — best local (≈67.7)
- **What it is.** Zhipu GLM-4.5-Air, agentic reasoning MoE (~106B/12B-active), FP8 on the DGX Spark.
- **Numbers.** 6 runs (gen13–14): avg 67.7 (range 66.0–69.2; 4-run gen13 mean 67.3 ± 0.6), **100%
  completion**, ~520 s/task (~110 min/run), $0 API.
- **What it does well.** Completion discipline — the only model at a clean 100%, never stalls. Best
  judgment of the locals; benefits most from the deterministic citation/security aids (+~28 over its
  own raw baseline across the solver gens).
- **Where it stumbles.** Citation is the wall (14.7/run): e.g. "do you have 1 of 'bosch gex 125
  accessory set' (but not PT-SND-BOS-GEX125-DUST)…" → answered `TRUE(1)` but **omitted the
  load-bearing excluded SKU** from refs. The residue is *which record is load-bearing* — judgment, not
  plumbing.
- **Verdict.** The local pick: data stays on the box, $0/run, ~68 — at the price of ~110 min/run. The
  Spark's bandwidth, not the model, is the ceiling.

### Gemma-4-26B-A4B — reasoning, if you turn it on (≈67.0)
- **What it is.** Google Gemma 4, multimodal MoE (25.2B/**3.8B-active**), NVFP4 on the DGX Spark — the
  most bandwidth-friendly capable local model (~40 tok/s no-think). **A reasoning model whose thinking
  is OFF by default.**
- **Numbers.** 3 runs **thinking-on / temp 1.0**: 65.5 / 65.4 / 70.1 → avg **67.0 ± 2.2**, 96%
  completion, ~233 s/task, $0 API. **No-think/greedy baseline: 49.9** — so reasoning is worth **+17**.
- **What it does well.** Reasoning lifts judgment to GLM's level *and* fixes completion (87% → 96% —
  it thinks its way to finishing). Fast per token; the lift comes from the thinking, not scale.
- **Where it stumbles.** **Citation is the wall (19.3/run — the worst of the field)**: it reasons to a
  correct answer but over/under-cites which record is load-bearing. Otherwise clean (security/arith/
  outcome all ≤1.7).
- **The trap.** `enable_thinking` is off by default and pi can't set `chat_template_kwargs`, so you
  must enable it **server-side** (`--default-chat-template-kwargs '{"enable_thinking": true}'`) — miss
  it and you measure 49.9, a 17-pt artifact. Greedy (`temperature 0`) also *hurts* a reasoning model;
  use Gemma's native `temp 1.0`.
- **Verdict.** Co-best local with GLM (~67), reached a different way — fewer active params + reasoning
  vs GLM's bigger-but-bandwidth-bound MoE. ~2× faster than GLM per run, but thinking is ~4.4× its own
  no-think speed.

### Gemma-4-31B-IT — the best local model (≈83.3)
- **What it is.** The **dense** Gemma 4 (~31B active, *not* MoE), multimodal, NVFP4 (~16 GB) on the
  DGX Spark. Same reasoning model + `gemma4` parsers as the A4B; thinking must be enabled server-side.
- **Numbers.** 3 full runs (thinking on): 84.5 / 80.5 / 84.9 → **83.3 ± 2.0**, **100% completion**,
  ~316 s/task, $0 API.
- **What it does well.** Everything the smaller locals can't: **+16 over the A4B/GLM and it beats two
  cloud models** (gpt-5.4-mini 71.8, deepseek-v4-flash 77.1) — the dense model's extra active params
  convert straight to score. The cleanest local profile here: completion ~0 failures, security/arith/
  outcome all ≤1.7. Only deepseek-pro and gpt-5.5 outscore it.
- **Where it stumbles.** Citation (8.3/run) — the same load-bearing-record judgment that walls every
  model, just milder. The shared dispatch ceiling (5.0) accounts for most of the rest.
- **The cost.** Dense → ~7 tok/s on the Spark's bandwidth → **~316 s/task, ~130 min/run, and a hard
  concurrency ≤ 4** (at 8, the saturated server returns empty on every task with no error logged).
- **Watch-out.** A 25-task probe (`t001–t025`) scored only **64%** and nearly buried this result —
  that contiguous slice ran ~19 pts harder than the full set. **Subset probes can lie; confirm with
  full runs.**
- **Verdict.** The new local ceiling — **~83, frontier-adjacent, on a single workstation**. Use it
  when you want the best local quality and can afford the hours; use the A4B (~67, ~3× faster) when
  you need throughput.

### gpt-oss-120b — capable but stalls (≈52.8)
- **What it is.** OpenAI open-weight 120B/5.1B-active MoE, MXFP4, harmony format, on the Spark.
- **Numbers.** 1 run: 52.8, **70% completion**, ~149 s/task, $0 API.
- **What it does well.** Highest per-task capability of the locals — **76% pass-rate on the tasks it
  finishes**. When it answers, it answers well.
- **Where it stumbles.** **Completion (30/run).** On a third of tasks it reasons the answer but stays
  in the harmony *analysis* channel and never emits a tool call / final answer — content comes back
  empty, unsalvageable. e.g. "complete checkout for basket-0025" → no answer recorded. Survived every
  fix (re-prompt, salvage, effort, maxTokens, dropping the reasoning parser): it's intrinsic to vLLM's
  gpt-oss serving, not the model's reasoning.
- **Verdict.** Not usable here until the serving stall is fixed; its real capability is ~20 pts above
  its score.

### Qwen3-Next-80B-A3B-Thinking — reasoning, citation-limited (≈51.5)
- **What it is.** Qwen3-Next 80B/3B-active MoE, **reasoning** variant, NVFP4, on the Spark.
- **Numbers.** 3 runs: 50.6 / 52.4 / 51.4 (avg 51.5), 95% completion, ~443 s/task, $0 API.
- **What it does well.** Completes on its own (no crutches); reasoning lifts it +11 over the Instruct
  sibling — judgment on security/outcome improves markedly.
- **Where it stumbles.** Citation (20/run) — its dominant loss. e.g. a count task → cited the
  `/proc/locations` *directory* instead of the specific `…/store-graz-liebenau.json` file. Picks the
  wrong granularity of record.
- **Verdict.** A clean demonstration that *reasoning alone* buys ~11 pts; still a small model, capped
  by citation/judgment precision.

### Qwen3-Next-80B-A3B-Instruct — the floor (≈40.6)
- **What it is.** Same 80B/3B-active MoE, **non-reasoning** Instruct, NVFP4. Raw baseline (before solver
  hardening) was ~6.6; deterministic aids lifted it to ~40.
- **Numbers.** 4 runs (gen3–6): avg 40.6 (36.7–43.1), 87% completion, ~210 s/task, $0 API.
- **What it does well.** Little unaided — it's the control. The +34 from raw shows how much the
  deterministic tooling carries a weak model.
- **Where it stumbles.** Everything: gives up on actions (refund pay-0013 → `NONE_UNSUPPORTED` instead
  of completing), under-denies injections (10/run), miscounts, mis-cites, doesn't reliably finish.
- **Verdict.** Not viable; useful only as the lower bound and to quantify the solver's contribution.

### Baselines — gpt-5.5 (94.8) & gpt-5.4-mini (71.8)
- **gpt-5.5** (cloud): 94.8 reference (90.8 at `low`), 98–100% completion, ~$10/run. The ceiling;
  its residual is dispatch/fraud — the same structural buckets, just smaller. The premium buys the
  last ~5 pts over deepseek-pro.
- **gpt-5.4-mini** (cloud, xhigh): 71.8 ± 1.0 (10 runs), ~$3.64/run. Strong on **security** (0.8
  under-denials — best of the mid models) but weak on **arithmetic** (14.7/run; miscomputed
  yesterday's date as 2026-06-20 vs 2026-04-21). The inverse profile of the small local models.

---

## Appendix — run artifacts

All runs on `bitgn/ecom1-prod` (100 tasks). Records under `data/runs/<label>-*.json`; solver on
branch `local-gen1` (gen1–14). Cost via the gated `COST_PROBE` in `src/agent.ts`.

| Model | Records (label) | Score(s) | Runs |
|---|---|---|---|
| deepseek-v4-pro | `dspro1`, `dspro2` | 89.3, 90.0 | 2 |
| deepseek-v4-flash | `dsflash1`, `dsflash2` | 75.1, 79.1 | 2 |
| **Gemma-4-31B-IT** (thinking) | `g31prod1`–`g31prod3` | 84.5, 80.5, 84.9 | 3 |
| Gemma-4-26B-A4B (thinking) | `gthinkprod1`–`gthinkprod3` | 65.5, 65.4, 70.1 | 3 |
| GLM-4.5-Air (gen13) | `glmprod5`–`glmprod8` | 66.6, 66.0, 69.2, 67.5 | 4 |
| GLM-4.5-Air (gen14) | `glmprod9`, `glmprod10` | 68.3, 68.4 | 2 |
| gpt-oss-120b | `gptossprod1` | 52.8 | 1 |
| Qwen3-Thinking | `thinkprod1`–`thinkprod3` | 50.6, 52.4, 51.4 | 3 |
| Qwen3-Instruct (gen3–6) | `lg3prod`–`lg6prod` | 43.1, 36.7, 42.6, 40.0 | 4 |
| gpt-5.4-mini (xhigh) | `g54mini1`–`g54mini10` | 67.7–79.3 (mean 71.8) | 10 |
| gpt-5.5 (low, cost probe) | `g55cost` | 90.8 | 1 |

Serving + integration details: `README-local-models.md`. Full timing/cost breakdown:
`README-local-models-run-summary.md`.
