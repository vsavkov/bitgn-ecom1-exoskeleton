# AGENTS.md

Это репозиторий разрабатываемого агента для челенджа ECOM1 for agentic ecommerce.

Цитата с сайта челенджа

> You write an agent, connect it to BitGN via API, and solve tasks inside a deterministic simulated commercial environment. BitGN evaluates observable outcomes such as tool calls, state changes, required flags/references, and forbidden actions avoided.

Агент работает через API в специальном Sandbox. На вход подаются разные тестовые задачи общения клиента или сотрудника с агентом.
Каждая задача оценивается. По результатам тестам формируется Leaderboard с лучшими результатами.

Ты должен мне помочь разработать агента, который получит максимум очков. 
При этом нужно не затачиваться под конкретные задачи, а выявлять на основе них нюансы и правила поведения,
которые должны быть отражены в агенте.

В [agent_original.py](agent_original.py) оригинальная версия агента, 
которая предоставляется как пример с https://github.com/bitgn/sample-agents/tree/main/ecom-py.

**Важно**
* В runs/bitgn__ecom1-dev история запусков в рамках периода обкатки на `BENCH_ID=bitgn/ecom1-dev`.
* В runs/bitgn__ecom1 история запусков в рамках текущего цикла улучшений под проходящий сейчас челендж `BENCH_ID=bitgn/ecom1`.

**Сейчас мы дорабатываем** под `BENCH_ID=bitgn/ecom1`.
Используй историю коммитов и запусков для dev, чтобы понимать, что когда и для чего менялось.
В PROD челендже между запусками может меняться довольно много в OS в одних и тех же тасках.
Поэтому нужно аккуратно анализировать историю запусков одного и того же таска.
Таски между `ecom1-dev` и `ecom1` не одно и то же. `t01` в одном не то же самое, что `t01` в другом.

## Commands

- Install or update the local environment: `make sync`
- Run the full benchmark via Make: `make run`
- Run selected tasks via Make: `make task TASKS="t01 t04"`
- Check linting and typing after any code changes: `make check`
- Run unit tests after any code changes: `make test`

После любых изменений в Python-коде или конфигурации проекта прогоняй `make check test`.

Прогоны тасков самостоятельно не запускать, чтобы не потратить лимиты на прогоны.

## Tests

- Тестовые файлы размещай в соответствии с исходными файлами: `module.py` покрывается `tests/test_module.py`, скрипты из `scripts/foo.py` покрываются `tests/scripts/test_foo.py`.
- Pure functions и детерминированные helper-ы покрывай unit-тестами без внешних API, BitGN runtime и LangSmith. Для runtime-адаптеров используй fake/stub объекты.

## Commits

Оформляй коммиты с подробными пояснениями, что в него включено.

## BITGN Architecture

Важные наблюдения, которые нужно учитывать при улучшении агента:

- Челендж состоит из `benchmark -> run -> trial`. `get_benchmark` дает описание и preview/hint задач, но конкретные инструкции внутри `start_run/start_trial` могут параметризоваться иначе; нельзя затачиваться на дословные тексты из preview.
- Каждый trial получает отдельный runtime URL и изолированный слепок ECOM OS. Действия оцениваются по наблюдаемому результату: вызовы runtime tools, изменения состояния, финальный `answer`, grounding refs, правильный outcome и отсутствие запрещенных мутаций.
- `StartPlayground` в SDK есть, но сервер отвечает, что sandbox mode больше не поддерживается. Для разведки окружения нужен normal run/trial; такие trial нужно аккуратно закрывать и не считать их продуктивными score-прогонами.
- Runtime выглядит как Unix-подобная файловая система с корнем `/`: `/AGENTS.MD`, `/docs`, `/proc`, `/bin`, `/run/actions`. Все пути в запросах к runtime tools должны быть абсолютными.
- Grounding refs важны для оценки. Для yes/no ответов явно использовать `<YES>` или `<NO>`, как требует `/AGENTS.MD`.
- В коде не завязываться на структуру папок, она может меняться

## LangSmith Trace Analysis

Для разбора уже выполненного прогона используй read-only helper `scripts/langsmith_trace_report.py`; он читает LangSmith traces и не стартует BitGN run/trial.

- Список последних root traces: `uv run python scripts/langsmith_trace_report.py --limit 80`.
- Несколько задач по индексам: `uv run python scripts/langsmith_trace_report.py --limit 80 --indices 3,6,14-16`.
- Детальный разбор с child LLM/tool spans: `uv run python scripts/langsmith_trace_report.py --limit 80 --indices 38-40 --children --output-limit 3000`.
- Один trace по id: `uv run python scripts/langsmith_trace_report.py --run-id <RUN_ID> --children`.
- Индексы в helper — это порядок root traces по `start_time`; перед сопоставлением с `tXX` проверь, нет ли более ранних одиночных запусков в том же проекте.

## Run Reports

Отчеты по прогонам складывай в `reports/report_run<N>_<YYYYMMDD_HHMMSS>.md`.

- В начале укажи источник данных: score output пользователя, LangSmith project, диапазон root spans/revision и факт, что BitGN прогоны во время анализа не запускались.
- Добавь сводку: итоговый score, количество full/zero/partial задач и основные группы проблем.
- Добавь общую таблицу по всем задачам: task, score, деталь из grader output, категория.
- Для неудачных и частичных кейсов опиши: затронутые task id, наблюдения из trace, корневую причину и обобщаемое предложение по улучшению агента.
- Не затачивай выводы под конкретные SKU, basket id, payment id или customer id; используй их только как evidence в отчете, а предложения формулируй как общие правила поведения агента.
