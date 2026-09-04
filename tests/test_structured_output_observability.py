"""Structured-output diagnostics, repair, redaction and schema-boundary tests."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field, ValidationError

from stock_research_agent.agents.validator import ThesisValidationModelOutput
from stock_research_agent.llm import (
    StructuredOutputInvocationError,
    StructuredOutputOptions,
    build_observable_structured_output,
    describe_exception,
)


class ExampleOutput(BaseModel):
    count: int = Field(ge=1)


class ScriptedRunnable:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RecordingChatModel:
    def __init__(self, runnable: ScriptedRunnable):
        self.runnable = runnable
        self.binding = None

    def with_structured_output(self, schema, **kwargs):
        self.binding = (schema, kwargs)
        return self.runnable


def _envelope(*, parsed=None, parsing_error=None, content="{}"):
    return {
        "raw": AIMessage(
            content=content,
            id="msg_test",
            response_metadata={"finish_reason": "stop", "model_name": "test-model"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        ),
        "parsed": parsed,
        "parsing_error": parsing_error,
    }


def _options(path: Path, *, repair_attempts: int = 1) -> StructuredOutputOptions:
    return StructuredOutputOptions(
        strict=True,
        repair_attempts=repair_attempts,
        diagnostics_path=path,
        raw_max_characters=2_000,
    )


def test_success_records_trace_finish_reason_and_usage(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    runnable = ScriptedRunnable([_envelope(parsed=ExampleOutput(count=2))])
    chat = RecordingChatModel(runnable)
    observable = build_observable_structured_output(
        chat,
        ExampleOutput,
        method="json_schema",
        operation="test.success",
        options=_options(path),
    )

    result = asyncio.run(
        observable.ainvoke([HumanMessage(content='{"run_id":"run_test","as_of":"2026-08-26"}')])
    )

    assert result.count == 2
    assert chat.binding[0]["title"] == "ExampleOutput"
    assert chat.binding[1] == {
        "method": "json_schema",
        "include_raw": True,
        "strict": True,
    }
    event = json.loads(path.read_text(encoding="utf-8"))
    assert event["status"] == "success"
    assert event["trace"]["run_id"] == "run_test"
    assert event["response"]["response_metadata"]["finish_reason"] == "stop"
    assert event["response"]["usage_metadata"]["total_tokens"] == 15


def test_validation_failure_is_repaired_with_field_level_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    try:
        ExampleOutput.model_validate({"count": 0})
    except ValidationError as exc:
        parsing_error = exc
    runnable = ScriptedRunnable(
        [
            _envelope(
                parsing_error=parsing_error,
                content='{"count":0}',
            ),
            _envelope(
                parsed=ExampleOutput(count=1),
                content='{"count":1}',
            ),
        ]
    )
    observable = build_observable_structured_output(
        RecordingChatModel(runnable),
        ExampleOutput,
        method="json_schema",
        operation="test.repair",
        options=_options(path),
    )

    result = asyncio.run(observable.ainvoke([HumanMessage(content='{"run_id":"run_repair"}')]))

    assert result.count == 1
    assert len(runnable.calls) == 2
    assert "必须逐条修复" in runnable.calls[1][-1].content
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["status"] for event in events] == ["validation_error", "repaired"]
    assert events[0]["validation_errors"][0].startswith("count:")
    assert events[1]["repaired_from_event"] == events[0]["event_id"]


def test_schema_override_and_post_validation_are_repaired(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    runnable = ScriptedRunnable(
        [
            _envelope(parsed=ExampleOutput(count=2), content='{"count":2}'),
            _envelope(parsed=ExampleOutput(count=1), content='{"count":1}'),
        ]
    )
    chat = RecordingChatModel(runnable)
    schema = ExampleOutput.model_json_schema()
    schema["properties"]["count"]["enum"] = [1]

    def require_allowed_count(value: ExampleOutput) -> ExampleOutput:
        if value.count != 1:
            raise ValueError("count 不在本次调用动态允许值内")
        return value

    observable = build_observable_structured_output(
        chat,
        ExampleOutput,
        method="json_schema",
        operation="test.dynamic_schema",
        options=_options(path),
        json_schema_override=schema,
        post_validate=require_allowed_count,
    )

    result = asyncio.run(observable.ainvoke([HumanMessage(content='{"run_id":"run_dynamic"}')]))

    assert result.count == 1
    assert chat.binding[0]["properties"]["count"]["enum"] == [1]
    assert len(runnable.calls) == 2
    assert "动态允许值" in runnable.calls[1][-1].content
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [event["status"] for event in events] == ["validation_error", "repaired"]


def test_pre_validation_normalizes_raw_mapping_before_pydantic(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    runnable = ScriptedRunnable([_envelope(parsed={"count": 0}, content='{"count":0}')])
    observable = build_observable_structured_output(
        RecordingChatModel(runnable),
        ExampleOutput,
        method="json_schema",
        operation="test.pre_validate",
        options=_options(path),
        pre_validate=lambda value: {**value, "count": 1},
    )

    result = asyncio.run(observable.ainvoke([HumanMessage(content="{}")]))

    assert result.count == 1
    assert len(runnable.calls) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "success"


def test_exhausted_failure_has_correlation_id_and_redacts_secrets(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    try:
        ExampleOutput.model_validate({"count": 0})
    except ValidationError as exc:
        parsing_error = exc
    secret = "sk-example-secret-value-123456789"
    runnable = ScriptedRunnable(
        [_envelope(parsing_error=parsing_error, content=f'{{"debug":"{secret}","count":0}}')]
    )
    observable = build_observable_structured_output(
        RecordingChatModel(runnable),
        ExampleOutput,
        method="json_schema",
        operation="test.failure",
        options=_options(path, repair_attempts=0),
    )

    with pytest.raises(StructuredOutputInvocationError) as caught:
        asyncio.run(observable.ainvoke([HumanMessage(content="{}")]))

    assert "diagnostic_event=llm_" in str(caught.value)
    assert "count:" in describe_exception(caught.value)
    persisted = path.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "[REDACTED]" in persisted


def test_validator_transport_schema_encodes_exclusive_decision_and_completed_statuses() -> None:
    schema = ThesisValidationModelOutput.model_json_schema()
    decision = schema["properties"]["decision"]
    finalization = schema["$defs"]["ThesisFinalizationPayload"]

    assert decision["discriminator"]["propertyName"] == "action"
    assert len(decision["oneOf"]) == 2
    status_schema = finalization["properties"]["final_status"]
    assert set(status_schema["enum"]) == {
        "SUPPORTED",
        "REFUTED",
        "MIXED",
        "INCONCLUSIVE",
    }
    assert finalization["properties"]["evidence_assessments"]["minItems"] == 1
