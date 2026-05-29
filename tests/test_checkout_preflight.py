from dataclasses import dataclass

from checkout_preflight import (
    active_customer_baskets,
    ambiguous_checkout_preflight,
    checkout_request_without_explicit_basket,
)


@dataclass
class ExecResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class FakeVM:
    def __init__(self, *, id_stdout: str, basket_rows: str) -> None:
        self.id_stdout = id_stdout
        self.basket_rows = basket_rows

    def exec(self, request) -> ExecResult:
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        if request.path == "/bin/sql":
            return ExecResult(stdout=self.basket_rows)
        raise AssertionError(f"unexpected exec path: {request.path}")


def test_checkout_request_without_explicit_basket_detects_ambiguous_wording() -> None:
    assert checkout_request_without_explicit_basket("check out my basket.")
    assert checkout_request_without_explicit_basket(
        "I am ready to buy what's in my basket; please check it out."
    )
    assert not checkout_request_without_explicit_basket("check out basket_001.")
    assert not checkout_request_without_explicit_basket("do you sell this basket?")


def test_active_customer_baskets_reads_customer_active_baskets() -> None:
    vm = FakeVM(
        id_stdout="",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
        ),
    )

    assert active_customer_baskets(vm, "cust_072") == [
        {
            "basket_id": "basket_145",
            "record_path": "/proc/baskets/basket_145.json",
            "basket_created_at": "2021-08-03T15:09:43Z",
        }
    ]


def test_ambiguous_checkout_preflight_returns_candidates_for_multiple_baskets() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
            "basket_053,/proc/baskets/basket_053.json,2021-07-23T07:46:43Z\n"
        ),
    )

    result = ambiguous_checkout_preflight(vm, "check out my basket.")

    assert result is not None
    assert result.basket_ids == ["basket_145", "basket_053"]
    assert result.basket_refs == [
        "/proc/baskets/basket_145.json",
        "/proc/baskets/basket_053.json",
    ]


def test_ambiguous_checkout_preflight_ignores_single_or_explicit_basket() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
        ),
    )

    assert ambiguous_checkout_preflight(vm, "check out my basket.") is None
    assert ambiguous_checkout_preflight(vm, "check out basket_145.") is None
