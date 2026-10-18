import hashlib
import logging
import shutil
import time
import uuid

import fitz
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app import main, providers
from app.migrations import upgrade_schema


def _pdf(text: str | None) -> bytes:
    document = fitz.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def _wait_task(client: TestClient, task_id: str) -> dict:
    for _ in range(300):
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] not in {"waiting", "running"}:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task did not finish: {task_id}")


def test_schema_v14_keeps_upload_metadata_and_task_trace_without_rewriting_existing_papers():
    legacy_engine = create_engine("sqlite://")
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE papers (
                id VARCHAR(60) PRIMARY KEY, title TEXT NOT NULL, authors JSON NOT NULL,
                arxiv_id VARCHAR(40) NOT NULL, source_url TEXT NOT NULL
            )
        """)
        connection.exec_driver_sql(
            "INSERT INTO papers (id,title,authors,arxiv_id,source_url) VALUES ('lightrag','LightRAG','[]','', '')")
    upgrade_schema(legacy_engine, main.Base.metadata)
    with legacy_engine.connect() as connection:
        row = connection.execute(text(
            "SELECT id,title,source,original_filename,status FROM papers WHERE id='lightrag'"
        )).one()
        assert tuple(row) == ("lightrag", "LightRAG", "manifest", None, "ready")
        assert connection.execute(text(
            "SELECT version FROM schema_migrations WHERE version=14"
        )).scalar_one() == 14
        task_columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation_tasks)")}
        assert "phase_trace" in task_columns
    # Simulate a pre-v12 local database and verify the additive upgrade preserves it.
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE operation_tasks DROP COLUMN phase_trace")
    upgrade_schema(legacy_engine, main.Base.metadata)
    with legacy_engine.connect() as connection:
        assert "phase_trace" in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(operation_tasks)")}
        assert tuple(connection.execute(text("SELECT id,title FROM papers WHERE id='lightrag'")).one()) == (
            "lightrag", "LightRAG")
    legacy_engine.dispose()


def test_user_pdf_upload_processing_dedupe_recovery_and_search(monkeypatch, caplog):
    test_root = main.ROOT / "var" / f"stage12-test-{uuid.uuid4().hex}"
    test_root.mkdir(parents=True)
    test_engine = create_engine(f"sqlite:///{(test_root / 'upload-test.db').as_posix()}",
                                connect_args={"check_same_thread": False})
    upgrade_schema(test_engine, main.Base.metadata)
    monkeypatch.setattr(main, "engine", test_engine)
    monkeypatch.setattr(main, "DATA_DIR", test_root / "data")
    monkeypatch.setattr(main.providers, "config_status", lambda: {
        "embedding_ready": True, "embedding_model": "local-hash-v1"
    })

    embedding_batches = []

    async def fake_embed(texts):
        embedding_batches.append(list(texts))
        return [providers.hash_embedding(text) for text in texts], "local-hash-v1"

    monkeypatch.setattr(main.providers, "embed_texts", fake_embed)
    indexing_batch_sizes = []
    original_ensure_index = main.ensure_index

    async def tracked_ensure_index(db, version, task_id=None, embedding_batch_size=10):
        indexing_batch_sizes.append(embedding_batch_size)
        return await original_ensure_index(db, version, task_id=task_id,
                                           embedding_batch_size=embedding_batch_size)

    monkeypatch.setattr(main, "ensure_index", tracked_ensure_index)
    caplog.set_level(logging.INFO, logger="papertrail.upload")
    content = _pdf("PaperTrail upload fixture: retrieval augmented generation evidence.")
    with TestClient(main.app) as client:
        uploaded = client.post("/api/v1/user-papers/upload",
                               files={"file": ("retrieval-notes.pdf", content, "application/pdf")})
        assert uploaded.status_code == 202, uploaded.text
        assert len(uploaded.headers["X-PaperTrail-Request-ID"]) == 32
        assert "user_upload_started" in caplog.text
        assert "user_upload_finished" in caplog.text
        first = uploaded.json()
        assert first["paper"]["source"] == "user_upload"
        assert first["paper"]["original_filename"] == "retrieval-notes.pdf"
        assert first["version"]["sha256"] == hashlib.sha256(content).hexdigest()
        completed = _wait_task(client, first["task"]["task_id"])
        assert completed["status"] == "succeeded", completed
        assert indexing_batch_sizes == [5]

        paper_id = first["paper"]["id"]
        version_id = first["version"]["id"]
        note = client.post("/api/v1/notes", json={
            "title": "上传论文笔记", "body": "用户自己的阅读记录。", "paper_ids": [paper_id],
        })
        assert note.status_code == 200, note.text
        assert note.json()["paper_ids"] == [paper_id]
        note_id = note.json()["note_id"]
        edited_note = client.put(f"/api/v1/notes/{note_id}", json={
            "title": "上传论文笔记已编辑", "body": "保存后仍然只是用户正文。", "paper_ids": [paper_id],
        })
        assert edited_note.status_code == 200, edited_note.text
        assert edited_note.json()["paper_ids"] == [paper_id]
        assert client.get(f"/api/v1/notes/{note_id}").json()["body"] == "保存后仍然只是用户正文。"
        paper = client.get(f"/api/v1/papers/{paper_id}").json()
        version = next(v for v in paper["versions"] if v["id"] == version_id)
        assert paper["status"] == "ready"
        assert version["status"] == "ready"
        assert version["block_count"] >= 1
        original_index_trace = version["quality"]["indexing"]
        with Session(test_engine) as db:
            chunk_count = db.scalar(select(func.count()).select_from(main.Chunk).where(
                main.Chunk.document_version_id == version_id))
            index = db.get(main.SearchIndex, main.index_identifier(
                db.get(main.DocumentVersion, version_id), "local-hash-v1"))
            assert chunk_count > 0
            assert index and index.status == "ready"
        assert index.dimensions == providers.EMBED_DIMENSIONS
        assert index.chunk_count == chunk_count
        initial_batches = len(embedding_batches)
        assert initial_batches == 1

        found = client.post("/api/v1/search", json={
            "query": "retrieval augmented generation", "paper_ids": [paper_id], "top_k": 5
        })
        assert found.status_code == 200, found.text
        assert found.json()["document_version_ids"] == [version_id]
        assert found.json()["results"]
        after_query_batches = len(embedding_batches)
        assert after_query_batches == initial_batches + 1

        duplicate = client.post("/api/v1/user-papers/upload",
                                files={"file": ("..\\evil.pdf", content, "application/pdf")})
        assert duplicate.status_code == 202, duplicate.text
        again = duplicate.json()
        assert again["deduplicated"] is True
        assert again["paper"]["id"] == paper_id
        assert again["version"]["id"] == version_id
        assert again["task"]["task_id"] == first["task"]["task_id"]
        with Session(test_engine) as db:
            assert db.scalar(select(func.count()).select_from(main.Paper).where(
                main.Paper.source == "user_upload")) == 1
            assert db.scalar(select(func.count()).select_from(main.DocumentVersion).where(
                main.DocumentVersion.paper_id == paper_id)) == 1
            assert db.scalar(select(func.count()).select_from(main.Chunk).where(
                main.Chunk.document_version_id == version_id)) == chunk_count

        newer = client.post("/api/v1/user-papers/upload",
                            files={"file": ("retrieval-notes.pdf",
                                            _pdf("A new immutable revision of the same named development paper."),
                                            "application/pdf")})
        assert newer.status_code == 202
        newer_data = newer.json()
        assert newer_data["paper"]["id"] == paper_id
        assert newer_data["version"]["id"] != version_id
        assert newer_data["version"]["source_version"] == "v2"
        assert _wait_task(client, newer_data["task"]["task_id"])["status"] == "succeeded"
        after_new_version_batches = len(embedding_batches)
        assert after_new_version_batches == after_query_batches + 1

        with Session(test_engine) as db:
            payload = main._enqueue_payload_for_version(db.get(main.DocumentVersion, version_id))
            now = main._utcnow()
            db.add(main.OperationTask(
                id="task_committed_upload_probe", kind="document_ingestion",
                idempotency_key="upload-committed-result-probe",
                request_hash=main._request_fingerprint(payload), request_payload=payload,
                status="running", phase="persisting", external_call_started=True,
                result_id=version_id, created_at=now, updated_at=now))
            db.commit()
        main.reconcile_operation_tasks()
        committed = client.get("/api/v1/tasks/task_committed_upload_probe").json()
        assert committed["status"] == "succeeded"
        assert committed["finished_at"] is not None

        invalid = client.post("/api/v1/user-papers/upload",
                              files={"file": ("bad.pdf", b"not really a pdf", "application/pdf")})
        assert invalid.status_code == 415
        corrupt = client.post("/api/v1/user-papers/upload",
                              files={"file": ("corrupt.pdf", b"%PDF-1.7\ninvalid structure", "application/pdf")})
        assert corrupt.status_code == 422

        blank = client.post("/api/v1/user-papers/upload",
                            files={"file": ("blank.pdf", _pdf(None), "application/pdf")})
        assert blank.status_code == 202, blank.text
        blank_task = _wait_task(client, blank.json()["task"]["task_id"])
        assert blank_task["status"] == "failed"
        assert blank_task["error"]["code"] == "no_extractable_text"
        blank_data = client.get(f"/api/v1/papers/{blank.json()['paper']['id']}").json()
        assert blank_data["status"] == "failed"
        with Session(test_engine) as db:
            blank_version = db.get(main.DocumentVersion, blank.json()["version"]["id"])
            assert blank_version.status == "failed"
            assert db.scalar(select(func.count()).select_from(main.SearchIndex).where(
                main.SearchIndex.document_version_id == blank_version.id)) == 0

        # Simulate a process restart with an unstarted persisted ingestion task.
        with Session(test_engine) as db:
            recovery_task = main.OperationTask(
                id="task_recovery_upload", kind="document_ingestion", idempotency_key="upload-recovery",
                request_hash=main._request_fingerprint(main._enqueue_payload_for_version(
                    db.get(main.DocumentVersion, version_id))),
                request_payload=main._enqueue_payload_for_version(db.get(main.DocumentVersion, version_id)),
                status="running", phase="parsing", external_call_started=False,
            )
            db.add(recovery_task)
            db.commit()
        main.reconcile_operation_tasks()
        recovered = client.get("/api/v1/tasks/task_recovery_upload").json()
        assert recovered["status"] == "interrupted"
        assert recovered["finished_at"] is not None
        assert client.get(f"/api/v1/papers/{paper_id}").json()["status"] == "ready"
        assert next(v for v in client.get(f"/api/v1/papers/{paper_id}").json()["versions"]
                    if v["id"] == version_id)["status"] == "interrupted"

        retried = client.post(f"/api/v1/user-papers/{paper_id}/versions/{version_id}/retry",
                              headers={"Idempotency-Key": "upload-recovery-retry"})
        assert retried.status_code == 202, retried.text
        retry_task = _wait_task(client, retried.json()["task_id"])
        assert retry_task["status"] == "succeeded", retry_task
        assert len(embedding_batches) == after_new_version_batches
        refreshed_paper = client.get(f"/api/v1/papers/{paper_id}").json()
        assert refreshed_paper["status"] == "ready"
        refreshed_version = next(v for v in refreshed_paper["versions"] if v["id"] == version_id)
        assert refreshed_version["quality"]["indexing"] == original_index_trace

        # The same custom idempotency key cannot create an orphan record for a
        # different PDF; conflict is returned before paper/version persistence.
        keyed_content = _pdf("A second uploaded development paper for idempotency testing.")
        keyed = client.post("/api/v1/user-papers/upload",
                            headers={"Idempotency-Key": "stage12-same-key"},
                            files={"file": ("keyed-one.pdf", keyed_content, "application/pdf")})
        assert keyed.status_code == 202
        keyed_task = _wait_task(client, keyed.json()["task"]["task_id"])
        assert keyed_task["status"] == "succeeded"
        with Session(test_engine) as db:
            before_papers = db.scalar(select(func.count()).select_from(main.Paper).where(
                main.Paper.source == "user_upload"))
            before_versions = db.scalar(select(func.count()).select_from(main.DocumentVersion))
        collision = client.post("/api/v1/user-papers/upload",
                                headers={"Idempotency-Key": "stage12-same-key"},
                                files={"file": ("keyed-two.pdf", _pdf("Different content under the reused key."),
                                                "application/pdf")})
        assert collision.status_code == 409
        with Session(test_engine) as db:
            assert db.scalar(select(func.count()).select_from(main.Paper).where(
                main.Paper.source == "user_upload")) == before_papers
            assert db.scalar(select(func.count()).select_from(main.DocumentVersion)) == before_versions

        # Ambiguous prior provider completion requires an explicit retry choice.
        with Session(test_engine) as db:
            payload = main._enqueue_payload_for_version(db.get(main.DocumentVersion, version_id))
            now = main._utcnow()
            db.add(main.OperationTask(
                id="task_unknown_upload_probe", kind="document_ingestion", idempotency_key="upload-unknown-probe",
                request_hash=main._request_fingerprint(payload), request_payload=payload,
                status="running", phase="embedding", external_call_started=True,
                created_at=now, updated_at=now))
            db.commit()
        main.reconcile_operation_tasks()
        unknown_recovered = client.get("/api/v1/tasks/task_unknown_upload_probe").json()
        assert unknown_recovered["status"] == "outcome_unknown"
        assert unknown_recovered["finished_at"] is not None
        no_confirm = client.post(f"/api/v1/user-papers/{paper_id}/versions/{version_id}/retry",
                                 headers={"Idempotency-Key": "stage12-unknown-no-confirm"})
        assert no_confirm.status_code == 409
        explicit = client.post(f"/api/v1/user-papers/{paper_id}/versions/{version_id}/retry",
                               headers={"Idempotency-Key": "stage12-unknown-confirm",
                                        "X-Confirm-Outcome-Unknown-Retry": "true"})
        assert explicit.status_code == 202
        explicit_task = _wait_task(client, explicit.json()["task_id"])
        assert explicit_task["status"] == "succeeded"

    test_engine.dispose()
    shutil.rmtree(test_root)
