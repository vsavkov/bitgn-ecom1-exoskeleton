# AGENTS.md — articles & diagrams

Local rules for the `articles/` folder. These supplement the project's root `AGENTS.md` (which still applies).

This folder holds the architecture write-ups and the diagram sources for them. Core rule: **diagrams are drawn from text.** Images are generated artifacts — never hand-edit them.

## What lives where

- `ARCHITECTURE_RU.md` — the article in Russian; `ARCHITECTURE.md` — the article in English.
- `diagrams/ru/*.mmd` and `diagrams/en/*.mmd` — **diagram sources** (Mermaid), one folder per language. The source of truth for diagram content. Only edit these.
- `images/ru/*.png` and `images/en/*.png` — **generated** images (transparent background), one folder per language. These are what the articles embed.
- `diagrams/mermaidConfig.json` — Mermaid theme and typography.
- `diagrams/puppeteerConfig.json` — headless-Chrome flags (`--no-sandbox`).
- `diagrams/render.sh` — renders every diagram.
- `images/score_dashboard.png` — screenshot of the run heatmap, shared by both languages (the one exception: a real screenshot, not generated from Mermaid).

Names are paired per language: `diagrams/<lang>/exoskeleton-NN-name.mmd` → `images/<lang>/exoskeleton-NN-name.png`.

## How to render

```bash
bash diagrams/render.sh            # re-render every diagram into images/<lang>/
```

Under the hood it runs `@mermaid-js/mermaid-cli` via `npx`, rendering in headless Chrome. It needs Node and a cached Chrome (`~/.cache/puppeteer`); the script locates it and exports `PUPPETEER_EXECUTABLE_PATH` itself. A single file by hand:

```bash
npx -y @mermaid-js/mermaid-cli@11 \
  -i diagrams/ru/exoskeleton-01-architecture.mmd \
  -o images/ru/exoskeleton-01-architecture.png \
  -b transparent -s 3 \
  -c diagrams/mermaidConfig.json -p diagrams/puppeteerConfig.json
```

The essentials: `-b transparent` (transparent canvas) and `-s 3` (scale, for a crisp PNG).

## Style guide: make them clean and dark-theme-safe

Goal — tidy, "card"-style diagrams that read **on both light and dark** blog themes.

1. **Transparent canvas.** No white rectangle behind the whole image — only the nodes themselves. That's `-b transparent`.
2. **Light cards with dark text.** Every node carries its own light fill and dark text, so it reads on any page background. Never rely on the theme's background color.
3. **Color = role, not decoration.** Color encodes the article's core thesis, "the model proposes, the code disposes." Palette (`classDef`):

   | Class | Meaning | fill / stroke / text |
   |---|---|---|
   | `model` | node runs on the LLM (`gpt-5.4-mini` / `-nano`) | `#ede9fe` / `#7c3aed` (2px) / `#2e1065` |
   | `code` | deterministic code | `#f8fafc` / `#64748b` / `#0f172a` |
   | `env` | runtime tools and domain helpers | `#eff6ff` / `#3b82f6` / `#0c4a6e` |
   | `io` | input/output (the task, `answer`) | `#334155` / `#94a3b8` / `#f8fafc` |
   | `obs` | observability | `#f0fdf4` / `#16a34a` / `#14532d` |
   | `deny` | refusal / negative outcome | `#fef2f2` / `#ef4444` / `#7f1d1d` |
   | `ok` | success branch | `#ecfdf5` / `#10b981` / `#064e3b` |
   | `human` | interactive / human-driven phase | `#fffbeb` / `#f59e0b` / `#78350f` |

4. **Stages are light panels.** Tint subgraphs with light fills via `style <id> fill:..,stroke:..,color:#0f172a`: start `#eff6ff/#bfdbfe`, preflights `#fff7ed/#fed7aa`, main loop `#f5f3ff/#ddd6fe`, assembly `#ecfeff/#a5f3fc`.
5. **Edge labels** sit on a white "pill" (`edgeLabelBackground: #ffffff` in the config); otherwise the text disappears on a dark theme.
6. **Shapes:** process — rounded `("…")`; input/output — stadium `(["…"])`; decision — diamond `{"…"}`.
7. **Font — `IBM Plex Sans`** (modern, technical, full Cyrillic), set in `mermaidConfig.json`. We use it as a **system** font (installed in `~/Library/Fonts`) rather than embedding a web font: mermaid-cli measures node widths *before* an async web font loads, so an embedded `woff2` clips the labels. If the font isn't installed, install IBM Plex Sans (Font Book / Google Fonts), otherwise you get a neutral system fallback. Keep labels short, break lines with `<br/>`, separate with `·`.

Keep `classDef`/`class`/`style` **inside** each `.mmd` (Mermaid requires them in the diagram body) — that way the styling travels with the source.

## Workflow

1. Edit `diagrams/<lang>/*.mmd` (content and classes).
2. `bash diagrams/render.sh` — `images/<lang>/*.png` are regenerated.
3. The articles reference `images/<lang>/<name>.png`. Don't touch the PNGs by hand.

## Translated (English) article

Each language has its own folder: `diagrams/ru/` + `images/ru/` and `diagrams/en/` + `images/en/`. The English diagrams are translations of the Russian ones — only the labels differ; node IDs and `classDef`/`class`/`style` are identical, so layout and colors match. `render.sh` renders every language folder. The heatmap screenshot `images/score_dashboard.png` is language-neutral and shared by both articles. To update a diagram, edit the matching `diagrams/<lang>/*.mmd` and re-run `render.sh`.

## Facts & tone (mirrors the root AGENTS.md)

- Don't tune to specific tasks/snapshots: SKUs, basket ids, payment ids appear only as illustration; conclusions are stated as general rules.
- No Russian/English mixing inside an article's prose — the Russian version stays Russian, the English version stays English; use foreign terms only when they are genuine technical terms, glossed on first use.
- Report numbers the way the leaderboards do (share of passed tasks, Hall of Fame placements); don't invent precision.
