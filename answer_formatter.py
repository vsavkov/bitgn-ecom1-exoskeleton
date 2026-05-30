import json
from collections.abc import Callable, MutableSequence
from typing import TYPE_CHECKING, Any, ParamSpec, Sequence, TypeVar

from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field

from config import (
    CLI_CLR,
    CLI_RED,
    CLI_YELLOW,
    helper_model,
    helper_reasoning_effort,
    render_prompt,
)

if TYPE_CHECKING:
    P = ParamSpec("P")
    R = TypeVar("R")

    def traceable(*args: Any, **kwargs: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            return func

        return decorator

else:
    from langsmith import traceable


class FormattedAnswer(BaseModel):
    missed_elements: str = Field(
        description=(
            "Concise diagnostic note describing message-format requirements missing "
            "from the original message. Do not report missing citations, records, or "
            "grounding references here. Use an empty string if nothing was missing."
        )
    )
    formatted_message: str = Field(
        description="Final user-visible answer after formatting fixes."
    )


ANSWER_FORMATTER_PROMPT = render_prompt("answer_formatter.j2")


def _emit(message: str, output_lines: MutableSequence[str] | None) -> None:
    if output_lines is None:
        print(message)
        return
    output_lines.append(message)


def _parsed_response(resp) -> FormattedAnswer | None:
    output_parsed = getattr(resp, "output_parsed", None)
    if isinstance(output_parsed, FormattedAnswer):
        return output_parsed
    if isinstance(output_parsed, dict):
        return FormattedAnswer.model_validate(output_parsed)

    for item in resp.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, FormattedAnswer):
                return parsed
            if isinstance(parsed, dict):
                return FormattedAnswer.model_validate(parsed)
    return None


@traceable(run_type="llm", name="Answer Formatter")
def format_completion_message(
    client,
    *,
    task_text: str,
    task_type: str = "other",
    current_message: str,
    outcome: str,
    completed_steps_laconic: Sequence[str],
    grounding_refs: Sequence[str],
    agents_md: str = "",
    debug: bool,
    output_lines: MutableSequence[str] | None = None,
) -> str:
    payload = {
        "task_text": task_text,
        "task_type": task_type,
        "agents_md": agents_md,
        "current_message": current_message,
        "outcome": outcome,
        "completed_steps_laconic": list(completed_steps_laconic),
        "grounding_refs": list(grounding_refs),
    }

    try:
        resp = client.responses.parse(
            model=helper_model(),
            instructions=ANSWER_FORMATTER_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }
            ],
            text_format=FormattedAnswer,
            reasoning=Reasoning(effort=helper_reasoning_effort()),
            max_output_tokens=1024,
        )
    except Exception as exc:
        if debug:
            print(f"{CLI_RED}ERR formatter: {exc}{CLI_CLR}")
        return current_message

    parsed = _parsed_response(resp)
    if parsed is None:
        if debug:
            print(f"{CLI_RED}ERR formatter: no parsed formatted answer{CLI_CLR}")
        return current_message

    formatted_message = parsed.formatted_message.strip()
    if not formatted_message:
        if debug:
            print(f"{CLI_RED}ERR formatter: empty formatted message{CLI_CLR}")
        return current_message

    if parsed.missed_elements:
        _emit(f"{CLI_YELLOW}FORMAT MISSED{CLI_CLR}: {parsed.missed_elements}", output_lines)

    if formatted_message != current_message:
        _emit(
            f"{CLI_YELLOW}FORMAT{CLI_CLR}: {current_message} -> {formatted_message}",
            output_lines,
        )

    return formatted_message
