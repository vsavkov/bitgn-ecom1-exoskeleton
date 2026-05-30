from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from doc_autocite import relevant_doc_refs_for_task_type
from submission_refs import dedupe_refs

if TYPE_CHECKING:
    from agent import ReportTaskCompletion


@dataclass
class EvidenceLedger:
    # Helpers can be invoked multiple times during a single trial (e.g. when the
    # LLM partitions a 13-item availability lookup, retries a support-note
    # parse, or verifies more than one manager). Each bucket merges across
    # calls instead of being overwritten so the final submission keeps every
    # piece of authoritative evidence the helpers produced.
    availability_count_refs: list[str] = field(default_factory=list)
    support_note_refs: list[str] = field(default_factory=list)
    manager_verified_refs: list[str] = field(default_factory=list)
    fraud_refs: list[str] = field(default_factory=list)
    fraud_total_message: str = ""
    receipt_refs: list[str] = field(default_factory=list)
    receipt_message: str = ""
    city_availability_refs: list[str] = field(default_factory=list)
    city_availability_message: str = ""
    loaded_doc_refs: list[str] = field(default_factory=list)

    def merge_availability_count(self, refs: list[str]) -> None:
        if refs:
            self.availability_count_refs = dedupe_refs(
                [*self.availability_count_refs, *refs]
            )

    def merge_support_note(self, refs: list[str]) -> None:
        if refs:
            self.support_note_refs = dedupe_refs([*self.support_note_refs, *refs])

    def merge_manager_verified(self, refs: list[str]) -> None:
        if refs:
            self.manager_verified_refs = dedupe_refs(
                [*self.manager_verified_refs, *refs]
            )

    def merge_fraud_result(
        self,
        *,
        refs: list[str],
        total_message: str,
    ) -> None:
        # Both analyze_archive_fraud_export and analyze_payment_fraud_history
        # return the same {refs_to_submit, total_message} shape and a single
        # trial only ever asks one of them, so a shared bucket is enough.
        if refs:
            self.fraud_refs = dedupe_refs([*self.fraud_refs, *refs])
        if total_message:
            self.fraud_total_message = total_message

    def merge_receipt_price_result(
        self,
        *,
        refs: list[str],
        formatted_message: str,
    ) -> None:
        if refs:
            self.receipt_refs = dedupe_refs([*self.receipt_refs, *refs])
        if formatted_message:
            self.receipt_message = formatted_message

    def merge_city_availability_result(
        self,
        *,
        refs: list[str],
        formatted_message: str,
    ) -> None:
        if refs:
            self.city_availability_refs = dedupe_refs(
                [*self.city_availability_refs, *refs]
            )
        if formatted_message:
            self.city_availability_message = formatted_message

    def register_loaded_docs(self, paths: list[str]) -> None:
        if paths:
            self.loaded_doc_refs = dedupe_refs([*self.loaded_doc_refs, *paths])

    def apply_to_completion(
        self,
        cmd: "ReportTaskCompletion",
        *,
        task_text: str = "",
    ) -> "ReportTaskCompletion":
        from agent import (
            _apply_archive_fraud_result,
            _apply_availability_count_catalog_refs,
            _apply_city_availability_result,
            _apply_loaded_doc_refs,
            _apply_receipt_price_result,
            _apply_support_note_catalog_refs,
            _apply_verified_manager_refs,
        )

        cmd = _apply_availability_count_catalog_refs(cmd, self.availability_count_refs)
        cmd = _apply_support_note_catalog_refs(cmd, self.support_note_refs)
        cmd = _apply_verified_manager_refs(cmd, self.manager_verified_refs)
        cmd = _apply_archive_fraud_result(
            cmd,
            total_message=self.fraud_total_message,
            refs_to_submit=self.fraud_refs,
            task_text=task_text,
        )
        cmd = _apply_receipt_price_result(
            cmd,
            formatted_message=self.receipt_message,
            refs_to_submit=self.receipt_refs,
        )
        cmd = _apply_city_availability_result(
            cmd,
            formatted_message=self.city_availability_message,
            refs_to_submit=self.city_availability_refs,
        )
        cmd = _apply_loaded_doc_refs(
            cmd,
            relevant_doc_refs_for_task_type(self.loaded_doc_refs, cmd.task_type),
        )
        return cmd
