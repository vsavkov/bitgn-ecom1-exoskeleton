from typing import Literal

from agent import ReportTaskCompletion
from evidence_ledger import EvidenceLedger

TaskTypeLit = Literal[
    "count",
    "availability_count",
    "availability_lookup",
    "catalog_lookup",
    "receipt_price_check",
    "checkout",
    "discount",
    "payment_recovery",
    "refund",
    "fraud_review",
    "other",
]
OutcomeLit = Literal[
    "OUTCOME_OK",
    "OUTCOME_DENIED_SECURITY",
    "OUTCOME_NONE_CLARIFICATION",
    "OUTCOME_NONE_UNSUPPORTED",
    "OUTCOME_ERR_INTERNAL",
]


def _completion(
    *,
    task_type: TaskTypeLit,
    row_refs: list[str],
    message: str = "current",
    outcome: OutcomeLit = "OUTCOME_OK",
) -> ReportTaskCompletion:
    return ReportTaskCompletion(
        completed_steps_laconic=["did work"],
        task_type=task_type,
        message=message,
        grounding_doc_refs=[],
        grounding_row_refs=row_refs,
        protected_record_denial=False,
        outcome=outcome,
    )


def test_availability_count_merges_across_calls() -> None:
    ledger = EvidenceLedger()
    ledger.merge_availability_count(
        [
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Brand/A.json",
        ]
    )
    ledger.merge_availability_count(
        [
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Brand/B.json",
        ]
    )

    assert ledger.availability_count_refs == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/Brand/A.json",
        "/proc/catalog/Brand/B.json",
    ]


def test_support_note_and_manager_merges_dedupe() -> None:
    ledger = EvidenceLedger()
    ledger.merge_support_note(["/proc/catalog/Brand/A.json"])
    ledger.merge_support_note(
        ["/proc/catalog/Brand/A.json", "/proc/catalog/Brand/B.json"]
    )
    ledger.merge_manager_verified(["/proc/stores/store_x.json"])
    ledger.merge_manager_verified(
        ["/proc/employees/emp_001.json", "/proc/stores/store_x.json"]
    )

    assert ledger.support_note_refs == [
        "/proc/catalog/Brand/A.json",
        "/proc/catalog/Brand/B.json",
    ]
    assert ledger.manager_verified_refs == [
        "/proc/stores/store_x.json",
        "/proc/employees/emp_001.json",
    ]


def test_fraud_refs_accumulate_total_overwrites() -> None:
    ledger = EvidenceLedger()
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R1"],
        total_message="EUR 10.00",
    )
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R2", "/archive/payments.tsv#row=R1"],
        total_message="EUR 25.00",
    )

    assert ledger.fraud_refs == [
        "/archive/payments.tsv#row=R1",
        "/archive/payments.tsv#row=R2",
    ]
    assert ledger.fraud_total_message == "EUR 25.00"


def test_empty_calls_do_not_clobber_existing_state() -> None:
    ledger = EvidenceLedger()
    ledger.merge_availability_count(["/proc/catalog/Brand/A.json"])
    ledger.merge_availability_count([])
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R1"], total_message="EUR 5.00"
    )
    ledger.merge_fraud_result(refs=[], total_message="")

    assert ledger.availability_count_refs == ["/proc/catalog/Brand/A.json"]
    assert ledger.fraud_refs == ["/archive/payments.tsv#row=R1"]
    assert ledger.fraud_total_message == "EUR 5.00"


def test_apply_to_completion_runs_all_postprocessors_in_order() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["did work"],
        task_type="availability_count",
        message="1 products",
        grounding_doc_refs=[],
        grounding_row_refs=[
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Brand/STALE.json",
        ],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    ledger = EvidenceLedger()
    ledger.merge_availability_count(
        [
            "/proc/stores/store_vienna_praterstern.json",
            "/proc/catalog/Brand/FRESH.json",
        ]
    )
    ledger.merge_manager_verified(["/proc/employees/emp_001.json"])

    updated = ledger.apply_to_completion(cmd)

    assert updated.grounding_row_refs == [
        "/proc/stores/store_vienna_praterstern.json",
        "/proc/catalog/Brand/FRESH.json",
        "/proc/employees/emp_001.json",
    ]


def test_apply_to_completion_uses_accumulated_archive_fraud_total() -> None:
    cmd = _completion(
        task_type="fraud_review",
        row_refs=["/archive/payments.tsv#row=KEEP"],
        message="placeholder",
    )

    ledger = EvidenceLedger()
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R1"],
        total_message="EUR 12.34",
    )
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R2"],
        total_message="EUR 50.00",
    )

    updated = ledger.apply_to_completion(
        cmd,
        task_text="Answer message must contain only the total fraudulent payment amount.",
    )

    assert updated.message == "EUR 50.00"
    assert updated.grounding_row_refs == [
        "/archive/payments.tsv#row=KEEP",
        "/archive/payments.tsv#row=R1",
        "/archive/payments.tsv#row=R2",
    ]


def test_apply_to_completion_omits_docs_for_explicit_archive_fraud_export() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["did work"],
        task_type="fraud_review",
        message="placeholder",
        grounding_doc_refs=["/docs/security.md"],
        grounding_row_refs=[],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    ledger = EvidenceLedger()
    ledger.register_loaded_docs(["/docs/payments/3ds.md"])
    ledger.merge_fraud_result(
        refs=["/archive/payments.tsv#row=R1"],
        total_message="EUR 12.34",
    )

    updated = ledger.apply_to_completion(
        cmd,
        task_text=(
            "Read /archive/payments.tsv and cite every fraud row using exactly "
            "/archive/payments.tsv#row=<RowID>."
        ),
    )

    assert updated.grounding_doc_refs == []
    assert updated.grounding_row_refs == ["/archive/payments.tsv#row=R1"]


def test_apply_to_completion_keeps_live_fraud_record_message() -> None:
    cmd = _completion(
        task_type="fraud_review",
        row_refs=[],
        message="Fraudulent payment records: pay_001",
    )

    ledger = EvidenceLedger()
    ledger.merge_fraud_result(
        refs=["/proc/payments/pay_001.json"],
        total_message="EUR 12.34",
    )

    updated = ledger.apply_to_completion(
        cmd,
        task_text="Identify the fraudulent payment records from history.",
    )

    assert updated.message == "Fraudulent payment records: pay_001"
    assert updated.grounding_row_refs == ["/proc/payments/pay_001.json"]


def test_apply_to_completion_uses_receipt_price_result() -> None:
    cmd = _completion(
        task_type="receipt_price_check",
        row_refs=["/uploads/receipt_ocr.txt"],
        message="placeholder",
    )

    ledger = EvidenceLedger()
    ledger.merge_receipt_price_result(
        refs=["/proc/catalog/FST-69283OWE.json"],
        formatted_message="<YES>",
    )

    updated = ledger.apply_to_completion(cmd)

    assert updated.message == "<YES>"
    assert updated.grounding_row_refs == [
        "/uploads/receipt_ocr.txt",
        "/proc/catalog/FST-69283OWE.json",
    ]


def test_apply_to_completion_uses_city_availability_result() -> None:
    cmd = _completion(
        task_type="availability_lookup",
        row_refs=["/proc/catalog/WRONG.json"],
        message="placeholder",
    )

    ledger = EvidenceLedger()
    ledger.merge_city_availability_result(
        refs=[
            "/proc/catalog/FST-1KPF96UD.json",
            "/proc/stores/store_vienna_meidling.json",
        ],
        formatted_message="count: 4",
    )

    updated = ledger.apply_to_completion(cmd)

    assert updated.message == "count: 4"
    assert updated.grounding_row_refs == [
        "/proc/catalog/FST-1KPF96UD.json",
        "/proc/stores/store_vienna_meidling.json",
    ]


def test_apply_to_completion_uses_catalog_availability_lookup_refs() -> None:
    cmd = ReportTaskCompletion(
        completed_steps_laconic=["checked pasted rows"],
        task_type="availability_lookup",
        message="table",
        grounding_doc_refs=[],
        grounding_row_refs=["/proc/catalog/A.json"],
        protected_record_denial=False,
        outcome="OUTCOME_OK",
    )

    ledger = EvidenceLedger()
    ledger.merge_catalog_availability_lookup(
        [
            "/proc/stores/store_graz_jakomini.json",
            "/proc/catalog/B.json",
        ]
    )

    updated = ledger.apply_to_completion(cmd)

    assert updated.grounding_row_refs == [
        "/proc/catalog/A.json",
        "/proc/stores/store_graz_jakomini.json",
        "/proc/catalog/B.json",
    ]


def test_apply_to_completion_autocites_loaded_docs_for_matching_task_type() -> None:
    cmd = _completion(
        task_type="discount",
        row_refs=["/proc/baskets/basket_001.json"],
    )

    ledger = EvidenceLedger()
    ledger.register_loaded_docs(
        [
            "/docs/security.md",
            "/docs/discounts.md",
            "/docs/checkout.md",
            "/docs/powertools-agentic-os-origin-story.md",
        ]
    )

    updated = ledger.apply_to_completion(cmd)

    # Discounts depend on checkoutability, so checkout.md is also relevant;
    # the origin story never matches anything.
    assert "/docs/security.md" in updated.grounding_doc_refs
    assert "/docs/discounts.md" in updated.grounding_doc_refs
    assert "/docs/checkout.md" in updated.grounding_doc_refs
    assert (
        "/docs/powertools-agentic-os-origin-story.md"
        not in updated.grounding_doc_refs
    )


def test_register_loaded_docs_dedupes_across_calls() -> None:
    ledger = EvidenceLedger()
    ledger.register_loaded_docs(["/docs/security.md", "/docs/checkout.md"])
    ledger.register_loaded_docs(["/docs/security.md", "/docs/discounts.md"])

    assert ledger.loaded_doc_refs == [
        "/docs/security.md",
        "/docs/checkout.md",
        "/docs/discounts.md",
    ]


def test_apply_to_completion_is_noop_for_unrelated_buckets() -> None:
    cmd = _completion(
        task_type="catalog_lookup",
        row_refs=["/proc/catalog/Brand/A.json"],
    )

    ledger = EvidenceLedger()
    ledger.merge_availability_count(["/proc/catalog/Brand/IGNORED.json"])

    updated = ledger.apply_to_completion(cmd)

    # availability_count refs only fire when cmd.task_type == 'availability_count';
    # for a catalog_lookup completion the original refs must survive untouched.
    assert updated.grounding_row_refs == cmd.grounding_row_refs
