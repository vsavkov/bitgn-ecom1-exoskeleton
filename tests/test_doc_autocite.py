from doc_autocite import relevant_doc_refs_for_task_type


def test_security_doc_matches_transactional_task_types() -> None:
    docs = ["/docs/security.md", "/docs/checkout.md"]

    assert "/docs/security.md" in relevant_doc_refs_for_task_type(docs, "discount")
    assert "/docs/security.md" in relevant_doc_refs_for_task_type(docs, "checkout")
    assert "/docs/security.md" in relevant_doc_refs_for_task_type(docs, "refund")
    assert "/docs/security.md" not in relevant_doc_refs_for_task_type(docs, "count")


def test_discount_doc_matches_discount_task_only() -> None:
    docs = ["/docs/discounts.md"]
    assert relevant_doc_refs_for_task_type(docs, "discount") == ["/docs/discounts.md"]
    assert relevant_doc_refs_for_task_type(docs, "checkout") == []


def test_sql_incident_variants_match_count() -> None:
    # Trial-specific filenames must still hit the count intent.
    for path in [
        "/docs/urgent-sql-incident.md",
        "/docs/current-updates/2024-07-17-sql-scratch-space.md",
        "/bin/sql-readme-2024-07-17.md",
    ]:
        assert relevant_doc_refs_for_task_type([path], "count") == [path]


def test_payments_doc_matches_payment_recovery_and_refund() -> None:
    docs = ["/docs/payments/3ds.md", "/docs/payments/refunds-policy.md"]
    matched = relevant_doc_refs_for_task_type(docs, "payment_recovery")
    assert "/docs/payments/3ds.md" in matched
    assert "/docs/payments/refunds-policy.md" in matched


def test_docs_without_matching_intent_are_skipped() -> None:
    docs = ["/docs/powertools-agentic-os-origin-story.md", "/AGENTS.MD"]
    assert relevant_doc_refs_for_task_type(docs, "discount") == []


def test_each_doc_returned_once_even_when_multiple_patterns_match() -> None:
    # security.md also contains "security" twice in path; ensure dedup.
    docs = ["/docs/security.md", "/docs/security.md"]
    assert relevant_doc_refs_for_task_type(docs, "discount") == ["/docs/security.md"]
