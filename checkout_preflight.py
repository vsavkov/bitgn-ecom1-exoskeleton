import csv
import io
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest

from submission_refs import parse_runtime_identity, sql_quote
from task_classifier import TaskClassification


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


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
    return _sql_rows(
        vm,
        "select basket_id, record_path, basket_created_at "
        "from shopping_baskets "
        f"where customer_id = {sql_quote(customer_id)} "
        "and basket_status = 'active' "
        "order by basket_created_at desc, basket_id;",
    )


def _sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
    if getattr(result, "exit_code", 0):
        return []

    stdout = (getattr(result, "stdout", "") or "").strip()
    if not stdout:
        return []
    return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]


def ambiguous_checkout_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> AmbiguousCheckout | None:
    if not checkout_request_without_explicit_basket(classification):
        return None

    try:
        identity = vm.exec(ExecRequest(path="/bin/id"))
    except Exception:
        return None

    user_id, _roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not user_id or not user_id.startswith("cust_"):
        return None

    baskets = active_customer_baskets(vm, user_id)
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
        identity = vm.exec(ExecRequest(path="/bin/id"))
    except Exception:
        return None

    user_id, _roles = parse_runtime_identity(getattr(identity, "stdout", "") or "")
    if not user_id or not user_id.startswith("cust_"):
        return None

    baskets = active_customer_baskets(vm, user_id)
    if not baskets:
        return None

    # active_customer_baskets is ordered by basket_created_at DESC, so the
    # first row is the newest active basket and the last row is the oldest.
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
