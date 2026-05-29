from types import SimpleNamespace

from catalog_tools import (
    ParsedCatalogConstraint,
    ParsedCatalogItems,
    _availability_qualifies,
    _candidate_constraint_matches,
    _constraint_matches_product_name,
    _norm_words,
    _number_from_text,
    _parsed_response,
    _property_label_candidates,
    _property_matches_constraint,
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
    assert _property_label_candidates("disc_diameter_mm") == [
        "disc diameter mm",
        "disc diameter",
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
