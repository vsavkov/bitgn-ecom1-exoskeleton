from types import SimpleNamespace

from catalog_tools import (
    CatalogLookupItem,
    ParsedCatalogConstraint,
    ParsedCatalogItems,
    ReqResolveCityAvailability,
    _extract_city_availability_request,
    _format_count_message,
    _availability_qualifies,
    _candidate_constraint_matches,
    _constraint_matches_product_name,
    _norm_words,
    _number_from_text,
    _parsed_response,
    _product_family_lookup_terms,
    _property_rows_from_json,
    _property_label_candidates,
    _property_matches_constraint,
    _refs_to_submit_for_availability_count,
    _split_support_note_constraints,
    _variant_tail_text,
    resolve_city_availability,
)


def test_parsed_response_accepts_structured_output() -> None:
    parsed = _parsed_response(
        SimpleNamespace(
            output_parsed={
                "items": [
                    {
                        "item_index": 0,
                        "brand": "Heco",
                        "product_kind": "wood screw",
                        "product_family": "TopFix",
                        "constraints": [],
                    }
                ]
            },
            output=[],
        )
    )

    assert isinstance(parsed, ParsedCatalogItems)
    assert parsed.items[0].brand == "Heco"

    nested = _parsed_response(
        SimpleNamespace(
            output_parsed=None,
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            parsed={
                                "items": [
                                    {
                                        "item_index": 0,
                                        "brand": "Bosch",
                                        "product_kind": "drill",
                                        "product_family": "X",
                                        "constraints": [],
                                    }
                                ]
                            }
                        )
                    ],
                )
            ],
        )
    )
    assert isinstance(nested, ParsedCatalogItems)
    assert nested.items[0].brand == "Bosch"


def test_catalog_text_normalization_and_numbers() -> None:
    assert _norm_words("Disc-Diameter: 180 mm") == "disc diameter 180 mm"
    assert _norm_words("2500ml 38cm 36V 3XL 10W-40") == (
        "2500 ml 38 cm 36 v 3 xl 10 w 40"
    )
    assert _norm_words("2 pcs 3pc pieces") == "2 pc 3 pc pc"
    assert _property_label_candidates("disc_diameter_mm") == [
        "disc diameter mm",
        "disc diameter",
        "diameter",
    ]
    assert _property_label_candidates("rated_voltage_v") == [
        "rated voltage v",
        "rated voltage",
        "voltage",
    ]
    assert _property_label_candidates("apparel_size") == ["apparel size", "size"]
    assert _property_label_candidates("paint_color") == [
        "color family",
        "paint color",
        "color",
    ]
    assert _property_label_candidates("charging_current_a") == [
        "charging current a",
        "charging current",
        "amperage",
        "current",
    ]
    assert _property_label_candidates("light_output_lm") == [
        "light output lm",
        "luminous flux",
        "light output",
        "lumens",
        "flux",
    ]
    assert _number_from_text("about 12,5 mm") == 12.5
    assert _number_from_text("none") is None


def test_property_rows_from_json_adds_variant_properties() -> None:
    assert _property_rows_from_json('{"rated_voltage_v": 400, "pack_count": "2pc"}') == [
        {
            "property_key": "rated_voltage_v",
            "property_value_text": "",
            "property_value_number": "400",
        },
        {
            "property_key": "pack_count",
            "property_value_text": "2pc",
            "property_value_number": "",
        },
    ]
    assert _property_rows_from_json("not json") == []


def test_catalog_constraint_matching() -> None:
    assert _property_matches_constraint(
        "disc diameter 180 mm",
        key="disc_diameter_mm",
        text_value="",
        number_value="180",
    )
    assert _property_matches_constraint(
        "color family black",
        key="color_family",
        text_value="Black",
        number_value="",
    )
    assert _property_matches_constraint(
        "size 3XL",
        key="apparel_size",
        text_value="3XL",
        number_value="",
    )
    assert _property_matches_constraint(
        "voltage 400 V",
        key="rated_voltage_v",
        text_value="",
        number_value="400",
    )
    assert _property_matches_constraint(
        "current 6 A",
        key="charging_current_a",
        text_value="",
        number_value="6",
    )
    assert _property_matches_constraint(
        "luminous flux 470 lm",
        key="lumens",
        text_value="",
        number_value="470",
    )
    assert _property_matches_constraint(
        "luminous flux 470 lm",
        key="light_output_lm",
        text_value="",
        number_value="470",
    )
    assert not _property_matches_constraint(
        "color family red",
        key="color_family",
        text_value="Black",
        number_value="",
    )
    assert _constraint_matches_product_name(
        "zinc plated", "Heco Zinc Plated TopFix Wood Screw"
    )
    assert not _constraint_matches_product_name("type", "Heco Screw")


def test_candidate_constraint_matches_and_availability() -> None:
    matched, missing = _candidate_constraint_matches(
        ["color family black", "zinc plated", "diameter 5 mm"],
        [{"property_key": "color_family", "property_value_text": "Black"}],
        "Heco Zinc Plated TopFix Screw",
    )

    assert matched == ["color family black", "zinc plated"]
    assert missing == ["diameter 5 mm"]
    assert _availability_qualifies(5, threshold=3, predicate="at_least")
    assert not _availability_qualifies(2, threshold=3, predicate="at_least")
    assert _availability_qualifies(2, threshold=3, predicate="below")
    assert _availability_qualifies(2, threshold=None, predicate="below") is None


def test_candidate_constraints_use_variant_json_properties_and_unit_aliases() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="cleaning type microfiber cloth",
                label="cleaning type",
                value="microfiber cloth",
            ),
            ParsedCatalogConstraint(
                text="pack count 2 pcs",
                label="pack count",
                value="2 pcs",
            ),
        ],
        _property_rows_from_json('{"cleaning_type": "microfiber cloth"}'),
        "Leifheit Fresh Profi Cloth Mop and Wipe microfiber cloth 2pc multi surface",
        product_family_name="Leifheit Fresh Profi Cloth Mop and Wipe",
    )

    assert matched == ["cleaning type microfiber cloth", "pack count 2 pcs"]
    assert missing == []


def test_parsed_property_constraints_do_not_fallback_to_product_name() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="screw type wood screw",
                label="screw type",
                value="wood screw",
            )
        ],
        [
            {
                "property_key": "screw_type",
                "property_value_text": "drywall screw",
                "property_value_number": "",
            }
        ],
        "Heco Zinc Plated TopFix Wood and Drywall Screw",
    )

    assert matched == []
    assert missing == ["screw type wood screw"]


def test_structured_constraints_can_match_variant_name_tail() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="color family Yellow",
                label="color family",
                value="Yellow",
            ),
            ParsedCatalogConstraint(text="size XL", label="size", value="XL"),
        ],
        [],
        "Uvex Bionic x-fit Y59-F8N Work Jacket Yellow XL thermal",
        product_family_name="Uvex Bionic x-fit Y59-F8N Work Jacket",
    )

    assert matched == ["color family Yellow", "size XL"]
    assert missing == []


def test_structured_constraints_match_compact_unit_variant_tail() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="tool type hammer",
                label="tool type",
                value="hammer",
            ),
            ParsedCatalogConstraint(
                text="length 250 mm",
                label="length",
                value="250 mm",
            ),
        ],
        [],
        "Fiskars PowerGear FSK 1CW-AXR Hammer Measuring and Cutting Tool hammer 250mm",
        product_family_name=(
            "Fiskars PowerGear FSK 1CW-AXR Hammer Measuring and Cutting Tool"
        ),
    )

    assert matched == ["tool type hammer", "length 250 mm"]
    assert missing == []


def test_structured_constraints_match_compact_current_variant_tail() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="product type jump starter",
                label="product type",
                value="jump starter",
            ),
            ParsedCatalogConstraint(
                text="voltage 12 V",
                label="voltage",
                value="12 V",
            ),
            ParsedCatalogConstraint(
                text="current 6 A",
                label="current",
                value="6 A",
            ),
        ],
        [],
        "Osram Classic Night 3QE-SVE Automotive Charger and Bulb jump starter 12V 6A",
        product_family_name="Osram Classic Night 3QE-SVE Automotive Charger and Bulb",
    )

    assert matched == ["product type jump starter", "voltage 12 V", "current 6 A"]
    assert missing == []


def test_structured_constraints_match_lumen_property_aliases() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="wattage 6 W",
                label="wattage",
                value="6 W",
            ),
            ParsedCatalogConstraint(
                text="luminous flux 470 lm",
                label="luminous flux",
                value="470 lm",
            ),
        ],
        [
            {
                "property_key": "wattage_w",
                "property_value_text": "",
                "property_value_number": "6",
            },
            {
                "property_key": "lumens",
                "property_value_text": "",
                "property_value_number": "470",
            },
        ],
        "Osram Warm Classic 2NL-Z7I LED Bulb G9 6W 2700K non-dimmable",
        product_family_name="Osram Warm Classic 2NL-Z7I LED Bulb",
    )

    assert matched == ["wattage 6 W", "luminous flux 470 lm"]
    assert missing == []


def test_structured_constraints_match_compact_metric_dimensions() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="power source petrol",
                label="power source",
                value="petrol",
            ),
            ParsedCatalogConstraint(
                text="cutting width 38 cm",
                label="cutting width",
                value="38 cm",
            ),
        ],
        [],
        "Wolf-Garten Silent WG 2IA-DMB Lawn Mower petrol 38cm",
        product_family_name="Wolf-Garten Silent WG 2IA-DMB Lawn Mower",
    )

    assert matched == ["power source petrol", "cutting width 38 cm"]
    assert missing == []


def test_structured_constraints_match_ip_rating_variant_tail() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="device type socket outlet",
                label="device type",
                value="socket outlet",
            ),
            ParsedCatalogConstraint(
                text="color family Green",
                label="color family",
                value="Green",
            ),
            ParsedCatalogConstraint(
                text="ip rating IP44",
                label="ip rating",
                value="IP44",
            ),
        ],
        [],
        "Kopp Professional KOP 1ME-LSV Wiring Device socket outlet Green IP44",
        product_family_name="Kopp Professional KOP 1ME-LSV Wiring Device",
    )

    assert matched == [
        "device type socket outlet",
        "color family Green",
        "ip rating IP44",
    ]
    assert missing == []


def test_variant_tail_matching_does_not_use_family_words() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="screw type wood screw",
                label="screw type",
                value="wood screw",
            )
        ],
        [],
        "Heco Zinc Plated TopFix Wood and Drywall Screw",
        product_family_name="Heco Zinc Plated TopFix Wood and Drywall Screw",
    )

    assert matched == []
    assert missing == ["screw type wood screw"]


def test_variant_tail_matching_handles_apparel_display_values() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="garment type t-shirt",
                label="garment type",
                value="t-shirt",
            ),
            ParsedCatalogConstraint(
                text="color family Black",
                label="color family",
                value="Black",
            ),
            ParsedCatalogConstraint(text="size L", label="size", value="L"),
        ],
        [],
        "Dickies Fleece Redhawk MRB-WYE Work Top t-shirt Black L",
        product_family_name="Dickies Fleece Redhawk MRB-WYE Work Top",
    )

    assert matched == ["garment type t-shirt", "color family Black", "size L"]
    assert missing == []


def test_variant_tail_matching_handles_storage_display_values() -> None:
    matched, missing = _candidate_constraint_matches(
        [
            ParsedCatalogConstraint(
                text="storage type shelving unit",
                label="storage type",
                value="shelving unit",
            ),
            ParsedCatalogConstraint(
                text="color family Blue",
                label="color family",
                value="Blue",
            ),
        ],
        [],
        "Raaco Compact 3YH-7PQ Tool Box and Bag shelving unit 40l Blue",
        product_family_name="Raaco Compact 3YH-7PQ Tool Box and Bag",
    )

    assert matched == ["storage type shelving unit", "color family Blue"]
    assert missing == []


def test_variant_tail_requires_family_prefix() -> None:
    assert (
        _variant_tail_text(
            "Unexpected Prefix Uvex Bionic x-fit Y59-F8N Work Jacket Yellow XL",
            "Uvex Bionic x-fit Y59-F8N Work Jacket",
        )
        == ""
    )


def test_product_family_lookup_terms_strip_trailing_line() -> None:
    assert _product_family_lookup_terms("Uvex Bionic x-fit Y59-F8N Work Jacket line") == [
        "Uvex Bionic x-fit Y59-F8N Work Jacket line",
        "Uvex Bionic x-fit Y59-F8N Work Jacket",
    ]


def test_availability_count_refs_exclude_zero_stock_for_below_predicate() -> None:
    matches = [
        {
            "record_path": "/proc/catalog/a.json",
            "available_today_quantity": 0,
            "availability_qualifies": True,
        },
        {
            "record_path": "/proc/catalog/b.json",
            "available_today_quantity": 4,
            "availability_qualifies": True,
        },
    ]

    assert _refs_to_submit_for_availability_count(matches, predicate="below") == [
        "/proc/catalog/b.json"
    ]
    assert _refs_to_submit_for_availability_count(matches, predicate="at_least") == [
        "/proc/catalog/b.json"
    ]


def test_support_note_constraints_split_base_and_extra_claim() -> None:
    constraints = [
        ParsedCatalogConstraint(
            text="storage type parts case",
            label="storage type",
            value="parts case",
        ),
        ParsedCatalogConstraint(
            text="stackable system only",
            label="stackable",
            value="system only",
        ),
    ]

    base, extra, extra_text = _split_support_note_constraints(
        CatalogLookupItem(
            item_id="support-note",
            description=(
                "the Tool Box and Bag from Festool in the Festool Stackable SYS "
                "3JJ-9LM Tool Box and Bag line that has storage type parts case "
                "and has stackable system only"
            ),
        ),
        constraints,
    )

    assert [constraint.text for constraint in base] == ["storage type parts case"]
    assert [constraint.text for constraint in extra] == ["stackable system only"]
    assert extra_text == "stackable system only"


def test_support_note_constraints_keep_extra_capability_as_text() -> None:
    constraints = [
        ParsedCatalogConstraint(
            text="machine type drill press",
            label="machine type",
            value="drill press",
        )
    ]

    base, extra, extra_text = _split_support_note_constraints(
        CatalogLookupItem(
            item_id="support-note",
            description=(
                "the Workshop Drill Grinder and Sander from Makita in the "
                "Makita Workshop DHS line that has machine type drill press "
                "and supports voice control"
            ),
        ),
        constraints,
    )

    assert [constraint.text for constraint in base] == ["machine type drill press"]
    assert extra == []
    assert extra_text == "voice control"


def test_extract_city_availability_request_and_format() -> None:
    cmd = _extract_city_availability_request(
        (
            "I can visit any PowerTool branch in Vienna today. Across every "
            "Vienna branch, including branches with 0 availability, how many "
            "units of product (the Wood and Drywall Screw from Heco in the "
            "Heco Zinc Plated TopFix GTU-YPJ Wood and Drywall Screw line that "
            "has screw type drywall screw) are available today? Answer exactly "
            "as \"count: %d\" and cite every city store record plus the product record."
        )
    )

    assert cmd is not None
    assert cmd.city == "Vienna"
    assert cmd.product_description.startswith("the Wood and Drywall Screw")
    assert _format_count_message(cmd.answer_format, 4) == "count: 4"


class CityAvailabilityExecResult:
    def __init__(self, stdout: str = "", exit_code: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = stderr


class CityAvailabilityFakeVM:
    def exec(self, request) -> CityAvailabilityExecResult:
        query = request.stdin
        if "sqlite_schema" in query:
            return CityAvailabilityExecResult(
                "name\n"
                "product_families\n"
                "product_kinds\n"
                "product_variant_properties\n"
                "product_variants\n"
                "store_inventory\n"
                "stores\n"
            )
        if "from product_variants pv" in query:
            return CityAvailabilityExecResult(
                "product_sku,record_path,product_name,brand,variant_properties,"
                "product_kind_name,product_family_name,family_properties\n"
                "FST-1KPF96UD,/proc/catalog/FST-1KPF96UD.json,"
                "Heco Zinc Plated TopFix GTU-YPJ Wood and Drywall Screw drywall screw,"
                "Heco,{},Wood and Drywall Screw,"
                "Heco Zinc Plated TopFix GTU-YPJ Wood and Drywall Screw,{}\n"
            )
        if "from product_variant_properties" in query:
            return CityAvailabilityExecResult(
                "product_sku,property_key,property_value_text,property_value_number\n"
                "FST-1KPF96UD,screw_type,drywall screw,\n"
            )
        if "from stores s" in query:
            return CityAvailabilityExecResult(
                "store_id,record_path,store_name,city,available_today_quantity\n"
                "store_vienna_meidling,/proc/stores/store_vienna_meidling.json,"
                "PowerTool Vienna Meidling,Vienna,4\n"
                "store_vienna_praterstern,/proc/stores/store_vienna_praterstern.json,"
                "PowerTool Vienna Praterstern,Vienna,0\n"
            )
        raise AssertionError(f"unexpected SQL: {query}")


def test_resolve_city_availability(monkeypatch) -> None:
    monkeypatch.setattr(
        "catalog_tools._parse_catalog_descriptions",
        lambda items: [
            ParsedCatalogItems.model_validate(
                {
                    "items": [
                        {
                            "item_index": 0,
                            "brand": "Heco",
                            "product_kind": "Wood and Drywall Screw",
                            "product_family": (
                                "Heco Zinc Plated TopFix GTU-YPJ Wood and Drywall Screw"
                            ),
                            "constraints": [
                                {
                                    "text": "screw type drywall screw",
                                    "label": "screw type",
                                    "value": "drywall screw",
                                }
                            ],
                        }
                    ]
                }
            ).items[0]
        ],
    )

    result = resolve_city_availability(
        CityAvailabilityFakeVM(),
        ReqResolveCityAvailability(
            product_description="the Wood and Drywall Screw from Heco ...",
            city="Vienna",
            answer_format="count: %d",
        ),
    )

    assert result["formatted_message"] == "count: 4"
    assert result["refs_to_submit"] == [
        "/proc/catalog/FST-1KPF96UD.json",
        "/proc/stores/store_vienna_meidling.json",
        "/proc/stores/store_vienna_praterstern.json",
    ]
