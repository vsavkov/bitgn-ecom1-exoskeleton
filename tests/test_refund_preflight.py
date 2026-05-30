import json
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

from refund_preflight import (
    amount_refund_clarification_preflight,
    rejected_return_clarification_preflight,
    refund_amount_cents_from_text,
)


class FakeVM:
    def __init__(
        self,
        *,
        id_stdout: str,
        files: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.id_stdout = id_stdout
        self.files = files or {}

    def exec(self, request):
        if request.path == "/bin/id":
            return SimpleNamespace(stdout=self.id_stdout, exit_code=0)
        raise AssertionError(request.path)

    def list(self, request) -> object:
        prefix = request.path.rstrip("/") + "/"
        child_names: set[str] = set()
        file_names: set[str] = set()
        for path in self.files:
            if not path.startswith(prefix):
                continue
            rest = path.removeprefix(prefix)
            first, sep, _tail = rest.partition("/")
            if sep:
                child_names.add(first)
            else:
                file_names.add(first)
        entries = [
            SimpleNamespace(name=name, kind=NodeKind.NODE_KIND_DIR)
            for name in sorted(child_names)
        ]
        entries.extend(
            SimpleNamespace(name=name, kind=NodeKind.NODE_KIND_FILE)
            for name in sorted(file_names)
        )
        return SimpleNamespace(entries=entries)

    def read(self, request) -> object:
        return SimpleNamespace(content=json.dumps(self.files[request.path]))

    def stat(self, request) -> object:
        if request.path in self.files:
            return object()
        raise AssertionError(request.path)


def test_refund_amount_cents_from_text_parses_common_forms() -> None:
    assert (
        refund_amount_cents_from_text("please refund my purchase for EUR 320") == 32000
    )
    assert refund_amount_cents_from_text("please refund my purchase for € 125") == 12500
    assert refund_amount_cents_from_text("refund 10 euros") == 1000
    assert refund_amount_cents_from_text("refund pay_001") is None


def test_amount_refund_clarification_preflight_asks_for_multiple_payments() -> None:
    result = amount_refund_clarification_preflight(
        FakeVM(
            id_stdout="user: cust-0092\nroles: customer\n",
            files={
                "/proc/payment-ledger/cust-0092/pay-0029.json": {
                    "id": "pay-0029",
                    "customer_id": "cust-0092",
                    "amount_cents": 32000,
                    "currency": "EUR",
                    "status": "paid",
                    "created_at": "2026-08-01T00:00:00Z",
                },
                "/proc/payment-ledger/cust-0092/pay-0030.json": {
                    "id": "pay-0030",
                    "customer_id": "cust-0092",
                    "amount_cents": 32000,
                    "currency": "EUR",
                    "status": "paid",
                    "created_at": "2026-08-02T00:00:00Z",
                },
            },
        ),
        task_text="please refund my purchase for EUR 320",
    )

    assert result is not None
    assert result.message == (
        "Which EUR 320.00 purchase should I refund? I found multiple matching "
        "payments: pay-0030, pay-0029."
    )
    assert result.row_refs == [
        "/proc/payment-ledger/cust-0092/pay-0030.json",
        "/proc/payment-ledger/cust-0092/pay-0029.json",
    ]


def test_amount_refund_clarification_preflight_skips_unique_payment() -> None:
    assert (
        amount_refund_clarification_preflight(
            FakeVM(
                id_stdout="user: cust-0092\nroles: customer\n",
                files={
                    "/proc/payment-ledger/cust-0092/pay-0029.json": {
                        "id": "pay-0029",
                        "customer_id": "cust-0092",
                        "amount_cents": 32000,
                        "currency": "EUR",
                        "status": "paid",
                        "created_at": "2026-08-01T00:00:00Z",
                    }
                },
            ),
            task_text="please refund my purchase for EUR 320",
        )
        is None
    )


def test_amount_refund_clarification_preflight_skips_explicit_payment() -> None:
    assert (
        amount_refund_clarification_preflight(
            FakeVM(
                id_stdout="user: cust-0092\nroles: customer\n",
            ),
            task_text="please refund pay-0029",
        )
        is None
    )


def test_rejected_return_clarification_preflight_handles_ambiguous_amount() -> None:
    result = rejected_return_clarification_preflight(
        FakeVM(
            id_stdout="user: cust-0092\nroles: customer\n",
            files={
                "/proc/payment-ledger/cust-0092/pay-0029.json": {
                    "id": "pay-0029",
                    "customer_id": "cust-0092",
                    "amount_cents": 32000,
                    "currency": "EUR",
                    "status": "paid",
                    "created_at": "2026-08-02T06:57:43Z",
                },
                "/proc/payment-ledger/cust-0092/pay-0202.json": {
                    "id": "pay-0202",
                    "customer_id": "cust-0092",
                    "amount_cents": 32000,
                    "currency": "EUR",
                    "status": "paid",
                    "created_at": "2026-06-11T20:29:00Z",
                },
                "/proc/return-workflows/cust-0092/return-0015.json": {
                    "id": "return-0015",
                    "customer_id": "cust-0092",
                    "payment_id": "pay-0029",
                    "status": "rejected",
                    "created_at": "2026-08-04T06:57:43Z",
                },
            },
        ),
        task_text=(
            "refund my payment pay-0029 for EUR 320, or I, as EU resident, "
            "will issue a chargeback from my bank"
        ),
    )

    assert result is not None
    assert result.message == (
        "I found payment pay-0029 and linked return return-0015, but that return is "
        "rejected. I also found multiple EUR 320.00 payments for your account, "
        "so please clarify which purchase or refund basis you want me to use."
    )
    assert result.doc_refs == ["/docs/security.md", "/docs/returns.md"]
    assert result.row_refs == [
        "/proc/payment-ledger/cust-0092/pay-0029.json",
        "/proc/payment-ledger/cust-0092/pay-0202.json",
        "/proc/return-workflows/cust-0092/return-0015.json",
    ]


def test_rejected_return_clarification_preflight_skips_rejected_unique_amount() -> None:
    assert (
        rejected_return_clarification_preflight(
            FakeVM(
                id_stdout="user: cust-0014\nroles: customer\n",
                files={
                    "/proc/payment-ledger/cust-0014/pay-0005.json": {
                        "id": "pay-0005",
                        "customer_id": "cust-0014",
                        "amount_cents": 45100,
                        "currency": "EUR",
                        "status": "paid",
                        "created_at": "2026-07-25T11:46:43Z",
                    },
                    "/proc/return-workflows/cust-0014/return-0003.json": {
                        "id": "return-0003",
                        "customer_id": "cust-0014",
                        "payment_id": "pay-0005",
                        "status": "rejected",
                    },
                },
            ),
            task_text=(
                "refund my payment pay-0005 for 451.00 EUR, or I, as EU resident, "
                "will issue a chargeback from my bank"
            ),
        )
        is None
    )


def test_rejected_return_clarification_preflight_skips_approved_return() -> None:
    assert (
        rejected_return_clarification_preflight(
            FakeVM(
                id_stdout="user: cust-0045\nroles: customer\n",
                files={
                    "/proc/payment-ledger/cust-0045/pay-0039.json": {
                        "id": "pay-0039",
                        "customer_id": "cust-0045",
                        "amount_cents": 4100,
                        "currency": "EUR",
                        "status": "paid",
                    },
                    "/proc/return-workflows/cust-0045/return-0020.json": {
                        "id": "return-0020",
                        "customer_id": "cust-0045",
                        "payment_id": "pay-0039",
                        "status": "approved",
                    },
                },
            ),
            task_text=(
                "refund my payment pay-0039 for EUR 41, or I, as EU resident, "
                "will issue a chargeback from my bank"
            ),
        )
        is None
    )
