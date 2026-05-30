from dataclasses import dataclass, field
from typing import Any

from payment_recovery_review import (
    PaymentRecoveryReview,
    _parsed_response,
    review_payment_recovery_state,
)


@dataclass
class FakeContent:
    parsed: Any = None


@dataclass
class FakeOutputItem:
    type: str = "message"
    content: list[FakeContent] = field(default_factory=list)


@dataclass
class FakeResponse:
    output_parsed: Any = None
    output: list[FakeOutputItem] = field(default_factory=list)


class FakeResponses:
    def __init__(self, *, payload: Any = None, raise_exc: Exception | None = None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResponse(output_parsed=self.payload)


class FakeClient:
    def __init__(self, responses: FakeResponses):
        self.responses = responses


def test_parsed_response_accepts_structured_output() -> None:
    parsed = _parsed_response(
        FakeResponse(
            output_parsed={
                "already_paid_terminal_state": True,
                "retry_lockout_state": False,
                "retry_available_at": "",
                "formatted_message": "payment is already paid",
            }
        )
    )

    assert parsed == PaymentRecoveryReview(
        already_paid_terminal_state=True,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message="payment is already paid",
    )

    nested = _parsed_response(
        FakeResponse(
            output=[
                FakeOutputItem(
                    content=[
                        FakeContent(
                            parsed={
                                "already_paid_terminal_state": False,
                                "retry_lockout_state": False,
                                "retry_available_at": "",
                                "formatted_message": "OUTCOME_NONE_UNSUPPORTED",
                            }
                        )
                    ]
                )
            ]
        )
    )
    assert nested == PaymentRecoveryReview(
        already_paid_terminal_state=False,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message="OUTCOME_NONE_UNSUPPORTED",
    )


def test_review_payment_recovery_state_uses_helper_payload() -> None:
    responses = FakeResponses(
        payload=PaymentRecoveryReview(
            already_paid_terminal_state=True,
            retry_lockout_state=False,
            retry_available_at="",
            formatted_message="OUTCOME_NONE_UNSUPPORTED: payment is already paid",
        )
    )
    client = FakeClient(responses)

    review = review_payment_recovery_state(
        client,
        task_text="Recover 3DS for pay_031 safely.",
        task_type="payment_recovery",
        outcome="OUTCOME_NONE_CLARIFICATION",
        current_message="OUTCOME_NONE_UNSUPPORTED",
        completed_steps_laconic=["Confirmed pay_031 has status paid."],
        grounding_refs=["/proc/payments/pay_031.json"],
    )

    assert review.already_paid_terminal_state is True
    assert review.formatted_message == "OUTCOME_NONE_UNSUPPORTED: payment is already paid"
    assert responses.calls
    assert responses.calls[0]["text_format"] is PaymentRecoveryReview


def test_review_payment_recovery_state_skips_unrelated_tasks() -> None:
    responses = FakeResponses()
    client = FakeClient(responses)

    review = review_payment_recovery_state(
        client,
        task_text="Check out basket_001.",
        task_type="checkout",
        outcome="OUTCOME_NONE_CLARIFICATION",
        current_message="Which basket?",
        completed_steps_laconic=[],
        grounding_refs=[],
    )

    assert review == PaymentRecoveryReview(
        already_paid_terminal_state=False,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message="Which basket?",
    )
    assert responses.calls == []


def test_review_payment_recovery_state_falls_back_on_helper_error() -> None:
    client = FakeClient(FakeResponses(raise_exc=RuntimeError("network")))

    review = review_payment_recovery_state(
        client,
        task_text="Recover 3DS for pay_031 safely.",
        task_type="payment_recovery",
        outcome="OUTCOME_NONE_UNSUPPORTED",
        current_message="OUTCOME_NONE_UNSUPPORTED",
        completed_steps_laconic=["Confirmed pay_031 has status paid."],
        grounding_refs=["/proc/payments/pay_031.json"],
    )

    assert review == PaymentRecoveryReview(
        already_paid_terminal_state=False,
        retry_lockout_state=False,
        retry_available_at="",
        formatted_message="OUTCOME_NONE_UNSUPPORTED",
    )


def test_review_payment_recovery_state_strips_retry_timestamp() -> None:
    client = FakeClient(
        FakeResponses(
            payload=PaymentRecoveryReview(
                already_paid_terminal_state=False,
                retry_lockout_state=True,
                retry_available_at=" 2024-07-18T14:49:48Z ",
                formatted_message=(
                    "OUTCOME_NONE_UNSUPPORTED: retry blocked until "
                    "2024-07-18T14:49:48Z"
                ),
            )
        )
    )

    review = review_payment_recovery_state(
        client,
        task_text="Recover 3DS for pay_031 safely.",
        task_type="payment_recovery",
        outcome="OUTCOME_NONE_UNSUPPORTED",
        current_message="OUTCOME_NONE_UNSUPPORTED",
        completed_steps_laconic=["A dated retry lockout applies."],
        grounding_refs=["/proc/payments/pay_031.json"],
    )

    assert review.retry_lockout_state is True
    assert review.retry_available_at == "2024-07-18T14:49:48Z"


def test_review_payment_recovery_state_discards_non_iso_retry_timestamp() -> None:
    client = FakeClient(
        FakeResponses(
            payload=PaymentRecoveryReview(
                already_paid_terminal_state=False,
                retry_lockout_state=True,
                retry_available_at="tomorrow",
                formatted_message="OUTCOME_NONE_UNSUPPORTED: retry blocked",
            )
        )
    )

    review = review_payment_recovery_state(
        client,
        task_text="Recover 3DS for pay_031 safely.",
        task_type="payment_recovery",
        outcome="OUTCOME_NONE_UNSUPPORTED",
        current_message="OUTCOME_NONE_UNSUPPORTED",
        completed_steps_laconic=["A dated retry lockout applies."],
        grounding_refs=["/proc/payments/pay_031.json"],
    )

    assert review.retry_lockout_state is True
    assert review.retry_available_at == ""
