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
| **open (cloud)** | **Kimi K2.6** (Moonshot) | **86.6 ± 1.0** | 90.0 | 79.6 | 99% | 79 s | $2.62 |
| **open (local)** | **Gemma-4-31B-IT** (thinking) | **83.3** | 84.9 | 80.5 | **100%** | 316 s | **$0 API²** |
| open (cloud) | **GLM-5.2** (Together) | **81.5 ± 1.3** | 88.9 | 74.7 | 99% | 43 s | $3.63 |
| **open (local)** | **Qwen3.6-27B-NVFP4-0815** (dense, thinking)⁹ | **80.2 ± 0.8** | 83.4 | 75.7 | **100%** | 204 s | **$0 API²** |
| **open (local)** | **Muse-Glimmer-30B-NVFP4** (dense, thinking)⁷ | **79.9 ± 0.8** | 83.4 | 76.4 | 98% | **322 s** | **$0 API²** |
| **open (local)** | **Muse-Glimmer-30B** (dense, thinking, BF16) | **77.9**⁶ | 79.1 | 76.7 | 97%⁶ | 776 s | **$0 API²** |
| **open (local)** | **Qwen3.6-27B** (thinking, no-MTP) | **77.4 ± 0.7** | 79.2 | 74.3 | 99% | 229 s | **$0 API²** |
| open (cloud) | **deepseek-v4-flash** | **77.1** | 79.1 | 75.1 | 95% | 26 s | **$0.17** |
| **open (local)** | **Qwen3.8-27B-NVFP4** (dense, thinking)⁸ | **75.2 ± 1.1** | 80.4 | 68.9 | **100%** | 375 s | **$0 API²** |
| baseline (cloud) | gpt-5.4-mini (xhigh) | 71.8 | 79.3 | 67.7 | 92% | 69 s | $3.64 |
| **open (local)** | **Qwen3.6-35B-A3B** (thinking, MoE, no-MTP) | **71.6** | 76.7 | 65.8 | 98% | 123 s | $0 API² |
| **open (local)** | **Nemotron-3-Super** (NVIDIA, 120B-A12B) | **71.0 ± 2.3** | 74.2 | 68.7 | 99% | 431 s | $0 API² |
| **open (local)** | **GLM-4.5-Air** | **67.7** | 69.2 | 66.0 | **100%** | 520 s | $0 API² |
| **open (local)** | **Gemma-4-26B-A4B** (thinking) | **67.0** | 70.1 | 65.4 | 96% | 233 s | $0 API² |
| **open (local)** | **deepseek-v4-flash** (q2, non-thinking; 1 Spark)⁵ | **63.4** | 67.7 | 61.3 | 82% | 375 s | $0 API² |
| **open (cloud)** | **ECOM1-32B-BF16** (Olmo-3.1-32B-Think + SFT) | **56.2** | 60.2 | 53.8 | 99% | 11 s³ | $0 API² |
| **open (local)** | **ECOM1-32B-NVFP4A16** (ECOM1-32B-BF16 quantized) | **54.6** | 57.2 | 51.8 | 98% | 50 s³ | $0 API² |
| open (local) | gpt-oss-120b | 52.8 | — | — | 70% | 149 s | $0 API² |
| open (local) | Qwen3-Next-80B-A3B-Thinking | 51.5 | 52.4 | 50.6 | 95% | 443 s | $0 API² |
| **open (cloud)** | **ECOM1-8B-A1B-BF16** (LFM2.5-8B-A1B + SFT; H100) | **44.0** | 46.0 | 42.0 | 96% | 10 s⁴ | $0 API² |
| open (local) | Qwen3-Next-80B-A3B-Instruct | 40.6 | 43.1 | 36.7 | 87% | 210 s | $0 API² |
| **open (local)** | **ECOM1-8B-A1B-BF16** (same weights, on-device; GB10) | **37.5** | 42.0 | 33.0 | 92% | 50 s⁴ | $0 API² |

¹ gpt-5.5's measured cost-probe run was at `low` effort (90.8); at higher effort the reference is
~94.8. ² Local = free per run on owned hardware, but ~40–110 min wall-clock/run (bandwidth-bound).
³ The two **ECOM1-32B** rows are *our fine-tune* of Olmo-3.1-32B-Think (see the Olmo-family section below), not raw open models: **BF16** measured on a rented Modal H200 (11 s/task); **NVFP4A16** on the owned GB10 Spark (50 s/task, ~30 min/run with `TASK_CAP_S=300`). Both `bitgn/ecom1-prod`; the ~1.6-pt BF16→NVFP4A16 gap is the quantization tax.
⁴ The two **ECOM1-8B-A1B-BF16** rows are our LoRA SFT of **`LiquidAI/LFM2.5-8B-A1B`** (Liquid AI's 8.3B-total / **1.5B-active** on-device MoE) — the *same* gpt-5.5 ECOM1 trajectories and the *same* imposed ChatML+Hermes pipeline as ECOM1-32B (`LORA_TARGETS=all-linear` for the MoE, r=32, 1 epoch, BF16 merge; `scripts/train_modal.py --base LiquidAI/LFM2.5-8B-A1B`). **Raw** LFM2.5 scored ~7% (it wouldn't commit to tool calls); the SFT fixed exactly that → 44. **Same BF16 weights, two boxes:** a rented **H100** (44.0, 3-run) and the owned **DGX Spark GB10** (37.5, 10-run) — the ~6.5-pt gap is the serving stack (Blackwell + vLLM 26.05 vs Hopper), not capability. Because SFT taught the Hermes format, serve with the packaged `--tool-call-parser hermes` (no recovery). See `README-LFM2.5-8B-A1B.md`.

⁵ **deepseek-v4-flash local** = the *same* model as the 77.1 cloud (FP8) row, but self-hosted on **one DGX Spark GB10** at **q2-imatrix** (81 GB) via **ds4/DwarfStar** (llama.cpp's V4 arch = gibberish). Non-thinking (`model=deepseek-chat`) is mandatory to fit the cap. 3-run mean **63.4** (67.7/61.4/61.3; conc 2, `TASK_CAP_S=600`, ~5 h/run). The 82% completion / 63.4 is **serving-speed-bound** — score tracks the timeout rate almost linearly, so it's ~14 pts under its own cloud FP8 (77.1) purely on the slowest ~1-in-5 tasks timing out on the ~9 K-seed prefill (decode measures ~16 t/s), not a competence gap. See its family-section subsection + `README-local-models.md`.

⁶ **Muse-Glimmer-30B** is a **2-run mean** (79.1 / 76.7) — with ±5–7 re-seed noise and only 2 runs, its
77.9 and Qwen3.6-27B's 77.4 are a **statistical tie**, not a ranking. Unlike every other sub-100% row, its
non-completions are **not stalls**: in *both* runs every one is *exactly* the `TASK_CAP_S=2400` wall-clock
cap (run 1: t002/t035/t055/t079; run 2: t024/t079), each a hard 0. Uncapped, those would likely score near
the runs' ~0.8 average → **~79–82 estimated** (an estimate, not a measurement). See its family section.
**The NVFP4 row settles this empirically**: at 2.9× the speed it hits 0 caps and 100%/98% completion.

⁷ **Muse-Glimmer-30B-NVFP4** = the *same* model as the BF16 row, quantized W4A4 by **RedHatAI**
(`RedHatAI/Muse-Glimmer-30B-NVFP4`, `compressed-tensors` / `nvfp4-pack-quantized`, 23 GB vs 56 GB;
vision tower + `lm_head` left in BF16), same image and flags, same solver config (conc 16,
`TASK_CAP_S=2400`). **10 runs: 79.94 ± 0.80** (sd 2.51, 76.4–83.4), **0.0 cap-timeouts per run**.
**It is strictly the better way to run this model** — ~2.9× decode, ~2.6× prefill, ~45 min/run vs ~105,
and the wall-clock cap failures vanish entirely. **No quantization penalty is detectable**: per-attempted-task
quality is **0.813 (NVFP4, 10 runs) vs 0.804 (BF16, 2 runs)** — i.e. 4-bit measures *slightly ahead*, and
BF16's 2-run mean (77.9) sits inside NVFP4's 10-run range. *(An earlier 2-run NVFP4 sample suggested a
~1.1-pt tax; that **reversed sign** once the sample reached 10 — a caution about 2-run quantization
comparisons, and the reason the BF16 leg, still only 2 runs, is the weak side of this comparison rather
than evidence of parity in the other direction.)*

⁸ **Qwen3.8-27B-NVFP4** — a **brand-new architecture** (released 2026-08-14, Apache-2.0,
`Qwen3_5ForConditionalGeneration`, dense 27B, 64 layers, **head_dim 256 / 4 KV heads**, 262K native
context, multimodal), benchmarked the same day. Served from `Inferact/Qwen3.8-27B-NVFP4` (modelopt,
**~25 GB**; BF16 ~52 GB) with `qwen3_coder` + `qwen3` parsers, `--kv-cache-dtype fp8`, MTP off, same
solver config as the Muse rows (conc 16, `TASK_CAP_S=2400`). **10 runs: 75.22 ± 1.07** (sd 3.37,
68.9–80.4), **99.9% completion**, 0.1 caps/run. **It is dominated by Muse-Glimmer-30B-NVFP4 on every
axis measured** — larger on disk (25 vs 23 GB), ~16% slower per task (375 vs 322 s) and 4.7 pts lower
— so there is no size/speed trade to recommend it on; it is ranked here purely on its merits.
**Caveat:** unlike the Muse rows there is **no BF16 leg** — only the 4-bit build was benchmarked, so
its quantization tax (if any) is unmeasured and the 75.2 could understate the unquantized model.

⁹ **Qwen3.6-27B-NVFP4-0815** = the **2026-08-15 re-upload** of `unsloth/Qwen3.6-27B-NVFP4`
(revision `ccdaab7e`), treated as a separate model version from the June build (`890bdef7`) in the
77.4 row. **10 runs: 80.23 ± 0.81** (sd 2.57, 75.7–83.4), 99.9% completion, 0.0 caps, ~204 s/task,
~87 min/run at conc 4. That is **+2.84 over the June build at 2.61 combined SE** — one of the few
deltas in this campaign that clears the noise band — and the gain sits exactly where a better model
should show it: **citation 12.5 → 11.3/run and arithmetic 8.8 → 7.2/run**, while the solver-ceiling
classes (dispatch 5.0, fraud 3.8 → 3.4) are unmoved. **But two things changed at once and the credit
cannot be split:** the re-upload quantizes `lm_head` (the June build left it in the quantization
`ignore` list), and that quantized head *cannot be loaded by NGC vLLM 26.05* — it dies at weight-load
with `no module or parameter named 'lm_head.weight_scale'` — so this version also had to move to a
newer engine (`vllm/vllm-openai:muse-glimmer`, 0.26.1rc1). **Weights and serving stack moved
together**; isolating them needs the June revision served on the new engine, which has not been run.
Serve with `--revision` pinned: `refs/main` moved under us mid-campaign, which is how the split was
discovered.

![ECOM1/prod score leaderboard — deepseek-v4-pro 89.6 leads the open models; Gemma-4-31B 83.3, Qwen3.6-27B-NVFP4-0815 80.2 and Muse-Glimmer-30B-NVFP4 79.9 lead the local ones](images/leaderboard.svg)

*The charts show **one row per model**: where a model has been measured in more than one build, the
representative build is charted and the superseded one lives in the table above. Qwen3.6-27B is
charted as its **0815** build (80.2); the June build (77.4) is table-only.*

**Takeaways.**
- **One open model reaches frontier-adjacent quality without an architecture.** deepseek-v4-pro
  (89.6) sits between gpt-5.4-mini and gpt-5.5, completes 99%, and its residual is the solver's own
  structural ceilings (dispatch, citation) — not a capability gap a helper model would close.
- **A clean ladder by class — but local now reaches higher than expected:** small/MoE local (Qwen3,
  gpt-oss, GLM, Gemma-A4B) tops out ~52–68; **dense Gemma-4-31B local jumps to ~83**, into mid-cloud
  territory (above gpt-5.4-mini 72 and deepseek-flash 77); large cloud (deepseek-pro, gpt-5.5) ~90–95.
  Architecture didn't move a model between rungs; **active-parameter count did** — the dense 31B's
  ~31B-active vs the MoE locals' 3–12B is the whole story (paid for in wall-clock, not dollars).
  Muse-Glimmer-30B (79.9, 10 runs) is the second dense-30B-class local to land on that upper rung,
  confirming the pattern from a different lineage — and it reaches *cloud GLM-5.2's* level for free.
  **A newer architecture, on its own, buys nothing.** Qwen3.8-27B — a day-old release with 262K native
  context — lands at **75.2 ± 1.1**, *below* its own predecessor Qwen3.6-27B (77.4 ± 0.7) and 4.7 below
  the same-class Muse-Glimmer. Three dense ~27–31B locals now span 75–83 on this solver, and where each
  falls is set by citation discipline, not by recency or context length.
- **Wall-clock is set by the *KV/attention* design, not the parameter count.** The two dense ~30B
  locals score similarly but run very differently: Gemma-4-31B is capped at **concurrency 4**, while
  Muse-Glimmer's 2-KV-head + sliding-window layout leaves KV nearly free (6.07M cached tokens at
  NVFP4) and scales ~linearly to **conc 32**. Same box, same rung, ~4× the concurrency and ~3× the
  run throughput — when picking a local model, read the attention config, not just the size.
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
| **Local — 2nd, and fastest *run*** | **Muse-Glimmer-30B-NVFP4** (dense, thinking) | **79.9 ± 0.8** (10 runs) — clears Qwen3.6-27B and cloud deepseek-flash, level with cloud GLM-5.2, and finishes a run in **~45 min**: the only big local that **batches** (conc 16–32) at 12.7 tok/s/stream. **Serve it NVFP4, not BF16.** |
| **Local — 2nd (tied)** | **Qwen3.6-27B-NVFP4-0815** (thinking, **no-MTP**) | **80.2 ± 0.8** (10 runs) — the August re-upload; **statistically level with Muse-Glimmer** (79.9) and +2.8 over its own June build at 2.6 SE. Free. **Pin `--revision ccdaab7e`** and serve on vLLM ≥ 0.26.1rc1 (NGC 26.05 cannot load its quantized `lm_head`). Dense, ~204 s/task, conc 4, ~87 min/run — pick Muse-Glimmer instead if run wall-clock matters. |
| **Local — 3rd / best value** | **Qwen3.6-27B** (June build, thinking, **no-MTP**) | **77.4** — beats cloud deepseek-flash (77.1) & gpt-5.4-mini, free. **Turn MTP off** (it costs ~4 pts → 73.2). Dense, ~229 s/task, conc 4. Superseded by the 0815 build above. |
| **Local — fast (MoE, conc 8)** | **Qwen3.6-35B-A3B** (no-MTP) | **71.6** at conc-8 throughput — beats Gemma-A4B (67) on *both* quality and speed. **Turn MTP off** (it costs ~6 pts). Gemma-A4B is the alternative if you want `enable_thinking` simplicity. |

![Quality vs cost per run — deepseek-v4-pro reaches ~90 at $0.46 while gpt-5.4-mini costs $3.64 for only 71.8; self-hosted models, including our free ECOM1 fine-tunes (37–56), cost $0 but score lower](images/quality-vs-cost.svg)

## Cross-cutting error classes

Inverting the per-model view — *where does each failure class show up* (● frequent ≳5/run,
○ occasional 1–5/run, blank ≲1):

| Error class | Instruct | Thinking | gpt-oss | GLM-Air | Gemma | Gemma31 | Q36-27B | Q36-35B | ds-flash | ds-pro | 5.4-mini | gpt-5.5 | GLM-5.2 | Kimi | Nemo | Muse⁶ | Q38⁸ | Q27-0815⁹ |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Completion failure (no `report_completion`) | ● | ○ | ●● | | ○ | | | ○ | ○ | ○ | ○ | ○ | ○ | | | ○* | | |
| Citation / grounding (missing·extra·wrong ref) | ● | ● | ● | ● | ●● | ● | ● | ●● | ● | ○ | ● | ○ | ● | ● | ●● | ● | ●● | ● |
| Security under-denial (obeys injection) | ● | ○ | | ○ | ○ | ○ | ○ | | | | | | | | ○ | | ○ | ○ |
| Arithmetic / value (wrong count·amount·date) | ● | ○ | | ○ | ○ | ○ | ○ | ○ | | ○ | ● | ○ | ○ | ○ | ○ | ● | ● | ● |
| Outcome judgment (OK vs clarify vs unsupported) | ● | ○ | ○ | ○ | ○ | ○ | ○ | ○ | | ○ | | ○ | | | ○ | ○ | | ○ |
| Dispatch sub-optimal (shared solver ceiling) | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Fraud detection | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |

\* Muse column = the 10 NVFP4 runs (citation 9.0/run, arithmetic 5.4, dispatch 4.9, fraud 3.6,
completion 1.6, outcome 1.2, security 0.4). Its completion figure is **not** a `report_completion`
discipline failure like every other model's: **0.0 of it is stalling and 0.0 is timeouts** — the residue
is step-loops (`MAX_STEPS`) and the solver's own injection-refusal short-circuit. On the BF16 serve the
same column was 3.0/run and was **entirely** wall-clock caps (footnote ⁶).

The **Q38 column** = Qwen3.8-27B-NVFP4's 10 runs (citation 15.5/run, arithmetic 7.0, dispatch 5.0,
fraud 3.8, security 2.2, outcome 0.7, completion 0.1). Its shape is the clearest illustration in the
table of **what actually moves a local model's score**: dispatch (5.0) and fraud (3.8) are the shared
solver ceilings and sit within 0.1–0.2 of every other column, while **citation alone tracks the score
almost monotonically across its own runs** — 12 citation failures → 79–80, 19 → 69–72. The 4.7-pt gap
to Muse-Glimmer is essentially the 6.5-citation-failures/run gap; on every other class the two are
within ~1.6.

The **Q27-0815 column** = the 10 runs of the August Qwen3.6-27B build (citation 11.3/run,
arithmetic 7.2, dispatch 5.0, fraud 3.4, outcome 1.1, security 1.0, completion 0.1) and it is the
cleanest before/after in the table. Against the June build of *the same model* (Q36-27B column:
citation 12.5, arithmetic 8.8) the two **model-driven** classes fall while the two **solver-ceiling**
classes do not move at all (dispatch 5.0 → 5.0, fraud 3.8 → 3.4). That is the signature of a real
model improvement rather than a lucky seed draw — and it is a useful sanity check on the matrix
itself: a change that moved dispatch or fraud would indicate a measurement artifact, not a better
model.

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
| GLM-5.2 (Together) | **$3.63** | $4.40/M output (reasoning) — ~price of gpt-5.4-mini, but deepseek-pro beats it on both | 81.5 |
| Kimi K2.6 (Moonshot) | **$2.62** | $4.00/M output (reasoning) — 2nd-best cloud; beats GLM-5.2 on score *and* cost | 86.6 |
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

### Kimi K2.6 (Moonshot) — 2nd-best cloud; the coding rep *transfers* (≈86.6)
- **What it is.** Moonshot's Kimi K2.6 (open-weight 1T-param MoE, 32B active), via the **native Moonshot API**
  (`api.moonshot.ai`, model `kimi-k2.6`) — reasons natively. Wired as the `moonshot` provider in `src/llm.ts`
  (key `KIMI_API_KEY`). Tier-2 limits (RPM 500) ran conc 16 fine.
- **Numbers.** 10 runs: **86.6 ± 1.0** (SE; range 79.6–90.0, sd 3.1), **99% completion**, ~79 s/task,
  **$2.62/run** (Moonshot $0.95/$4.00/$0.16 per M).
- **What it does well.** **2nd-best cloud, behind only deepseek-pro (89.6)** — and it **dominates GLM-5.2 on
  both axes (86.6 vs 81.5, $2.62 vs $3.63)**. Clean profile: citation 6.7/run is its only notable bucket; the
  dispatch JSON-echo bug that cost GLM-5.2 ~2 tasks/run **does not appear** here (completion 0.7, arithmetic 1.9).
- **The contrast with GLM-5.2.** Both are open-weight 1M-ctx coding-flagship MoEs run with native reasoning, no
  aids — but Kimi's SWE-bench-class strength **transferred to ecom1** (grounding/formatting discipline) where
  GLM-5.2's didn't. The difference isn't capability headroom; it's output discipline (Kimi echoes structured
  answers cleanly and grounds tighter).
- **Where it stumbles.** Citation (6.7/run) + the shared dispatch (5.0) / fraud (3.5) ceilings — the same
  structural walls every model hits, gpt-5.5 included.
- **Verdict.** The best **cloud** option after deepseek-pro, and the clear winner over GLM-5.2. deepseek-pro is
  still both higher *and* ~6× cheaper, so Kimi isn't the value pick — but it's a genuinely strong agent here.

### GLM-5.2 (Together) — strong, but a *local* model beats it (≈81.5)
- **What it is.** Zhipu's GLM-5.2 (open-weight MoE, 1M context), served via **Together** (`zai-org/GLM-5.2`,
  FP4) — reasons natively. (Our own z.ai key is a *Coding Plan* key — only `/api/coding/paas/v4`. That
  endpoint **drops requests mid-agent-loop: ~42% of tasks never reach `report_completion` even at conc 1**
  — a coding-plan reliability limit for sustained batch runs, *not* fixable by lowering concurrency. The
  general pay-per-token endpoint returns `1113` insufficient balance. So **Together is the working path**.)
- **Numbers.** 10 runs: **81.5 ± 1.3** (SE; range 74.7–88.9, sd 4.0), **99% completion**, ~43 s/task,
  **$3.63/run** (Together $1.40/$4.40/$0.26 per M).
- **What it does well.** Solid mid-upper cloud tier — clears deepseek-flash (77.1) and gpt-5.4-mini
  (71.8); clean profile (citation 7.4/run, everything else low).
- **The two things that stand out.** (1) **A *local* model beats it** — Gemma-4-31B (83.3, free, on a
  single Spark) edges out this 1M-context cloud flagship. (2) **It's dominated on value** — at $3.63/run
  it's ~the price of gpt-5.4-mini, yet **deepseek-v4-pro is both cheaper ($0.46) and higher (89.6)**.
- **Verdict.** A capable model, but no niche here: if you'll pay cloud rates, deepseek-pro is better and
  ~8× cheaper; if you want ~83 for free, the local Gemma-4-31B already does it.

#### Why 81.5, not higher — failure analysis (10 runs, 244 imperfect tasks)

GLM-5.2 is *not* failing on capability (99% completion; it reasons correctly — e.g. it rightly judged a
discount blocked, it just didn't cite the policy doc). The score is held down by four causes — two
GLM-specific, two structural:

1. **Dispatch JSON echo corruption (GLM-specific, ~2 tasks/run → hard 0.00).** The `dispatch_plan` tool
   *computes the optimal plan deterministically and hands the model the exact JSON to echo verbatim*
   (`agent.ts` dispatch preamble). GLM-5.2 inconsistently **re-escapes it** — submits
   `{\"assignments\":…}` instead of `{"assignments":…}`, so the grader can't parse it → **0.00**. The
   *same* task with a clean echo scores ~0.80; tasks t014/t024 corrupt it in **all 10 runs**. This is an
   output-fidelity / "echo verbatim" failure, not a routing one — the single most damaging GLM-specific
   issue (~2 free tasks/run thrown away). Other models echo the blob cleanly.
2. **Citation / grounding discipline (7.5/run — the biggest bucket).** It mis-grounds in *both*
   directions: **misses** the load-bearing doc (correct "discount blocked" answer but no
   `/docs/checkout.md` cite → 0.00) and **over-cites** records it only examined (extra `/proc/catalog/…`
   ref). On ecom1 the `grounding_refs` are graded as strictly as the answer.
3. **No cloud salvage — the asymmetry vs local models.** GLM-5.2's two biggest weaknesses (citation,
   fraud precision) are *exactly* the buckets the solver has deterministic fixes for — citation
   reconstruction/repair (`agent.ts:1536/1589`), the fraud guide (`:1801`), and the re-prompt/salvage
   loop (`:1876/1887`) — but all are **`localProvider`-gated**. Cloud GLM-5.2 runs the bare path with no
   safety net, so its raw slips are never auto-repaired. **The local Gemma-4-31B (83.3) that edges it out
   is partly riding those aids** — a like-for-like (aids-on) comparison would narrow or reverse the gap.
4. **Archive-fraud over-flagging (~3/run partial).** On the historical-archive fraud task it recovers
   the fraud amount but flags too many legit payments as false positives (low precision) → partial credit
   (0.49–0.89). The `FRAUD_GUIDE` that would steer this is, again, local-only.
5. **Shared structural ceiling.** The solver's `solveDispatch` is itself only ~0.80-optimal (it incurs
   late-delivery penalties), so *every* model — gpt-5.5 included — tops out ~0.80 on a *clean* dispatch
   echo. ~2.5/run of the dispatch loss is this ceiling, not GLM.
6. **Benchmark ≠ coding.** GLM-5.2's headline strength is long-horizon *coding*; ecom1 rewards grounding
   discipline, fraud precision, and exact output formatting — orthogonal skills, so the "gpt-5.5-level"
   reputation doesn't transfer.

**Bottom line:** ~2/run is a fixable JSON-echo bug, ~10/run is citation/fraud discipline the local-only
aids would partly repair, and ~5/run is the shared benchmark ceiling. So **81.5 understates GLM-5.2's
ceiling on this solver** — fixing the echo and/or enabling the citation+fraud aids (it's `localProvider`
that gates them, trivially flippable for a cloud model) would likely close much of the gap to
deepseek-pro. *(Hypothesis — not yet measured; an aids-on GLM-5.2 run would confirm it.)*

### Muse-Glimmer-30B — 2nd-best local, and the only one that batches (≈79.9 NVFP4 / 77.9 BF16)
- **What it is.** A **dense ~30B** multimodal reasoning model (`meta-models/Muse-Glimmer-30B`), served
  **BF16 (~56 GB)** on the DGX Spark from a purpose-built image (`vllm/vllm-openai:muse-glimmer`) with
  its own **`muse_glimmer` tool-call *and* reasoning parsers**. Architecturally Gemma-shaped: 52 layers,
  hidden 6656, **2 KV heads**, sliding-window 2048 with every-4th layer global, logit softcapping,
  131K context, native sampling `temp 1.0 / top_p 0.95 / top_k 64` (via `--generation-config auto`).
  **Reasoning is on by default** — no `enable_thinking` trap like Gemma-4.
- **Numbers.** **NVFP4, 10 runs: 79.94 ± 0.80** (sd 2.51, range 76.4–83.4), **98% completion**,
  ~322 s/task, ~45 min/run at **concurrency 16**, $0 API. (BF16, 2 runs: 77.9 — see the NVFP4
  subsection; serve it quantized.) At 10 runs the **gap over Qwen3.6-27B (77.4 ± 0.7) is real**
  — ~2.5 pts, ≈2.4 combined SE — where the first 2 runs had looked like a tie.
- **It works on the bare native loop.** First contact was clean: correct `tool_calls` on the first
  request, thinking split into a separate `reasoning` field with `content` left clean. **No
  `LOCAL_TOOLCALL_RECOVER`, no `FORCE_TOOL_CHOICE_REQUIRED`, no custom template** — contrast Olmo
  (can't tool-call), LFM2.5 (unparseable wrapper), Magistral (won't submit). A 4-task smoke scored
  **4/4**, including a correct `NONE_UNSUPPORTED` judgment.
- **The headline: it batches, and no other big local does.** Per-stream decode is slow (~4.3 tok/s —
  56 GB of BF16 weights against ~273 GB/s) but aggregate throughput scales **~linearly to conc 32**
  (17 → 34 → 67 → 124 tok/s at conc 4/8/16/32; per-stream latency degrades only ~10% across that
  range). Two properties cause it: the **2-KV-head + sliding-window design makes KV nearly free**
  (vLLM allocates **3.83M tokens / 54 GiB** of cache — 29× concurrency at *full* 131K context), and
  **prefill is healthy at ~1255 tok/s** (16.2K-token prompt in 12.9 s). So it sidesteps *both* local
  walls seen earlier: Gemma-4-31B's hard **concurrency ≤ 4** (its smaller KV pool returned empty at 8)
  and DeepSeek-V4-flash's **prefill** bottleneck. **21.6 GPU-hours of task time compressed into ~1.75 h
  wall-clock.** For a dense model on one Spark, that is the interesting result — not the score.
- **Where it stumbles.** Over 10 NVFP4 runs: **citation 9.0/run is the wall**, as for every model, then
  arithmetic/value 5.4, the shared dispatch (4.9) and fraud (3.6) ceilings, completion 1.6,
  outcome 1.2, and **security under-denial just 0.4** — among the cleanest security columns of any
  local, cloud-tier rather than small-model.
  **A caution this model taught us twice.** (1) Run 1 showed *zero* security and *zero* outcome errors
  and looked like the cleanest local profile ever measured; run 2 surfaced 2 and 3. (2) Its first two
  NVFP4 runs (79.1, 77.8) turned out to be the two *lowest* of the eventual ten, dragging the 2-run mean
  to 78.5 against a true 79.9. **On a 100-task set that re-seeds, neither a score nor a clean error
  bucket means anything at n=2** — the same trap as the doc's 25-task-probe warning, one level up.
- **The lost tasks are the cap, not the model.** In *both* runs, **every** non-completion hit
  `TASK_CAP_S=2400` *exactly* → hard 0 (run 1: t002/t035/t055/t079; run 2: t024/t079; t079 both times).
  Two of run 1's (t035, t055) burned the entire 40 min on a **single step**: at ~3.5 tok/s a runaway
  chain against `LOCAL_LLM_MAXTOK=24576` cannot finish inside any sane cap. **Fix before re-running:
  lower `LOCAL_LLM_MAXTOK` to bound one generation, and/or raise the cap** — worth ~1.5–3 pts. Same
  lesson as local DeepSeek-V4-flash: **score tracks cap rate**, so this is serving-bound, not a
  competence ceiling. On `report_completion` *discipline* it never fails.
- **Verdict.** **~80 — the 2nd-best local**, clearly above Qwen3.6-27B (77.4) and *cloud*
  deepseek-flash (77.1), and **statistically level with cloud GLM-5.2** (81.5 ± 1.3; the 1.6-pt gap is
  ~1.0 combined SE). Gemma-4-31B (83.3) still leads it by ~3.4, but on only 3 runs (SE ~1.4) that
  separation is borderline (~2.1 SE) rather than settled. And it gets there **~3× faster per run**
  (~45 min vs ~130): Gemma-4-31B is stuck at conc 4, this batches at 16–32. Best local pick when a
  *run* has to finish; Gemma-4-31B if you want the top *score* and can spend the hours.

#### NVFP4 (W4A4) — same score, 2.9× the speed: **serve it quantized**
The BF16 row above is the *wrong* way to run this model. Quantized to **NVFP4 by RedHatAI**
(`compressed-tensors`/`nvfp4-pack-quantized`, **23 GB vs 56 GB**), same image, flags and solver config:

| | BF16 (2 runs) | **NVFP4 (10 runs)** | |
|---|---:|---:|---|
| Score | 77.9 (76.7–79.1) | **79.9 ± 0.8** (76.4–83.4) | +2.0 |
| Quality per *attempted* task | 0.804 | **0.813** | no tax detectable |
| Completion | 97% | **98%** | |
| Wall-clock caps / run | 3.0 | **0.0** | failure mode gone |
| Decode (1 stream) | 4.4 tok/s | **12.7 tok/s** | **2.9×** |
| Aggregate @ conc 16 / 32 | 67 / 124 tok/s | **194 / 365** | 2.9× |
| Prefill | 1255 tok/s | **3273 tok/s** | 2.6× |
| GPU-h of task time / run | 21.6 | **8.9** | 2.4× |
| Wall-clock / run | ~105 min | **~45 min** | 2.3× |
| KV cache | 54 GiB / 3.83M tok | **86 GiB / 6.07M tok** | 46× conc @131K |

- **It lands on the real FP4 path.** vLLM logs `Using FlashInferCutlassNvFp4LinearKernel for NVFP4 GEMM`
  — Blackwell FP4 tensor cores, not emulation. That's why the gain tracks the 2.5× weight-size
  reduction instead of falling short of it. Loaded first try; correctness, the `reasoning`/`content`
  split, and native tool-calling all survived unchanged.
- **No quantization tax is detectable — and the first estimate of one was an artifact.** On the initial
  2 NVFP4 runs, quality per *attempted* task read 0.793 vs BF16's 0.804, which we reported as a ~1.1-pt
  tax offset by recovered timeouts. **At 10 runs it reversed to 0.813 — 4-bit measures slightly ahead**,
  and BF16's 2-run mean sits inside the NVFP4 range. The honest reading is *no measurable penalty*,
  with the **BF16 leg (still n=2) now the weak side** — not evidence that 4-bit is genuinely better.
  Two-run quantization comparisons on this benchmark are worthless; the swing here was ~2 pts and it
  changed the sign of the conclusion.
- **Speed exposed a real bug the cap was hiding.** t079 timed out in *both* BF16 runs; at 2.9× it no
  longer runs out of clock — it runs out of **`MAX_STEPS=35`** instead. So t079 is a **step-loop**, not
  a slow task, which the BF16 wall-clock cap had misattributed. General lesson: **a wall-clock cap
  masks the difference between "slow" and "looping"** — speed up the serve before concluding a task is
  merely slow.
- **Which quant.** Prefer **RedHatAI** (`compressed-tensors`) over the `modelopt`-format alternatives
  (e.g. `Inferact/…-NVFP4-W4A4`, 25.4 GB): compressed-tensors is vLLM's *native* quantization path, the
  repo is tagged `vllm`, and the campaign has already been bitten by a modelopt-format quant failing on
  this box (NVIDIA's official Qwen3.6-35B-A3B NVFP4 → needed the `unsloth` rebuild). **Untested here:**
  Inferact was never benchmarked, so this recommendation rests on the format argument plus the fact
  that the RedHatAI build loaded first try onto the fast kernel — not on a head-to-head score.
- **Verdict.** **Always serve this model NVFP4.** No measurable quality cost, 2.9× faster, 2.3× shorter
  runs, and it removes the cap failure mode outright (0.0 caps across all 10 runs). The BF16 numbers are
  kept above only as the quantization baseline.

### deepseek-v4-flash — fast & cheap (≈77.1)
- **What it is.** DeepSeek V4 "flash", reasoning, ~$0.14/M in / $0.28/M out.
- **Numbers.** 2 runs: 75.1 / 79.1 (avg 77.1), 95% completion, ~26 s/task (fastest measured), **$0.17/run**.
- **What it does well.** Clears the entire local field; strong security/arithmetic like its pro sibling.
- **Where it stumbles.** Citation precision (10.5/run) — e.g. "need code: bare stihl hsa 50… sku only"
  → answered `PT-HDG-STI-HSA50-BODY` (correct SKU) but **over-cited** sibling variants
  (`…-AK10.json` …), an extra-reference failure. A few not-completions (5.5/run).
- **Verdict.** The cost/speed sweet spot below pro; loses ~12 pts to pro mostly on citation discipline.

#### deepseek-v4-flash *run locally* on one DGX Spark — 63.4 (q2), a poor trade vs the API
The 77.1 above is the **cloud FP8** endpoint. We also ran the *same model* **locally on a single GB10 Spark** to
see whether a 284B / 13B-active MoE is viable self-hosted. It is — barely — and it isn't worth it:
- **Fit.** Full FP8 needs two Sparks; one node caps you at 2–3-bit. Unsloth's **UD-IQ3_XXS (103 GB)** fits the
  128 GB box (MLA keeps the KV compact — the *weights* are the limit), but **llama.cpp's DeepSeek-V4 arch is
  broken** (coherent-looking load, then **gibberish** at the raw `/completion` level; not just the tool path).
  The engine that works is **ds4 / DwarfStar** (antirez's purpose-built V4 engine, `make cuda-spark`), which
  needs its **own q2-imatrix GGUF** (81 GB) — coherent, and OpenAI `tool_calls` parse correctly.
- **Non-thinking is mandatory** to fit the task cap — thinking mode cap-storms. It's a *per-request* control in
  ds4 (`model=deepseek-chat` / `think:false`), not a server flag.
- **Result: 63.4 mean** (3× `bitgn/ecom1-prod`: 67.7 / 61.4 / 61.3; caps 17/22/28; conc 2, `TASK_CAP_S=600`,
  **~5 h/run**). Decode measures **~16 t/s** (two-point on the live server), but the **~9 K-seed prefill is the
  bottleneck**. The score tracks the **cap rate** almost linearly — each cap is a hard 0 — so this is
  **serving-speed-bound, not a competence ceiling**: it answers correctly when it finishes, but the slowest ~1-in-5
  tasks time out on prefill.
- **Verdict.** Local q2 loses **~14 pts to its own cloud FP8 (77.1)** and sits **~20 pts under the local crown
  `Gemma-4-31B` NVFP4** (83.3, ~9× smaller, ~40 t/s, ~10 GB). A faster serve recovers a few points but can't
  close either gap. **Use the API for V4-Flash's 77.1; use Gemma-4-31B for the best local.** (Details +
  scripts in `README-local-models.md`.)

### Magistral Medium (Mistral) — tested, UNUSABLE: can't drive the agentic loop (~12, not ranked)
- **What it is.** Mistral's *reasoning* model (`magistral-medium-2509`) via `api.mistral.ai`, wired as the
  `mistral` provider (which also hosts the non-reasoning Mistral Large 3). $2.00/$5.00 per M.
- **Integration gotcha (fixed).** Mistral's API does STRICT request validation and **422s on OpenAI's
  `store` param that pi sends** — every first request failed (0 steps → DENIED_SECURITY) until a fetch shim
  in `src/llm.ts` stripped it. (Worth knowing for any pi↔Mistral integration.)
- **Result — not a usable score.** Bare-path **3.8** (5% completion). Even WITH the re-prompt+salvage aid
  that *fully* recovers local non-reasoning models, only **11.6** (28% completion): **72% of tasks never call
  `report_completion`, and Magistral ignores the explicit "call report_completion now" nudge.**
- **Why.** It does long internal CoT, takes ~1 tool step, then stops without submitting. Its tool-discipline
  is fundamentally incompatible with the agentic loop — **unique among the cloud models** (Kimi 86.6,
  deepseek-pro 89.6, GLM-5.2 81.5 all complete **99%** on the bare path).
- **Lesson.** *A strong reasoning model is not automatically a good agent.* "Ties GPT-5.5 on SWE-bench" and
  "reliably drives a tool-calling ops loop" are orthogonal; ECOM1 grades the latter, and Magistral fails it.
  **Left off the leaderboard** — ~12 measures the protocol failure, not the model's reasoning.

### Qwen-AgentWorld-35B-A3B (Qwen) — considered, NOT benchmarked: a world model, not an agent
A "language **world** model" — given an action + history it predicts the **next environment state** (7
domains: MCP/Search/Terminal/SWE/Android/Web/OS); it does **not** tool-call (the official serve command has
no tool-call parser). It's the *environment* half of an agent loop, not the agent, so it can't drive the
ECOM1 solver (native tools + `report_completion`). Its strength is also moot here — a world model is for
when you *can't* query the real environment, but ECOM1's `/proc`·`/bin`·`/docs` is available and cheap. The
only direct path — a text-ReAct loop using it as a plain LLM — would underperform, since it predicts outputs
rather than choosing actions. **Not run.** Serving + the three (non-competitive) usage options are in
`README-local-models.md`. *(Companion lesson to Magistral: a model strong in a different shape —
world-modeling — doesn't transfer to agentic-ops solving.)*

### Olmo 3.1 family (AllenAI) — considered, ruled out: no native tool-calling
AllenAI's Olmo 3.1 open-*science* models were checked as solver candidates and **ruled out for lack of
native tool-calling** — the one thing the ECOM1 solver runs on. Both **`Olmo-3.1-32B-Instruct`** and
**`Olmo-3.1-32B-Think`** (reasoning, emitted as inline `<think>…</think>` tags) ship a plain
`system`/`user`/`assistant` chat template with **no `tools` section and no vLLM `--tool-call-parser`**;
**`Olmo-3.1-7B-RL-Zero-Math`** is a math-RL specialist, not an agent. They all *serve* on the Spark fine
(7B trivially; 32B at ~32 GB FP8 / ~16 GB NVFP4) — they just emit no `tool_calls`, so the solver takes
**0 tool steps → DENIED_SECURITY fallback**. The Think variant doesn't rescue it: per the Magistral
result, reasoning ≠ tool-discipline, and Olmo is a step behind — it can't tool-call at all. **Not run.**
(Using one would need a text-ReAct solver variant, which doesn't exist here.) Completes the trio of
"why a capable model still can't solve ECOM1": **wrong shape** (Qwen-AgentWorld), **won't submit**
(Magistral), **can't call tools** (Olmo).

**…but SFT turns it into a usable agent — `ECOM1-32B` (56.2 / 54.6).** The "ruled out" verdict is
about the *raw* base. We fine-tuned `Olmo-3.1-32B-Think` on **11,248 gpt-5.5 teacher trajectories**
(LoRA r=32, 1 epoch) that *impose* the ChatML+Hermes tool-calling the base never learned →
**`ECOM1-32B-BF16` = 56.2** (H200, 3-run), and the GB10-quantized **`ECOM1-32B-NVFP4A16` = 54.6**
(10-run) — the fully-open Olmo, made to drive the loop. Two lessons: (1) the no-native-tool-calling
wall is a *format* gap SFT closes, not a competence gap; (2) even so, ECOM1-32B lands **~21 pts below
raw Qwen3.6-27B (77.4)** — a stronger *base* beats a fine-tuned weaker one, so **the base model is the
ceiling** (Qwen does `export` natively at 0.64 where distilled Olmo is stuck at 0.00). Full recipe:
`README-Hugging-Face.md`, `README-OPSD.md`.

### Making a non-tool-calling model usable (remediation menu)

The Olmo blocker is **format** (no parseable `tool_calls`) — separable from **competence** (choosing the
right tool + finishing). Ways to add tool-calling, lightest to heaviest:

**Without training — fix the *format*:**
- **Prompt + parse (text-ReAct).** Tool specs + few-shot in the prompt; the model replies with a structured
  block (`{"tool":…}` / `<tool_call>…</tool_call>`); the harness parses it. No model change; reliability is
  the weak point (free-form drifts). This is the pre-SDK ECOM1 protocol.
- **Guided / constrained decoding** *(the strong no-train option)*. vLLM `guided_json` / `guided_grammar` /
  structured outputs force the model to emit **only** a valid tool call against a JSON schema — a model
  never trained for it then *can't* produce unparseable output. (`outlines` / `guidance` / `instructor` do
  the same at the client.)
- **Custom chat template + generic parser.** Override the template (`--chat-template`) to render the tools in
  e.g. Hermes format and serve with `--enable-auto-tool-choice --tool-call-parser hermes`; the *existing*
  native-tool-calling solver then works unchanged. Only as reliable as the model mimics that format — back
  it with guided decoding.

**With training — fix the *competence*:**
- **SFT / LoRA on function-calling data** (Glaive, ToolACE, Hermes-FC, xLAM, APIGen). QLoRA on a 32B is
  tractable; yields native, parseable tool-calling *and* better tool choice.
- **Agentic RL post-training** — RL on tool-use trajectories to push **completion discipline** (the exact
  thing that sank Magistral). Heaviest, but it's what closes the competence gap.

**The caveat (learned here):** the no-train tricks solve *format*, not *competence*. Magistral tool-called
fine and still scored ~12 (wouldn't `report_completion`); a format-fixed Olmo with no agentic training would
likewise *participate but underperform* — though ECOM1's local-model aids (citation/fraud/re-prompt) recover
such models partway. **Cheap test:** guided-JSON + custom template → point the current solver at it → dev
smoke. **Robust fix:** LoRA SFT.

### LFM2.5-8B-A1B (Liquid AI) — on-device; raw is unparseable, SFT rescues it to ECOM1-8B (44 / 37.5)
Liquid AI's **`LFM2.5-8B-A1B`** (8.3B-total / **1.5B-active** hybrid conv+attention on-device MoE) was tested
as the *smallest* viable base — a phone-/laptop-class model. Unlike Olmo it **does** emit tool calls, but in
its own `<|tool_call_start|>[list(path="/docs")]<|tool_call_end|>` (pythonic) wrapper that **no packaged vLLM
parser reads** — the `lfm2` parser is absent from every Spark image (26.05 / v0.14.0 / nightly all `KeyError`),
and generic `pythonic` can't see the wrapper → `tool_calls:[]` → 0 steps. We built a reusable harness-side
fallback (**`LOCAL_TOOLCALL_RECOVER`**, `src/lfm-toolcall.ts`) that parses the call the server couldn't — but
even parsed, **raw tops out at ~7%**. The residue is *commitment*, not plumbing: it won't reliably emit a call
or `report_completion` (reasoning ≠ tool-discipline — the Magistral lesson again), and
`FORCE_TOOL_CHOICE_REQUIRED` backfires (1.2 — it loops to max-steps).

**…but SFT turns it into a real on-device agent — `ECOM1-8B` (44.0 / 37.5).** Same recipe as ECOM1-32B: LoRA
SFT on the gpt-5.5 teacher trajectories that *impose* ChatML+Hermes (the only new knob is
`LORA_TARGETS=all-linear` for the MoE). The SFT'd model emits Hermes calls the **packaged** parser reads (no
recovery) and commits — **0/100 zero-step tasks (raw: 43), 96/100 completed** → **`ECOM1-8B-A1B-BF16` = 44.0**
(rented H100, 3-run) and **37.5** on the owned **DGX Spark GB10** (10-run; the ~6.5-pt gap is serving-stack —
Blackwell + vLLM 26.05 vs Hopper — not capability). That's **above our SFT'd 7B Olmo (38)** and the
Qwen3-Instruct floor (40.6), from **1.5B active params on a laptop-class box**. Same two lessons as Olmo: SFT
closes the *format* gap; and active-param count is the ceiling (44 « the 3–4B-active MoEs at 67–72 « raw
Qwen3.6-27B at 77.4). Full recipe + both measurements: `README-LFM2.5-8B-A1B.md`.

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

### Qwen3.8-27B — newest architecture tested, but it doesn't beat its own predecessor (≈75.2)
- **What it is.** Qwen's **brand-new dense 27B** (`Qwen/Qwen3.8-27B`, released **2026-08-14**,
  Apache-2.0), `Qwen3_5ForConditionalGeneration` / `qwen3_5`: 64 layers, **4 KV heads at head_dim
  256**, 262K native context, multimodal, **reasoning on by default**. Benchmarked the day it shipped,
  NVFP4 (`Inferact/Qwen3.8-27B-NVFP4`, modelopt, **~25 GB**; BF16 ~52 GB) on the Spark, MTP left off
  per the official vLLM recipe (and per this campaign's repeated finding that MTP costs 4–6 pts).
- **Numbers.** **10 runs: 75.22 ± 1.07** (sd 3.37, range 68.9–80.4), **99.9% completion**
  (999/1000 tasks; the single miss is one `TASK_CAP_S=2400` wall), ~375 s/task, ~46 min/run at
  concurrency 16, $0 API.
- **It works on the bare native loop** — correct `tool_calls` first request, `<think>` split into a
  separate `reasoning` field with `content` left clean. No `LOCAL_TOOLCALL_RECOVER`, no forced tool
  choice, no custom template.
- **The parser names are a trap.** The vLLM modules are called `qwen3_engine_tool_parser` /
  `qwen3_engine_reasoning_parser`, but **`qwen3_engine` is not a registered parser name** — the server
  dies at arg-parse with `invalid tool call parser`. The chat template emits **nested XML**
  (`<tool_call><function=name><parameter=key>…`), not Hermes JSON, so serve with
  `--tool-call-parser qwen3_coder` (or `qwen3_xml`) `--reasoning-parser qwen3`.
- **`--max-model-len` *is* the concurrency knob.** head_dim 256 × 4 KV heads × 64 layers ≈ **256 KB per
  token** — roughly **4× a normal 27B's** KV cost, the one architectural fact that dictates how you
  serve it. At the model's native 262K context a *single* sequence would need ~68 GB, so the default
  full-context serve is not viable on a 128 GB box. At `--max-model-len 65536 --kv-cache-dtype fp8`
  vLLM reports **79.0 GiB KV = 2,252,800 tokens → 34.4× concurrency** (12.1× without the fp8 KV).
- **Throughput.** ~8.0 tok/s/stream decode at conc 16, aggregate **71.6 / 127.6 / 208.3 tok/s** at conc
  8/16/32 — about **2× the BF16 build** (33.5 / 63.1). Healthy, but *below* Muse-Glimmer-30B-NVFP4's
  194/365 at conc 16/32, which is why its runs come in at ~46 min despite a similar nominal size.
- **Where it stumbles.** **Citation 15.5/run is the wall, and it is the whole story of this model's
  spread**: across the 10 runs citation failures track score almost monotonically (12 → 79–80,
  19 → 69–72), while dispatch (5.0) and fraud (3.8) sit at the shared solver ceilings in *every* run.
  Then arithmetic/value 7.0, security 2.2, outcome 0.7. Completion discipline is excellent (0.1/run).
- **It does not improve on Qwen3.6-27B.** The older, smaller-on-disk 27B scores **77.4 ± 0.7** against
  this model's **75.2 ± 1.1** — a 2.2-pt deficit at ~1.7 combined SE, so *statistically borderline
  rather than a settled regression*, but there is certainly no generational gain here on this workload.
  A newer architecture and 262K context did not convert into agentic score.
- **Verdict.** **Dominated by Muse-Glimmer-30B-NVFP4 on every axis measured** — bigger on disk (25 vs
  23 GB), ~16% slower per task (375 vs 322 s), and 4.7 pts lower (75.2 vs 79.9). There is no
  VRAM-or-speed argument that recovers it, so it is **not a recommended local pick**: it lands 4th
  among locals, behind Gemma-4-31B (83.3), Muse-Glimmer (79.9) and Qwen3.6-27B (77.4). What it *is*
  worth: a clean, no-workaround serve on a day-old architecture, above cloud gpt-5.4-mini (71.8) and
  every MoE local tested. **Caveat:** only the 4-bit build was run — there is **no BF16 leg**, so any
  quantization tax is unmeasured and 75.2 may understate the unquantized model.

### Qwen3.6-27B-NVFP4-0815 — the August re-upload, and a rare *real* gain (≈80.2)
- **What it is.** The **same model** as the row below, but the **2026-08-15 re-upload** of
  `unsloth/Qwen3.6-27B-NVFP4` (revision `ccdaab7e`, 5 shards) instead of the June build
  (`890bdef7`, single `model.safetensors`). Tracked as a **separate version** because two things
  changed together — see the caveat below.
- **Numbers.** **10 runs: 80.23 ± 0.81** (sd 2.57, range 75.7–83.4), **99.9% completion**,
  **0.0 cap-timeouts**, ~204 s/task, ~87 min/run at concurrency 4, $0 API.
- **The delta is real.** **+2.84 over the June build's 77.38 ± 0.72 at 2.61 combined SE.** Almost
  everything else in this campaign lands inside the ±5–7 re-seed band and gets called noise; this
  does not. The error profile says the same thing more convincingly than the aggregate does: the
  two **model-driven** classes improve (**citation 12.5 → 11.3/run, arithmetic 8.8 → 7.2/run**) while
  the two **solver-ceiling** classes are untouched (dispatch 5.0 → 5.0, fraud 3.8 → 3.4). Perfect
  tasks rise 70.3 → 73.6/100 and it is ~11% faster per task.
- **CAVEAT — two variables moved at once.** The re-upload **quantizes `lm_head`**; the June build
  listed `lm_head` in the quantization `ignore` list, leaving it in higher precision. That quantized
  head **cannot be loaded by NGC vLLM 26.05**, which builds an unquantized `ParallelLMHead` for this
  architecture and dies at weight-load with `ValueError: There is no module or parameter named
  'lm_head.weight_scale'`. So this version also had to move engines, to
  `vllm/vllm-openai:muse-glimmer` (vLLM 0.26.1rc1). **Weights and serving stack changed together, and
  the +2.84 cannot be attributed to either alone.** The control arm that would split them — June
  revision `890bdef7` served on the *new* engine — has not been run.
- **Operational lesson: pin `--revision`.** `refs/main` moved under this campaign mid-flight. An
  unpinned serve silently swaps the weights, which here would have meant a "continuation" of the
  June series that was quietly a different model. The failure was only *visible* because the new
  checkpoint happened to crash the old engine; had it loaded, the swap would have been invisible and
  the pooled mean would have been meaningless. **Always pin the revision; treat a moved `refs/main`
  as a new model version, not an update.**
- **Verdict.** At 80.2 it is **statistically level with Muse-Glimmer-30B-NVFP4** (79.9 ± 0.8 — a
  0.3 gap on ~1.1 combined SE is a tie, not a lead) and clears cloud deepseek-flash and
  gpt-5.4-mini. It also beats Qwen's own *newer* Qwen3.8-27B (75.2) by 5 points, which keeps the
  odd shape of the Qwen line intact: on this workload the 3.6 generation is the stronger one, in
  both of its builds. Slower per run than Muse-Glimmer (~87 min vs ~45) because it is dense and
  capped at concurrency 4.

### Qwen3.6-27B — 2nd-best local, beats deepseek-flash; turn MTP OFF (≈77.4)
- **What it is.** Dense 27B, multimodal, NVFP4 (~19 GB, community `unsloth` quant) on the Spark —
  reasoning **on by default**. `qwen3_coder` / `qwen3` parsers.
- **Numbers.** 6 full runs **without MTP**: 78.4/77.0/74.3/79.2/76.8/78.6 → **77.4 ± 0.7**, 99%
  completion, ~229 s/task, $0 API.
- **MTP is a trap here too.** Enabling the MTP speculative head **drops it to 73.2** (3-run) — the same
  ~+4–6 accuracy tax MTP imposed on the 35B-A3B sibling. **Leave `--speculative-config` off** (you also
  lose the speedup — no-MTP is ~229 s/task vs ~146 with MTP — but the accuracy is worth it).
- **What it does well.** **2nd-best local — and it now beats *cloud* deepseek-flash (77.1)** and
  gpt-5.4-mini (71.8), free and on the box. Clears GLM/Gemma-A4B (~67) by ~10.
- **Where it stumbles.** Citation (12.5/run — higher than Gemma-31B's 8.3); a few more tool-call slips
  (missing tool / relative path) than the Gemmas. Still ~5 below the local champ Gemma-4-31B (83.3).
- **Note.** Even at 77.4 it lands **below muxx's 0.809** — the Qwen3.6 family suits this solver a touch
  less than the Gemmas/DeepSeeks (consistent across both Qwen3.6 sizes).
- **Verdict.** The clear local #2: ~77, free, beats the cheap cloud tier; just slower (dense, conc 4)
  and ~5 behind Gemma-4-31B.
- **Superseded.** These 6 runs are the **June revision `890bdef7`** on NGC vLLM 26.05. The August
  re-upload of the same repo scores **80.2** (section above) and is the build to use; this row is
  kept as the before-side of that comparison, not as a current recommendation.

### Qwen3.6-35B-A3B — fast local 3rd place, but turn MTP OFF (≈71.6)
- **What it is.** The **MoE** sibling of the 27B (35B total / **3B active**), multimodal, NVFP4
  (`unsloth/Qwen3.6-35B-A3B-NVFP4`), thinking on by default. MoE → runs at **concurrency 8**.
- **Numbers.** 6 full runs **without MTP**: 75.9/65.8/68.4/73.2/69.8/76.7 → **71.6 ± 4.0**, 98%
  completion, **~123 s/task** (fastest local reasoner), $0 API.
- **MTP is a trap here.** Enabling the MTP speculative-decoding head **drops the score to 65.2** (3-run)
  for only ~10 s/task of speedup — a bad trade (speculative tokens occasionally accepted wrongly). At
  greedy-ish thinking sampling, **`--speculative-config` costs ~6 points; leave it off.**
- **What it does well.** No-MTP, it's **3rd-best local** — beats GLM/Gemma-A4B (~67) *and* is faster
  than them (123 s vs 233 s), nearly ties the 27B (73.2) and cloud gpt-5.4-mini (71.8). The MoE makes
  it the **fastest** local reasoner per-task at conc 8.
- **Where it stumbles.** Citation (16/run — still high) and higher run-to-run variance (sd 4.0) than
  the Gemmas. Like the 27B it lands **~6 below muxx (0.717)** — the Qwen3.6 family suits this solver a
  bit less than the Gemmas/DeepSeeks.
- **Verdict.** A real fast-local option once MTP is off: ~72 at conc-8 speed, free, data on the box.
  Just behind the 27B (73.2) and slightly faster.

### Nemotron-3-Super-120B-A12B — NVIDIA's "best Spark agent", doesn't win here (≈71.0)
- **What it is.** NVIDIA's flagship agentic model and its **officially recommended best agent for the DGX
  Spark** (hybrid Latent-MoE: Mamba-2 + MoE, 120B total / 12B active, NVFP4). Served via the official
  `vllm/vllm-openai:cu130-nightly` image; `nemotron_v3` + `qwen3_coder` parsers (serving notes in
  `README-local-models.md`).
- **Numbers.** 6 runs: **71.0 ± 2.3** (68.7–74.2), 99% completion, **$0 API**. The catch is **speed: ~431
  s/task** (max 3221 s — 53 min on one task), the **slowest local we measured** despite only 12B active —
  the 120B of resident weights are bandwidth-bound on the Spark's ~273 GB/s.
- **The headline.** NVIDIA's pick **lands below our best local Gemma-4-31B (83.3)** by ~12, and below
  Qwen3.6-27B (77.4); it roughly **ties the much smaller Qwen3.6-35B-A3B (71.6)**. Agent-tuning that tops
  *NVIDIA's own* agent benchmarks doesn't translate to ECOM1's demands.
- **Where it stumbles.** **Citation/grounding (14.7/run) is its wall** — the highest of the upper-half
  models, the same failure every local hits but more so. Otherwise a clean spread (security 3.2, fraud 3.8,
  dispatch 4.5, completion 0.8).
- **Verdict.** Capable and 99%-completing, but **no reason to pick it on the Spark**: Gemma-4-31B is +12
  and faster, the Qwen3.6-27B is +6 and ~2× faster. NVIDIA's "best Spark agent" claim rests on different
  benchmarks than grounding-heavy ops.

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
| **Kimi K2.6** (Moonshot, `kimi-k2.6`) | `kimiprod1`–`kimiprod10` | 79.6–90.0 (mean 86.6, $2.62/run) | 10 |
| GLM-5.2 (Together, `zai-org/GLM-5.2`) | `glm52tgprod1`–`glm52tgprod10` | 74.7–88.9 (mean 81.5, $3.63/run) | 10 |
| deepseek-v4-flash | `dsflash1`, `dsflash2` | 75.1, 79.1 | 2 |
| **Gemma-4-31B-IT** (thinking) | `g31prod1`–`g31prod3` | 84.5, 80.5, 84.9 | 3 |
| **Muse-Glimmer-30B-NVFP4** (RedHatAI W4A4, conc 16, `TASK_CAP_S=2400`) | `musenvfp4a`–`musenvfp4j` | 76.4–83.4 (mean 79.94 ± 0.80) | 10 |
| **Muse-Glimmer-30B** (dense, BF16, conc 16, `TASK_CAP_S=2400`) | `museprod1`, `museprod2` | 79.1, 76.7 (mean 77.9) | 2 |
| **Qwen3.8-27B-NVFP4** (Inferact modelopt W4A4, thinking, no-MTP, conc 16, `TASK_CAP_S=2400`) | `qwen38a`–`qwen38j` | 68.9–80.4 (mean 75.22 ± 1.07) | 10 |
| **Qwen3.6-27B-NVFP4-0815** (rev `ccdaab7e`, vLLM 0.26.1rc1, thinking, no-MTP, conc 4) | `q270815a`–`q270815j` | 75.7–83.4 (mean 80.23 ± 0.81) | 10 |
| **Qwen3.6-27B-NVFP4** (June rev `890bdef7`, NGC vLLM 26.05, thinking, **no-MTP**) | `q27nomtp1`–`q27nomtp6` | 78.4, 77.0, 74.3, 79.2, 76.8, 78.6 (mean 77.38 ± 0.72) | 6 |
| Qwen3.6-27B-NVFP4 (thinking, MTP — worse) | `q36prod1`–`q36prod3` | 72.8, 74.2, 72.6 | 3 |
| **Qwen3.6-35B-A3B-NVFP4** (thinking, MoE, **no-MTP**) | `q36nomtp1`–`q36nomtp6` | 75.9, 65.8, 68.4, 73.2, 69.8, 76.7 | 6 |
| Qwen3.6-35B-A3B-NVFP4 (thinking, MoE, MTP — worse) | `q36mprod1`–`q36mprod3` | 63.9, 63.2, 68.4 | 3 |
| Gemma-4-26B-A4B (thinking) | `gthinkprod1`–`gthinkprod3` | 65.5, 65.4, 70.1 | 3 |
| GLM-4.5-Air (gen13) | `glmprod5`–`glmprod8` | 66.6, 66.0, 69.2, 67.5 | 4 |
| GLM-4.5-Air (gen14) | `glmprod9`, `glmprod10` | 68.3, 68.4 | 2 |
| **ECOM1-32B-BF16** (Olmo-3.1-32B-Think + gpt-5.5 SFT, H200) | `32bv4a`–`32bv4c` | 53.8, 54.7, 60.2 (mean 56.2) | 3 |
| **ECOM1-32B-NVFP4A16** (GB10 Spark, `TASK_CAP_S=300`) | `sparkv4a`–`sparkv4j` | 51.8–57.2 (mean 54.6) | 10 |
| **ECOM1-8B-A1B-BF16** (LFM2.5-8B-A1B + gpt-5.5 SFT, H100) | `lfmsft1`–`lfmsft3` | 44.0, 42.0, 46.0 (mean 44.0) | 3 |
| **ECOM1-8B-A1B-BF16** (same weights, on the GB10) | `lfmsftgb1`–`lfmsftgb10` | 33.0–42.0 (mean 37.5) | 10 |
| gpt-oss-120b | `gptossprod1` | 52.8 | 1 |
| Qwen3-Thinking | `thinkprod1`–`thinkprod3` | 50.6, 52.4, 51.4 | 3 |
| Qwen3-Instruct (gen3–6) | `lg3prod`–`lg6prod` | 43.1, 36.7, 42.6, 40.0 | 4 |
| gpt-5.4-mini (xhigh) | `g54mini1`–`g54mini10` | 67.7–79.3 (mean 71.8) | 10 |
| gpt-5.5 (low, cost probe) | `g55cost` | 90.8 | 1 |

Serving + integration details: `README-local-models.md`. Full timing/cost breakdown:
`README-local-models-run-summary.md`.
