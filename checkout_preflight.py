from dataclasses import dataclass
from typing import Any, Literal, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ListRequest, ReadRequest, StatRequest

from runtime_calls import runtime_exec
from runtime_state import (
    CART_ROOTS,
    record_created_at,
    record_id,
    record_status,
    records_for_customer,
)
from submission_refs import is_customer_identity, parse_runtime_identity
from task_classifier import TaskClassification


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...

    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


@dataclass(frozen=True)
class AmbiguousCheckout:
    basket_ids: list[str]
    basket_refs: list[str]


BasketSelectorKind = Literal["newest", "oldest"]


@dataclass(frozen=True)
class SelectedBasket:
    selector: BasketSelectorKind
    basket_id: str
    basket_ref: str


def checkout_request_without_explicit_basket(
    classification: TaskClassification,
) -> bool:
    if not classification.checkout_intent:
        return False
    if classification.explicit_basket_id:
        return False
    # A deterministic selector ("newest"/"oldest") is unambiguous so it must not
    # go through the clarification preflight; selected_basket_preflight handles
    # those cases by injecting the resolved basket into context instead.
    return classification.basket_selector == "none"


def active_customer_baskets(vm: RuntimeVM, customer_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records_for_customer(vm, CART_ROOTS, customer_id):
        status = record_status(record).strip().lower()
        if status not in {"active", "open"}:
            continue
        basket_id = record_id(record)
        if not basket_id:
            continue
        rows.append(
            {
                "basket_id": basket_id,
                "record_path": record.path,
                "basket_created_at": record_created_at(record),
            }
        )
    return sorted(
        rows,
        key=lambda row: (row.get("basket_created_at") or "", row.get("basket_id") or ""),
        reverse=True,
    )


def ambiguous_checkout_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> AmbiguousCheckout | None:
    if not checkout_request_without_explicit_basket(classification):
        return None

    try:
        identity = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except Exception:
        return None

    user_id, _roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not is_customer_identity(user_id):
        return None

    baskets = active_customer_baskets(vm, user_id or "")
    if len(baskets) <= 1:
        return None

    basket_ids: list[str] = []
    basket_refs: list[str] = []
    for row in baskets:
        basket_id = row.get("basket_id") or ""
        record_path = row.get("record_path") or ""
        if basket_id and record_path.startswith("/"):
            basket_ids.append(basket_id)
            basket_refs.append(record_path)

    if len(basket_refs) <= 1:
        return None
    return AmbiguousCheckout(basket_ids=basket_ids, basket_refs=basket_refs)


def selected_basket_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SelectedBasket | None:
    if not classification.checkout_intent:
        return None
    if classification.explicit_basket_id:
        return None
    if classification.basket_selector not in {"newest", "oldest"}:
        return None

    try:
        identity = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except Exception:
        return None

    user_id, _roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not is_customer_identity(user_id):
        return None

    baskets = active_customer_baskets(vm, user_id or "")
    if not baskets:
        return None

    # active_customer_baskets is ordered by created_at DESC.
    chosen = baskets[0] if classification.basket_selector == "newest" else baskets[-1]
    basket_id = chosen.get("basket_id") or ""
    basket_ref = chosen.get("record_path") or ""
    if not basket_id or not basket_ref.startswith("/"):
        return None

    selector: BasketSelectorKind = (
        "newest" if classification.basket_selector == "newest" else "oldest"
    )
    return SelectedBasket(
        selector=selector,
        basket_id=basket_id,
        basket_ref=basket_ref,
    )
