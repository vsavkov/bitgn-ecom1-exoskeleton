from payment_recovery import (
    payment_ids_from_refs_and_text,
    payment_recovery_message_with_retry_timestamp,
    retry_available_at_from_policy_text,
)


def test_payment_ids_from_refs_and_text_merges_sources() -> None:
    assert payment_ids_from_refs_and_text(
        ["/proc/payments/pay_002.json"],
        "Recover pay_034 safely.",
    ) == {"pay_002", "pay_034"}


def test_payment_ids_from_prod_refs_and_text_merges_sources() -> None:
    assert payment_ids_from_refs_and_text(
        ["/proc/payment-ledger/cust-0114/pay-0014.json"],
        "Recover pay-0034 safely.",
    ) == {"pay-0014", "pay-0034"}


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
