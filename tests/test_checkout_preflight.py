from dataclasses import dataclass

from checkout_preflight import (
    active_customer_baskets,
    ambiguous_checkout_preflight,
    checkout_request_without_explicit_basket,
    selected_basket_preflight,
)
from task_classifier import BasketSelector, TaskClassification


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


def _checkout(
    *,
    explicit_basket_id: str = "",
    basket_selector: BasketSelector = "none",
    checkout_intent: bool = True,
) -> TaskClassification:
    return TaskClassification(
        explicit_basket_id=explicit_basket_id,
        checkout_intent=checkout_intent,
        basket_selector=basket_selector,
    )


def test_checkout_request_without_explicit_basket_uses_classification() -> None:
    assert checkout_request_without_explicit_basket(_checkout())
    assert not checkout_request_without_explicit_basket(_checkout(checkout_intent=False))
    assert not checkout_request_without_explicit_basket(
        _checkout(explicit_basket_id="basket_001")
    )
    assert not checkout_request_without_explicit_basket(
        _checkout(basket_selector="newest")
    )
    assert not checkout_request_without_explicit_basket(
        _checkout(basket_selector="oldest")
    )


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

    result = ambiguous_checkout_preflight(vm, _checkout())

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

    assert ambiguous_checkout_preflight(vm, _checkout()) is None
    assert (
        ambiguous_checkout_preflight(vm, _checkout(explicit_basket_id="basket_145"))
        is None
    )


def test_ambiguous_checkout_preflight_ignores_deterministic_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
            "basket_053,/proc/baskets/basket_053.json,2021-07-23T07:46:43Z\n"
        ),
    )

    assert (
        ambiguous_checkout_preflight(vm, _checkout(basket_selector="newest")) is None
    )


def test_selected_basket_preflight_returns_newest_for_newest_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
            "basket_053,/proc/baskets/basket_053.json,2021-07-23T07:46:43Z\n"
        ),
    )

    result = selected_basket_preflight(vm, _checkout(basket_selector="newest"))

    assert result is not None
    assert result.selector == "newest"
    assert result.basket_id == "basket_145"
    assert result.basket_ref == "/proc/baskets/basket_145.json"


def test_selected_basket_preflight_returns_oldest_for_oldest_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
            "basket_053,/proc/baskets/basket_053.json,2021-07-23T07:46:43Z\n"
        ),
    )

    result = selected_basket_preflight(vm, _checkout(basket_selector="oldest"))

    assert result is not None
    assert result.selector == "oldest"
    assert result.basket_id == "basket_053"
    assert result.basket_ref == "/proc/baskets/basket_053.json"


def test_selected_basket_preflight_skips_without_selector_or_intent() -> None:
    vm = FakeVM(
        id_stdout="user: cust_072\nroles: customer\n",
        basket_rows=(
            "basket_id,record_path,basket_created_at\n"
            "basket_145,/proc/baskets/basket_145.json,2021-08-03T15:09:43Z\n"
        ),
    )

    assert selected_basket_preflight(vm, _checkout()) is None
    assert (
        selected_basket_preflight(vm, _checkout(checkout_intent=False)) is None
    )
    assert (
        selected_basket_preflight(
            vm, _checkout(explicit_basket_id="basket_145", basket_selector="newest")
        )
        is None
    )


def test_selected_basket_preflight_skips_for_guest_identity() -> None:
    vm = FakeVM(
        id_stdout="user: guest_xyz\nroles: guest\n",
        basket_rows="",
    )

    assert (
        selected_basket_preflight(vm, _checkout(basket_selector="newest")) is None
    )
