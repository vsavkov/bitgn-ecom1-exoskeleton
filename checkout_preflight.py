import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest

from submission_refs import parse_runtime_identity, sql_quote


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


@dataclass(frozen=True)
class AmbiguousCheckout:
    basket_ids: list[str]
    basket_refs: list[str]


CHECKOUT_INTENT_RE = re.compile(
    r"\b(?:check\s*out|checkout|buy|finish(?:\s+my)?\s+order|complete(?:\s+my)?\s+order)\b",
    re.IGNORECASE,
)
BASKET_WORD_RE = re.compile(r"\bbaskets?\b", re.IGNORECASE)
EXPLICIT_BASKET_ID_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:baskets?|bask)[_-]?\d+(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def checkout_request_without_explicit_basket(task_text: str) -> bool:
    if EXPLICIT_BASKET_ID_RE.search(task_text):
        return False
    return bool(CHECKOUT_INTENT_RE.search(task_text) and BASKET_WORD_RE.search(task_text))


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
    task_text: str,
) -> AmbiguousCheckout | None:
    if not checkout_request_without_explicit_basket(task_text):
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
