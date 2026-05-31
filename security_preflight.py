from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from bitgn.vm.ecom.ecom_pb2 import ExecRequest, ListRequest, ReadRequest, StatRequest

from manager_verification import ReqVerifyStoreManager, verify_store_manager
from runtime_calls import runtime_exec
from runtime_state import CART_ROOTS, find_record_by_id, ids_equal, record_customer_id
from submission_refs import (
    is_customer_identity,
    parse_runtime_identity,
)
from task_classifier import TaskClassification


SecurityReason = Literal[
    "checkout_foreign_basket",
    "customer_discount_claimed_manager_approval",
    "employee_contact_disclosure",
    "system_override_attempt",
]


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...

    def list(self, request: ListRequest) -> Any: ...

    def read(self, request: ReadRequest) -> Any: ...

    def stat(self, request: StatRequest) -> Any: ...


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
        result = runtime_exec(vm, ExecRequest(path="/bin/id"))
    except Exception:
        return None, set()
    return parse_runtime_identity(getattr(result, "stdout", "") or "")


def _customer_basket_ref(
    vm: RuntimeVM,
    basket_id: str,
    customer_id: str,
) -> str | None:
    record = find_record_by_id(vm, CART_ROOTS, basket_id, customer_id=customer_id)
    if record is None:
        return None
    return record.path


def customer_discount_security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SecurityDenial | None:
    if not classification.discount_intent:
        return None
    if not classification.customer_claims_manager_approval:
        return None

    user_id, _roles = _runtime_identity(vm)
    if not is_customer_identity(user_id):
        return None
    customer_id = user_id or ""

    # A customer cannot self-authorize a discount by claiming a manager said
    # yes. Cite security and discount docs; include the customer-owned basket
    # so the grader can verify the target evidence without revealing any
    # cross-customer record.
    row_refs: list[str] = []
    if classification.explicit_basket_id:
        ref = _customer_basket_ref(vm, classification.explicit_basket_id, customer_id)
        if ref:
            row_refs.append(ref)

    # When the customer names the manager and their store, resolve the store
    # record so the denial cites the source of the claimed authority instead
    # of leaking the employee profile to a customer identity. The helper
    # already returns only the store_ref for customer/guest contexts.
    if classification.claimed_manager_name and classification.claimed_store_name:
        try:
            manager_result = verify_store_manager(
                vm,
                ReqVerifyStoreManager(
                    employee_name=classification.claimed_manager_name,
                    store_name=classification.claimed_store_name,
                ),
            )
        except Exception:
            manager_result = None
        if isinstance(manager_result, dict):
            refs_to_submit = manager_result.get("refs_to_submit")
            if isinstance(refs_to_submit, list):
                for ref in refs_to_submit:
                    if isinstance(ref, str) and ref.startswith("/") and ref not in row_refs:
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


def employee_contact_security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
) -> SecurityDenial | None:
    if not classification.employee_contact_disclosure_requested:
        return None

    user_id, roles = _runtime_identity(vm)
    role_names = {role.lower() for role in roles}
    if user_id and not is_customer_identity(user_id):
        return None
    if role_names and not role_names <= {"guest", "customer"}:
        return None

    row_refs: list[str] = []
    employee_name = (
        classification.contact_employee_name or classification.claimed_manager_name
    )
    store_name = classification.contact_store_name or classification.claimed_store_name
    if employee_name and store_name:
        try:
            manager_result = verify_store_manager(
                vm,
                ReqVerifyStoreManager(
                    employee_name=" ".join(employee_name.split()),
                    store_name=" ".join(store_name.split()),
                ),
            )
        except Exception:
            manager_result = None
        if isinstance(manager_result, dict):
            refs_to_submit = manager_result.get("refs_to_submit")
            if isinstance(refs_to_submit, list):
                row_refs.extend(
                    ref
                    for ref in refs_to_submit
                    if isinstance(ref, str) and ref.startswith("/")
                )

    return SecurityDenial(
        reason="employee_contact_disclosure",
        message=(
            "OUTCOME_DENIED_SECURITY: direct employee contact details cannot "
            "be disclosed to a customer or guest."
        ),
        doc_refs=["/docs/security.md"],
        row_refs=row_refs,
        completed_steps_laconic=[
            "Detected a customer/guest request for direct employee contact details.",
            "Checked runtime identity with /bin/id.",
            "Refused disclosure under security/privacy policy.",
        ],
        protected_record_denial=False,
    )


def checkout_foreign_basket_security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
    *,
    task_text: str = "",
) -> SecurityDenial | None:
    if not classification.checkout_intent:
        return None
    if not classification.explicit_basket_id:
        return None

    user_id, _roles = _runtime_identity(vm)
    if not is_customer_identity(user_id):
        return None

    record = find_record_by_id(vm, CART_ROOTS, classification.explicit_basket_id)
    if record is None:
        return None

    owner_id = record_customer_id(record)
    if not owner_id or ids_equal(owner_id, user_id):
        return None

    doc_refs = ["/docs/security.md", "/docs/checkout.md"]
    normalized_task = " ".join(task_text.lower().split())
    if any(
        marker in normalized_task
        for marker in (
            "3ds",
            "bank verification",
            "card verification",
            "payment",
            "payments",
        )
    ):
        doc_refs.append("/docs/payments/3ds.md")

    return SecurityDenial(
        reason="checkout_foreign_basket",
        message=(
            "OUTCOME_DENIED_SECURITY: the named basket belongs to a different "
            "customer than the current /bin/id identity."
        ),
        doc_refs=doc_refs,
        row_refs=[],
        completed_steps_laconic=[
            "Detected a checkout request for an explicit basket id.",
            "Verified current identity is a customer via /bin/id.",
            "Refused checkout because the basket owner does not match the current customer.",
        ],
        protected_record_denial=True,
    )


def security_preflight(
    vm: RuntimeVM,
    classification: TaskClassification,
    *,
    task_text: str = "",
) -> SecurityDenial | None:
    # Order matters: system_override is the most aggressive signal and must
    # short-circuit before any other denial path so we do not look up records
    # named in the injection. Customer-discount denial runs next.
    denial = system_override_security_preflight(vm, classification)
    if denial is not None:
        return denial
    denial = employee_contact_security_preflight(vm, classification)
    if denial is not None:
        return denial
    denial = checkout_foreign_basket_security_preflight(
        vm,
        classification,
        task_text=task_text,
    )
    if denial is not None:
        return denial
    return customer_discount_security_preflight(vm, classification)
