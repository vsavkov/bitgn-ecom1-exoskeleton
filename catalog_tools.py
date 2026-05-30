import csv
import io
import json
import re
from collections.abc import Sequence
from typing import Any, Literal, Protocol

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
from runtime_calls import runtime_exec


class RuntimeVM(Protocol):
    def exec(self, request: ExecRequest) -> Any: ...


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


class ReqResolveCityAvailability(BaseModel):
    product_description: str = Field(
        description=(
            "Raw single product description from a city-wide availability task, "
            "for example the text inside 'product (...)'."
        )
    )
    city: str = Field(
        description="Store city to aggregate, e.g. Vienna, Graz, Brno, or Linz."
    )
    answer_format: str = Field(
        description=(
            "Exact user-requested answer format containing %d, e.g. "
            "'count: %d' or 'qty %d'."
        )
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
        ordered.append(_repair_parsed_catalog_item(item, items[index].description))
    return ordered


def _sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _sql_rows(vm: RuntimeVM, query: str) -> list[dict[str, str]]:
    try:
        result = runtime_exec(vm, ExecRequest(path="/bin/sql", stdin=query))
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


def _has_catalog_projection(vm: RuntimeVM) -> bool:
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
    "a",
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
    "amperage",
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
    "flux",
    "ip",
    "kit",
    "length",
    "lumen",
    "lumens",
    "luminous",
    "machine",
    "mask",
    "pack",
    "power",
    "product",
    "protection",
    "season",
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
    "current",
}
_STRUCTURED_PROPERTY_LABEL_WORDS = {
    "amperage",
    "class",
    "color",
    "connection",
    "connector",
    "contents",
    "count",
    "current",
    "diameter",
    "drive",
    "family",
    "finish",
    "flux",
    "ip",
    "length",
    "lumen",
    "lumens",
    "luminous",
    "material",
    "mask",
    "pack",
    "power",
    "product",
    "protection",
    "rating",
    "screw",
    "season",
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
_LINE_THAT_HAS_RE = re.compile(r"\s+line\s+that\s+has\s+", re.IGNORECASE)
_CONSTRAINT_THAT_HAS_RE = re.compile(r"\s+that\s+has\s+", re.IGNORECASE)
_CONSTRAINT_SPLIT_RE = re.compile(
    r"\s*,\s*|\s+\band\s+has\s+|\s+\band\s+(?="
    r"(?:adapter|adhesive|anchor|bar|battery|cleaner|color|connection|connector|"
    r"cutting|diameter|disc|drive|fastener|finish|fitting|kit|length|luminous|"
    r"machine|material|pack|power|product|protection|screw|sealant|size|source|"
    r"storage|thread|tool|type|vehicle|viscosity|voltage|volume|wattage|width)\b)",
    re.IGNORECASE,
)
_CATALOG_DESCRIPTION_RE = re.compile(
    r"\bthe\s+(?P<kind>.*?)\s+from\s+(?P<brand>.*?)\s+in\s+the\s+"
    r"(?P<family>.*?)\s+line\b",
    re.IGNORECASE | re.DOTALL,
)
_CITY_AVAILABILITY_RE = re.compile(
    r"Across\s+every\s+(?P<city>[A-Za-z][A-Za-z\s-]*?)\s+branch\b.*?"
    r"product\s*\((?P<product>the\s+.*?)\)\s+are\s+available\s+today\?"
    r".*?Answer\s+exactly\s+as\s+\"(?P<format>[^\"]*%d[^\"]*)\"",
    re.IGNORECASE | re.DOTALL,
)
_UNIT_WORD_ALIASES = {
    "amp": "a",
    "amps": "a",
    "ampere": "a",
    "amperes": "a",
    "pcs": "pc",
    "pieces": "pc",
}


def _norm_words(value: str) -> str:
    value = value.lower().replace("-", " ")
    value = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    words = [_UNIT_WORD_ALIASES.get(word, word) for word in value.split()]
    return " ".join(words)


def _property_rows_from_json(raw_properties: str) -> list[dict[str, str]]:
    if not raw_properties:
        return []
    try:
        parsed = json.loads(raw_properties)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []

    rows: list[dict[str, str]] = []
    for key, value in parsed.items():
        property_key = str(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item is None:
                continue
            if isinstance(item, dict):
                text_value = item.get("text") or item.get("value_text") or item.get("value")
                number_value = item.get("number") or item.get("value_number")
            else:
                text_value = "" if isinstance(item, int | float) else item
                number_value = item if isinstance(item, int | float) else ""

            rows.append(
                {
                    "property_key": property_key,
                    "property_value_text": str(text_value or ""),
                    "property_value_number": str(number_value or ""),
                }
            )
    return rows


def _extract_city_availability_request(task_text: str) -> ReqResolveCityAvailability | None:
    match = _CITY_AVAILABILITY_RE.search(task_text)
    if not match:
        return None
    return ReqResolveCityAvailability(
        product_description=" ".join(match.group("product").split()),
        city=" ".join(match.group("city").split()),
        answer_format=match.group("format"),
    )


def _format_count_message(answer_format: str, total: int) -> str:
    return answer_format.replace("%d", str(total), 1)


def _constraint_segments(text: str) -> list[str]:
    return [
        segment.strip(" ,.;")
        for segment in _CONSTRAINT_SPLIT_RE.split(text)
        if segment.strip(" ,.;")
    ]


def _constraint_from_text(text: str) -> ParsedCatalogConstraint:
    words = text.split()
    normalized_words = _norm_words(text).split()
    label = ""
    value = text.strip()

    if len(normalized_words) >= 3 and normalized_words[1] in {
        "class",
        "contents",
        "count",
        "diameter",
        "family",
        "length",
        "platform",
        "source",
        "type",
        "volume",
        "width",
    }:
        label = " ".join(words[:2])
        value = " ".join(words[2:])
    elif normalized_words and normalized_words[0] in _STRUCTURED_PROPERTY_LABEL_WORDS:
        label = words[0]
        value = " ".join(words[1:]) if len(words) > 1 else ""

    return ParsedCatalogConstraint(text=text.strip(), label=label, value=value.strip())


def _constraints_from_text(text: str) -> list[ParsedCatalogConstraint]:
    return [_constraint_from_text(segment) for segment in _constraint_segments(text)]


def _repair_parsed_catalog_item(
    parsed: ParsedCatalogItem,
    source_description: str,
) -> ParsedCatalogItem:
    source_match = _CATALOG_DESCRIPTION_RE.search(source_description)
    product_family = parsed.product_family.strip()
    product_kind = parsed.product_kind.strip()
    brand = parsed.brand.strip()
    constraints = list(parsed.constraints)
    extra_constraints_text = ""

    if source_match:
        # The source task text follows a stable catalogue phrase. Prefer that
        # exact family boundary over helper output that may absorb variant
        # suffixes such as "Green XL" into product_family.
        product_kind = " ".join(source_match.group("kind").split())
        brand = " ".join(source_match.group("brand").split())
        product_family = " ".join(source_match.group("family").split())

    # Helper-model parsing occasionally leaves the "line that has ..." suffix
    # inside product_family. That makes the SQL family equality miss every row,
    # so repair the canonical split deterministically before querying.
    if match := _LINE_THAT_HAS_RE.search(product_family):
        extra_constraints_text = product_family[match.end() :]
        product_family = product_family[: match.start()].strip()
    elif match := _CONSTRAINT_THAT_HAS_RE.search(product_family):
        extra_constraints_text = product_family[match.end() :]
        product_family = product_family[: match.start()].strip()

    if extra_constraints_text:
        constraints.extend(_constraints_from_text(extra_constraints_text))

    if not constraints and (match := _THAT_HAS_RE.search(source_description)):
        constraints = _constraints_from_text(source_description[match.end() :])

    if not product_family:
        product_family = parsed.product_family.strip()

    seen_constraints: set[str] = set()
    deduped_constraints: list[ParsedCatalogConstraint] = []
    for constraint in constraints:
        key = _norm_words(constraint.text or f"{constraint.label} {constraint.value}")
        if not key or key in seen_constraints:
            continue
        seen_constraints.add(key)
        deduped_constraints.append(constraint)

    return parsed.model_copy(
        update={
            "brand": brand,
            "product_kind": product_kind,
            "product_family": product_family,
            "constraints": deduped_constraints,
        }
    )


def _property_label_candidates(key: str) -> list[str]:
    label = key.replace("_", " ")
    parts = label.split()
    original_parts = parts[:]
    unit_suffix = parts[-1] if parts and parts[-1] in _UNIT_SUFFIXES else ""
    labels = [label]
    if len(parts) > 1 and unit_suffix:
        labels.append(" ".join(parts[:-1]))
        parts = parts[:-1]
    if len(parts) > 1 and parts[-1] in {
        "count",
        "current",
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
    # Catalogue schemas vary between snapshots: LED brightness may be exposed as
    # luminous_flux_lm, lumens, or a generic *_lm output key, while automotive
    # charger current may be current_a, charging_current_a, or output_a.
    if unit_suffix == "lm" or set(original_parts) & {"flux", "lumen", "lumens"}:
        labels.extend(["luminous flux", "flux", "lumens"])
    if unit_suffix == "a" or set(original_parts) & {"amperage", "current"}:
        labels.extend(["current", "amperage"])
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
    vm: RuntimeVM,
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
        "pv.properties as variant_properties, "
        "pk.product_kind_name, pf.product_family_name, "
        "pf.properties as family_properties "
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
        "pv.properties as variant_properties, "
        "pk.product_kind_name, pf.product_family_name, "
        "pf.properties as family_properties "
        "from product_variants pv "
        "join product_kinds pk on pk.product_kind_id = pv.product_kind_id "
        "join product_families pf on pf.product_family_id = pv.product_family_id "
        f"where lower(pv.brand) = lower({_sql_quote(brand)}) "
        f"and lower(pk.product_kind_name) = lower({_sql_quote(kind)}) "
        f"and ({like_family_clause}) "
        "order by pv.product_sku limit 60;",
    )


def _fetch_variant_properties(
    vm: RuntimeVM,
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


def _fetch_store(vm: RuntimeVM, store_id: str | None) -> dict[str, str] | None:
    if not store_id:
        return None
    rows = _sql_rows(
        vm,
        "select store_id, store_name, city, record_path from stores "
        f"where store_id = {_sql_quote(store_id)} limit 1;",
    )
    return rows[0] if rows else None


def _fetch_availability(
    vm: RuntimeVM,
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


def catalog_quote_table_message_from_result(result: Any) -> str:
    if not isinstance(result, dict):
        return ""

    items = result.get("items")
    if not isinstance(items, list) or not items:
        return ""

    rows = ["RowID\tSKU\tin_stock\tmatch"]
    for item in items:
        if not isinstance(item, dict):
            return ""
        item_id = str(item.get("item_id") or "")
        exact_matches = item.get("exact_matches")
        if not isinstance(exact_matches, list) or len(exact_matches) != 1:
            rows.append(f"{item_id}\t\t\tfalse")
            continue

        match = exact_matches[0]
        if not isinstance(match, dict):
            rows.append(f"{item_id}\t\t\tfalse")
            continue

        sku = str(match.get("sku") or "")
        available_today = match.get("available_today_quantity")
        in_stock = "" if available_today is None else str(available_today)
        qualifies = "true" if match.get("availability_qualifies") is True else "false"
        rows.append(f"{item_id}\t{sku}\t{in_stock}\t{qualifies}")

    # Quote-list tasks require this exact TSV shape. Building it from the
    # resolver output keeps the answer aligned with the same canonical matching
    # and availability decisions used for refs.
    return "\n".join(rows)


def resolve_catalog_items(
    vm: RuntimeVM,
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
            props = [
                *properties_by_sku.get(sku, []),
                *_property_rows_from_json(candidate.get("variant_properties") or ""),
            ]
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


def _select_unique_exact_match(
    parsed: ParsedCatalogItem,
    candidates: list[dict[str, str]],
    properties_by_sku: dict[str, list[dict[str, str]]],
) -> dict[str, Any] | None:
    exact_matches: list[dict[str, Any]] = []
    for candidate in candidates:
        sku = candidate.get("product_sku") or ""
        props = [
            *properties_by_sku.get(sku, []),
            *_property_rows_from_json(candidate.get("variant_properties") or ""),
        ]
        matched, missing = _candidate_constraint_matches(
            parsed.constraints,
            props,
            candidate.get("product_name") or "",
            product_family_name=candidate.get("product_family_name") or "",
        )
        if missing:
            continue
        exact_matches.append(
            {
                "sku": sku,
                "record_path": candidate.get("record_path") or "",
                "product_name": candidate.get("product_name") or "",
                "matched_constraints": matched,
            }
        )

    if len(exact_matches) != 1:
        return None
    return exact_matches[0]


def resolve_city_availability(
    vm: RuntimeVM,
    cmd: ReqResolveCityAvailability,
) -> dict[str, Any]:
    if not _has_catalog_projection(vm):
        raise RuntimeError("known ECOM catalogue SQL projection is unavailable")

    parsed = _parse_catalog_descriptions(
        [CatalogLookupItem(description=cmd.product_description)]
    )[0]
    candidates = _fetch_catalog_candidates(vm, parsed)
    skus = sorted({row.get("product_sku") or "" for row in candidates if row.get("product_sku")})
    properties_by_sku = _fetch_variant_properties(vm, skus)
    exact_match = _select_unique_exact_match(parsed, candidates, properties_by_sku)
    if exact_match is None:
        return {
            "status": "no_unique_exact_match",
            "city": cmd.city,
            "formatted_message": _format_count_message(cmd.answer_format, 0),
            "total_available_today": 0,
            "product_ref": "",
            "store_refs": [],
            "refs_to_submit": [],
        }

    rows = _sql_rows(
        vm,
        "select s.store_id, s.record_path, s.store_name, s.city, "
        "coalesce(i.available_today_quantity, 0) as available_today_quantity "
        "from stores s "
        "left join store_inventory i on i.store_id = s.store_id "
        f"and i.product_sku = {_sql_quote(exact_match['sku'])} "
        f"where lower(s.city) = lower({_sql_quote(cmd.city)}) "
        "order by s.store_id;",
    )
    total = 0
    store_refs: list[str] = []
    store_availability: list[dict[str, Any]] = []
    for row in rows:
        try:
            available_today = int(row.get("available_today_quantity") or "0")
        except ValueError:
            available_today = 0
        total += available_today
        store_ref = row.get("record_path") or ""
        if store_ref.startswith("/"):
            store_refs.append(store_ref)
        store_availability.append(
            {
                "store_id": row.get("store_id") or "",
                "store_name": row.get("store_name") or "",
                "record_path": store_ref,
                "available_today_quantity": available_today,
            }
        )

    product_ref = exact_match["record_path"]
    return {
        "status": "ok",
        "city": cmd.city,
        "product": exact_match,
        "product_ref": product_ref,
        "store_availability": store_availability,
        "store_refs": _dedupe_strings(store_refs),
        "total_available_today": total,
        "formatted_message": _format_count_message(cmd.answer_format, total),
        "refs_to_submit": _dedupe_strings([product_ref, *store_refs]),
    }


def resolve_city_availability_from_task_text(
    vm: RuntimeVM,
    task_text: str,
) -> dict[str, Any] | None:
    cmd = _extract_city_availability_request(task_text)
    if cmd is None:
        return None
    return resolve_city_availability(vm, cmd)
