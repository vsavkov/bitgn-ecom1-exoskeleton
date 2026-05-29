from typing import Any, Literal

from openai import OpenAI
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field

from config import (
    helper_model,
    helper_reasoning_effort,
    openai_client_kwargs,
    render_prompt,
)


BasketSelector = Literal["newest", "oldest", "none"]


class TaskClassification(BaseModel):
    explicit_basket_id: str = Field(
        default="",
        description=(
            "Literal basket id like 'basket_145' if the task names one. Use an "
            "empty string when no basket id appears."
        ),
    )
    checkout_intent: bool = Field(
        default=False,
        description=(
            "True only when the task asks to check out, finalize, complete, "
            "ring up, pay for, or process a basket/cart/order."
        ),
    )
    basket_selector: BasketSelector = Field(
        default="none",
        description=(
            "Deterministic selector when checkout_intent is true and no explicit "
            "basket id is given. 'newest', 'oldest', or 'none'."
        ),
    )


TASK_CLASSIFIER_PROMPT = render_prompt("task_classifier.j2")


def _parsed_response(resp: Any) -> TaskClassification | None:
    output_parsed = getattr(resp, "output_parsed", None)
    if isinstance(output_parsed, TaskClassification):
        return output_parsed
    if isinstance(output_parsed, dict):
        return TaskClassification.model_validate(output_parsed)

    for item in resp.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, TaskClassification):
                return parsed
            if isinstance(parsed, dict):
                return TaskClassification.model_validate(parsed)
    return None


def classify_task(
    client: Any,
    task_text: str,
) -> TaskClassification:
    """Classify ``task_text`` into a structured router signal.

    ``client`` is any object that exposes ``responses.parse(**kwargs)`` matching
    the OpenAI Responses API (the real OpenAI client passes; tests pass a fake).
    Pass ``None`` to construct a fresh OpenAI client from config.
    """
    if not task_text.strip():
        return TaskClassification()

    if client is None:
        client = OpenAI(**openai_client_kwargs())

    try:
        resp = client.responses.parse(
            model=helper_model(),
            instructions=TASK_CLASSIFIER_PROMPT,
            input=[{"role": "user", "content": task_text}],
            text_format=TaskClassification,
            reasoning=Reasoning(effort=helper_reasoning_effort()),
            max_output_tokens=512,
        )
    except Exception:
        # On any classifier failure (timeout, schema mismatch, model error)
        # fall back to the empty classification so preflights become no-ops and
        # the main agent decides without injected hints.
        return TaskClassification()

    parsed = _parsed_response(resp)
    if parsed is None:
        return TaskClassification()
    return parsed
