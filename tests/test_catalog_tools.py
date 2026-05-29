from types import SimpleNamespace

from catalog_tools import (
    CatalogLookupItem,
    ParsedCatalogConstraint,
    ParsedCatalogItems,
    _availability_qualifies,
    _candidate_constraint_matches,
    _constraint_matches_product_name,
    _norm_words,
    _number_from_text,
    _parsed_response,
    _product_family_lookup_terms,
    _property_label_candidates,
    _property_matches_constraint,
    _refs_to_submit_for_availability_count,
    _split_support_note_constraints,
    _variant_tail_text,
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
    assert _number_from_text("about 12,5 mm") == 12.5
    assert _number_from_text("none") is None


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
