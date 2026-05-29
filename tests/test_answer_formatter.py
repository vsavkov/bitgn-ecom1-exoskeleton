from types import SimpleNamespace

from answer_formatter import (
    FormattedAnswer,
    _leading_yes_no_token_message,
    _parsed_response,
    format_completion_message,
)


def test_leading_yes_no_token_message_rewrites_only_word_plus_space() -> None:
    assert _leading_yes_no_token_message(" yes it is") == "<YES> it is"
    assert _leading_yes_no_token_message("NO thanks") == "<NO> thanks"
    assert _leading_yes_no_token_message("yes") == "<YES>"
    assert _leading_yes_no_token_message(" No ") == "<NO>"
    assert _leading_yes_no_token_message("yesterday was fine") is None


def test_format_completion_message_fast_path_does_not_call_client() -> None:
    class Client:
        @property
        def responses(self):
            raise AssertionError("formatter client should not be used")

    output_lines: list[str] = []

    assert (
        format_completion_message(
            Client(),
            task_text="Can I?",
            current_message="Yes this is allowed",
            outcome="OUTCOME_OK",
            completed_steps_laconic=[],
            grounding_refs=[],
            debug=False,
            output_lines=output_lines,
        )
        == "<YES> this is allowed"
    )
    assert output_lines


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
