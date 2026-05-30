import json
from dataclasses import dataclass
from types import SimpleNamespace

from bitgn.vm.ecom.ecom_pb2 import NodeKind

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
    def __init__(
        self,
        *,
        id_stdout: str,
        files: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.id_stdout = id_stdout
        self.files = files or {}

    def exec(self, request) -> ExecResult:
        if request.path == "/bin/id":
            return ExecResult(stdout=self.id_stdout)
        raise AssertionError(f"unexpected exec path: {request.path}")

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
        raise AssertionError(f"missing path: {request.path}")


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
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            },
            "/proc/carts/cust-0072/basket-0146.json": {
                "id": "basket-0146",
                "customer_id": "cust-0072",
                "status": "checked_out",
                "created_at": "2026-08-04T15:09:43Z",
            },
        },
    )

    assert active_customer_baskets(vm, "cust-0072") == [
        {
            "basket_id": "basket-0145",
            "record_path": "/proc/carts/cust-0072/basket-0145.json",
            "basket_created_at": "2026-08-03T15:09:43Z",
        }
    ]


def test_ambiguous_checkout_preflight_returns_candidates_for_multiple_baskets() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            },
            "/proc/carts/cust-0072/basket-0053.json": {
                "id": "basket-0053",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-07-23T07:46:43Z",
            },
        },
    )

    result = ambiguous_checkout_preflight(vm, _checkout())

    assert result is not None
    assert result.basket_ids == ["basket-0145", "basket-0053"]
    assert result.basket_refs == [
        "/proc/carts/cust-0072/basket-0145.json",
        "/proc/carts/cust-0072/basket-0053.json",
    ]


def test_ambiguous_checkout_preflight_ignores_single_or_explicit_basket() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            }
        },
    )

    assert ambiguous_checkout_preflight(vm, _checkout()) is None
    assert (
        ambiguous_checkout_preflight(vm, _checkout(explicit_basket_id="basket_145"))
        is None
    )


def test_ambiguous_checkout_preflight_ignores_deterministic_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            },
            "/proc/carts/cust-0072/basket-0053.json": {
                "id": "basket-0053",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-07-23T07:46:43Z",
            },
        },
    )

    assert (
        ambiguous_checkout_preflight(vm, _checkout(basket_selector="newest")) is None
    )


def test_selected_basket_preflight_returns_newest_for_newest_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            },
            "/proc/carts/cust-0072/basket-0053.json": {
                "id": "basket-0053",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-07-23T07:46:43Z",
            },
        },
    )

    result = selected_basket_preflight(vm, _checkout(basket_selector="newest"))

    assert result is not None
    assert result.selector == "newest"
    assert result.basket_id == "basket-0145"
    assert result.basket_ref == "/proc/carts/cust-0072/basket-0145.json"


def test_selected_basket_preflight_returns_oldest_for_oldest_selector() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            },
            "/proc/carts/cust-0072/basket-0053.json": {
                "id": "basket-0053",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-07-23T07:46:43Z",
            },
        },
    )

    result = selected_basket_preflight(vm, _checkout(basket_selector="oldest"))

    assert result is not None
    assert result.selector == "oldest"
    assert result.basket_id == "basket-0053"
    assert result.basket_ref == "/proc/carts/cust-0072/basket-0053.json"


def test_selected_basket_preflight_skips_without_selector_or_intent() -> None:
    vm = FakeVM(
        id_stdout="user: cust-0072\nroles: customer\n",
        files={
            "/proc/carts/cust-0072/basket-0145.json": {
                "id": "basket-0145",
                "customer_id": "cust-0072",
                "status": "active",
                "created_at": "2026-08-03T15:09:43Z",
            }
        },
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
    )

    assert (
        selected_basket_preflight(vm, _checkout(basket_selector="newest")) is None
    )
