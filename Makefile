# AICODE-NOTE: Keep these wrappers aligned with the README commands so the sample
# stays trivial to run from a fresh checkout without inventing parallel workflows.

.PHONY: sync run task dump runs-html check

sync:
	uv sync

run:
	uv run python main.py

task:
	@if [ -z "$(TASKS)" ]; then echo "usage: make task TASKS='t01 t03'"; exit 1; fi
	uv run python main.py $(TASKS)

runs-html:
	uv run python scripts/generate_runs_report.py

check:
	uv run ruff check .
	uv run ty check .
