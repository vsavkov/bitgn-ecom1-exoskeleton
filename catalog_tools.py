import csv
import io
import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from bitgn.vm.ecom.ecom_connect import EcomRuntimeClientSync
from bitgn.vm.ecom.ecom_pb2 import ExecRequest
from connectrpc.errors import ConnectError
from openai import OpenAI
from openai.types.shared_params import Reasoning
from pydantic import BaseModel, Field

from config import (
    helper_model,
    helper_reasoning_effort,
    openai_client_kwargs,
    render_prompt,
)


class CatalogLookupItem(BaseModel):
    item_id: str = Field(
        default="",
        description="Optional row id from the user input, such as quote table RowID.",
    )
    description: str = Field(
        description=(
            "Raw catalogue product description from the task, for example "
            "'the X from Brand in the Brand Series Model X line that has ...'."
        )
    )
    requested_quantity: int | None = Field(
        default=None,
        description=(
            "Requested quantity for availability checks. Leave null for pure "
            "catalogue existence checks."
        ),
    )


class ReqResolveCatalogItems(BaseModel):
    items: list[CatalogLookupItem] = Field(
        description="Catalogue descriptions to parse and resolve.",
        min_length=1,
        max_length=12,
    )
    store_id: str | None = Field(
        default=None,
        description=(
            "Optional store_id for same-day availability, e.g. "
            "store_graz_lend. Resolve it from /bin/id or SQL first."
        ),
    )
    availability_threshold: int | None = Field(
        default=None,
        description=(
            "Fallback threshold for availability-count tasks when an item does "
            "not have its own requested_quantity."
        ),
    )
    availability_predicate: Literal["at_least", "below"] = Field(
        default="at_least",
        description=(
            "How to evaluate available_today_quantity against the threshold: "
            "at_least means available >= threshold/requested_quantity; below "
            "means available < threshold."
        ),
    )


class ParsedCatalogConstraint(BaseModel):
    text: str = Field(
        description=(
            "Original normalized property constraint, e.g. "
            "'disc diameter 180 mm' or 'mask type half mask'."
        )
    )
    label: str = Field(description="Property label, e.g. 'disc diameter'.")
    value: str = Field(description="Property value, e.g. '180 mm'.")


class ParsedCatalogItem(BaseModel):
    item_index: int
    brand: str
    product_kind: str
    product_family: str
    constraints: list[ParsedCatalogConstraint] = Field(default_factory=list)


class ParsedCatalogItems(BaseModel):
    items: list[ParsedCatalogItem]


CATALOG_PARSER_PROMPT = render_prompt("catalog_parser.j2")


def _parsed_response(resp) -> ParsedCatalogItems | None:
    output_parsed = getattr(resp, "output_parsed", None)
    if isinstance(output_parsed, ParsedCatalogItems):
        return output_parsed
    if isinstance(output_parsed, dict):
        return ParsedCatalogItems.model_validate(output_parsed)

    for item in resp.output or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, ParsedCatalogItems):
                return parsed
            if isinstance(parsed, dict):
                return ParsedCatalogItems.model_validate(parsed)
    return None


def _parse_catalog_descriptions(items: list[CatalogLookupItem]) -> list[ParsedCatalogItem]:
    payload = {
        "items": [
            {"item_index": index, "description": item.description}
            for index, item in enumerate(items)
        ]
    }
    client = OpenAI(**openai_client_kwargs())
    resp = client.responses.parse(
        model=helper_model(),
        instructions=CATALOG_PARSER_PROMPT,
        input=[
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, indent=2),
            }
        ],
        text_format=ParsedCatalogItems,
        reasoning=Reasoning(effort=helper_reasoning_effort()),
        max_output_tokens=4096,
    )
    parsed = _parsed_response(resp)
    if parsed is None:
        raise RuntimeError("catalog parser returned no structured output")

    by_index = {item.item_index: item for item in parsed.items}
    if len(by_index) != len(items):
        raise RuntimeError(
            f"catalog parser returned {len(by_index)} unique items for {len(items)} inputs"
        )

    ordered: list[ParsedCatalogItem] = []
    for index in range(len(items)):
        item = by_index.get(index)
        if item is None:
            raise RuntimeError(f"catalog parser omitted item_index={index}")
        if not item.brand or not item.product_kind or not item.product_family:
            raise RuntimeError(
                f"catalog parser returned incomplete item_index={index}: {item}"
            )
        ordered.append(item)
    return ordered


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_rows(vm: EcomRuntimeClientSync, query: str) -> list[dict[str, str]]:
    try:
        result = vm.exec(ExecRequest(path="/bin/sql", stdin=query))
    except ConnectError as exc:
        raise RuntimeError(f"catalog SQL query failed: {exc.message}") from exc

    if getattr(result, "exit_code", 0):
        raise RuntimeError(
            "catalog SQL query exited with "
            f"{result.exit_code}: {(result.stderr or '').strip()}"
        )

    stdout = (result.stdout or "").strip()
    if not stdout:
        raise RuntimeError("catalog SQL returned invalid CSV") from None

    try:
        return [dict(row) for row in csv.DictReader(io.StringIO(stdout))]
    except csv.Error as exc:
        raise RuntimeError("catalog SQL returned invalid CSV") from exc


def _has_catalog_projection(vm: EcomRuntimeClientSync) -> bool:
    required_tables = {
        "product_variants",
        "product_variant_properties",
        "product_kinds",
        "product_families",
        "stores",
        "store_inventory",
    }
    quoted = ", ".join(_sql_quote(name) for name in sorted(required_tables))
    rows = _sql_rows(
        vm,
        "select name from sqlite_schema "
        f"where type = 'table' and name in ({quoted}) order by name;",
    )
    found = {row.get("name") for row in rows}
    return required_tables.issubset(found)


_UNIT_SUFFIXES = {
    "cm",
    "k",
    "kw",
    "l",
    "lm",
    "m",
    "ml",
    "mm",
    "pc",
    "pcs",
    "v",
    "w",
}
_GENERIC_CONSTRAINT_WORDS = {
    "adapter",
    "adhesive",
    "anchor",
    "bar",
    "battery",
    "class",
    "cleaner",
    "color",
    "connection",
    "connector",
    "contents",
    "cutting",
    "diameter",
    "disc",
    "family",
    "fastener",
    "finish",
    "fitting",
    "ip",
    "kit",
    "length",
    "luminous",
    "machine",
    "mask",
    "power",
    "product",
    "protection",
    "sealant",
    "screw",
    "source",
    "storage",
    "tool",
    "type",
    "viscosity",
    "voltage",
    "volume",
    "wattage",
    "width",
}
_STRUCTURED_PROPERTY_LABEL_WORDS = {
    "class",
    "color",
    "connection",
    "connector",
    "contents",
    "diameter",
    "drive",
    "family",
    "finish",
    "ip",
    "length",
    "luminous",
    "material",
    "mask",
    "power",
    "product",
    "protection",
    "rating",
    "screw",
    "size",
    "source",
    "storage",
    "thread",
    "type",
    "viscosity",
    "voltage",
    "volume",
    "wattage",
    "width",
}
_SUPPORT_NOTE_ID_RE = re.compile(r"\b(?:support|claim)\b", re.IGNORECASE)
_THAT_HAS_RE = re.compile(r"\bthat\s+has\b", re.IGNORECASE)
_EXTRA_CLAIM_BOUNDARY_RE = re.compile(
    r"\band\s+(?:has|supports|is)\b",
    re.IGNORECASE,
)


def _norm_words(value: str) -> str:
    value = value.lower().replace("-", " ")
    value = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _property_label_candidates(key: str) -> list[str]:
    label = key.replace("_", " ")
    parts = label.split()
    labels = [label]
    if len(parts) > 1 and parts[-1] in _UNIT_SUFFIXES:
        labels.append(" ".join(parts[:-1]))
        parts = parts[:-1]
    if len(parts) > 1 and parts[-1] in {
        "count",
        "diameter",
        "family",
        "length",
        "platform",
        "power",
        "profile",
        "size",
        "source",
        "type",
        "voltage",
        "volume",
        "wattage",
        "width",
    }:
        labels.append(parts[-1])
    if "color" in parts:
        labels.append("color family")
        labels.append("color")
    return sorted(set(labels), key=len, reverse=True)


def _number_from_text(value: str) -> float | None:
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _constraint_matches_product_name(constraint: str, product_name: str) -> bool:
    product_words = set(_norm_words(product_name).split())
    constraint_words = [
        word
        for word in _norm_words(constraint).split()
        if word not in _GENERIC_CONSTRAINT_WORDS
    ]
    if not constraint_words:
        return False
    return all(word in product_words for word in constraint_words)


def _word_phrase_in_text(phrase: str, text: str) -> bool:
    phrase_words = _norm_words(phrase).split()
    text_words = _norm_words(text).split()
    if not phrase_words or len(phrase_words) > len(text_words):
        return False

    phrase_len = len(phrase_words)
    return any(
        text_words[index : index + phrase_len] == phrase_words
        for index in range(len(text_words) - phrase_len + 1)
    )


def _variant_tail_text(product_name: str, product_family_name: str) -> str:
    product_words = _norm_words(product_name).split()
    family_words = _norm_words(product_family_name).split()
    if not product_words or not family_words:
        return ""
    if product_words[: len(family_words)] != family_words:
        return ""
    return " ".join(product_words[len(family_words) :])


def _constraint_matches_variant_tail(
    constraint: str | ParsedCatalogConstraint,
    product_name: str,
    product_family_name: str,
) -> bool:
    if isinstance(constraint, ParsedCatalogConstraint):
        candidates = [constraint.value]
    else:
        candidates = [
            word
            for word in _norm_words(constraint).split()
            if word not in _GENERIC_CONSTRAINT_WORDS
        ]

    tail = _variant_tail_text(product_name, product_family_name)
    if not tail:
        return False

    # Business rule: variant labels such as color, size, storage type, or
    # garment type are sometimes absent from product_variant_properties but
    # present in the display-name suffix after the canonical family prefix.
    # Matching only that suffix avoids treating family text as variant evidence.
    return any(
        bool(_norm_words(candidate)) and _word_phrase_in_text(candidate, tail)
        for candidate in candidates
    )


def _constraint_text_and_label(
    constraint: str | ParsedCatalogConstraint,
) -> tuple[str, str]:
    if isinstance(constraint, ParsedCatalogConstraint):
        text = constraint.text or f"{constraint.label} {constraint.value}".strip()
        return text, constraint.label
    return constraint, ""


def _constraint_text(constraint: str | ParsedCatalogConstraint) -> str:
    text, _label = _constraint_text_and_label(constraint)
    return text


def _support_note_claim_text_parts(item: CatalogLookupItem) -> tuple[str, str] | None:
    if not (
        _SUPPORT_NOTE_ID_RE.search(item.item_id)
        or _SUPPORT_NOTE_ID_RE.search(item.description)
    ):
        return None

    that_has = _THAT_HAS_RE.search(item.description)
    if not that_has:
        return None

    constraint_text = item.description[that_has.end() :]
    extra_boundary = _EXTRA_CLAIM_BOUNDARY_RE.search(constraint_text)
    if not extra_boundary:
        return None

    base_text = constraint_text[: extra_boundary.start()].strip(" ,.;")
    extra_text = constraint_text[extra_boundary.end() :].strip(" ,.;")
    if not base_text or not extra_text:
        return None
    return base_text, extra_text


def _constraint_appears_in_text(
    constraint: str | ParsedCatalogConstraint,
    text: str,
) -> bool:
    normalized_text = _norm_words(text)
    if not normalized_text:
        return False

    if isinstance(constraint, ParsedCatalogConstraint):
        candidates = [
            constraint.text,
            f"{constraint.label} {constraint.value}".strip(),
            constraint.value,
        ]
    else:
        candidates = [constraint]

    return any(
        bool(normalized_candidate)
        and normalized_candidate in normalized_text
        for candidate in candidates
        if (normalized_candidate := _norm_words(candidate))
    )


def _split_support_note_constraints(
    item: CatalogLookupItem,
    constraints: Sequence[ParsedCatalogConstraint],
) -> tuple[list[ParsedCatalogConstraint], list[ParsedCatalogConstraint], str]:
    parts = _support_note_claim_text_parts(item)
    if parts is None:
        return list(constraints), [], ""

    base_text, extra_text = parts
    base_constraints: list[ParsedCatalogConstraint] = []
    extra_constraints: list[ParsedCatalogConstraint] = []
    for constraint in constraints:
        in_extra = _constraint_appears_in_text(constraint, extra_text)
        in_base = _constraint_appears_in_text(constraint, base_text)
        if in_extra and not in_base:
            extra_constraints.append(constraint)
        else:
            base_constraints.append(constraint)

    return base_constraints or list(constraints), extra_constraints, extra_text


def _requires_structured_property_match(constraint: str, label: str = "") -> bool:
    normalized_label = _norm_words(label)
    if set(normalized_label.split()) & _STRUCTURED_PROPERTY_LABEL_WORDS:
        return True

    words = _norm_words(constraint).split()
    if len(words) < 2:
        return False

    # Constraints with an explicit property label, e.g. "screw type wood screw",
    # must be validated against product_variant_properties. Falling back to the
    # product name would confuse family text ("Wood and Drywall Screw") with the
    # actual typed variant value ("drywall screw").
    label_words = set(words[:2])
    return bool(label_words & _STRUCTURED_PROPERTY_LABEL_WORDS)


def _property_matches_constraint(
    constraint: str,
    *,
    key: str,
    text_value: str,
    number_value: str,
) -> bool:
    normalized_constraint = _norm_words(constraint)
    for label in _property_label_candidates(key):
        normalized_label = _norm_words(label)
        if normalized_constraint == normalized_label:
            remainder = ""
        elif normalized_constraint.startswith(f"{normalized_label} "):
            remainder = normalized_constraint[len(normalized_label) :].strip()
        else:
            continue

        requested_number = _number_from_text(constraint)
        if requested_number is not None and number_value:
            try:
                return float(number_value) == requested_number
            except ValueError:
                return False

        if text_value:
            return _norm_words(text_value) == remainder

        if number_value:
            return _norm_words(number_value) == remainder

    return False


def _candidate_constraint_matches(
    constraints: Sequence[str | ParsedCatalogConstraint],
    properties: list[dict[str, str]],
    product_name: str,
    *,
    product_family_name: str = "",
) -> tuple[list[str], list[str]]:
    matched: list[str] = []
    missing: list[str] = []
    for constraint in constraints:
        constraint_text, constraint_label = _constraint_text_and_label(constraint)
        found = False
        for prop in properties:
            if _property_matches_constraint(
                constraint_text,
                key=prop.get("property_key") or "",
                text_value=prop.get("property_value_text") or "",
                number_value=prop.get("property_value_number") or "",
            ):
                found = True
                break
        requires_structured = _requires_structured_property_match(
            constraint_text,
            constraint_label,
        )
        if (
            not found
            and requires_structured
            and _constraint_matches_variant_tail(
                constraint,
                product_name,
                product_family_name,
            )
        ):
            found = True
        if (
            not found
            and not requires_structured
            and _constraint_matches_product_name(constraint_text, product_name)
        ):
            found = True
        if found:
            matched.append(constraint_text)
        else:
            missing.append(constraint_text)
    return matched, missing


def _product_family_lookup_terms(product_family: str) -> list[str]:
    terms = [product_family.strip()]
    without_line = re.sub(r"\s+line\s*$", "", product_family.strip(), flags=re.IGNORECASE)
    if without_line:
        terms.append(without_line)
    return list(dict.fromkeys(term for term in terms if term))


def _fetch_catalog_candidates(
    vm: EcomRuntimeClientSync,
    parsed: ParsedCatalogItem,
) -> list[dict[str, str]]:
    brand = parsed.brand
    kind = parsed.product_kind
    family_terms = _product_family_lookup_terms(parsed.product_family)
    exact_family_clause = " or ".join(
        f"lower(pf.product_family_name) = lower({_sql_quote(term)})"
        for term in family_terms
    )
    rows = _sql_rows(
        vm,
        "select pv.product_sku, pv.record_path, pv.product_name, pv.brand, "
        "pk.product_kind_name, pf.product_family_name "
        "from product_variants pv "
        "join product_kinds pk on pk.product_kind_id = pv.product_kind_id "
        "join product_families pf on pf.product_family_id = pv.product_family_id "
        f"where lower(pv.brand) = lower({_sql_quote(brand)}) "
        f"and lower(pk.product_kind_name) = lower({_sql_quote(kind)}) "
        f"and ({exact_family_clause}) "
        "order by pv.product_sku limit 60;",
    )
    if rows:
        return rows

    like_family_clause = " or ".join(
        "lower(pf.product_family_name) like '%' || "
        f"lower({_sql_quote(term)}) || '%'"
        for term in family_terms
    )
    return _sql_rows(
        vm,
        "select pv.product_sku, pv.record_path, pv.product_name, pv.brand, "
        "pk.product_kind_name, pf.product_family_name "
        "from product_variants pv "
        "join product_kinds pk on pk.product_kind_id = pv.product_kind_id "
        "join product_families pf on pf.product_family_id = pv.product_family_id "
        f"where lower(pv.brand) = lower({_sql_quote(brand)}) "
        f"and lower(pk.product_kind_name) = lower({_sql_quote(kind)}) "
        f"and ({like_family_clause}) "
        "order by pv.product_sku limit 60;",
    )


def _fetch_variant_properties(
    vm: EcomRuntimeClientSync,
    skus: list[str],
) -> dict[str, list[dict[str, str]]]:
    if not skus:
        return {}
    sku_values = ", ".join(_sql_quote(sku) for sku in skus)
    rows = _sql_rows(
        vm,
        "select product_sku, property_key, property_value_text, "
        "property_value_number "
        "from product_variant_properties "
        f"where product_sku in ({sku_values}) "
        "order by product_sku, property_key;",
    )
    properties_by_sku: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        properties_by_sku.setdefault(row.get("product_sku") or "", []).append(row)
    return properties_by_sku


def _fetch_store(vm: EcomRuntimeClientSync, store_id: str | None) -> dict[str, str] | None:
    if not store_id:
        return None
    rows = _sql_rows(
        vm,
        "select store_id, store_name, city, record_path from stores "
        f"where store_id = {_sql_quote(store_id)} limit 1;",
    )
    return rows[0] if rows else None


def _fetch_availability(
    vm: EcomRuntimeClientSync,
    store_id: str | None,
    skus: list[str],
) -> dict[str, int]:
    if not store_id or not skus:
        return {}
    sku_values = ", ".join(_sql_quote(sku) for sku in skus)
    rows = _sql_rows(
        vm,
        "select product_sku, available_today_quantity from store_inventory "
        f"where store_id = {_sql_quote(store_id)} "
        f"and product_sku in ({sku_values});",
    )
    availability: dict[str, int] = {}
    for row in rows:
        try:
            availability[row.get("product_sku") or ""] = int(
                row.get("available_today_quantity") or "0"
            )
        except ValueError:
            availability[row.get("product_sku") or ""] = 0
    return availability


def _availability_qualifies(
    available_today: int,
    *,
    threshold: int | None,
    predicate: Literal["at_least", "below"],
) -> bool | None:
    if threshold is None:
        return None
    if predicate == "below":
        return available_today < threshold
    return available_today >= threshold


def _refs_to_submit_for_availability_count(
    exact_matches: list[dict[str, Any]],
    *,
    predicate: Literal["at_least", "below"],  # noqa: ARG001 - kept for call-site clarity
) -> list[str]:
    refs: list[str] = []
    for match in exact_matches:
        if match.get("availability_qualifies") is not True:
            continue
        # Count tasks can qualify zero-stock products for "fewer than N", but
        # AGENTS.MD says availability answers should not cite unavailable
        # products. Keep zero-stock products in qualifying_item_count only.
        if (match.get("available_today_quantity") or 0) > 0:
            refs.append(match["record_path"])
    return refs


def resolve_catalog_items(
    vm: EcomRuntimeClientSync,
    cmd: ReqResolveCatalogItems,
) -> dict[str, Any]:
    if not _has_catalog_projection(vm):
        raise RuntimeError("known ECOM catalogue SQL projection is unavailable")

    store = _fetch_store(vm, cmd.store_id)
    parsed_items = _parse_catalog_descriptions(cmd.items)
    all_skus: list[str] = []
    candidate_rows_by_index: list[list[dict[str, str]]] = []

    for parsed in parsed_items:
        candidates = _fetch_catalog_candidates(vm, parsed)
        candidate_rows_by_index.append(candidates)
        all_skus.extend(row.get("product_sku") or "" for row in candidates)

    all_skus = sorted({sku for sku in all_skus if sku})
    properties_by_sku = _fetch_variant_properties(vm, all_skus)
    availability_by_sku = _fetch_availability(vm, cmd.store_id, all_skus)

    resolved_items: list[dict[str, Any]] = []
    for item, parsed, candidates in zip(
        cmd.items, parsed_items, candidate_rows_by_index, strict=True
    ):
        constraints = parsed.constraints
        support_base_constraints, support_extra_constraints, support_extra_text = (
            _split_support_note_constraints(item, constraints)
        )
        exact_matches: list[dict[str, Any]] = []
        closest_candidates: list[dict[str, Any]] = []
        support_note_base_matches: list[dict[str, Any]] = []
        threshold = item.requested_quantity or cmd.availability_threshold

        for candidate in candidates:
            sku = candidate.get("product_sku") or ""
            props = properties_by_sku.get(sku, [])
            matched, missing = _candidate_constraint_matches(
                constraints,
                props,
                candidate.get("product_name") or "",
                product_family_name=candidate.get("product_family_name") or "",
            )
            available_today = availability_by_sku.get(sku, 0) if cmd.store_id else None
            qualifies = (
                _availability_qualifies(
                    available_today or 0,
                    threshold=threshold,
                    predicate=cmd.availability_predicate,
                )
                if cmd.store_id
                else None
            )
            candidate_payload = {
                "sku": sku,
                "record_path": candidate.get("record_path") or "",
                "product_name": candidate.get("product_name") or "",
                "matched_constraints": matched,
                "missing_constraints": missing,
                "available_today_quantity": available_today,
                "availability_qualifies": qualifies,
            }
            if not missing:
                exact_matches.append(candidate_payload)
            closest_candidates.append(candidate_payload)

            if support_extra_text:
                base_matched, base_missing = _candidate_constraint_matches(
                    support_base_constraints,
                    props,
                    candidate.get("product_name") or "",
                    product_family_name=candidate.get("product_family_name") or "",
                )
                if not base_missing:
                    extra_matched, extra_missing = _candidate_constraint_matches(
                        support_extra_constraints,
                        props,
                        candidate.get("product_name") or "",
                        product_family_name=candidate.get("product_family_name") or "",
                    )
                    if not support_extra_constraints:
                        extra_missing = [support_extra_text]
                    support_note_base_matches.append(
                        {
                            "sku": sku,
                            "record_path": candidate.get("record_path") or "",
                            "product_name": candidate.get("product_name") or "",
                            "base_matched_constraints": base_matched,
                            "extra_claim_text": support_extra_text,
                            "extra_claim_matched_constraints": extra_matched,
                            "extra_claim_missing_constraints": extra_missing,
                            "available_today_quantity": available_today,
                            "availability_qualifies": qualifies,
                        }
                    )

        closest_candidates.sort(
            key=lambda candidate: (
                -len(candidate["matched_constraints"]),
                len(candidate["missing_constraints"]),
                candidate["sku"],
            )
        )
        if not candidates:
            status = "no_base_match"
        elif len(exact_matches) == 1:
            status = "unique_exact_match"
        elif len(exact_matches) > 1:
            status = "ambiguous_exact_match"
        else:
            status = "no_exact_match"

        matched_refs = [match["record_path"] for match in exact_matches]
        qualifying_refs = [
            match["record_path"]
            for match in exact_matches
            if match.get("availability_qualifies") is True
        ]
        available_qualifying_refs = [
            match["record_path"]
            for match in exact_matches
            if match.get("availability_qualifies") is True
            and (match.get("available_today_quantity") or 0) > 0
        ]
        qualifies_for_availability_count = any(
            match.get("availability_qualifies") is True for match in exact_matches
        )
        refs_to_submit_for_availability_count = _refs_to_submit_for_availability_count(
            exact_matches,
            predicate=cmd.availability_predicate,
        )
        support_note_refs_to_submit = [
            match["record_path"] for match in support_note_base_matches
        ]
        if support_note_base_matches:
            if any(
                not match["extra_claim_missing_constraints"]
                for match in support_note_base_matches
            ):
                support_note_answer_hint = "base_product_and_extra_claim_match"
            else:
                support_note_answer_hint = "base_product_exists_extra_claim_absent_answer_no"
        else:
            support_note_answer_hint = (
                "base_product_absent" if support_extra_text else ""
            )

        resolved_items.append(
            {
                "item_id": item.item_id,
                "description": item.description,
                "parsed": parsed.model_dump(),
                "status": status,
                "exact_matches": exact_matches[:12],
                "closest_candidates": closest_candidates[:6],
                "matched_refs": matched_refs,
                "qualifying_refs": qualifying_refs,
                "available_qualifying_refs": available_qualifying_refs,
                "qualifies_for_availability_count": qualifies_for_availability_count,
                "refs_to_submit_for_availability_count": (
                    refs_to_submit_for_availability_count
                ),
                "support_note_extra_claim": {
                    "base_constraints": [
                        _constraint_text(constraint)
                        for constraint in support_base_constraints
                    ],
                    "extra_constraints": [
                        _constraint_text(constraint)
                        for constraint in support_extra_constraints
                    ],
                    "extra_claim_text": support_extra_text,
                    "base_matches": support_note_base_matches[:12],
                    "refs_to_submit": support_note_refs_to_submit,
                    "answer_hint": support_note_answer_hint,
                }
                if support_extra_text
                else None,
            }
        )

    refs_to_submit_for_availability_count = [
        ref
        for item in resolved_items
        for ref in item.get("refs_to_submit_for_availability_count", [])
    ]
    qualifying_item_count = sum(
        1 for item in resolved_items if item.get("qualifies_for_availability_count")
    )

    return {
        "status": "ok",
        "availability_predicate": cmd.availability_predicate,
        "store": store,
        "store_ref": (store or {}).get("record_path"),
        "qualifying_item_count": qualifying_item_count,
        "refs_to_submit_for_availability_count": refs_to_submit_for_availability_count,
        "items": resolved_items,
    }
