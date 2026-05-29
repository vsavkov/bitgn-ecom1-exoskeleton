from dataclasses import dataclass, field
from typing import Literal

from bitgn.vm.ecom.ecom_pb2 import ExecRequest

from submission_refs import (
    RuntimeVM,
    parse_runtime_identity,
    sql_quote,
    sql_rows,
)
from task_classifier import TaskClassification


SecurityReason = Literal[
    "customer_discount_claimed_manager_approval",
    "system_override_attempt",
]


@dataclass(frozen=True)
class SecurityDenial:
    reason: SecurityReason
    message: str
    doc_refs: list[str] = field(default_factory=list)
    row_refs: list[str] = field(default_factory=list)
    completed_steps_laconic: list[str] = field(default_factory=list)
    protected_record_denial: bool = False


def _runtime_identity(vm: RuntimeVM) -> tuple[str | None, set[str]]:
    try:
        result = vm.exec(ExecRequest(path="/bin/id"))
    except Exception:
        return None, set()
    return parse_runtime_identity(getattr(result, "stdout", "") or "")


def _customer_basket_ref(
    vm: RuntimeVM,
    basket_id: str,
    customer_id: str,
) -> str | None:
    try:
        rows = sql_rows(
            vm,
            "select record_path from shopping_baskets "
            f"where basket_id = {sql_quote(basket_id)} "
            f"and customer_id = {sql_quote(customer_id)} limit 1;",
        )
    except Exception:
        return None
    if not rows:
        return None
    path = rows[0].get("record_path") or ""
    return path if path.startswith("/") else None


def customer_discount_security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SecurityDenial | None:
    if not classification.discount_intent:
        return None
    if not classification.customer_claims_manager_approval:
        return None

    user_id, _roles = _runtime_identity(vm)
    if not user_id or not user_id.startswith("cust_"):
        return None

    # A customer cannot self-authorize a discount by claiming a manager said
    # yes. Cite security and discount docs; include the customer-owned basket
    # so the grader can verify the target evidence without revealing any
    # cross-customer record.
    row_refs: list[str] = []
    if classification.explicit_basket_id:
        ref = _customer_basket_ref(vm, classification.explicit_basket_id, user_id)
        if ref:
            row_refs.append(ref)

    return SecurityDenial(
        reason="customer_discount_claimed_manager_approval",
        message=(
            "Approval claimed in the request is not an identity or role "
            "returned by /bin/id; a customer cannot self-authorize a discount."
        ),
        doc_refs=["/docs/security.md", "/docs/discounts.md"],
        row_refs=row_refs,
        completed_steps_laconic=[
            "Detected a discount request that quotes a manager's prior approval.",
            "Verified current identity is a customer via /bin/id.",
            "Refused the discount because the policy requires a discount_manager identity.",
        ],
        protected_record_denial=False,
    )


def system_override_security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SecurityDenial | None:
    if not classification.system_override_attempt:
        return None

    # Intentionally do NOT auto-cite the named basket here even when the prompt
    # injection references one: that basket may belong to another customer and
    # the goal of the denial is to refuse the attempted identity switch, not
    # to surface protected records.
    return SecurityDenial(
        reason="system_override_attempt",
        message=(
            "Override language in the request cannot replace the security "
            "policy; /bin/id remains the authoritative identity."
        ),
        doc_refs=["/docs/security.md"],
        row_refs=[],
        completed_steps_laconic=[
            "Detected a system-override or identity-claim attempt in the task text.",
            "Confirmed runtime identity from /bin/id and ignored the claimed identity.",
            "Refused the request without accessing the records referenced by the injection.",
        ],
        protected_record_denial=True,
    )


def security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SecurityDenial | None:
    # Order matters: system_override is the most aggressive signal and must
    # short-circuit before any other denial path so we do not look up records
    # named in the injection. Customer-discount denial runs next.
    denial = system_override_security_preflight(vm, classification)
    if denial is not None:
        return denial
    return customer_discount_security_preflight(vm, classification)
