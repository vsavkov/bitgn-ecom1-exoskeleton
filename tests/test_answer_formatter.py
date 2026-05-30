from types import SimpleNamespace

from answer_formatter import (
    FormattedAnswer,
    _parsed_response,
    format_completion_message,
)


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_parsed={
                "missed_elements": "used tenant boolean format",
                "formatted_message": "TRUE(1)",
            },
            output=[],
        )


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def test_format_completion_message_passes_agents_md_to_formatter() -> None:
    client = FakeClient()

    result = format_completion_message(
        client,
        task_text="Can I?",
        current_message="Yes this is allowed",
        outcome="OUTCOME_OK",
        completed_steps_laconic=[],
        grounding_refs=["/docs/security.md"],
        agents_md="For yes/no answers, answer exactly `TRUE(1)` or `FALSE(0)`.",
        debug=False,
    )

    assert result == "TRUE(1)"
    payload = client.responses.calls[0]["input"][0]["content"]
    assert "agents_md" in payload
    assert "TRUE(1)" in payload
    assert "/docs/security.md" in payload


def test_parsed_response_accepts_top_level_and_nested_structured_output() -> None:
    parsed = _parsed_response(
        SimpleNamespace(
            output_parsed={
                "missed_elements": "missing token",
                "formatted_message": "<YES> ok",
            },
            output=[],
        )
    )
    assert parsed == FormattedAnswer(
        missed_elements="missing token",
        formatted_message="<YES> ok",
    )

    nested = _parsed_response(
        SimpleNamespace(
            output_parsed=None,
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            parsed={
                                "missed_elements": "",
                                "formatted_message": "count: 1",
                            }
                        )
                    ],
                )
            ],
        )
    )
    assert nested == FormattedAnswer(missed_elements="", formatted_message="count: 1")
