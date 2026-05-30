from types import SimpleNamespace

from refund_preflight import (
    amount_refund_clarification_preflight,
    rejected_return_clarification_preflight,
    refund_amount_cents_from_text,
)


class FakeVM:
    def __init__(self, *, id_stdout: str, payment_rows: str) -> None:
        self.id_stdout = id_stdout
        self.payment_rows = payment_rows

    def exec(self, request):
        if request.path == "/bin/id":
            return SimpleNamespace(stdout=self.id_stdout, exit_code=0)
        if request.path == "/bin/sql":
            return SimpleNamespace(stdout=self.payment_rows, exit_code=0)
        raise AssertionError(request.path)


def test_refund_amount_cents_from_text_parses_common_forms() -> None:
    assert refund_amount_cents_from_text("please refund my purchase for EUR 320") == 32000
    assert refund_amount_cents_from_text("please refund my purchase for € 125") == 12500
    assert refund_amount_cents_from_text("refund 10 euros") == 1000
    assert refund_amount_cents_from_text("refund pay_001") is None


def test_amount_refund_clarification_preflight_asks_for_multiple_payments() -> None:
    result = amount_refund_clarification_preflight(
        FakeVM(
            id_stdout="user: cust_092\nroles: customer\n",
            payment_rows=(
                "payment_id,record_path,payment_status,payment_created_at\n"
                "pay_029,/proc/payments/pay_029.json,paid,2021-08-01T00:00:00Z\n"
                "pay_030,/proc/payments/pay_030.json,paid,2021-08-02T00:00:00Z\n"
            ),
        ),
        task_text="please refund my purchase for EUR 320",
    )

    assert result is not None
    assert result.message == (
        "Which EUR 320.00 purchase should I refund? I found multiple matching "
        "payments: pay_029, pay_030."
    )
    assert result.row_refs == [
        "/proc/payments/pay_029.json",
        "/proc/payments/pay_030.json",
    ]


def test_amount_refund_clarification_preflight_skips_unique_payment() -> None:
    assert (
        amount_refund_clarification_preflight(
            FakeVM(
                id_stdout="user: cust_092\nroles: customer\n",
                payment_rows=(
                    "payment_id,record_path,payment_status,payment_created_at\n"
                    "pay_029,/proc/payments/pay_029.json,paid,2021-08-01T00:00:00Z\n"
                ),
            ),
            task_text="please refund my purchase for EUR 320",
        )
        is None
    )


def test_amount_refund_clarification_preflight_skips_explicit_payment() -> None:
    assert (
        amount_refund_clarification_preflight(
            FakeVM(
                id_stdout="user: cust_092\nroles: customer\n",
                payment_rows="payment_id,record_path,payment_status,payment_created_at\n",
            ),
            task_text="please refund pay_029",
        )
        is None
    )


def test_rejected_return_clarification_preflight_handles_explicit_payment() -> None:
    result = rejected_return_clarification_preflight(
        FakeVM(
            id_stdout="user: cust_092\nroles: customer\n",
            payment_rows=(
                "payment_id,payment_record_path,customer_id,payment_amount_cents,"
                "payment_currency,return_id,return_record_path,return_status\n"
                "pay_029,/proc/payments/pay_029.json,cust_092,32000,EUR,"
                "ret_015,/proc/returns/ret_015.json,rejected\n"
            ),
        ),
        task_text=(
            "refund my payment pay_029 for EUR 320, or I, as EU resident, "
            "will issue a chargeback from my bank"
        ),
    )

    assert result is not None
    assert result.message == (
        "I found payment pay_029 and linked return ret_015, but that return is "
        "rejected. Please clarify whether you want to dispute the rejected "
        "return or provide a new refund basis."
    )
    assert result.doc_refs == ["/docs/security.md", "/docs/returns.md"]
    assert result.row_refs == [
        "/proc/payments/pay_029.json",
        "/proc/returns/ret_015.json",
    ]


def test_rejected_return_clarification_preflight_handles_unique_amount() -> None:
    result = rejected_return_clarification_preflight(
        FakeVM(
            id_stdout="user: cust_092\nroles: customer\n",
            payment_rows=(
                "payment_id,payment_record_path,customer_id,payment_amount_cents,"
                "payment_currency,return_id,return_record_path,return_status\n"
                "pay_029,/proc/payments/pay_029.json,cust_092,32000,EUR,"
                "ret_015,/proc/returns/ret_015.json,rejected\n"
            ),
        ),
        task_text="please refund my purchase for EUR 320",
    )

    assert result is not None
    assert result.row_refs == [
        "/proc/payments/pay_029.json",
        "/proc/returns/ret_015.json",
    ]


def test_rejected_return_clarification_preflight_skips_approved_return() -> None:
    assert (
        rejected_return_clarification_preflight(
            FakeVM(
                id_stdout="user: cust_045\nroles: customer\n",
                payment_rows=(
                    "payment_id,payment_record_path,customer_id,payment_amount_cents,"
                    "payment_currency,return_id,return_record_path,return_status\n"
                    "pay_039,/proc/payments/pay_039.json,cust_045,4100,EUR,"
                    "ret_020,/proc/returns/ret_020.json,approved\n"
                ),
            ),
            task_text=(
                "refund my payment pay_039 for EUR 41, or I, as EU resident, "
                "will issue a chargeback from my bank"
            ),
        )
        is None
    )
