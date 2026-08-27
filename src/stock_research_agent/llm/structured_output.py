"""Strict structured-output calls with repair retries and durable diagnostics."""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

StructuredOutputMethod = Literal["function_calling", "json_schema"]

_WRITE_LOCK = threading.Lock()
_SECRET_KEY = re.compile(
    r"^(?:api[_-]?key|authorization|password|secret|access[_-]?token|"
    r"refresh[_-]?token|bearer[_-]?token|token)$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(?:sk|tk)[-_][A-Za-z0-9._-]{12,}\b|\bBearer\s+[A-Za-z0-9._-]{12,}\b"
)


@dataclass(frozen=True, slots=True)
class StructuredOutputOptions:
    """Runtime policy shared by every LLM structured-output adapter."""

    strict: bool = True
    repair_attempts: int = 1
    diagnostics_path: Path = Path(".artifacts/llm-structured-output.jsonl")
    raw_max_characters: int = 20_000

    def __post_init__(self) -> None:
        if not 0 <= self.repair_attempts <= 3:
            raise ValueError("repair_attempts must be between 0 and 3")
        if self.raw_max_characters < 1_000:
            raise ValueError("raw_max_characters must be at least 1000")


class StructuredOutputInvocationError(RuntimeError):
    """A model response could not be transported or validated.

    The exception intentionally carries a short report-safe summary. Full raw response
    metadata and field-level validation details live in the referenced JSONL event.
    """

    def __init__(
        self,
        *,
        event_id: str,
        operation: str,
        schema_name: str,
        category: Literal["transport_error", "validation_error"],
        diagnostic_path: Path,
        details: tuple[str, ...],
    ) -> None:
        self.event_id = event_id
        self.operation = operation
        self.schema_name = schema_name
        self.category = category
        self.diagnostic_path = diagnostic_path
        self.details = details
        detail = "; ".join(details[:3]) or "no field-level detail available"
        super().__init__(
            f"{category} in {operation}/{schema_name}; {detail}; "
            f"diagnostic_event={event_id}; diagnostic_path={diagnostic_path}"
        )


class ObservableStructuredOutput[ModelT: BaseModel]:
    """Small runnable-like wrapper around LangChain's ``with_structured_output``.

    ``include_raw=True`` prevents parser failures from destroying the response. When a
    response violates local Pydantic rules, the wrapper persists a diagnostic event and
    can ask the model to correct only the invalid structure while preserving context.
    """

    def __init__(
        self,
        chat_model: Any,
        schema: type[ModelT],
        *,
        method: StructuredOutputMethod,
        operation: str,
        options: StructuredOutputOptions,
    ) -> None:
        self._schema = schema
        self._method = method
        self._operation = operation
        self._options = options
        binding_kwargs: dict[str, Any] = {
            "method": method,
            "include_raw": True,
        }
        binding_schema: Any = schema
        if method == "json_schema":
            binding_kwargs["strict"] = options.strict
            # Passing a Pydantic class makes the OpenAI SDK parse inside the transport
            # call, before LangChain's include_raw fallback can preserve the message.
            # A plain JSON Schema keeps cloud-side strictness while reserving all local
            # business validation, diagnostics and repair for this wrapper.
            binding_schema = schema.model_json_schema()
        self._runnable = chat_model.with_structured_output(binding_schema, **binding_kwargs)

    async def ainvoke(self, messages: list[BaseMessage]) -> ModelT:
        trace_context = _trace_context(messages)
        current_messages = list(messages)
        last_event_id = ""
        last_details: tuple[str, ...] = ()

        for attempt in range(self._options.repair_attempts + 1):
            started = monotonic()
            try:
                envelope = await self._runnable.ainvoke(current_messages)
            except Exception as exc:
                event_id = _event_id()
                details = _exception_details(exc)
                validation_failure = _find_validation_error(exc) is not None
                status = "validation_error" if validation_failure else "transport_error"
                _record_event(
                    options=self._options,
                    event_id=event_id,
                    operation=self._operation,
                    schema_name=self._schema.__name__,
                    method=self._method,
                    strict=self._options.strict,
                    attempt=attempt + 1,
                    status=status,
                    duration_ms=_elapsed_ms(started),
                    trace_context=trace_context,
                    details=details,
                    raw=None,
                )
                if validation_failure and attempt < self._options.repair_attempts:
                    last_event_id = event_id
                    last_details = details
                    current_messages = _repair_messages(
                        messages,
                        schema_name=self._schema.__name__,
                        details=details,
                    )
                    continue
                raise StructuredOutputInvocationError(
                    event_id=event_id,
                    operation=self._operation,
                    schema_name=self._schema.__name__,
                    category=("validation_error" if validation_failure else "transport_error"),
                    diagnostic_path=self._options.diagnostics_path,
                    details=details,
                ) from exc

            parsed, parsing_error, raw = _unpack_envelope(envelope)
            try:
                if parsing_error is not None:
                    raise parsing_error
                validated = self._schema.model_validate(parsed)
            except Exception as exc:
                event_id = _event_id()
                details = _exception_details(exc)
                _record_event(
                    options=self._options,
                    event_id=event_id,
                    operation=self._operation,
                    schema_name=self._schema.__name__,
                    method=self._method,
                    strict=self._options.strict,
                    attempt=attempt + 1,
                    status="validation_error",
                    duration_ms=_elapsed_ms(started),
                    trace_context=trace_context,
                    details=details,
                    raw=raw,
                )
                last_event_id = event_id
                last_details = details
                if attempt >= self._options.repair_attempts:
                    raise StructuredOutputInvocationError(
                        event_id=event_id,
                        operation=self._operation,
                        schema_name=self._schema.__name__,
                        category="validation_error",
                        diagnostic_path=self._options.diagnostics_path,
                        details=details,
                    ) from exc
                current_messages = _repair_messages(
                    messages,
                    schema_name=self._schema.__name__,
                    details=details,
                )
                continue

            status = "repaired" if attempt else "success"
            _record_event(
                options=self._options,
                event_id=_event_id(),
                operation=self._operation,
                schema_name=self._schema.__name__,
                method=self._method,
                strict=self._options.strict,
                attempt=attempt + 1,
                status=status,
                duration_ms=_elapsed_ms(started),
                trace_context=trace_context,
                details=(),
                raw=raw,
                repaired_from_event=last_event_id or None,
            )
            return validated

        raise AssertionError(f"unreachable structured output state: {last_details}")


def build_observable_structured_output[ModelT: BaseModel](
    chat_model: Any,
    schema: type[ModelT],
    *,
    method: StructuredOutputMethod,
    operation: str,
    options: StructuredOutputOptions | None = None,
) -> ObservableStructuredOutput[ModelT]:
    return ObservableStructuredOutput(
        chat_model,
        schema,
        method=method,
        operation=operation,
        options=options or StructuredOutputOptions(),
    )


def describe_exception(exc: Exception, *, max_characters: int = 1_000) -> str:
    """Return a report-safe error including structured diagnostic correlation IDs."""

    if isinstance(exc, StructuredOutputInvocationError):
        text = f"{type(exc).__name__}: {exc}"
    elif isinstance(exc, ValidationError):
        details = _exception_details(exc)
        text = f"ValidationError: {'; '.join(details[:5])}"
    else:
        # Arbitrary provider exceptions may embed request bodies or credentials. Only the
        # structured wrapper above is allowed to surface audited, redacted details.
        text = type(exc).__name__
    return text[:max_characters]


def _unpack_envelope(envelope: Any) -> tuple[Any, Exception | None, Any]:
    if isinstance(envelope, Mapping) and {"raw", "parsed", "parsing_error"} <= set(envelope):
        return envelope.get("parsed"), envelope.get("parsing_error"), envelope.get("raw")
    # Scripted test doubles historically return the parsed Pydantic model directly.
    return envelope, None, envelope if hasattr(envelope, "response_metadata") else None


def _repair_messages(
    original_messages: list[BaseMessage],
    *,
    schema_name: str,
    details: tuple[str, ...],
) -> list[BaseMessage]:
    correction = (
        f"上一条输出没有通过本地 {schema_name} 校验。请保持原任务事实不变，"
        "仅修正 JSON 结构和字段值；不要解释，不要输出 Markdown。\n"
        "必须逐条修复以下错误：\n- "
        + "\n- ".join(details[:12])
    )
    repaired = list(original_messages)
    repaired.append(HumanMessage(content=correction))
    return repaired


def _trace_context(messages: list[BaseMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if not isinstance(message, HumanMessage) or not isinstance(message.content, str):
            continue
        try:
            payload = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        result: dict[str, Any] = {}
        for key in ("run_id", "as_of", "current_round", "round_number"):
            if key in payload:
                result[key] = payload[key]
        for target_key in ("scope_target", "research_target", "target"):
            target = payload.get(target_key)
            if isinstance(target, dict):
                result["target"] = {
                    key: target.get(key) for key in ("type", "code", "name") if key in target
                }
                break
        thesis = payload.get("thesis")
        if isinstance(thesis, dict) and thesis.get("thesis_id"):
            result["thesis_id"] = thesis["thesis_id"]
        return result
    return {}


def _record_event(
    *,
    options: StructuredOutputOptions,
    event_id: str,
    operation: str,
    schema_name: str,
    method: str,
    strict: bool,
    attempt: int,
    status: str,
    duration_ms: int,
    trace_context: Mapping[str, Any],
    details: tuple[str, ...],
    raw: Any,
    repaired_from_event: str | None = None,
) -> None:
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_id": event_id,
        "operation": operation,
        "schema": schema_name,
        "method": method,
        "strict": strict if method == "json_schema" else None,
        "attempt": attempt,
        "status": status,
        "duration_ms": duration_ms,
        "trace": _safe_value(dict(trace_context)),
        "validation_errors": list(details),
        "response": _response_observation(raw, options.raw_max_characters),
        "repaired_from_event": repaired_from_event,
    }
    path = options.diagnostics_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with _WRITE_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        logger.error("failed to persist LLM diagnostic event %s: %s", event_id, exc)
    if status in {"transport_error", "validation_error"}:
        logger.error(
            "LLM structured output %s: operation=%s schema=%s event=%s path=%s errors=%s",
            status,
            operation,
            schema_name,
            event_id,
            path,
            details[:3],
        )


def _response_observation(raw: Any, limit: int) -> dict[str, Any] | None:
    if raw is None:
        return None
    content, truncated = _raw_content(raw, limit=limit)
    return {
        "message_id": getattr(raw, "id", None),
        "content": _redact(content),
        "content_truncated": truncated,
        "response_metadata": _safe_value(getattr(raw, "response_metadata", None)),
        "usage_metadata": _safe_value(getattr(raw, "usage_metadata", None)),
    }


def _raw_content(raw: Any, *, limit: int) -> tuple[str, bool]:
    content = getattr(raw, "content", raw)
    if content is None:
        return "", False
    if isinstance(content, str):
        text = content
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(content)
    return text[:limit], len(text) > limit


def _exception_details(exc: Exception) -> tuple[str, ...]:
    validation = _find_validation_error(exc)
    if validation is not None:
        result: list[str] = []
        for error in validation.errors(include_url=False, include_context=False):
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            message = _redact(str(error.get("msg", "validation failed")))
            error_type = str(error.get("type", "value_error"))
            input_value = error.get("input")
            preview = ""
            if input_value is not None:
                preview_text = _redact(_compact(input_value))[:300]
                preview = f"; input={preview_text}"
            result.append(f"{location}: {message} [{error_type}]{preview}")
        if result:
            return tuple(result)
    message = _redact(str(exc)).strip()
    return (message[:2_000] or type(exc).__name__,)


def _find_validation_error(exc: BaseException | None) -> ValidationError | None:
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return _redact(value) if isinstance(value, str) else value
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _safe_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item) for item in value]
    return _redact(str(value))


def _compact(value: Any) -> str:
    try:
        return json.dumps(_safe_value(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return _redact(str(value))


def _redact(value: str) -> str:
    return _SECRET_VALUE.sub("[REDACTED]", value)


def _event_id() -> str:
    return f"llm_{uuid4().hex[:20]}"


def _elapsed_ms(started: float) -> int:
    return round((monotonic() - started) * 1_000)
