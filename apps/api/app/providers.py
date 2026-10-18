"""Configurable embedding and chat providers. Local hash vectors are lexical, not semantic."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from contextvars import ContextVar
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from .retrieval import tokenize

load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)


EMBED_DIMENSIONS = 384
CHAT_CONNECT_TIMEOUT_SECONDS = 10.0
CHAT_READ_TIMEOUT_DEFAULT_SECONDS = 180.0
CHAT_MAX_TOKENS_DEFAULT = 16384
CHAT_WRITE_TIMEOUT_SECONDS = 30.0
CHAT_POOL_TIMEOUT_SECONDS = 10.0
EMBEDDING_TIMEOUT_SECONDS = {"connect": 10.0, "read": 90.0, "write": 30.0, "pool": 10.0}
WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]")
_LAST_EMBEDDING_DIAGNOSTICS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "papertrail_last_embedding_diagnostics", default=None)


def take_embedding_diagnostics() -> list[dict[str, Any]]:
    """Return and clear safe per-request metadata for the current async context."""
    value = _LAST_EMBEDDING_DIAGNOSTICS.get() or []
    _LAST_EMBEDDING_DIAGNOSTICS.set(None)
    return [dict(item) for item in value]


def _embedding_request_id(response: httpx.Response) -> str | None:
    for name in ("x-dashscope-request-id", "x-request-id", "request-id"):
        value = response.headers.get(name, "").strip()
        if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
            return value
    return None


def _embedding_phase_from_trace(events: list[str], exc: Exception | None = None) -> str:
    event = events[-1].lower() if events else ""
    for needle, phase in (("connect_tcp", "connecting"), ("start_tls", "tls"),
                          ("send_request_headers", "sending_request_headers"),
                          ("send_request_body", "sending_request_body"),
                          ("receive_response_headers", "waiting_response_headers"),
                          ("receive_response_body", "reading_response_body")):
        if needle in event:
            return phase
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "connecting"
    if isinstance(exc, httpx.PoolTimeout):
        return "waiting_for_connection_pool"
    if isinstance(exc, (httpx.WriteError, httpx.WriteTimeout)):
        return "sending_request"
    if isinstance(exc, (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError)):
        return "awaiting_or_reading_response"
    return "processing_response"


def _safe_embedding_usage(payload: Any) -> dict[str, int]:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {}
    allowed = ("prompt_tokens", "input_tokens", "total_tokens")
    return {key: value for key in allowed for value in (usage.get(key),)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9}


def chat_timeout_config() -> dict[str, float]:
    """Return finite phase-specific limits; only the provider response read is configurable."""
    raw_read = os.getenv("PAPERTRAIL_LLM_READ_TIMEOUT_SECONDS", str(CHAT_READ_TIMEOUT_DEFAULT_SECONDS))
    try:
        read_seconds = float(raw_read)
    except (TypeError, ValueError) as exc:
        raise ChatProviderError("configuration_error", False) from exc
    if not math.isfinite(read_seconds) or not 1.0 <= read_seconds <= 600.0:
        raise ChatProviderError("configuration_error", False)
    return {"connect": CHAT_CONNECT_TIMEOUT_SECONDS, "read": read_seconds,
            "write": CHAT_WRITE_TIMEOUT_SECONDS, "pool": CHAT_POOL_TIMEOUT_SECONDS}


def _chat_httpx_timeout() -> httpx.Timeout:
    return httpx.Timeout(**chat_timeout_config())


def chat_max_tokens_config() -> int:
    """Bound provider output so a stalled or runaway completion cannot stream indefinitely."""
    raw = os.getenv("PAPERTRAIL_LLM_MAX_TOKENS", str(CHAT_MAX_TOKENS_DEFAULT))
    try:
        maximum = int(raw)
    except (TypeError, ValueError) as exc:
        raise ChatProviderError("configuration_error", False) from exc
    if not 256 <= maximum <= 16384:
        raise ChatProviderError("configuration_error", False)
    return maximum


class EmbeddingProviderError(RuntimeError):
    """Safe, credential-free classification for errors from the embedding endpoint."""

    def __init__(self, reason: str, retryable: bool, status_code: int | None = None,
                 *, outcome_unknown: bool = False, diagnostics: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.diagnostics = diagnostics or {}


class ChatProviderError(RuntimeError):
    """Credential-free classification for a chat transport or HTTP failure."""

    def __init__(self, reason: str, retryable: bool, status_code: int | None = None,
                 diagnostics: dict[str, Any] | None = None, *, outcome_unknown: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.status_code = status_code
        self.diagnostics = diagnostics or {}
        self.outcome_unknown = outcome_unknown


def _chat_provider_error(exc: Exception) -> ChatProviderError:
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            reason, retryable = "authentication_error", False
        elif status in (400, 404, 422):
            reason, retryable = "parameter_error", False
        else:
            reason, retryable = "provider_http_error", status >= 500 or status == 429
        return ChatProviderError(reason, retryable, status, outcome_unknown=status >= 500)
    if isinstance(exc, httpx.TimeoutException):
        unknown = not isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
        return ChatProviderError("timeout", True, outcome_unknown=unknown)
    if isinstance(exc, httpx.ConnectError):
        return ChatProviderError("connection_error", True)
    if isinstance(exc, httpx.ReadError):
        return ChatProviderError("read_error", True, outcome_unknown=True)
    if isinstance(exc, httpx.ProtocolError):
        return ChatProviderError("protocol_error", True, outcome_unknown=True)
    if isinstance(exc, httpx.TransportError):
        return ChatProviderError("transport_error", True, outcome_unknown=True)
    return ChatProviderError("request_error", False)


def chat_failure_diagnostics(exc: Exception, elapsed_ms: float, request_id: str | None = None) -> dict[str, Any]:
    base_url = os.getenv("PAPERTRAIL_LLM_BASE_URL", "https://api.openai.com/v1")
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        host = None
    chain: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    try:
        timeout = chat_timeout_config()
    except ChatProviderError:
        timeout = None
    result = {"reason": exc.reason if isinstance(exc, ChatProviderError) else type(exc).__name__,
            "phase": "generation", "endpoint_host": host,
            "elapsed_ms": round(elapsed_ms, 1), "exception_chain": chain,
            "status_code": exc.status_code if isinstance(exc, ChatProviderError) else None,
            "outcome_unknown": exc.outcome_unknown if isinstance(exc, ChatProviderError) else False,
            "effective_timeout_seconds": timeout}
    if isinstance(exc, ChatProviderError):
        result.update(exc.diagnostics)
    if request_id:
        result["request_id"] = request_id
    return result


def _host_bypasses_proxy(host: str | None) -> bool:
    if not host:
        return False
    entries = [entry.strip().lower() for name in ("NO_PROXY", "no_proxy")
               for entry in os.getenv(name, "").split(",") if entry.strip()]
    host = host.lower().rstrip(".")
    return any(entry == "*" or host == entry.lstrip(".").rstrip(".") or
               host.endswith("." + entry.lstrip(".").rstrip(".")) for entry in entries)

ANSWER_SYSTEM_PROMPT = (
    "Answer using only the supplied paper evidence. Return one JSON object with keys answer and claims. "
    "Before drafting, identify every requested sub-question and address each one in the answer. "
    "Keep the answer concise: use only the facts needed to answer the question, normally no more than 8 claims, "
    "and combine related facts instead of repeating them as separate claims. Keep answer and claims consistent; "
    "do not add explanations or evidence details that are not needed to answer the requested parts. "
    "For yes/no, ranking, or comparison questions, state an explicit conclusion and explain why; "
    "do not treat a list of individually supported facts as a complete answer. If the evidence cannot resolve a part, say so. "
    "claims is an array of objects {text, evidence_ids, quote}. Each factual claim must cite one or more supplied evidence_ids. "
    "quote must be an exact verbatim substring of the cited evidence. Do not invent ids, locations, numbers, or paper facts. "
    "Keep metrics, datasets, model groups, and baselines attached to every number. When subtracting two percentages on the same metric, "
    "state the absolute difference in percentage points (百分点), not as a percent increase. Call a relative change a relative percentage change. "
    "Do not claim that values from different metrics, datasets, or experimental settings are directly rankable without evidence establishing comparability."
)

SUPPORT_SYSTEM_PROMPT = (
    "For each claim, assess only whether its associated supplied evidence supports it. "
    "Do not assess whether the original question was fully answered; support and answer completeness are separate. "
    "Return JSON {results:[{claim_index,status,reason,evidence_ids}]}. status must be supported, partially_supported, unsupported, or conflicting. "
    "Do not infer from outside knowledge. Numeric values must match the stated metric, model, dataset, and comparison. "
    "Include only supplied evidence IDs."
)

REVISE_SYSTEM_PROMPT = (
    "Revise the answer using only supplied paper evidence. First cover every sub-question in the original question. "
    "If the payload includes completeness_check.missing_requirements, address every listed requirement explicitly in the revised answer; "
    "do not merely restate supported facts or assume a missing part is implied. "
    "For a yes/no, ranking, or comparison question, state an explicit conclusion and the evidence-based reason; "
    "do not equate supported facts with a complete answer. Remove unsupported or conflicting claims and state gaps plainly. "
    "Keep model, dataset, metric, and baseline attached to numbers. For absolute differences between percentage values on the same metric, "
    "say percentage points (百分点), not percent increase; distinguish relative percentage change. "
    "Do not directly rank values from different metrics/datasets/settings unless the evidence establishes comparability. "
    "The displayed answer is assembled from claims, so every substantive answer statement and every supplied missing requirement "
    "must appear in a corresponding claim. Keep answer and claims consistent. Return JSON {answer,claims}; each claim is "
    "{text,evidence_ids,quote}. Cite only supplied IDs and use an exact source substring."
)


def _api_key(kind: str) -> str:
    return os.getenv(f"PAPERTRAIL_{kind.upper()}_API_KEY", "")


def _endpoint_identity(base_url: str) -> str:
    """Return a non-secret fingerprint so different services use distinct indexes."""
    return hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()[:10]


def config_status() -> dict[str, Any]:
    embedding_provider = os.getenv("PAPERTRAIL_EMBEDDING_PROVIDER", "local_hash").lower()
    chat_model = os.getenv("PAPERTRAIL_LLM_MODEL", "")
    embedding_base = os.getenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    chat_base = os.getenv("PAPERTRAIL_LLM_BASE_URL", "https://api.openai.com/v1")
    embedding_model = "local-hash-v1" if embedding_provider == "local_hash" else os.getenv("PAPERTRAIL_EMBEDDING_MODEL", "")
    if embedding_provider == "openai" and embedding_model:
        embedding_model = f"openai:{embedding_model}@{_endpoint_identity(embedding_base)}"
    try:
        timeout = chat_timeout_config()
    except ChatProviderError:
        timeout = None
    try:
        max_tokens = chat_max_tokens_config()
    except ChatProviderError:
        max_tokens = None
    return {"embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            "embedding_ready": embedding_provider == "local_hash" or bool(_api_key("embedding") and os.getenv("PAPERTRAIL_EMBEDDING_MODEL")),
            "embedding_endpoint_configured": bool(os.getenv("PAPERTRAIL_EMBEDDING_BASE_URL")),
            "embedding_endpoint_host": urlsplit(embedding_base).hostname,
            "chat_model": chat_model,
            "chat_ready": bool(chat_model and _api_key("llm")),
            "chat_endpoint_configured": bool(os.getenv("PAPERTRAIL_LLM_BASE_URL")),
            "chat_endpoint_host": urlsplit(chat_base).hostname,
            "chat_timeout_seconds": timeout,
            "chat_max_tokens": max_tokens,
            "httpx_trust_env": True,
            "explicit_proxy_configured": False,
            "no_proxy_chat_host": _host_bypasses_proxy(urlsplit(chat_base).hostname),
            "no_proxy_embedding_host": _host_bypasses_proxy(urlsplit(embedding_base).hostname)}


def hash_embedding(text: str) -> list[float]:
    """Stable signed feature hashing over words and word character trigrams."""
    features: dict[str, float] = {}
    tokens = tokenize(text)
    for token in tokens:
        features[token] = features.get(token, 0.0) + 1.0
        padded = f"^{token}$"
        for i in range(max(0, len(padded) - 2)):
            gram = padded[i:i + 3]
            features["#" + gram] = features.get("#" + gram, 0.0) + 0.25
    vector = [0.0] * EMBED_DIMENSIONS
    for feature, weight in features.items():
        raw = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(raw, "little")
        index = value % EMBED_DIMENSIONS
        vector[index] += weight if (value >> 32) & 1 else -weight
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


async def embed_texts(texts: list[str]) -> tuple[list[list[float]], str]:
    _LAST_EMBEDDING_DIAGNOSTICS.set([])
    provider = os.getenv("PAPERTRAIL_EMBEDDING_PROVIDER", "local_hash").lower()
    if provider == "local_hash":
        return [hash_embedding(text) for text in texts], "local-hash-v1"
    if provider != "openai":
        raise EmbeddingProviderError("configuration_error", False)
    api_key = _api_key("embedding")
    model = os.getenv("PAPERTRAIL_EMBEDDING_MODEL", "")
    base_url = os.getenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    if not api_key or not model:
        raise EmbeddingProviderError("configuration_error", False)
    vectors = []
    # No implicit retry. The indexer persists each successful batch before sending another.
    async with httpx.AsyncClient(timeout=httpx.Timeout(**EMBEDDING_TIMEOUT_SECONDS)) as client:
        for start in range(0, len(texts), 10):
            batch = texts[start:start + 10]
            started = time.monotonic()
            payload = {"model": model, "input": batch}
            encoded_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                         allow_nan=False).encode("utf-8")
            trace_events: list[str] = []

            async def trace(name: str, info: dict[str, Any]) -> None:
                # HTTPX trace info can contain request/response objects. Keep only event names.
                trace_events.append(name[:120])

            request_meta: dict[str, Any] = {
                "input_count": len(batch),
                "input_characters": sum(len(value) for value in batch),
                "input_utf8_bytes": sum(len(value.encode("utf-8")) for value in batch),
                "max_item_characters": max((len(value) for value in batch), default=0),
                "max_item_utf8_bytes": max((len(value.encode("utf-8")) for value in batch), default=0),
                "request_body_bytes": len(encoded_payload),
            }
            try:
                response = await client.post(f"{base_url.rstrip('/')}/embeddings",
                                             headers={"Authorization": f"Bearer {api_key}"},
                                             json=payload, extensions={"trace": trace})
                request_meta["status_code"] = response.status_code
                request_id = _embedding_request_id(response)
                if request_id:
                    request_meta["provider_request_id"] = request_id
                response.raise_for_status()
                response_payload = response.json()
                request_meta.update(_safe_embedding_usage(response_payload))
                data = response_payload.get("data", [])
                data.sort(key=lambda item: item["index"])
                vectors.extend(item["embedding"] for item in data)
                request_meta.update({"elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                                     "status": "succeeded", "failed_phase": None,
                                     "transport_event": trace_events[-1] if trace_events else None})
                _LAST_EMBEDDING_DIAGNOSTICS.get().append(request_meta)
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, TypeError, AttributeError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if isinstance(exc, httpx.HTTPStatusError):
                    request_meta["status_code"] = status
                    request_id = _embedding_request_id(exc.response)
                    if request_id:
                        request_meta["provider_request_id"] = request_id
                if status in (401, 403):
                    reason, retryable, unknown = "authentication_error", False, False
                elif status in (400, 404, 422):
                    reason, retryable, unknown = "parameter_error", False, False
                elif status is not None:
                    reason, retryable, unknown = "provider_http_error", status >= 500 or status == 429, status >= 500
                elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
                    reason, retryable, unknown = "connection_error", True, False
                elif isinstance(exc, httpx.TimeoutException):
                    reason, retryable, unknown = "timeout", True, True
                elif isinstance(exc, httpx.TransportError):
                    reason, retryable, unknown = "read_error" if isinstance(exc, httpx.ReadError) else "transport_error", True, True
                else:
                    reason, retryable, unknown = "invalid_response", False, False
                chain = []
                current: BaseException | None = exc
                while current is not None and len(chain) < 6:
                    chain.append(type(current).__name__)
                    current = current.__cause__ or current.__context__
                diagnostics = {"phase": "embedding", "endpoint_host": urlsplit(base_url).hostname,
                               "batch_index": start // 10 + 1, "batch_size": len(batch),
                               "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                               "exception_chain": chain,
                               "effective_timeout_seconds": EMBEDDING_TIMEOUT_SECONDS.copy()}
                request_meta.update({"batch_index": start // 10 + 1, "elapsed_ms": diagnostics["elapsed_ms"],
                                     "status": "outcome_unknown" if unknown else "failed",
                                     "failed_phase": _embedding_phase_from_trace(trace_events, exc),
                                     "transport_event": trace_events[-1] if trace_events else None})
                if request_meta.get("provider_request_id"):
                    diagnostics["provider_request_id"] = request_meta["provider_request_id"]
                diagnostics.update({key: value for key, value in request_meta.items()
                                    if key not in ("status", "transport_event") and value is not None})
                diagnostics["failed_phase"] = request_meta["failed_phase"]
                diagnostics["transport_event"] = request_meta["transport_event"]
                _LAST_EMBEDDING_DIAGNOSTICS.get().append(request_meta)
                if isinstance(exc, httpx.HTTPStatusError):
                    diagnostics.update(_safe_embedding_error_fields(exc.response))
                raise EmbeddingProviderError(reason, retryable, status, outcome_unknown=unknown,
                                             diagnostics=diagnostics) from exc
    if (len(vectors) != len(texts) or
            any(not isinstance(vector, list) or not vector for vector in vectors) or
            (vectors and any(len(vector) != len(vectors[0]) or
                             any(not isinstance(value, (int, float)) or not math.isfinite(value)
                                 for value in vector) for vector in vectors))):
        raise EmbeddingProviderError("invalid_response", False)
    return vectors, f"openai:{model}@{_endpoint_identity(base_url)}"


def _safe_embedding_error_fields(response: httpx.Response) -> dict[str, str]:
    """Keep only structured provider error identifiers; never retain response text or input excerpts."""
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return {}
    safe: dict[str, str] = {}
    for source, target in (("code", "provider_error_code"), ("type", "provider_error_type"),
                           ("param", "provider_error_param")):
        value = error.get(source)
        if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            safe[target] = value
    message = error.get("message")
    if isinstance(message, str):
        normalized = message.lower()
        if any(term in normalized for term in ("input is too long", "input exceeds", "token limit", "maximum tokens")):
            safe["provider_error_category"] = "input_length"
        elif "dimension" in normalized:
            safe["provider_error_category"] = "dimensions"
        elif "model" in normalized and any(term in normalized for term in ("invalid", "unsupported", "not found")):
            safe["provider_error_category"] = "model"
        elif "input" in normalized and any(term in normalized for term in ("invalid", "empty", "required")):
            safe["provider_error_category"] = "input"
        else:
            safe["provider_error_category"] = "provider_rejected_request"
    return safe


async def generate_answer(query: str, evidence: list[dict[str, Any]],
                          *, request_id: str | None = None) -> tuple[dict[str, Any], str]:
    context = [{"evidence_id": item["evidence_id"], "paper_id": item["paper_id"], "version": item["source_version"],
                "pdf_page_index": item.get("pdf_page_index", (item.get("spans") or [{}])[0].get("pdf_page_index")),
                "section_path": item["section_path"], "text": item["text"]} for item in evidence]
    parsed, model = await _chat_json(ANSWER_SYSTEM_PROMPT,
                                    {"question": query, "evidence": context},
                                    request_id=request_id)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answer"), str) or not isinstance(parsed.get("claims"), list):
        raise ChatProviderError("invalid_model_response", False,
                                diagnostics={"response_shape": "answer_and_claims_required"})
    return parsed, model


async def verify_claim_support(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Judge claims only against their cited excerpts; this is automated, not human review."""
    return await _chat_json(
        SUPPORT_SYSTEM_PROMPT,
        {"claims": claims, "evidence": evidence},
    )


async def make_repair_query(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> tuple[str, str]:
    """Create one targeted query for this run from claim gaps and cited excerpts."""
    parsed, model = await _chat_json(
        "Create one concise retrieval query that targets the most important missing information in the supplied claims. "
        "Return JSON {query:string}. Use paper terminology where possible. "
        "Do not answer the claims or add outside knowledge.",
        {"claims": claims, "evidence": evidence},
    )
    query = parsed.get("query")
    if not isinstance(query, str):
        raise RuntimeError("Chat provider returned an invalid repair query")
    return query, model


async def revise_answer(query: str, answer: str, claims: list[dict[str, Any]], evidence: list[dict[str, Any]],
                        *, completeness_check: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    payload = {"question": query, "previous_answer": answer, "claims": claims, "evidence": evidence}
    if completeness_check is not None:
        payload["completeness_check"] = completeness_check
    return await _chat_json(
        REVISE_SYSTEM_PROMPT,
        payload,
    )


def _safe_provider_request_id(response: httpx.Response) -> str | None:
    for name in ("x-request-id", "request-id"):
        value = response.headers.get(name)
        if value and len(value) <= 160 and all(ch.isalnum() or ch in "-_." for ch in value):
            return value
    return None


def _usage_diagnostics(body: Any) -> dict[str, int]:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    safe = {}
    for source, target in (("prompt_tokens", "prompt_tokens"),
                           ("completion_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens")):
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            safe[target] = value
    return safe


def _parse_chat_json_response(response: httpx.Response) -> tuple[dict[str, Any], dict[str, Any]]:
    request_id = _safe_provider_request_id(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise ChatProviderError("invalid_provider_json", False, response.status_code,
                                {"provider_request_id": request_id,
                                 "response_json_error": type(exc).__name__}) from exc
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ChatProviderError("invalid_provider_response", False, response.status_code,
                                {"provider_request_id": request_id, **_usage_diagnostics(body),
                                 "response_shape": "choices_missing"})
    choice = choices[0]
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    finish_reason = choice.get("finish_reason")
    metadata: dict[str, Any] = {"provider_request_id": request_id,
                                "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
                                **_usage_diagnostics(body)}
    if not isinstance(content, str):
        raise ChatProviderError("invalid_provider_response", False, response.status_code,
                                {**metadata, "response_shape": "message_content_not_text"})
    metadata["response_content_chars"] = len(content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        truncated = finish_reason in {"length", "max_tokens"}
        raise ChatProviderError("output_truncated" if truncated else "invalid_json_response",
                                False, response.status_code,
                                {**metadata, "json_error": exc.msg[:100],
                                 "json_error_line": exc.lineno, "json_error_column": exc.colno,
                                 "json_error_position": exc.pos,
                                 "content_starts_object": content.lstrip().startswith("{"),
                                 "content_ends_object": content.rstrip().endswith("}"),
                                 "content_wrapped_in_fence": content.lstrip().startswith("```"),
                                 "configured_max_tokens": chat_max_tokens_config()}) from exc
    except (TypeError, ValueError) as exc:
        raise ChatProviderError("invalid_json_response", False, response.status_code,
                                {**metadata, "json_error": type(exc).__name__}) from exc
    if not isinstance(parsed, dict):
        raise ChatProviderError("invalid_model_response", False, response.status_code,
                                {**metadata, "response_shape": "json_object_required"})
    return parsed, metadata


async def _chat_json(system: str, payload: dict[str, Any], *,
                     request_id: str | None = None) -> tuple[dict[str, Any], str]:
    api_key = _api_key("llm")
    model = os.getenv("PAPERTRAIL_LLM_MODEL", "")
    base_url = os.getenv("PAPERTRAIL_LLM_BASE_URL", "https://api.openai.com/v1")
    if not api_key or not model:
        raise RuntimeError("Configure PAPERTRAIL_LLM_API_KEY and PAPERTRAIL_LLM_MODEL")
    http_started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=_chat_httpx_timeout()) as client:
            response = await client.post(f"{base_url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {api_key}"},
                                         json={"model": model, "temperature": 0, "max_tokens": chat_max_tokens_config(),
                                               "response_format": {"type": "json_object"},
                                               "messages": [{"role": "system", "content": system},
                                                            {"role": "user", "content": json_dumps(payload)}]})
            response.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise _chat_provider_error(exc) from exc
    try:
        parsed, _response_metadata = _parse_chat_json_response(response)
    except ChatProviderError as exc:
        diagnostics = dict(exc.diagnostics)
        diagnostics["response_elapsed_ms"] = round((time.perf_counter() - http_started) * 1000, 1)
        if request_id:
            diagnostics["request_id"] = request_id
        raise ChatProviderError(exc.reason, exc.retryable, exc.status_code, diagnostics,
                                outcome_unknown=exc.outcome_unknown) from exc
    return parsed, model


def json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)
