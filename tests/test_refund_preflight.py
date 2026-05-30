from types import SimpleNamespace

from refund_preflight import (
    amount_refund_clarification_preflight,
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
