"""JSON extraction, syntax repair, and Schema validation for internal role replies; pure parsing with no I/O."""

import json
from importlib.metadata import version
from typing import Any

import jsonschema
from json_repair import repair_json

from .models import OutputProtocolError
from .record_types import JsonRecovery


def validate_json(data: Any, schema: dict) -> dict:
    """Syntax recovery never substitutes for Schema validation or coerces missing/wrong field types."""
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        raise OutputProtocolError(str(exc)) from exc
    return data


def parse_and_validate_json(text: str, schema: dict) -> dict:
    """Programmatic verifiers use strict JSON parsing with no syntax repair."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OutputProtocolError(str(exc)) from exc
    return validate_json(data, schema)


def parse_model_json(text: str) -> tuple[Any, JsonRecovery | None]:
    """Prefer exact json.loads; otherwise recover only from a unique candidate span and return an audit record."""
    try:
        return json.loads(text, object_pairs_hook=_unique_keys), None
    except json.JSONDecodeError:
        pass

    candidate, span = _single_candidate(text)
    try:
        data = json.loads(candidate, object_pairs_hook=_unique_keys)
        method, parser = "extracted", "stdlib.json"
    except json.JSONDecodeError:
        try:
            # Delegate syntax-only repair to json_repair without passing Schema so values are never fabricated.
            data = repair_json(
                candidate, return_objects=True, skip_json_loads=True, strict=True
            )
        except ValueError as exc:
            raise OutputProtocolError(
                f"JSON syntax could not be unambiguously repaired: {exc}"
            ) from exc
        method, parser = "repaired", f"json-repair/{version('json-repair')}"
    return data, {
        "method": method,
        "source_range": span,
        "parser": parser,
        "value": data,
    }


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise OutputProtocolError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _scan_containers(
    text: str, quotes: str
) -> tuple[list[tuple[int, int]], int | None]:
    candidates: list[tuple[int, int]] = []
    start: int | None = None
    stack: list[str] = []
    quote, escaped = None, False
    for index, char in enumerate(text):
        if start is None:
            if char in "{[":
                start = index
                stack.append("}" if char == "{" else "]")
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in quotes:
            # English apostrophes inside bracketed prose (e.g., [user's note]) do not open single-quoted strings.
            if (
                char == "'"
                and index > 0
                and index + 1 < len(text)
                and text[index - 1].isalnum()
                and text[index + 1].isalnum()
            ):
                continue
            quote = char
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if char != stack.pop():
                raise OutputProtocolError("Mismatched JSON container brackets")
            if not stack:
                candidates.append((start, index + 1))
                start = None
    # Unescaped quotes inside a closed outer container may be repaired; truncated mid-string output is rejected.
    if quote and not text.rstrip().endswith(stack[0]):
        raise OutputProtocolError("Unterminated JSON string; refusing to fabricate text")
    if start is not None:
        candidates.append((start, len(text)))
    return candidates, start


def _valid_spans(text: str, candidates: list[tuple[int, int]]) -> list[tuple[int, int]]:
    valid: list[tuple[int, int]] = []
    for left, right in candidates:
        try:
            json.loads(text[left:right])
        except json.JSONDecodeError:
            continue
        valid.append((left, right))
    return valid


def _single_candidate(text: str) -> tuple[str, list[int]]:
    """Locate a single valid JSON container first; allow syntax repair only when exactly one container exists."""
    # Standard JSON uses double quotes; scan with double quotes first so apostrophes in prose prefixes cannot swallow JSON.
    try:
        strict_candidates, strict_unclosed = _scan_containers(text, '"')
    except OutputProtocolError:
        strict_candidates, strict_unclosed = [], None
    else:
        valid_strict = _valid_spans(text, strict_candidates)
        if len(valid_strict) > 1:
            raise OutputProtocolError(
                "Multiple valid JSON objects or arrays found; refusing to pick one"
            )
        if len(valid_strict) == 1:
            left, right = valid_strict[0]
            if strict_unclosed is not None and left < strict_unclosed:
                raise OutputProtocolError(
                    "Trailing JSON container is unclosed; refusing to fall back to an earlier candidate"
                )
            trailing = text[right:]
            if any(ch in trailing for ch in "{["):
                try:
                    _, trailing_unclosed = _scan_containers(trailing, "\"'")
                except OutputProtocolError as exc:
                    raise OutputProtocolError(
                        "Trailing JSON container is unclosed; refusing to fall back to an earlier candidate"
                    ) from exc
                if trailing_unclosed is not None:
                    raise OutputProtocolError(
                        "Trailing JSON container is unclosed; refusing to fall back to an earlier candidate"
                    )
            return text[left:right], [left, right]

    candidates, start = _scan_containers(text, "\"'")
    # Balanced brackets in prose (e.g., D2={forms:42} or [note]) are not valid JSON and should not block a unique valid JSON object.
    valid_candidates = _valid_spans(text, candidates)
    if len(valid_candidates) == 1:
        # An unclosed container after a valid object may be a truncated revision; do not silently return the earlier draft.
        if start is not None and valid_candidates[0][0] < start:
            raise OutputProtocolError(
                "Trailing JSON container is unclosed; refusing to fall back to an earlier candidate"
            )
        candidates = valid_candidates
    elif len(valid_candidates) > 1:
        raise OutputProtocolError(
            "Multiple valid JSON objects or arrays found; refusing to pick one"
        )
    elif len(candidates) != 1:
        raise OutputProtocolError(
            "No unique JSON object or array found; cannot unambiguously recover"
        )
    start, end = candidates[0]
    return text[start:end], [start, end]
