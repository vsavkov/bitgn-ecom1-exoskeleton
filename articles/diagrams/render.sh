#!/usr/bin/env bash
# Renders diagrams/<lang>/exoskeleton-*.mmd to images/<lang>/ for each language.
# Transparent canvas. Requires node/npx and a cached Chrome (puppeteer).
set -euo pipefail
cd "$(dirname "$0")/.."
CHROME=$(find "$HOME/.cache/puppeteer/chrome" -type f -name 'Google Chrome for Testing' 2>/dev/null | head -1 || true)
[ -n "${CHROME:-}" ] && export PUPPETEER_EXECUTABLE_PATH="$CHROME"
render_dir () {  # $1 = source dir, $2 = output dir
  mkdir -p "$2"
  for mmd in "$1"/exoskeleton-*.mmd; do
    name=$(basename "$mmd" .mmd)
    npx -y @mermaid-js/mermaid-cli@11 -i "$mmd" -o "$2/$name.png" \
      -b transparent -s 3 -c diagrams/mermaidConfig.json -p diagrams/puppeteerConfig.json
    echo "rendered $2/$name.png"
  done
}
for lang in ru en; do
  [ -d "diagrams/$lang" ] && render_dir "diagrams/$lang" "images/$lang"
done
