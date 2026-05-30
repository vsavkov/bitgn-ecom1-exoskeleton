import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field

from config import helper_model, helper_reasoning_effort, render_prompt
from payment_recovery import ISO_TIMESTAMP_RE

if TYPE_CHECKING:
    P = ParamSpec("P")
    R = TypeVar("R")

    def traceable(*args: Any, **kwargs: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            return func

        return decorator

else:
    from langsmith import traceable


class PaymentRecoveryReview(BaseModel):
    already_paid_terminal_state: bool = Field(
        description=(
            "True only when the completion evidence says the target payment is "
            "already paid or has payment_status/status paid."
        )
    )
    retry_lockout_state: bool = Field(
        description=(
            "True only when the completion evidence says 3DS recovery is blocked "
            "by a retry lockout, retry delay, cooldown, or retry_available_at policy."
        )
    )
    retry_available_at: str = Field(
        description=(
            "Exact ISO timestamp for retry availability when supplied in the "
            "completion evidence; otherwise an empty string."
        )
    )
    formatted_message: str = Field(
        description=(
            "Final answer message. If a terminal state is true, it must make "
            "that state explicit; otherwise it must equal the current message."
        )
    )


PAYMENT_RECOVERY_REVIEW_PROMPT = render_prompt("payment_recovery_review.j2")


def _fallback_review(message: str) -> PaymentRecoveryReview:
    return PaymentRecoveryReview(
        already_paid_terminal_state=False,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message=message,
    )


def _parsed_response(resp: Any) -> PaymentRecoveryReview | None:
    output_parsed = getattr(resp, "output_parsed", None)
    if isinstance(output_parsed, PaymentRecoveryReview):
        return output_parsed
    if isinstance(output_parsed, dict):
        return PaymentRecoveryReview.model_validate(output_parsed)

    for item in resp.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, PaymentRecoveryReview):
                return parsed
            if isinstance(parsed, dict):
                return PaymentRecoveryReview.model_validate(parsed)
    return None


@traceable(run_type="llm", name="Payment Recovery Review")
def review_payment_recovery_state(
    client: Any,
    *,
    task_text: str,
    task_type: str,
    outcome: str,
    current_message: str,
    completed_steps_laconic: Sequence[str],
    grounding_refs: Sequence[str],
) -> PaymentRecoveryReview:
    if task_type != "payment_recovery" or outcome not in {
        "OUTCOME_NONE_CLARIFICATION",
        "OUTCOME_NONE_UNSUPPORTED",
    }:
        return _fallback_review(current_message)

    payload = {
        "task_text": task_text,
        "outcome": outcome,
        "current_message": current_message,
        "completed_steps_laconic": list(completed_steps_laconic),
        "grounding_refs": list(grounding_refs),
    }

    try:
        resp = client.responses.parse(
            model=helper_model(),
            instructions=PAYMENT_RECOVERY_REVIEW_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }
            ],
            text_format=PaymentRecoveryReview,
            reasoning=Reasoning(effort=helper_reasoning_effort()),
            max_output_tokens=512,
        )
    except Exception:
        return _fallback_review(current_message)

    parsed = _parsed_response(resp)
    if parsed is None:
        return _fallback_review(current_message)

    formatted_message = parsed.formatted_message.strip() or current_message
    retry_available_at = parsed.retry_available_at.strip()
    if retry_available_at and not ISO_TIMESTAMP_RE.fullmatch(retry_available_at):
        retry_available_at = ""
    return parsed.model_copy(
        update={
            "formatted_message": formatted_message,
            "retry_available_at": retry_available_at,
        }
    )
