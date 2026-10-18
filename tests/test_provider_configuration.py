"""Provider configuration and protocol checks; these are not live service tests."""
import asyncio
import hashlib
import json
import uuid

import httpx
import pytest

from app import providers


def test_embedding_and_chat_credentials_are_independent(monkeypatch):
    for name in ("PAPERTRAIL_EMBEDDING_API_KEY", "PAPERTRAIL_LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_MODEL", "chat-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_API_KEY", "test-chat-key")
    status = providers.config_status()
    assert status["embedding_ready"] is False
    assert status["chat_ready"] is True
    assert "test-chat-key" not in str(status)


def test_embedding_identity_includes_endpoint_and_provider_model(monkeypatch):
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://one.example/v1")
    first = providers.config_status()["embedding_model"]
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://two.example/v1")
    second = providers.config_status()["embedding_model"]
    assert first.startswith("openai:embed-v1@")
    assert second.startswith("openai:embed-v1@")
    assert first != second


def test_chat_transport_errors_distinguish_rejected_requests_from_unknown_outcomes():
    connect = providers._chat_provider_error(httpx.ConnectError("connect refused"))
    connect_timeout = providers._chat_provider_error(httpx.ConnectTimeout("connect timeout"))
    read_timeout = providers._chat_provider_error(httpx.ReadTimeout("read timeout"))
    protocol = providers._chat_provider_error(httpx.RemoteProtocolError("connection dropped"))
    assert connect.reason == "connection_error" and connect.retryable and not connect.outcome_unknown
    assert connect_timeout.retryable and not connect_timeout.outcome_unknown
    assert read_timeout.retryable and read_timeout.outcome_unknown
    assert protocol.retryable and protocol.outcome_unknown

    request = httpx.Request("POST", "https://chat.example/v1/chat/completions")
    rejected_429 = providers._chat_provider_error(
        httpx.HTTPStatusError("rate limited", request=request,
                              response=httpx.Response(429, request=request)))
    server_5xx = providers._chat_provider_error(
        httpx.HTTPStatusError("server error", request=request,
                              response=httpx.Response(503, request=request)))
    assert rejected_429.retryable and not rejected_429.outcome_unknown
    assert server_5xx.retryable and server_5xx.outcome_unknown


def test_embedding_provider_sends_configured_model_and_orders_vectors(monkeypatch):
    requests = []

    class Response:
        def __init__(self, input_count):
            self.input_count = input_count
            self.status_code = 200
            self.headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"index": index, "embedding": [float(index), 1.0]}
                             for index in reversed(range(self.input_count))]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json, **kwargs):
            assert "trace" in kwargs["extensions"]
            requests.append((url, json["model"], headers["Authorization"], json["input"]))
            return Response(len(json["input"]))

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "only-test-secret")
    inputs = [f"text-{i}" for i in range(21)]
    async def run_and_take_diagnostics():
        result = await providers.embed_texts(inputs)
        return *result, providers.take_embedding_diagnostics()
    vectors, identity, diagnostics = asyncio.run(run_and_take_diagnostics())
    assert len(vectors) == len(inputs)
    assert vectors[0] == [0.0, 1.0]
    assert vectors[10] == [0.0, 1.0]
    assert vectors[20] == [0.0, 1.0]
    assert identity == providers.config_status()["embedding_model"]
    assert [len(item[3]) for item in requests] == [10, 10, 1]
    assert all(item[:3] == ("https://embed.example/v1/embeddings", "embed-v1", "Bearer only-test-secret")
               for item in requests)
    assert [item["input_count"] for item in diagnostics] == [10, 10, 1]
    assert all(item["request_body_bytes"] > item["input_utf8_bytes"] for item in diagnostics)
    assert all(item["status"] == "succeeded" and item["failed_phase"] is None for item in diagnostics)
    assert "only-test-secret" not in str(diagnostics)
    assert all(text not in str(diagnostics) for text in inputs)


@pytest.mark.parametrize(("error", "reason", "retryable"), [
    (httpx.ConnectError("refused"), "connection_error", True),
    (httpx.ReadTimeout("timed out"), "timeout", True),
    (httpx.ReadError("reset"), "read_error", True),
    (httpx.ConnectTimeout("timed out"), "connection_error", True),
])
def test_embedding_connection_failures_are_classified_without_response_details(monkeypatch, error, reason, retryable):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): raise error

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "test-key")
    with pytest.raises(providers.EmbeddingProviderError) as caught:
        asyncio.run(providers.embed_texts(["test input"]))
    assert (caught.value.reason, caught.value.retryable, caught.value.status_code) == (reason, retryable, None)
    assert caught.value.outcome_unknown is isinstance(error, (httpx.ReadTimeout, httpx.ReadError))
    assert caught.value.diagnostics["endpoint_host"] == "embed.example"
    assert caught.value.diagnostics["batch_size"] == 1
    assert caught.value.diagnostics["effective_timeout_seconds"] == providers.EMBEDDING_TIMEOUT_SECONDS
    assert caught.value.diagnostics["failed_phase"] == "awaiting_or_reading_response" if isinstance(
        error, (httpx.ReadTimeout, httpx.ReadError)) else caught.value.diagnostics["failed_phase"] in {
            "connecting", "waiting_for_connection_pool"}
    assert caught.value.diagnostics["input_count"] == 1
    assert caught.value.diagnostics["request_body_bytes"] > len("test input")
    assert "test-key" not in str(caught.value)


@pytest.mark.parametrize(("status", "reason", "retryable"), [
    (401, "authentication_error", False),
    (403, "authentication_error", False),
    (400, "parameter_error", False),
    (404, "parameter_error", False),
    (422, "parameter_error", False),
    (429, "provider_http_error", True),
    (503, "provider_http_error", True),
])
def test_embedding_http_statuses_have_safe_retry_classification(monkeypatch, status, reason, retryable):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs): return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "test-key")
    with pytest.raises(providers.EmbeddingProviderError) as caught:
        asyncio.run(providers.embed_texts(["test input"]))
    assert (caught.value.reason, caught.value.retryable, caught.value.status_code) == (reason, retryable, status)
    assert caught.value.outcome_unknown is (status >= 500)


def test_embedding_http_error_keeps_only_redacted_structured_provider_details(monkeypatch):
    rejected_text = "private paper excerpt must never be stored"

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(400, request=httpx.Request("POST", url), json={"error": {
                "code": "InvalidParameter", "type": "invalid_request_error", "param": "input.10",
                "message": f"input exceeds token limit; rejected input: {rejected_text}"}})

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "test-key")
    with pytest.raises(providers.EmbeddingProviderError) as caught:
        asyncio.run(providers.embed_texts(["test input"]))

    diagnostics = caught.value.diagnostics
    assert diagnostics["provider_error_code"] == "InvalidParameter"
    assert diagnostics["provider_error_type"] == "invalid_request_error"
    assert diagnostics["provider_error_param"] == "input.10"
    assert diagnostics["provider_error_category"] == "input_length"
    assert rejected_text not in str(diagnostics)
    assert "input exceeds token limit" not in str(diagnostics)
    assert "test-key" not in str(diagnostics)


def test_embedding_success_response_is_preserved(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"data": [{"index": 0, "embedding": [0.2, 0.8]}]})

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "test-key")
    async def run_and_take_diagnostics():
        result = await providers.embed_texts(["test input"])
        return *result, providers.take_embedding_diagnostics()
    vectors, identity, diagnostics = asyncio.run(run_and_take_diagnostics())
    assert vectors == [[0.2, 0.8]]
    assert identity == providers.config_status()["embedding_model"]
    assert diagnostics[0]["status"] == "succeeded"


def test_embedding_success_diagnostics_keep_only_safe_headers_and_usage(monkeypatch):
    secret_text = "private source text must never appear"
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  headers={"x-dashscope-request-id": "req_abc-123"},
                                  json={"data": [{"index": 0, "embedding": [0.1, 0.2]}],
                                        "usage": {"prompt_tokens": 17, "total_tokens": 17,
                                                  "private": secret_text}})
    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_MODEL", "embed-v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://embed.example/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_API_KEY", "test-key")
    async def run_and_take_diagnostics():
        result = await providers.embed_texts([secret_text])
        return *result, providers.take_embedding_diagnostics()
    vectors, _, diagnostics = asyncio.run(run_and_take_diagnostics())
    assert vectors == [[0.1, 0.2]]
    assert diagnostics[0]["provider_request_id"] == "req_abc-123"
    assert diagnostics[0]["prompt_tokens"] == 17
    assert diagnostics[0]["total_tokens"] == 17
    assert "private" not in diagnostics[0]
    assert secret_text not in str(diagnostics)
    assert "test-key" not in str(diagnostics)


@pytest.mark.parametrize(("status", "reason", "retryable"), [
    (401, "authentication_error", False), (403, "authentication_error", False),
    (400, "parameter_error", False), (404, "parameter_error", False),
    (422, "parameter_error", False), (429, "provider_http_error", True),
    (503, "provider_http_error", True),
])
def test_chat_http_statuses_have_safe_retry_classification(monkeypatch, status, reason, retryable):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs): return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_LLM_MODEL", "chat-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_API_KEY", "test-chat-secret")
    with pytest.raises(providers.ChatProviderError) as caught:
        asyncio.run(providers.generate_answer("question", []))
    assert (caught.value.reason, caught.value.retryable, caught.value.status_code) == (reason, retryable, status)
    assert "test-chat-secret" not in str(caught.value)


def test_chat_read_error_has_safe_phase_host_elapsed_diagnostics(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): raise httpx.ReadError("secret-like body should not be retained")

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_LLM_MODEL", "chat-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_API_KEY", "test-chat-secret")
    with pytest.raises(providers.ChatProviderError) as caught:
        asyncio.run(providers.generate_answer("question", []))
    diagnostic = providers.chat_failure_diagnostics(caught.value, 12.34, "req-test-123")
    assert caught.value.reason == "read_error"
    assert caught.value.retryable is True
    assert diagnostic == {"reason": "read_error", "phase": "generation", "endpoint_host": "chat.example",
                          "elapsed_ms": 12.3, "exception_chain": ["ChatProviderError", "ReadError"], "status_code": None,
                          "outcome_unknown": True,
                          "effective_timeout_seconds": {"connect": 10.0, "read": 180.0, "write": 30.0, "pool": 10.0},
                          "request_id": "req-test-123"}
    assert "secret-like" not in str(diagnostic)


def test_runtime_config_status_exposes_safe_effective_transport_settings(monkeypatch):
    monkeypatch.setenv("PAPERTRAIL_LLM_BASE_URL", "https://api.deepseek.com/v1")
    monkeypatch.setenv("PAPERTRAIL_EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_READ_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("NO_PROXY", "existing.local,api.deepseek.com,dashscope.aliyuncs.com")
    status = providers.config_status()
    assert status["chat_endpoint_host"] == "api.deepseek.com"
    assert status["embedding_endpoint_host"] == "dashscope.aliyuncs.com"
    assert status["chat_timeout_seconds"] == {"connect": 10.0, "read": 180.0, "write": 30.0, "pool": 10.0}
    assert status["chat_max_tokens"] == 16384
    assert status["httpx_trust_env"] is True
    assert status["explicit_proxy_configured"] is False
    assert status["no_proxy_chat_host"] is True
    assert status["no_proxy_embedding_host"] is True
    assert "NO_PROXY" not in json.dumps(status)


def test_chat_phase_timeouts_reach_generation_and_support_clients(monkeypatch):
    timeouts = []
    request_bodies = []

    class Client:
        def __init__(self, **kwargs):
            timeouts.append(kwargs["timeout"])
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            request_bodies.append(kwargs["json"])
            return httpx.Response(200, request=httpx.Request("POST", url),
                                   json={"choices": [{"message": {"content": '{"answer":"ok","claims":[]}'}}]})

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_LLM_MODEL", "chat-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_API_KEY", "test-chat-secret")
    monkeypatch.setenv("PAPERTRAIL_LLM_READ_TIMEOUT_SECONDS", "180")
    asyncio.run(providers.generate_answer("question", []))
    asyncio.run(providers._chat_json("support", {"claims": []}))

    assert len(timeouts) == 2
    for timeout in timeouts:
        assert isinstance(timeout, httpx.Timeout)
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10.0, 180.0, 30.0, 10.0)
    assert [body["max_tokens"] for body in request_bodies] == [16384, 16384]


@pytest.mark.parametrize(("content", "finish_reason", "reason"), [
    ('{"answer":"truncated', "length", "output_truncated"),
    ('{"answer":"not closed"', "stop", "invalid_json_response"),
])
def test_chat_json_parse_failure_has_safe_response_diagnostics(monkeypatch, content, finish_reason, reason):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("POST", url),
                                  headers={"x-request-id": "provider-req-42"},
                                  json={"choices": [{"message": {"content": content},
                                                     "finish_reason": finish_reason}],
                                        "usage": {"prompt_tokens": 31, "completion_tokens": 8,
                                                  "total_tokens": 39}})

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setenv("PAPERTRAIL_LLM_MODEL", "chat-v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("PAPERTRAIL_LLM_API_KEY", "never-return-this-key")
    with pytest.raises(providers.ChatProviderError) as caught:
        asyncio.run(providers.generate_answer("question", [], request_id="task_test_1"))
    diagnostic = providers.chat_failure_diagnostics(caught.value, 82_000, "task_test_1")
    assert caught.value.reason == reason
    assert diagnostic["provider_request_id"] == "provider-req-42"
    assert diagnostic["request_id"] == "task_test_1"
    assert diagnostic["prompt_tokens"] == 31
    assert diagnostic["completion_tokens"] == 8
    assert diagnostic["configured_max_tokens"] == 16384
    assert diagnostic["response_content_chars"] == len(content)
    assert "never-return-this-key" not in str(diagnostic)
    assert content not in str(diagnostic)


def test_chat_output_token_limit_is_finite_and_configurable(monkeypatch):
    assert providers.chat_max_tokens_config() == 16384
    monkeypatch.setenv("PAPERTRAIL_LLM_MAX_TOKENS", "4096")
    assert providers.chat_max_tokens_config() == 4096
    for raw in ("255", "16385", "inf", "invalid"):
        monkeypatch.setenv("PAPERTRAIL_LLM_MAX_TOKENS", raw)
        with pytest.raises(providers.ChatProviderError) as caught:
            providers.chat_max_tokens_config()
        assert caught.value.reason == "configuration_error"
        assert caught.value.retryable is False


def test_revision_receives_structured_completeness_feedback(monkeypatch):
    captured = {}

    async def fake_chat(system, payload):
        captured["system"] = system
        captured["payload"] = payload
        return {"answer": "revised", "claims": []}, "mock"

    monkeypatch.setattr(providers, "_chat_json", fake_chat)
    check = {"status": "incomplete", "check": "decision_and_reason_presence",
             "missing_parts": ["explicit_decision"],
             "missing_requirements": ["Give a direct, explicit answer to the original question."]}
    result, _ = asyncio.run(providers.revise_answer("Question?", "Facts only", [], [], completeness_check=check))
    assert result["answer"] == "revised"
    assert captured["payload"]["completeness_check"] == check
    assert "address every listed requirement explicitly" in captured["system"]


def test_chat_read_timeout_must_be_finite_and_bounded(monkeypatch):
    for raw in ("0", "inf", "601", "invalid"):
        monkeypatch.setenv("PAPERTRAIL_LLM_READ_TIMEOUT_SECONDS", raw)
        with pytest.raises(providers.ChatProviderError) as caught:
            providers.chat_timeout_config()
        assert caught.value.reason == "configuration_error"
        assert caught.value.retryable is False


def test_index_reuses_cached_vectors_for_identical_chunk_content(monkeypatch):
    """Cross-version cache links are checked in an isolated SQLite database."""
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session
    from app import main
    from app.migrations import upgrade_schema

    root = main.ROOT / "var" / f"stage13-cache-test-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{(root / 'cache.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    upgrade_schema(engine, main.Base.metadata)
    monkeypatch.setattr(main, "engine", engine)
    model = "openai:isolated-cache-model@test"
    monkeypatch.setattr(providers, "config_status", lambda: {
        "embedding_ready": True, "embedding_model": model})
    try:
        with Session(engine) as db:
            db.add(main.Paper(id="user_cache_source", title="Source", authors=[], arxiv_id="",
                              source_url="", source="user_upload", status="ready"))
            db.add(main.Paper(id="user_cache_target", title="Target", authors=[], arxiv_id="",
                              source_url="", source="user_upload", status="indexing"))
            for version_id, paper_id in (("doc_cache_source", "user_cache_source"),
                                         ("doc_cache_target", "user_cache_target")):
                db.add(main.DocumentVersion(id=version_id, paper_id=paper_id, source_version="v1",
                                            source_url="", sha256=version_id.encode().hex().ljust(64, "0")[:64],
                                            path="", status="indexing", parser_version="test", page_count=1,
                                            block_count=2, quality={}))
            first_text = "Identical source paragraph with a stable content hash and a source location."
            for ordinal, text in enumerate((first_text, "A different paragraph needs a new vector.")):
                db.add(main.ParsedBlock(id=f"blk_target_{ordinal}", document_version_id="doc_cache_target",
                                        pdf_page_index=0, ordinal=ordinal, text=text,
                                        bbox=[10, 10+ordinal*20, 300, 25+ordinal*20], page_width=600,
                                        page_height=800, section_path=[f"section-{ordinal}"], kind="text"))
            digest = hashlib.sha256(first_text.encode()).hexdigest()
            db.add(main.Chunk(id="ch_cross_cache_seed", document_version_id="doc_cache_source", ordinal=1,
                              text=first_text, content_hash=digest, token_count=12,
                              section_path=[], spans=[]))
            db.add(main.ChunkVector(id="vec_cross_cache_seed", chunk_id="ch_cross_cache_seed",
                                    embedding_model=model, dimensions=3, vector=[1.0,0.0,0.0]))
            db.commit()
            requested = []

            async def fake_embed(texts):
                requested.extend(texts)
                return [[0.0,1.0,0.0] for _ in texts], model

            monkeypatch.setattr(providers, "embed_texts", fake_embed)
            version = db.get(main.DocumentVersion, "doc_cache_target")
            first = asyncio.run(main.ensure_index(db, version))
            assert first.status == "ready" and first.chunk_count == 2
            assert requested == ["A different paragraph needs a new vector."]
            assert db.scalar(select(func.count()).select_from(main.ChunkVector).where(
                main.ChunkVector.embedding_model == model)) == 3
            assert db.get(main.DocumentVersion, version.id).quality["indexing"]["reused_cached_vectors"] == 1
            second = asyncio.run(main.ensure_index(db, version))
            assert second.id == first.id
            assert len(requested) == 1
    finally:
        engine.dispose()
