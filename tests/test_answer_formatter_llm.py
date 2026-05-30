import os

import pytest
from openai import OpenAI

from answer_formatter import format_completion_message
from config import load_dotenv, openai_client_kwargs


load_dotenv()

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        os.getenv("RUN_LLM_TESTS") != "1" or not os.getenv("OPENAI_API_KEY"),
        reason="set RUN_LLM_TESTS=1 and OPENAI_API_KEY to run formatter LLM tests",
    ),
]


PROD_TRUE_FALSE_AGENTS_MD = """
# ECOM1 Production Workspace

For yes/no answers, answer exactly `TRUE(1)` or `FALSE(0)`.
"""

PROD_YES_NO_AGENTS_MD = """
# Founding principles of Agentic E-Commerce OS System (v2/PROD)

When answering yes/no questions - include <YES> or <NO> tokens in the response.
"""


def _client() -> OpenAI:
    return OpenAI(**openai_client_kwargs())


@pytest.mark.parametrize(
    ("task_text", "current_message", "agents_md", "expected"),
    [
        pytest.param(
            (
                "Do you have 9 of 'bosch gws 1400 not the plain 125 listing' "
                "(but not PT-GRD-BOS-GWS1400-CASE) in stock in PowerTools near "
                "Mitte Innsbruck?"
            ),
            "Yes, that matching product has enough same-day stock.",
            PROD_TRUE_FALSE_AGENTS_MD,
            "TRUE(1)",
            id="prod-t002-true1-token",
        ),
        pytest.param(
            (
                "Look at the uploaded OCR receipt /uploads/iB1wmugW_receipt_ocr.txt. "
                "Can I buy this exact basket today from the same branch? "
                "Answer as a yes/no only."
            ),
            "No, the exact basket is not available today from that branch.",
            PROD_YES_NO_AGENTS_MD,
            "<NO>",
            id="prod-t023-angle-no-token",
        ),
        pytest.param(
            (
                "Look up product SKU PT-DRL-BOS-GSR55-5AH. Return the exact "
                "value of `properties.brushless` only."
            ),
            "The properties.brushless value is true.",
            PROD_TRUE_FALSE_AGENTS_MD,
            "true",
            id="prod-t090-exact-json-boolean",
        ),
    ],
)
def test_formatter_llm_follows_prod_run_format_examples(
    task_text: str,
    current_message: str,
    agents_md: str,
    expected: str,
) -> None:
    output = format_completion_message(
        _client(),
        task_text=task_text,
        task_type="catalog_lookup",
        current_message=current_message,
        outcome="OUTCOME_OK",
        completed_steps_laconic=["Resolved the request from runtime evidence."],
        grounding_refs=["/docs/catalogue-lookup.md"],
        agents_md=agents_md,
        debug=True,
        output_lines=[],
    )

    assert output == expected
