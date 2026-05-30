from payment_recovery import (
    mentions_paid_terminal_state,
    payment_ids_from_refs_and_text,
    payment_recovery_outcome_for_terminal_state,
    payment_recovery_message_with_retry_timestamp,
    retry_available_at_from_policy_text,
)


def test_payment_ids_from_refs_and_text_merges_sources() -> None:
    assert payment_ids_from_refs_and_text(
        ["/proc/payments/pay_002.json"],
        "Recover pay_034 safely.",
    ) == {"pay_002", "pay_034"}


def test_retry_available_at_from_policy_text_matches_payment() -> None:
    content = """
    - payment_id: pay_002
    - retry_available_at: 2024-07-18T14:49:48Z
    """

    assert (
        retry_available_at_from_policy_text(content, payment_ids={"pay_002"})
        == "2024-07-18T14:49:48Z"
    )
    assert retry_available_at_from_policy_text(content, payment_ids={"pay_034"}) == ""


def test_payment_recovery_message_with_retry_timestamp_appends_once() -> None:
    assert (
        payment_recovery_message_with_retry_timestamp(
            "OUTCOME_NONE_UNSUPPORTED",
            retry_available_at="2024-07-18T14:49:48Z",
        )
        == "OUTCOME_NONE_UNSUPPORTED: retry blocked until 2024-07-18T14:49:48Z"
    )
    assert (
        payment_recovery_message_with_retry_timestamp(
            "retry blocked until 2024-07-18T14:49:48Z",
            retry_available_at="2024-07-18T14:49:48Z",
        )
        == "retry blocked until 2024-07-18T14:49:48Z"
    )


def test_mentions_paid_terminal_state_matches_status_phrasing() -> None:
    assert mentions_paid_terminal_state("pay_031 has status paid")
    assert mentions_paid_terminal_state("payment_status=paid")
    assert mentions_paid_terminal_state("payment status: paid")
    assert mentions_paid_terminal_state("Payment is already paid.")
    assert not mentions_paid_terminal_state("payment_status=requires_3ds_action")


def test_payment_recovery_terminal_paid_state_is_unsupported() -> None:
    assert (
        payment_recovery_outcome_for_terminal_state(
            task_type="payment_recovery",
            outcome="OUTCOME_NONE_CLARIFICATION",
            message="Payment pay_069 is already paid.",
            completed_steps_laconic=[],
        )
        == "OUTCOME_NONE_UNSUPPORTED"
    )
    assert (
        payment_recovery_outcome_for_terminal_state(
            task_type="payment_recovery",
            outcome="OUTCOME_NONE_CLARIFICATION",
            message="Need more detail.",
            completed_steps_laconic=["Confirmed pay_031 has status paid."],
        )
        == "OUTCOME_NONE_UNSUPPORTED"
    )
    assert (
        payment_recovery_outcome_for_terminal_state(
            task_type="payment_recovery",
            outcome="OUTCOME_NONE_CLARIFICATION",
            message="Which payment?",
            completed_steps_laconic=[],
        )
        == "OUTCOME_NONE_CLARIFICATION"
    )
