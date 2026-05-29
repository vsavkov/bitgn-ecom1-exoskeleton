import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ParamSpec, Sequence, TypeVar

from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field

from config import (
    CLI_CLR,
    CLI_RED,
    CLI_YELLOW,
    answer_formatter_model,
    answer_formatter_reasoning_effort,
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
            "Concise diagnostic note describing formatting requirements missing "
            "from the original message. Use an empty string if nothing was missing."
        )
    )
    formatted_message: str = Field(
        description="Final user-visible answer after formatting fixes."
    )


ANSWER_FORMATTER_PROMPT = render_prompt("answer_formatter.j2")


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
    current_message: str,
    outcome: str,
    completed_steps_laconic: Sequence[str],
    grounding_refs: Sequence[str],
    debug: bool,
) -> str:
    payload = {
        "task_text": task_text,
        "current_message": current_message,
        "outcome": outcome,
        "completed_steps_laconic": list(completed_steps_laconic),
        "grounding_refs": list(grounding_refs),
    }

    try:
        resp = client.responses.parse(
            model=answer_formatter_model(),
            instructions=ANSWER_FORMATTER_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }
            ],
            text_format=FormattedAnswer,
            reasoning=Reasoning(effort=answer_formatter_reasoning_effort()),
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
        print(f"{CLI_YELLOW}FORMAT MISSED{CLI_CLR}: {parsed.missed_elements}")

    if formatted_message != current_message:
        print(f"{CLI_YELLOW}FORMAT{CLI_CLR}: {current_message} -> {formatted_message}")

    return formatted_message
