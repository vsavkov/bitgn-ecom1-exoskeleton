# DGX Spark (GB10) — Thermal Monitoring Under Inference Load

How to watch a DGX Spark's temperature, power, and clocks while it serves an LLM, and what a sustained
run actually does to it. Goal: see the idle→load ramp, the steady-state, and **whether it thermally
throttles**.

## The box

- GB10 Grace-Blackwell, 128 GB unified memory, ~273 GB/s.
- **Sensors available:**
  - `nvidia-smi` reports GPU **temperature, power, SM/graphics clocks, util** (note: `memory.used`
    comes back `N/A` — it's unified memory).
  - **`tegrastats` is NOT installed** on the stock image.
  - **7 ACPI thermal zones** (`/sys/class/thermal/thermal_zone*`, all type `acpitz`) give the board/SoC
    temps. They're unlabeled; track the **hottest** as the board hotspot.

## Setup

Everything runs **on the Spark**: a logger samples the sensors to a CSV while a load generator drives the
served model. Capture ~30 s of idle baseline before starting the load.

### 1. The logger (`thermlog.sh`) — samples every 5 s

```bash
#!/bin/bash
# GPU (nvidia-smi) + board hotspot (hottest thermal zone), every 5s, as CSV.
echo "time,gpu_C,power_W,sm_MHz,util_pct,board_max_C"
while true; do
  g=$(nvidia-smi --query-gpu=temperature.gpu,power.draw,clocks.sm,utilization.gpu --format=csv,noheader,nounits | tr -d ' ')
  bmax=$(cat /sys/class/thermal/thermal_zone*/temp | sort -rn | head -1 | awk '{printf "%.1f",$1/1000}')
  echo "$(date +%T),$g,$bmax"
  sleep 5
done
```

Run it detached (e.g. in `tmux`/`screen`, or):

```bash
nohup bash thermlog.sh > thermal.csv 2>&1 &
```

### 2. The load

Serve a model with vLLM on the Spark (here: Nemotron-3-Super on `:8888`), then drive **sustained**
inference against the local endpoint. The kind of load matters a lot (see Observations) — two
reproducible generators, parameterised by env vars:

```js
// stress.mjs — continuous parallel requests, saturating the server's slots (no gaps).
//   DECODE-bound  (memory-bandwidth-bound, runs cool):  MAXTOK=3000  PROMPT=<short>
//   PREFILL-bound (compute-bound, the real ceiling):    MAXTOK=8     PROMPT=<~11k tokens>
const URL    = process.env.URL    || "http://localhost:8888/v1/chat/completions";
const MODEL  = process.env.MODEL  || "nemotron-3-super";
const WORKERS= Number(process.env.WORKERS || 12);
const MAXTOK = Number(process.env.MAXTOK  || 8);
const PROMPT = process.env.PROMPT || ("The quick brown fox jumps over the lazy dog. ".repeat(900)); // ~11k tokens
async function worker(){ while(true){ try{
  const r = await fetch(URL,{method:"POST",headers:{"Content-Type":"application/json"},
    body: JSON.stringify({model:MODEL, messages:[{role:"user",content:PROMPT}], max_tokens:MAXTOK, temperature:0})});
  await r.json();
}catch(e){} } }
for (let i=0;i<WORKERS;i++) worker();
```

```bash
# prefill ceiling (compute-bound):
node stress.mjs
# decode (memory-bound):
MAXTOK=3000 WORKERS=16 PROMPT="Write a very long essay." node stress.mjs
```

Keep the client's worker count ≥ the server's `--max-num-seqs` so the GPU slots stay full. A **real
agentic workload** (an LLM agent taking tool-calling steps) behaves like a *bursty* prefill load — shown
as a reference row below.

### 3. Analyze

```bash
tail -n +2 thermal.csv | awk -F, '{if($2>tmax)tmax=$2; if($3>pmax)pmax=$3; if($4>cmax)cmax=$4; if($4<cmin||!cmin)cmin=$4; if($6>bmax)bmax=$6; ts+=$2; n++} END{printf "GPU peak %d C, avg %.0f | power peak %.0f W | SM clock %d-%d MHz | board peak %.0f C\n",tmax,ts/n,pmax,cmin,cmax,bmax}'
```

## Observations — Nemotron-3-Super, real agentic load (~13 min, 158 samples)

| metric | idle | steady-state under load |
|---|---|---|
| **GPU die temp** | 33–34 °C | **63–72 °C** (avg ~60, oscillates with the bursty load) |
| **SM clock** | 2405 MHz | **2450–2528 MHz — stays *boosted above* idle base** |
| **power draw** | ~11 W | **40 W ↔ 70 W** (peak 69.8) |
| **GPU util** | 0 % | 96 % |
| **board hotspot** (acpitz) | 35 °C | **up to 81 °C** |

- **Ramp:** GPU 33 → ~70 °C in **~3 minutes**, then stable for the rest of the run.
- **No thermal throttling.** The SM clock **never dropped below its 2405 MHz idle base** — it held
  *above* it (2450–2528) the whole time, even at 72 °C. GPU dies typically throttle ~85–90 °C, so
  there's comfortable margin.
- **Power is bursty** (40↔70 W) because the agentic workload has gaps; the GPU is *not* pinned at max
  continuous draw. A pure compute load would draw more and run hotter.
- **The board hotspot (81 °C) runs warmer than the GPU die (72 °C)** — the `acpitz` sensor is likely the
  SoC/VRM area. Warm but normal for a compact box.

**Verdict:** the GB10 sustains a realistic LLM inference run **without throttling**, GPU die ~63–72 °C,
board hotspot ~81 °C, peak ~70 W. Thermal headroom is not the limiter for this kind of workload.

## Harder stress — what actually heats the GB10 (compute vs memory)

The agentic load is *bursty*. To find the real ceiling, drive the endpoint **continuously** (parallel
workers saturating the server's slots, no gaps) — two ways, which behave very differently:

- **Decode-bound** — short prompt, long generation (`max_tokens 3000`, 16 workers). GPU sits at 96 % util
  but only **~40 W / 61 °C**: long-form *decode* is memory-bandwidth-bound, so it barely heats the chip.
- **Prefill-bound** — ~11k-token prompts, `max_tokens 8`, 12 workers. Continuous *compute*: **~78 W /
  76–82 °C GPU / 90 °C board** — the real ceiling.

| load | util | power | GPU temp | board | SM clock |
|---|:--:|:--:|:--:|:--:|:--:|
| idle | 0 % | 11 W | 33 °C | 35 °C | 2405 (base) |
| **decode** stress (memory-bound) | 96 % | 40 W | 61 °C | 71 °C | 2509 (full boost) |
| agentic workload (bursty prefill) | 96 % | 40↔70 W | 72 °C | 82 °C | 2509 |
| **prefill** stress (compute-bound) | 96 % | **78 W** | **76–82 °C** | **90 °C** | **~2440 (boost trimmed)** |

**Takeaways:**
1. **Compute heats it, not utilization.** Decode and prefill both read 96 % util, but prefill draws ~2×
   the power (78 vs 40 W) and runs ~17 °C hotter. Power tracks *compute intensity* (prefill matmuls), not
   the util-% number — `nvidia-smi util` alone is a poor proxy for thermal load here.
2. **Soft boost management, no hard throttle.** Under sustained compute the SM clock trims from the 2509
   boost to **~2440 — but never below the 2405 idle base**. The GB10 pulls back *boost* to stay in its
   power/thermal envelope; it does not lose baseline performance to heat (and it plateaued — no runaway).
3. **The board/SoC is the hotspot, not the GPU die** — 90 °C board vs 82 °C die. If anything thermally
   limits this box under sustained compute, it's the SoC/VRM area, not the GPU.

## Caveats

- **Even the prefill ceiling is bounded by the server config** (`--max-num-seqs`): only that many
  sequences run at once. A larger batch / a pure GPU-burn kernel could push higher still.
- `nvidia-smi memory.used` = `N/A` (unified memory).
- `acpitz` zones are unlabeled; the hottest is reported as the board hotspot.
