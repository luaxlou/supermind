"""Deterministic credential redaction for all searchable metadata."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import fields, replace
from typing import Any, TypeVar
from urllib.parse import quote, unquote_plus

import yaml
from yaml.tokens import AnchorToken, ScalarToken, TagToken, ValueToken

from supermind_memory.types import (
    Capability,
    Event,
    Evidence,
    Relationship,
    RequirementEvent,
    RequirementObservation,
    RequirementProfile,
    ReuseResult,
)


REDACTION = "[REDACTED]"

_SENSITIVE_KEY = (
    r"(?:api[_-]?key|(?:access|refresh|id|auth)[_-]?token|"
    r"(?:aws[_-]?)?(?:secret[_-]?)?access[_-]?key(?:[_-]?id)?|"
    r"client[_-]?secret|private[_-]?key|password|passwd|pwd|secret|session|token|"
    r"(?:proxy[_-]?)?authorization|x[_-]api[_-]key)"
)
_SENSITIVE_KEY_PATTERN = re.compile(_SENSITIVE_KEY, re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?<![\w])(?P<prefix>[\"']?" + _SENSITIVE_KEY + r"[\"']?\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:''|\\.|[^'\\])*'|[^\s,;&}\"']+)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"(?<!\w)([\"']?(?:proxy[-_]?)?authorization[\"']?\s*[:=]\s*)"
    r"(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_YAML_KEY = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?(?P<key>[\"']?" + _SENSITIVE_KEY
    + r"[\"']?)[ \t]*:[ \t]*)",
    re.IGNORECASE | re.MULTILINE,
)
# YAML permits comments and physical line breaks between '?' and its key.
_YAML_KEY_SEPARATION = r"(?:[ \t]|(?:#[^\r\n]*)?(?:\r\n|\r|\n))"
_YAML_FLOW_KEY = re.compile(
    r"[\[{,]\s*(?:\?" + _YAML_KEY_SEPARATION + r"+)?(?P<key>[\"']?" + _SENSITIVE_KEY
    + r"[\"']?)(?:[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n))*[ \t]*:[ \t]*",
    re.IGNORECASE,
)
_YAML_EXPLICIT_KEY = re.compile(
    r"^(?P<prefix>[ \t]*(?:-[ \t]+)?(?P<key>\?" + _YAML_KEY_SEPARATION + r"+[\"']?" + _SENSITIVE_KEY
    + r"[\"']?)[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n)"
    r"(?:[ \t]*(?:#[^\r\n]*)?(?:\r\n|\r|\n))*[ \t]*:[ \t]*)",
    re.IGNORECASE,
)
_BARE_AUTH = re.compile(r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{12,})", re.IGNORECASE)
_URI = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_LOCAL_PATH = re.compile(
    r'''"(?:file://(?:localhost)?/|/)[^"\r\n]*"|'''
    r"'(?:file://(?:localhost)?/|/)[^'\r\n]*'|"
    r"(?<![\w:/])(?:file://(?:localhost)?/|/)[^\s<>\"']+",
    re.IGNORECASE,
)
_FORMATS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----",
        re.DOTALL,
    ),
)
_TOKEN_LIKE = re.compile(r"(?<![\w])([A-Za-z0-9_+/=-]{28,})(?![\w])")


def redact_text(value: str) -> str:
    """Sanitize structured credentials before applying generic token detection."""
    # Consume structured scalar values before token/assignment heuristics.
    # Header folding permits tabs that YAML forbids. Remove whole credentials
    # first so a YAML scalar cannot consume only their scheme name.
    value = _AUTHORIZATION.sub(lambda match: match.group(1) + REDACTION, value)
    redacted = _redact_yaml_scalars(value)
    for pattern in _FORMATS:
        redacted = pattern.sub(REDACTION, redacted)
    redacted = _URI.sub(lambda match: _redact_uri_parts(match.group()), redacted)
    return _redact_plain(redacted)


def _redact_plain(value: str) -> str:
    redacted = _AUTHORIZATION.sub(lambda match: match.group(1) + REDACTION, value)
    redacted = _BARE_AUTH.sub(_redact_auth_token, redacted)
    redacted = _ASSIGNMENT.sub(_redact_assignment, redacted)
    return _redact_entropy(redacted)


def _redact_yaml_scalars(value: str) -> str:
    parts = []
    consumed = 0
    for header in _YAML_FLOW_KEY.finditer(value):
        if header.start() < consumed:
            continue
        offset = header.start("key")
        # An explicit scanner key also permits a real flow key whose ':' is
        # on a following line. Keep the original header in the output.
        scalar = _yaml_value_token("{? " + value[offset:])
        if scalar is None:
            continue
        end = offset + scalar.end_mark.index - 3
        quoted = scalar.style if scalar.style in {"'", '"'} else '"'
        parts.append(value[consumed:header.end()] + quoted + REDACTION + quoted)
        consumed = end
    parts.append(value[consumed:])
    value = "".join(parts)
    lines = value.splitlines(keepends=True)
    output = []
    index = 0
    while index < len(lines):
        header = _YAML_KEY.match(lines[index])
        if header is None and re.match(r"^[ \t]*(?:-[ \t]+)?\?(?:\s|$)", lines[index]):
            header = _YAML_EXPLICIT_KEY.match("".join(lines[index:]))
        if header is None:
            output.append(lines[index])
            index += 1
            continue
        # Explicit keys own their separate ':' line, including intervening
        # comments. Indentation bounds apply after that complete header.
        end = index + len(header.group().splitlines())
        while end < len(lines):
            line = lines[end]
            indent = len(line) - len(line.lstrip(" \t"))
            if line.strip() and not line.lstrip().startswith("#") and indent <= header.start("key"):
                break
            end += 1
        fragment = "".join(lines[index:end])
        # Scan tokens only: tags/anchors never instantiate objects. Bounding the
        # fragment first prevents incomplete properties consuming a sibling.
        offset = header.start("key")
        scalar = _yaml_value_token(
            fragment[offset:], quoted_continuation="".join(lines[index:])[offset:],
        )
        if scalar is None:
            output.append(lines[index])
            index += 1
            continue
        scalar_end = offset + scalar.end_mark.index
        if scalar_end > len(fragment):
            # Quoted YAML scalars may contain dedented physical lines. Once a
            # quote started inside the value boundary, its closing quote owns
            # the span, not the indentation of its body.
            tail = "".join(lines[index:])
            newline = re.search(r"\r\n|\r|\n", tail[scalar_end:])
            fragment = tail[:scalar_end + newline.end()] if newline else tail
            end = index + len(fragment.splitlines(keepends=True))
        quoted = scalar.style if scalar.style in {"'", '"'} else ""
        replacement = quoted + REDACTION + quoted
        # Block token spans include their terminating newline; retain it to
        # keep the following sibling a separate mapping entry.
        ending = ""
        if scalar.style in {"|", ">"}:
            newline = re.search(r"(?:\r\n|\r|\n)$", fragment[:scalar_end])
            ending = newline.group() if newline else ""
        output.append(header.group("prefix") + replacement + ending + fragment[scalar_end:])
        index = end
    return "".join(output)


def _yaml_value_token(fragment: str, *, quoted_continuation: str | None = None) -> ScalarToken | None:
    try:
        tokens = iter(yaml.scan(fragment, Loader=yaml.SafeLoader))
        for token in tokens:
            if isinstance(token, ValueToken):
                token = next(tokens)
                while isinstance(token, (AnchorToken, TagToken)):
                    token = next(tokens)
                return token if isinstance(token, ScalarToken) else None
    except yaml.YAMLError as error:
        if quoted_continuation is not None and getattr(error, "context", None) == "while scanning a quoted scalar":
            return _yaml_value_token(quoted_continuation)
    except StopIteration:
        pass
    return None


def _redact_entropy(value: str) -> str:
    def replace_tokens(text: str) -> str:
        return _TOKEN_LIKE.sub(
            lambda match: REDACTION if _looks_secret(match.group(1)) else match.group(1),
            text,
        )

    # Protect the complete path, including dotted ancestors, before token matching
    # splits it into misleading high-entropy substrings. Structured secrets have
    # already been removed, and text outside each path keeps the normal policy.
    parts = []
    end = 0
    for path in _LOCAL_PATH.finditer(value):
        parts.extend((replace_tokens(value[end:path.start()]), path.group()))
        end = path.end()
    parts.append(replace_tokens(value[end:]))
    return "".join(parts)


def _redact_auth_token(match: re.Match[str]) -> str:
    token = match.group(2)
    if re.search(r"[0-9_~+/=-]", token) or sum(char.isupper() for char in token) >= 2:
        return match.group(1) + " " + REDACTION
    return match.group()


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("value")
    marker = f"{value[0]}{REDACTION}{value[0]}" if value[0] in "\"'" else REDACTION
    return match.group("prefix") + marker


def redact_uri(value: str) -> str:
    """Use the same policy for standalone references and URIs embedded in text."""
    return redact_text(value)


def sanitize_json(value: object) -> Any:
    """Return a JSON-only value with every text value crossing redaction.

    Event payloads are untrusted structured input.  Keeping this conversion next
    to the text redactor makes the privacy boundary explicit and reusable while
    leaving event-shape limits to the authority event validator.
    """
    return _sanitize_json(value, active=set())


def _sanitize_json(value: object, *, active: set[int]) -> Any:
    if value is None or type(value) in {bool, int, float}:
        return value
    if type(value) is str:
        return redact_text(value)
    if isinstance(value, dict):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON payload cannot contain cycles")
        active.add(identity)
        try:
            clean: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                clean[key] = (
                    REDACTION
                    if _SENSITIVE_KEY_PATTERN.fullmatch(key)
                    else _sanitize_json(item, active=active)
                )
            return clean
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("JSON payload cannot contain cycles")
        active.add(identity)
        try:
            return [_sanitize_json(item, active=active) for item in value]
        finally:
            active.remove(identity)
    raise ValueError(f"unsupported JSON payload value: {type(value).__name__}")


def _redact_uri_parts(value: str) -> str:
    # Split delimiters without reserializing safe paths, percent escapes or hashes.
    # This also handles malformed references conservatively without a parse fallback.
    value = re.sub(r"(://)[^/?#]*@", lambda match: match.group(1) + REDACTION + "@", value)
    base, fragment_mark, fragment = value.partition("#")
    path, query_mark, query = base.partition("?")
    if query_mark:
        base = path + query_mark + _redact_parameters(query)
    if fragment_mark:
        route, query_mark, query = fragment.partition("?")
        fragment = (
            route + query_mark + _redact_parameters(query)
            if query_mark else _redact_parameters(fragment)
        )
    return base + fragment_mark + fragment


def _redact_parameters(value: str) -> str:
    parts = re.split(r"([&;])", value)
    for index in range(0, len(parts), 2):
        key, separator, item = parts[index].partition("=")
        if not separator:
            continue
        decoded = unquote_plus(item)
        safe = (
            REDACTION
            if _SENSITIVE_KEY_PATTERN.fullmatch(unquote_plus(key))
            else redact_text(decoded)
        )
        if safe != decoded:
            parts[index] = key + separator + quote(safe, safe="[]")
    return "".join(parts)


def redact_identifier(value: str) -> str:
    """Normalize unsafe identities using only sanitized text; safe IDs are stable."""
    safe = redact_text(value)
    if safe == value:
        return value
    return "redacted-" + hashlib.sha256(safe.encode("utf-8")).hexdigest()[:32]


_Record = TypeVar("_Record")


def _redact_record(record: _Record, *, identifiers: tuple[str, ...] = ()) -> _Record:
    values = {}
    for field in fields(record):
        value = getattr(record, field.name)
        sanitize = redact_identifier if field.name in identifiers else redact_text
        # String enums remain enum values; only text fields cross this boundary.
        if type(value) is str:
            values[field.name] = sanitize(value)
        elif isinstance(value, tuple):
            values[field.name] = tuple(
                sanitize(item) if type(item) is str else item for item in value
            )
    return replace(record, **values)


def redact_capability(capability: Capability) -> Capability:
    """Return the immutable safe representation used for hashing and storage."""
    return _redact_record(capability, identifiers=("id", "dependencies", "embedding_generation"))


def redact_evidence(evidence: Evidence) -> Evidence:
    return _redact_record(evidence, identifiers=("id", "capability_id", "source_project"))


def redact_event(event: Event) -> Event:
    return _redact_record(event, identifiers=("id", "capability_id", "source_context"))


def redact_relationship(relationship: Relationship) -> Relationship:
    return _redact_record(relationship, identifiers=("id", "source_id", "target_id", "evidence_ids"))


def redact_requirement(requirement: RequirementProfile) -> RequirementProfile:
    return _redact_record(requirement, identifiers=("id", "project_id"))


def redact_requirement_event(event: RequirementEvent) -> RequirementEvent:
    return _redact_record(event, identifiers=("id", "observation_id", "capability_id"))


def redact_requirement_observation(observation: RequirementObservation) -> RequirementObservation:
    return replace(
        _redact_record(observation, identifiers=("id", "linked_capability_id")),
        requirement=redact_requirement(observation.requirement),
    )


def redact_reuse_result(result: ReuseResult) -> ReuseResult:
    return _redact_record(result, identifiers=("capability_id", "project"))


def _looks_secret(value: str) -> bool:
    if value.startswith("/"):
        return False
    if re.fullmatch(r"generation-\d{8}T\d{6,12}Z-[0-9a-f]{32}", value):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return False
    classes = sum(
        bool(pattern.search(value))
        for pattern in (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"\d"))
    )
    if classes < 3:
        return False
    counts = Counter(value)
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )
    return entropy >= 3.5
