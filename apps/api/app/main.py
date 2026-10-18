from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
import unicodedata
import uuid
from urllib.parse import quote as url_quote
from contextlib import contextmanager
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import pymupdf as fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint, create_engine, select, inspect, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .chunking import CHUNKER_VERSION, build_chunks, stable_chunk_id
from .migrations import CURRENT_SCHEMA_VERSION, upgrade_schema
from . import providers, ocr
from .comparison_reports import (DEFAULT_DIMENSIONS, SYSTEM_PROMPT as COMPARISON_SYSTEM_PROMPT,
                                 InvalidReport, attach_citations, normalize_generated_report,
                                 report_filename, report_markdown, retrieval_query as comparison_query)
from .retrieval import bm25_scores, dense_scores, reciprocal_rank_fusion


ROOT = Path(__file__).resolve().parents[3]
logger = logging.getLogger("papertrail.qa")
upload_logger = logging.getLogger("papertrail.upload")
load_dotenv(ROOT / ".env", override=False)
MANIFEST = ROOT / "data/samples/manifest.json"
DATA_DIR = Path(os.getenv("PAPERTRAIL_DATA_DIR", str(ROOT / "var"))).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv("PAPERTRAIL_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'papertrail.db').as_posix()}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list] = mapped_column(JSON)
    arxiv_id: Mapped[str] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(30), default="manifest", server_default="manifest")
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="ready", server_default="ready")
    versions: Mapped[list[DocumentVersion]] = relationship(back_populates="paper")


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("paper_id", "sha256"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"), index=True)
    source_version: Mapped[str] = mapped_column(String(30))
    source_url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    parser_version: Mapped[str] = mapped_column(String(40))
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    paper: Mapped[Paper] = relationship(back_populates="versions")


class ParsedBlock(Base):
    __tablename__ = "parsed_blocks"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    pdf_page_index: Mapped[int] = mapped_column(Integer, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list] = mapped_column(JSON)
    page_width: Mapped[float] = mapped_column()
    page_height: Mapped[float] = mapped_column()
    section_path: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(20), default="text")


class ParseSnapshot(Base):
    """Version-pinned, non-default parsed representation stored beside legacy blocks."""
    __tablename__ = "parse_snapshots"
    __table_args__ = (UniqueConstraint("document_version_id", "source_sha256", "parser_identity",
                                      "protocol_version", name="uq_parse_snapshot_generation"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    source_sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser_identity: Mapped[str] = mapped_column(String(160))
    protocol_version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20), index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    structure_data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ParseSnapshotBlock(Base):
    """Normalized block locations for an immutable parse snapshot."""
    __tablename__ = "parse_snapshot_blocks"
    __table_args__ = (UniqueConstraint("parse_snapshot_id", "source_block_id", name="uq_snapshot_source_block"),
                      UniqueConstraint("parse_snapshot_id", "pdf_page_index", "ordinal", name="uq_snapshot_page_ordinal"))
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    parse_snapshot_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshots.id"), index=True)
    source_block_id: Mapped[str] = mapped_column(String(80))
    pdf_page_index: Mapped[int] = mapped_column(Integer, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    page_width: Mapped[float | None] = mapped_column(nullable=True)
    page_height: Mapped[float | None] = mapped_column(nullable=True)
    section_path: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(40), default="text")
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reading_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    quality_flags: Mapped[list] = mapped_column(JSON, default=list)
    structure_data: Mapped[dict] = mapped_column(JSON, default=dict)


class ParseSnapshotChunk(Base):
    __tablename__ = "parse_snapshot_chunks"
    __table_args__ = (UniqueConstraint("parse_snapshot_id", "chunker_generation", "ordinal",
                                      name="uq_snapshot_chunk_ordinal"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    parse_snapshot_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshots.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    chunker_generation: Mapped[str] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[list] = mapped_column(JSON, default=list)
    spans: Mapped[list] = mapped_column(JSON)
    source_block_ids: Mapped[list] = mapped_column(JSON, default=list)


class ParseSnapshotVector(Base):
    """Content-addressed experimental vector cache, isolated by model identity and dimension."""
    __tablename__ = "parse_snapshot_vectors"
    __table_args__ = (UniqueConstraint("embedding_model", "dimensions", "content_hash",
                                      name="uq_snapshot_vector_content_model"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    embedding_model: Mapped[str] = mapped_column(String(180), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    vector: Mapped[list] = mapped_column(JSON)


class ParseSnapshotChunkVector(Base):
    __tablename__ = "parse_snapshot_chunk_vectors"
    __table_args__ = (UniqueConstraint("parse_snapshot_chunk_id", "embedding_model",
                                      name="uq_snapshot_chunk_vector_model"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    parse_snapshot_chunk_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshot_chunks.id"), index=True)
    embedding_model: Mapped[str] = mapped_column(String(180), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    vector_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshot_vectors.id"), index=True)


class ParseSnapshotIndex(Base):
    __tablename__ = "parse_snapshot_indexes"
    __table_args__ = (UniqueConstraint("parse_snapshot_id", "chunker_generation", "embedding_model",
                                      "dimensions", name="uq_snapshot_index_generation"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    parse_snapshot_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshots.id"), index=True)
    chunker_generation: Mapped[str] = mapped_column(String(100))
    embedding_model: Mapped[str] = mapped_column(String(180))
    dimensions: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ParseSnapshotIndexChunk(Base):
    __tablename__ = "parse_snapshot_index_chunks"
    index_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshot_indexes.id"), primary_key=True)
    parse_snapshot_chunk_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshot_chunks.id"), primary_key=True)


class ParseSnapshotEvidence(Base):
    """Evidence from an experimental parse generation; never points at legacy chunks."""
    __tablename__ = "parse_snapshot_evidence"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    qa_run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id"), index=True)
    parse_snapshot_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshots.id"), index=True)
    parse_snapshot_chunk_id: Mapped[str] = mapped_column(ForeignKey("parse_snapshot_chunks.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    exact_quote: Mapped[str] = mapped_column(Text)
    spans: Mapped[list] = mapped_column(JSON)
    retrieval_details: Mapped[dict] = mapped_column(JSON)
    location_valid: Mapped[bool] = mapped_column(default=False)
    quote_valid: Mapped[bool] = mapped_column(default=False)
    support_status: Mapped[str] = mapped_column(String(30), default="pending")


class DocumentIndexSelection(Base):
    """Explicit opt-in active pointer; absence means the unchanged legacy index path."""
    __tablename__ = "document_index_selections"
    __table_args__ = (
        CheckConstraint(
            "(active_legacy_index_id IS NOT NULL AND active_snapshot_index_id IS NULL) OR "
            "(active_legacy_index_id IS NULL AND active_snapshot_index_id IS NOT NULL)",
            name="ck_active_index_exactly_one"),
        CheckConstraint(
            "(previous_legacy_index_id IS NULL OR previous_snapshot_index_id IS NULL)",
            name="ck_previous_index_at_most_one"),
    )
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), primary_key=True)
    active_legacy_index_id: Mapped[str | None] = mapped_column(ForeignKey("search_indexes.id"), nullable=True)
    active_snapshot_index_id: Mapped[str | None] = mapped_column(ForeignKey("parse_snapshot_indexes.id"), nullable=True)
    previous_legacy_index_id: Mapped[str | None] = mapped_column(ForeignKey("search_indexes.id"), nullable=True)
    previous_snapshot_index_id: Mapped[str | None] = mapped_column(ForeignKey("parse_snapshot_indexes.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SearchIndex(Base):
    __tablename__ = "search_indexes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    parser_version: Mapped[str] = mapped_column(String(60))
    chunker_version: Mapped[str] = mapped_column(String(60))
    embedding_model: Mapped[str] = mapped_column(String(120))
    dimensions: Mapped[int] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_version_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    token_count: Mapped[int] = mapped_column(Integer)
    section_path: Mapped[list] = mapped_column(JSON, default=list)
    spans: Mapped[list] = mapped_column(JSON)


class ChunkVector(Base):
    __tablename__ = "chunk_vectors"
    __table_args__ = (UniqueConstraint("chunk_id", "embedding_model"),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), index=True)
    embedding_model: Mapped[str] = mapped_column(String(120), index=True)
    dimensions: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list] = mapped_column(JSON)


class IndexChunk(Base):
    __tablename__ = "index_chunks"
    index_id: Mapped[str] = mapped_column(ForeignKey("search_indexes.id"), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), primary_key=True)


class QaRun(Base):
    __tablename__ = "qa_runs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    query: Mapped[str] = mapped_column(Text)
    retrieval_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    paper_ids: Mapped[list] = mapped_column(JSON)
    document_version_ids: Mapped[list] = mapped_column(JSON)
    parse_snapshot_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answer: Mapped[str] = mapped_column(Text)
    baseline_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    support_trace: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    claims: Mapped[list] = mapped_column(JSON)
    chat_model: Mapped[str] = mapped_column(String(120))
    embedding_model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    qa_run_id: Mapped[str] = mapped_column(ForeignKey("qa_runs.id"), index=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    exact_quote: Mapped[str] = mapped_column(Text)
    spans: Mapped[list] = mapped_column(JSON)
    retrieval_details: Mapped[dict] = mapped_column(JSON)
    location_valid: Mapped[bool] = mapped_column(default=False)
    quote_valid: Mapped[bool] = mapped_column(default=False)
    support_status: Mapped[str] = mapped_column(String(30), default="pending")


class EvidenceNote(Base):
    __tablename__ = "evidence_notes"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    source_qa_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    source_query_snapshot: Mapped[str] = mapped_column(Text, default="")
    claims_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    citations_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    paper_ids_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ComparisonReport(Base):
    __tablename__ = "comparison_reports"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    paper_ids: Mapped[list] = mapped_column(JSON)
    document_version_ids: Mapped[list] = mapped_column(JSON)
    report_data: Mapped[dict] = mapped_column(JSON)
    chat_model: Mapped[str] = mapped_column(String(120))
    embedding_model: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class ComparisonReportRevision(Base):
    __tablename__ = "comparison_report_revisions"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_report_id: Mapped[str] = mapped_column(String(80), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    report_data: Mapped[dict] = mapped_column(JSON)
    changes: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OperationTask(Base):
    __tablename__ = "operation_tasks"
    __table_args__ = (UniqueConstraint("kind", "idempotency_key", name="uq_operation_task_idempotency"),)
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    request_hash: Mapped[str] = mapped_column(String(64))
    request_payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), index=True)
    phase: Mapped[str] = mapped_column(String(40), default="waiting")
    external_call_started: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    result_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    phase_trace: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SearchRequest(BaseModel):
    query: str
    retrieval_query: str | None = None
    paper_ids: list[str]
    top_k: int = 10
    # When supplied, one explicit snapshot ID or null (legacy) per paper, in paper_ids order.
    parse_snapshot_ids: list[str | None] | None = None


class AskRequest(SearchRequest):
    verify_support: bool = True
    # Explicit ablation switch. The default preserves the existing Hybrid path.
    retrieval_mode: Literal["hybrid", "bm25"] = "hybrid"
    # Optional immutable scope for reproducible runs and offline comparisons.
    # When supplied, retrieve() requires an existing ready index and never builds one.
    document_version_ids: list[str] | None = None


class NoteCreateRequest(BaseModel):
    title: str
    body: str = ""
    source_qa_run_id: str | None = None
    paper_ids: list[str] | None = None
    claim_ids: list[str] = []
    evidence_ids: list[str] = []


class NoteUpdateRequest(BaseModel):
    title: str
    body: str = ""
    paper_ids: list[str] | None = None


class ComparisonReportCreateRequest(BaseModel):
    title: str
    paper_ids: list[str]
    document_version_ids: list[str]
    dimensions: list[str] = DEFAULT_DIMENSIONS
    note_ids: list[str] = []


class ComparisonReportRevisionChange(BaseModel):
    claim_id: str
    text: str
    reason: str


class ComparisonReportRevisionRequest(BaseModel):
    changes: list[ComparisonReportRevisionChange]


upgrade_schema(engine, Base.metadata)
ACTIVE_TASKS: dict[str, asyncio.Task] = {}
TASK_SEMAPHORE = asyncio.Semaphore(1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_ms(task: OperationTask, now: datetime | None = None) -> int:
    end = task.finished_at or now or _utcnow()
    # Include queue/wait time in the user-visible end-to-end task duration.
    start = task.created_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def _task_data(task: OperationTask, db: Session | None = None) -> dict:
    summary = {}
    payload = task.request_payload or {}
    if task.kind == "qa":
        summary = {key: payload[key] for key in ("query", "paper_ids", "document_version_ids",
                                                  "parse_snapshot_ids") if key in payload}
    elif task.kind == "comparison_report":
        summary = {key: payload[key] for key in ("title", "paper_ids", "document_version_ids", "dimensions") if key in payload}
    elif task.kind == "document_ingestion":
        summary = {key: payload[key] for key in ("paper_id", "document_version_id", "sha256") if key in payload}
    data = {"task_id": task.id, "kind": task.kind, "status": task.status,
            "phase": task.phase, "created_at": task.created_at.isoformat(),
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "elapsed_ms": _elapsed_ms(task), "cancel_requested": task.cancel_requested,
            "request_summary": summary,
            "result_id": task.result_id, "error": task.error,
            "phase_trace": task.phase_trace or []}
    if task.kind == "document_ingestion":
        trace = task.phase_trace or []
        data["progress"] = next((entry["progress"] for entry in reversed(trace)
                                 if isinstance(entry, dict) and isinstance(entry.get("progress"), dict)), None)
        if db is not None:
            data["progress"] = _ingestion_progress(db, payload.get("document_version_id"))
    return data


def _valid_stored_vector(dimensions: int, vector: list) -> bool:
    return (isinstance(dimensions, int) and dimensions > 0 and isinstance(vector, list)
            and len(vector) == dimensions
            and all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector))


def _ingestion_progress(db: Session, version_id: str | None) -> dict | None:
    """Read-only, content-free upload indexing state for task polling and recovery UI."""
    if not version_id:
        return None
    version = db.get(DocumentVersion, version_id)
    if not version:
        return None
    chunk_ids = db.scalars(select(Chunk.id).where(Chunk.document_version_id == version_id)).all()
    index = db.scalars(select(SearchIndex).where(SearchIndex.document_version_id == version_id)
                       .order_by(SearchIndex.created_at.desc(), SearchIndex.id.desc())).first()
    model = index.embedding_model if index else None
    vectors = []
    if model and chunk_ids:
        vectors = db.execute(select(ChunkVector.chunk_id, ChunkVector.dimensions, ChunkVector.vector)
                             .where(ChunkVector.chunk_id.in_(chunk_ids),
                                    ChunkVector.embedding_model == model)).all()
    valid_chunks = {row.chunk_id for row in vectors if _valid_stored_vector(row.dimensions, row.vector)}
    total = len(chunk_ids)
    linked = set()
    if index:
        linked = set(db.scalars(select(IndexChunk.chunk_id).where(IndexChunk.index_id == index.id)).all())
    dimensions = {row.dimensions for row in vectors if row.chunk_id in valid_chunks}
    ready = bool(index and index.status == "ready" and total > 0 and index.chunk_count == total
                 and valid_chunks == set(chunk_ids) and linked == set(chunk_ids)
                 and dimensions == {index.dimensions})
    return {"completed_vectors": len(valid_chunks), "total_vectors": total,
            "missing_vectors": max(0, total - len(valid_chunks)),
            "batch_size": 5, "index_status": index.status if index else "not_started",
            "ready": ready}


def _update_task(db: Session, task_id: str, *, status: str | None = None,
                 phase: str | None = None, external_call_started: bool | None = None,
                 result_id: str | None = None, error: dict | None = None,
                 progress: dict | None = None,
                 finished: bool = False, started: bool = False) -> None:
    task = db.get(OperationTask, task_id)
    if not task:
        return
    if status is not None:
        task.status = status
    if phase is not None:
        task.phase = phase
    if external_call_started is not None:
        task.external_call_started = external_call_started
    if result_id is not None:
        task.result_id = result_id
    if error is not None:
        task.error = error
    now = _utcnow()
    trace = list(task.phase_trace or [])
    active = trace[-1] if trace and trace[-1].get("ended_at") is None else None
    if progress is not None and active is not None:
        active = dict(active)
        active["progress"] = progress
        trace[-1] = active
    phase_changed = phase is not None and (active is None or active.get("phase") != phase)
    if active and (phase_changed or finished):
        active = dict(active)
        active["ended_at"] = now.isoformat()
        try:
            began = datetime.fromisoformat(active["started_at"])
            active["elapsed_ms"] = max(0, int((now - began).total_seconds() * 1000))
        except (KeyError, ValueError, TypeError):
            active["elapsed_ms"] = None
        trace[-1] = active
        active = None
    if phase is not None and active is None and not (finished and not phase_changed):
        trace.append({"phase": phase, "started_at": now.isoformat(), "ended_at": None, "elapsed_ms": None})
    if finished and trace and trace[-1].get("ended_at") is None:
        last = dict(trace[-1])
        last["ended_at"] = now.isoformat()
        try:
            began = datetime.fromisoformat(last["started_at"])
            last["elapsed_ms"] = max(0, int((now - began).total_seconds() * 1000))
        except (KeyError, ValueError, TypeError):
            last["elapsed_ms"] = None
        trace[-1] = last
    task.phase_trace = trace
    if started and task.started_at is None:
        task.started_at = now
    if finished:
        task.finished_at = _utcnow()
    task.updated_at = now
    # Progress is intentionally durable at phase boundaries. This also releases
    # SQLite read transactions before long provider calls.
    db.commit()


def _task_error_from_exception(exc: Exception) -> tuple[str, dict]:
    if isinstance(exc, HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = str(detail.get("code", "request_failed"))
        retryable = detail.get("retryable") is True
        details = detail.get("details") if isinstance(detail.get("details"), dict) else {}
        allowed = {key: details[key] for key in
                   ("reason", "status_code", "phase", "endpoint_host", "elapsed_ms", "response_elapsed_ms",
                   "request_id", "provider_request_id", "exception_chain", "finish_reason", "prompt_tokens",
                   "completion_tokens", "total_tokens", "embedding_requests", "embedding_tokens",
                    "input_count", "input_tokens", "input_characters", "input_utf8_bytes", "max_item_characters",
                    "max_item_utf8_bytes", "request_body_bytes", "status", "transport_event",
                    "provider_error_code", "provider_error_type", "provider_error_param", "provider_error_category",
                    "response_content_chars", "json_error",
                    "json_error_line", "json_error_column", "json_error_position", "content_starts_object",
                   "content_ends_object", "content_wrapped_in_fence", "configured_max_tokens", "response_shape",
                    "response_json_error", "effective_timeout_seconds", "outcome_unknown",
                    "batch_index", "batch_size", "cached_vectors", "vectors_present", "vectors_missing",
                    "total_vectors", "failed_phase", "retrieval_timings_ms",
                    "timings_ms")
                   if key in details}
        message = str(detail.get("message") or "任务失败")[:500]
        return ("outcome_unknown" if details.get("outcome_unknown", retryable) else "failed",
                {"code": code, "message": message, "retryable": retryable, "details": allowed})
    logger.error("operation_task_failed exception_type=%s", type(exc).__name__)
    return "failed", {"code": "internal_error", "message": "任务失败，未自动重试。请查看本地服务日志。",
                       "retryable": False, "details": {"exception_type": type(exc).__name__}}


def _task_result_exists(db: Session, task: OperationTask) -> bool:
    if not task.result_id:
        return False
    if task.kind == "qa":
        return db.get(QaRun, task.result_id) is not None
    if task.kind == "comparison_report":
        return db.get(ComparisonReport, task.result_id) is not None
    if task.kind == "document_ingestion":
        version = db.get(DocumentVersion, task.result_id)
        if not version or version.status not in ("ready", "quality_warning"):
            return False
        return db.scalar(select(SearchIndex.id).where(
            SearchIndex.document_version_id == version.id, SearchIndex.status == "ready")) is not None
    return False


def _set_ingestion_terminal(db: Session, task: OperationTask, status: str) -> None:
    if task.kind != "document_ingestion":
        return
    payload = task.request_payload or {}
    version = db.get(DocumentVersion, payload.get("document_version_id", ""))
    paper = db.get(Paper, payload.get("paper_id", ""))
    if version:
        version.status = ("indexing_unknown" if status == "outcome_unknown" else status)
        version.error = "processing_interrupted" if status in {"interrupted", "cancelled"} else version.error
    latest_version_id = db.scalar(select(DocumentVersion.id).where(
        DocumentVersion.paper_id == paper.id).order_by(
            DocumentVersion.created_at.desc(), DocumentVersion.id.desc()).limit(1)) if paper else None
    if paper and version and latest_version_id == version.id:
        paper.status = status


def reconcile_operation_tasks() -> None:
    """Never replay persisted work after restart; resolve only known saved results."""
    with Session(engine) as db:
        active = db.scalars(select(OperationTask).where(
            OperationTask.status.in_(("waiting", "running")))).all()
        for task in active:
            if task.status == "running" and _task_result_exists(db, task):
                task.status = "succeeded"
                task.phase = "completed"
                task.error = None
            elif task.status == "running" and task.external_call_started:
                task.status = "outcome_unknown"
                task.phase = "interrupted"
                task.error = {"code": "process_interrupted", "message": "服务重启时外部请求可能已被处理；系统未自动重试。请先检查历史记录。",
                              "retryable": False, "details": {"recovery": "manual_review_required"}}
            else:
                task.status = "interrupted"
                task.phase = "interrupted"
                task.error = {"code": "process_interrupted", "message": "任务未完成且没有已知外部请求结果；系统未自动重试。",
                              "retryable": False, "details": {"recovery": "manual_resubmit_allowed"}}
            task.finished_at = task.finished_at or _utcnow()
            task.updated_at = _utcnow()
            if task.status != "succeeded":
                _set_ingestion_terminal(db, task, task.status)
        db.commit()


@asynccontextmanager
async def app_lifespan(_app):
    reconcile_operation_tasks()
    yield
    runners = list(ACTIVE_TASKS.values())
    for runner in runners:
        runner.cancel()
    if runners:
        await asyncio.gather(*runners, return_exceptions=True)
    engine.dispose()


app = FastAPI(title="PaperTrail API", version="0.2.0", lifespan=app_lifespan)


@app.middleware("http")
async def trace_user_pdf_upload(request: Request, call_next):
    if request.method != "POST" or request.url.path != "/api/v1/user-papers/upload":
        return await call_next(request)
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    raw_content_length = request.headers.get("content-length", "")
    content_length = raw_content_length if re.fullmatch(r"\d{1,12}", raw_content_length) else "unknown"
    upload_logger.info("user_upload_started request_id=%s content_length=%s", request_id, content_length)
    try:
        response = await call_next(request)
    except Exception as exc:
        upload_logger.warning("user_upload_exception request_id=%s elapsed_ms=%.1f exception_type=%s",
                             request_id, (time.perf_counter() - started) * 1000, type(exc).__name__)
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["X-PaperTrail-Request-ID"] = request_id
    upload_logger.info("user_upload_finished request_id=%s status_code=%s elapsed_ms=%.1f",
                       request_id, response.status_code, elapsed_ms)
    return response


@app.exception_handler(HTTPException)
async def api_error_handler(request: Request, exc: HTTPException):
    code = "http_error"
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)
    if exc.status_code == 404:
        code = "not_found"
    elif exc.status_code == 422:
        code = "validation_error"
    elif exc.status_code == 503:
        code = "provider_unavailable"
    return JSONResponse(status_code=exc.status_code,
                        content={"code": code, "message": str(exc.detail), "retryable": exc.status_code >= 500,
                                 "run_id": None, "details": None}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"code": "validation_error", "message": "Request validation failed",
                                                  "retryable": False, "run_id": None, "details": jsonable_encoder(exc.errors())})


def manifest_papers() -> dict[str, dict]:
    return {p["paper_id"]: p for p in json.loads(MANIFEST.read_text(encoding="utf-8"))["papers"]}


def validate_note_paper_ids(db: Session, paper_ids: list[str]) -> list[str]:
    requested = list(dict.fromkeys(paper_ids))
    invalid = set(requested) - set(manifest_papers())
    if invalid:
        available_uploads = set(db.scalars(select(Paper.id).join(
            DocumentVersion, DocumentVersion.paper_id == Paper.id).where(
                Paper.id.in_(invalid), Paper.source == "user_upload",
                DocumentVersion.status.in_(("ready", "quality_warning"))).distinct()).all())
        invalid -= available_uploads
    if invalid:
        raise HTTPException(422, "Note paper IDs must refer to fixed papers or ready uploaded papers")
    return requested


def require_query_scope(query: str, paper_ids: list[str], db: Session | None = None) -> None:
    if not query.strip() or len(query) > 2000:
        raise HTTPException(422, {"code": "invalid_query", "message": "query must contain 1-2000 characters", "retryable": False, "run_id": None, "details": None})
    valid = set(manifest_papers())
    invalid = set(paper_ids) - valid
    if invalid and db is not None:
        uploaded = set(db.scalars(select(Paper.id).join(
            DocumentVersion, DocumentVersion.paper_id == Paper.id).where(
                Paper.id.in_(invalid), Paper.source == "user_upload",
                DocumentVersion.status.in_(("ready", "quality_warning"))).distinct()).all())
        invalid -= uploaded
    if not paper_ids or len(paper_ids) > 3 or len(set(paper_ids)) != len(paper_ids) or invalid:
        scope_label = "fixed manifest or ready user-uploaded papers" if db is not None else "the sample manifest"
        raise HTTPException(422, {"code": "invalid_paper_scope", "message": f"Select 1-3 distinct paper IDs from {scope_label}", "retryable": False, "run_id": None, "details": None})


def selected_versions(db: Session, paper_ids: list[str]) -> list[DocumentVersion]:
    versions = []
    for paper_id in paper_ids:
        paper = db.get(Paper, paper_id)
        if not paper:
            raise HTTPException(409, {"code": "paper_not_imported", "message": f"Paper {paper_id} has not been imported", "retryable": False, "run_id": None, "details": {"paper_id": paper_id}})
        version = db.scalars(select(DocumentVersion).where(
            DocumentVersion.paper_id == paper_id,
            DocumentVersion.status.in_(("ready", "quality_warning"))
        ).order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())).first()
        if not version or version.status not in ("ready", "quality_warning"):
            raise HTTPException(409, {"code": "document_not_ready", "message": f"Paper {paper_id} has no parsed document ready for search", "retryable": False, "run_id": None, "details": {"paper_id": paper_id}})
        versions.append(version)
    return versions


def index_identifier(version: DocumentVersion, embedding_model: str) -> str:
    signature = f"{version.id}:{version.parser_version}:{CHUNKER_VERSION}:{embedding_model}"
    return "idx_" + hashlib.sha256(signature.encode()).hexdigest()[:24]


async def ensure_index(db: Session, version: DocumentVersion, task_id: str | None = None,
                       embedding_batch_size: int = 10) -> SearchIndex:
    if not isinstance(embedding_batch_size, int) or not 1 <= embedding_batch_size <= 10:
        raise ValueError("embedding_batch_size must be between 1 and 10")
    config = providers.config_status()
    if not config["embedding_ready"]:
        raise HTTPException(503, {"code": "embedding_provider_unavailable", "message": "Embedding provider is not configured", "retryable": False, "run_id": None, "details": None})
    model = config["embedding_model"]
    index_id = index_identifier(version, model)
    current = db.get(SearchIndex, index_id)
    if current and current.status == "ready":
        return current
    if current:
        snapshot = current
        db.query(IndexChunk).filter_by(index_id=index_id).delete()
        snapshot.dimensions = 0
        snapshot.chunk_count = 0
        snapshot.status = "building"
    else:
        snapshot = SearchIndex(id=index_id, document_version_id=version.id, parser_version=version.parser_version,
                               chunker_version=CHUNKER_VERSION, embedding_model=model, dimensions=0,
                               chunk_count=0, status="building")
        db.add(snapshot)
    db.commit()
    block_load_started = time.perf_counter()
    blocks = db.scalars(select(ParsedBlock).where(ParsedBlock.document_version_id == version.id)
                        .order_by(ParsedBlock.pdf_page_index, ParsedBlock.ordinal)).all()
    block_load_ms = round((time.perf_counter() - block_load_started) * 1000, 1)
    chunk_build_started = time.perf_counter()
    raw_blocks = [{"block_id": b.id, "pdf_page_index": b.pdf_page_index, "ordinal": b.ordinal,
                   "text": b.text, "bbox": b.bbox, "page_width": b.page_width, "page_height": b.page_height,
                   "section_path": b.section_path} for b in blocks]
    if task_id:
        _update_task(db, task_id, phase="chunking")
    chunk_data = build_chunks(raw_blocks)
    if not chunk_data:
        snapshot.status = "failed"
        db.commit()
        raise HTTPException(422, {"code": "no_indexable_text", "message": "This document contains no indexable text", "retryable": False, "run_id": None, "details": None})
    generation = f"{version.parser_version}:{CHUNKER_VERSION}"
    chunk_ids = [stable_chunk_id(version.id, chunk["ordinal"], chunk["content_hash"], generation)
                 for chunk in chunk_data]
    content_hashes = [chunk["content_hash"] for chunk in chunk_data]
    chunk_build_ms = round((time.perf_counter() - chunk_build_started) * 1000, 1)
    ordinal_base = int(hashlib.sha256(generation.encode()).hexdigest()[:10], 16) * 1000
    request_count = 0
    cached_count = 0
    started = time.monotonic()
    indexing_timings = {"parsed_block_load_ms": block_load_ms, "chunk_build_ms": chunk_build_ms}
    embedding_batches: list[dict] = []
    try:
        # Persist deterministic chunks before remote calls so a retry can reuse
        # successful vectors without reparsing or recomputing prior batches.
        phase_started = time.perf_counter()
        for chunk, chunk_id in zip(chunk_data, chunk_ids, strict=True):
            stored_chunk = db.get(Chunk, chunk_id)
            if not stored_chunk:
                db.add(Chunk(id=chunk_id, document_version_id=version.id,
                             ordinal=ordinal_base + chunk["ordinal"], text=chunk["text"],
                             content_hash=chunk["content_hash"], token_count=chunk["token_count"],
                             section_path=chunk["section_path"], spans=chunk["spans"]))
            else:
                stored_chunk.text = chunk["text"]
                stored_chunk.content_hash = chunk["content_hash"]
                stored_chunk.token_count = chunk["token_count"]
                stored_chunk.section_path = chunk["section_path"]
                stored_chunk.spans = chunk["spans"]
        db.commit()
        indexing_timings["chunk_persist_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)

        phase_started = time.perf_counter()
        cached_rows = db.execute(select(Chunk.content_hash, ChunkVector.dimensions, ChunkVector.vector)
                                 .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
                                 .where(ChunkVector.embedding_model == model,
                                        Chunk.content_hash.in_(set(content_hashes)))).all()
        cache_candidates: dict[str, list[tuple[int, list]]] = {}
        for row in cached_rows:
            if _valid_stored_vector(row.dimensions, row.vector):
                cache_candidates.setdefault(row.content_hash, []).append((row.dimensions, row.vector))
        cached = {}
        for content_hash, candidates in cache_candidates.items():
            # Conflicting cache rows for an immutable model/content identity are
            # not safe to reuse; a fresh successful response can repair the link.
            if all(value == candidates[0] for value in candidates[1:]):
                cached[content_hash] = candidates[0]
        cached_count = len(content_hashes) - len([value for value in content_hashes if value not in cached])
        # A content hash may be cached under a different paper's chunk ID. Materialize
        # the cached vector for this immutable chunk before deciding what to request.
        for chunk_id, content_hash in zip(chunk_ids, content_hashes, strict=True):
            if content_hash not in cached:
                continue
            dimensions, vector = cached[content_hash]
            existing_vector = db.scalars(select(ChunkVector).where(
                ChunkVector.chunk_id == chunk_id, ChunkVector.embedding_model == model)).first()
            if existing_vector and _valid_stored_vector(existing_vector.dimensions, existing_vector.vector):
                continue
            if existing_vector:
                existing_vector.dimensions = dimensions
                existing_vector.vector = vector
            else:
                vector_id = "vec_" + hashlib.sha256(f"{chunk_id}:{model}".encode()).hexdigest()[:24]
                db.add(ChunkVector(id=vector_id, chunk_id=chunk_id, embedding_model=model,
                                   dimensions=dimensions, vector=vector))
        db.commit()
        indexing_timings["vector_cache_lookup_and_link_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)
        missing_hashes = list(dict.fromkeys(value for value in content_hashes if value not in cached))
        indexing_timings["cache_hit_vectors"] = cached_count
        indexing_timings["vectors_to_request"] = len(missing_hashes)
        text_by_hash = {chunk["content_hash"]: chunk["text"] for chunk in chunk_data}
        if task_id:
            _update_task(db, task_id, phase="indexing",
                         progress={"completed_vectors": len(set(chunk_ids)) - len(missing_hashes),
                                   "total_vectors": len(set(chunk_ids)),
                                   "missing_vectors": len(missing_hashes),
                                   "batch_size": embedding_batch_size, "index_status": "building",
                                   "ready": False})
        current_batch_hashes: list[str] = []
        for offset in range(0, len(missing_hashes), embedding_batch_size):
            batch_hashes = missing_hashes[offset:offset + embedding_batch_size]
            current_batch_hashes = batch_hashes
            request_count += 1
            if task_id:
                _update_task(db, task_id, phase="embedding", external_call_started=True,
                             progress={"completed_vectors": len(set(chunk_ids)) - len(missing_hashes),
                                       "total_vectors": len(set(chunk_ids)),
                                       "missing_vectors": len(missing_hashes),
                                       "batch_size": embedding_batch_size, "current_batch": request_count,
                                       "index_status": "building", "ready": False})
            batch_started = time.perf_counter()
            batch_status = "succeeded"
            batch_exception = None
            # Clear only the current async task's previous provider metadata; mocked
            # providers keep the existing call signature and simply report no trace.
            providers.take_embedding_diagnostics()
            try:
                batch_vectors, actual_model = await providers.embed_texts([text_by_hash[key] for key in batch_hashes])
            except BaseException as exc:
                batch_status = "failed_or_unknown"
                batch_exception = type(exc).__name__
                raise
            finally:
                provider_requests = providers.take_embedding_diagnostics()
                batch_record = {"batch_index": request_count, "input_count": len(batch_hashes),
                                "elapsed_ms": round((time.perf_counter() - batch_started) * 1000, 1),
                                "status": batch_status, "exception_type": batch_exception}
                if provider_requests:
                    batch_record["provider_requests"] = provider_requests
                embedding_batches.append(batch_record)
            if actual_model != model or len(batch_vectors) != len(batch_hashes):
                raise RuntimeError("embedding provider identity or response shape changed during indexing")
            if any(not isinstance(vector, list) or not vector
                   or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector)
                   for vector in batch_vectors):
                raise RuntimeError("embedding provider returned an invalid vector")
            batch_dimensions = {len(vector) for vector in batch_vectors}
            if len(batch_dimensions) != 1:
                raise RuntimeError("embedding provider returned inconsistent vector dimensions")
            persist_batch_started = time.perf_counter()
            for content_hash, vector in zip(batch_hashes, batch_vectors, strict=True):
                linked_ids = dict.fromkeys(chunk_ids[i] for i, value in enumerate(content_hashes) if value == content_hash)
                for chunk_id in linked_ids:
                    existing_vector = db.scalars(select(ChunkVector).where(
                        ChunkVector.chunk_id == chunk_id, ChunkVector.embedding_model == model)).first()
                    if existing_vector and not _valid_stored_vector(existing_vector.dimensions, existing_vector.vector):
                        existing_vector.dimensions = len(vector)
                        existing_vector.vector = vector
                    elif not existing_vector:
                        vector_id = "vec_" + hashlib.sha256(f"{chunk_id}:{model}".encode()).hexdigest()[:24]
                        db.add(ChunkVector(id=vector_id, chunk_id=chunk_id, embedding_model=model,
                                           dimensions=len(vector), vector=vector))
            # Commit each completed provider batch: interrupted retries reuse it.
            db.commit()
            embedding_batches[-1]["vector_persist_ms"] = round((time.perf_counter() - persist_batch_started) * 1000, 1)
            present_count = db.scalar(select(func.count(func.distinct(ChunkVector.chunk_id)))
                                      .join(Chunk, Chunk.id == ChunkVector.chunk_id)
                                      .where(Chunk.document_version_id == version.id,
                                             ChunkVector.embedding_model == model)) or 0
            if task_id:
                _update_task(db, task_id, phase="embedding",
                             progress={"completed_vectors": present_count,
                                       "total_vectors": len(set(chunk_ids)),
                                       "missing_vectors": max(0, len(set(chunk_ids)) - present_count),
                                       "batch_size": embedding_batch_size, "current_batch": request_count,
                                       "index_status": "building", "ready": False})
            current_batch_hashes = []
        indexing_timings["embedding_batches"] = embedding_batches
        indexing_timings["embedding_total_ms"] = round(sum(x["elapsed_ms"] for x in embedding_batches), 1)
        phase_started = time.perf_counter()
        cached_rows = db.execute(select(Chunk.id, ChunkVector.dimensions, ChunkVector.vector)
                                 .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
                                 .where(Chunk.id.in_(chunk_ids), ChunkVector.embedding_model == model)).all()
        vectors_by_chunk = {row.id: (row.dimensions, row.vector) for row in cached_rows}
        if len(vectors_by_chunk) != len(set(chunk_ids)):
            raise RuntimeError("not all document chunks have a cached vector")
        dimensions = {value[0] for value in vectors_by_chunk.values()}
        if len(dimensions) != 1:
            raise RuntimeError("stored and newly generated vectors have inconsistent dimensions")
        if any(not _valid_stored_vector(dimension, vector)
               for dimension, vector in vectors_by_chunk.values()):
            raise RuntimeError("stored or generated vector contents failed integrity validation")
        for chunk_id in dict.fromkeys(chunk_ids):
            if not db.get(IndexChunk, {"index_id": index_id, "chunk_id": chunk_id}):
                db.add(IndexChunk(index_id=index_id, chunk_id=chunk_id))
        db.flush()
        linked_chunk_ids = set(db.scalars(select(IndexChunk.chunk_id).where(
            IndexChunk.index_id == index_id)).all())
        if linked_chunk_ids != set(chunk_ids):
            raise RuntimeError("index chunk links failed completeness validation")
        if task_id:
            _update_task(db, task_id, phase="persisting")
        snapshot.embedding_model = model
        snapshot.dimensions = next(iter(dimensions))
        snapshot.chunk_count = len(set(chunk_ids))
        snapshot.status = "ready"
        version = db.get(DocumentVersion, version.id)
        quality = dict(version.quality or {})
        quality["indexing"] = {"index_id": index_id, "embedding_model": model,
                               "dimensions": snapshot.dimensions, "chunk_count": snapshot.chunk_count,
                               "embedding_requests": request_count if model != "local-hash-v1" else 0,
                               "vector_generation_batches": request_count,
                               "embedding_texts_requested": len(missing_hashes),
                               "embedding_tokens": None,
                               "elapsed_ms": int((time.monotonic() - started) * 1000),
                               "reused_cached_vectors": len(content_hashes) - len(missing_hashes)}
        version.quality = quality
        indexing_timings["index_link_prepare_ms"] = round((time.perf_counter() - phase_started) * 1000, 1)
        quality["indexing"]["timings_ms"] = indexing_timings
        version.quality = quality
        db.commit()
        if task_id:
            _update_task(db, task_id, phase="persisting",
                         progress={"completed_vectors": len(set(chunk_ids)),
                                   "total_vectors": len(set(chunk_ids)), "missing_vectors": 0,
                                   "batch_size": embedding_batch_size, "index_status": "ready",
                                   "ready": True})
    except HTTPException:
        db.rollback()
        snapshot = db.get(SearchIndex, index_id)
        if snapshot:
            snapshot.status = "failed"
            version = db.get(DocumentVersion, version.id)
            quality = dict(version.quality or {})
            quality["indexing_attempt"] = {"embedding_model": model,
                                           "embedding_requests": request_count if model != "local-hash-v1" else 0,
                                           "vector_generation_batches": request_count,
                                           "embedding_tokens": None,
                                           "elapsed_ms": int((time.monotonic() - started) * 1000),
                                           "timings_ms": {**indexing_timings,
                                                          "embedding_batches": embedding_batches,
                                                          "embedding_total_ms": round(sum(x["elapsed_ms"] for x in embedding_batches), 1)},
                                           "status": "failed"}
            version.quality = quality
            db.commit()
        raise
    except providers.EmbeddingProviderError as exc:
        db.rollback()
        vectors_present = db.scalar(select(func.count(func.distinct(ChunkVector.chunk_id)))
                                            .join(Chunk, Chunk.id == ChunkVector.chunk_id)
                                            .where(Chunk.document_version_id == version.id,
                                                   ChunkVector.embedding_model == model)) or 0
        vectors_total = db.scalar(select(func.count()).select_from(Chunk).where(
            Chunk.document_version_id == version.id)) or 0
        snapshot = db.get(SearchIndex, index_id)
        if snapshot:
            snapshot.status = "failed"
            version = db.get(DocumentVersion, version.id)
            quality = dict(version.quality or {})
            quality["indexing_attempt"] = {"embedding_model": model,
                                           "embedding_requests": request_count if model != "local-hash-v1" else 0,
                                           "embedding_tokens": None,
                                           "cached_vectors": cached_count,
                                           "elapsed_ms": int((time.monotonic() - started) * 1000),
                                           "timings_ms": {**indexing_timings,
                                                          "embedding_batches": embedding_batches,
                                                          "embedding_total_ms": round(sum(x["elapsed_ms"] for x in embedding_batches), 1)},
                                           "status": "outcome_unknown" if exc.outcome_unknown else "failed",
                                           "reason": exc.reason,
                                           "batch_index": request_count,
                                           "batch_size": len(current_batch_hashes) if current_batch_hashes else None,
                                           "vectors_present": vectors_present,
                                           "vectors_missing": max(0, vectors_total - vectors_present)}
            version.quality = quality
            db.commit()
        if task_id:
            _update_task(db, task_id, phase="embedding",
                         progress={"completed_vectors": vectors_present, "total_vectors": vectors_total,
                                   "missing_vectors": max(0, vectors_total - vectors_present),
                                   "batch_size": embedding_batch_size, "current_batch": request_count,
                                   "index_status": "outcome_unknown" if exc.outcome_unknown else "failed",
                                   "ready": False})
        logger.warning("document_embedding_failed version_id=%s reason=%s status=%s batch=%s outcome_unknown=%s",
                       version.id, exc.reason, exc.status_code, request_count, exc.outcome_unknown)
        raise HTTPException(503 if exc.retryable else 422,
                            {"code": "embedding_failed", "message": "Embedding request failed; review task status before retrying",
                             "retryable": exc.retryable, "run_id": None,
                             "details": {**exc.diagnostics, "reason": exc.reason, "status_code": exc.status_code,
                                         "outcome_unknown": exc.outcome_unknown,
                                         "batch_index": request_count,
                                         "embedding_requests": request_count if model != "local-hash-v1" else 0,
                                         "cached_vectors": cached_count,
                                         "vectors_present": vectors_present,
                                         "vectors_missing": max(0, vectors_total - vectors_present),
                                         "total_vectors": vectors_total}}) from exc
    except Exception as exc:
        db.rollback()
        snapshot = db.get(SearchIndex, index_id)
        if snapshot:
            snapshot.status = "failed"
            version = db.get(DocumentVersion, version.id)
            quality = dict(version.quality or {})
            quality["indexing_attempt"] = {"embedding_model": model,
                                           "embedding_requests": request_count if model != "local-hash-v1" else 0,
                                           "vector_generation_batches": request_count,
                                           "embedding_tokens": None,
                                           "elapsed_ms": int((time.monotonic() - started) * 1000),
                                           "timings_ms": {**indexing_timings,
                                                          "embedding_batches": embedding_batches,
                                                          "embedding_total_ms": round(sum(x["elapsed_ms"] for x in embedding_batches), 1)},
                                           "status": "failed"}
            version.quality = quality
            db.commit()
        logger.warning("document_index_failed version_id=%s exception_type=%s", version.id, type(exc).__name__)
        raise HTTPException(500, {"code": "indexing_failed", "message": "Indexing failed locally; no automatic retry", "retryable": False, "run_id": None, "details": {"reason": type(exc).__name__, "outcome_unknown": request_count > 0,
                            "embedding_requests": request_count if model != "local-hash-v1" else 0,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)}}) from exc
    return db.get(SearchIndex, index_id)


async def retrieve(db: Session, query: str, paper_ids: list[str], top_k: int,
                   document_version_ids: list[str] | None = None,
                   timings: dict | None = None,
                   parse_snapshot_ids: list[str | None] | None = None,
                   retrieval_mode: Literal["hybrid", "bm25"] = "hybrid") -> tuple[list[dict], list[DocumentVersion]]:
    total_started = time.perf_counter()
    scope_started = time.perf_counter()
    timings = timings if timings is not None else {}
    if retrieval_mode not in {"hybrid", "bm25"}:
        raise HTTPException(422, {"code": "invalid_retrieval_mode", "message": "Unsupported retrieval mode",
                                  "retryable": False, "run_id": None, "details": None})
    timings["retrieval_mode"] = retrieval_mode
    timings["query_embedding_requests"] = 0 if retrieval_mode == "bm25" else 1
    require_query_scope(query, paper_ids, db)
    if top_k < 1 or top_k > 20:
        raise HTTPException(422, {"code": "invalid_top_k", "message": "top_k must be between 1 and 20", "retryable": False, "run_id": None, "details": None})
    if document_version_ids is None:
        versions = selected_versions(db, paper_ids)
    else:
        if len(document_version_ids) != len(paper_ids) or len(set(document_version_ids)) != len(paper_ids):
            raise HTTPException(422, "Provide exactly one distinct fixed document version per paper")
        versions = []
        for paper_id, version_id in zip(paper_ids, document_version_ids):
            fixed = db.get(DocumentVersion, version_id)
            if not fixed or fixed.paper_id != paper_id or fixed.status not in ("ready", "quality_warning"):
                raise HTTPException(422, {"code": "invalid_fixed_version", "message": "Selected document version is unavailable or belongs to another paper", "retryable": False, "run_id": None, "details": {"paper_id": paper_id}})
            versions.append(fixed)
    if parse_snapshot_ids is not None and (
            len(parse_snapshot_ids) != len(paper_ids) or
            (not any(parse_snapshot_ids))):
        raise HTTPException(422, {"code": "invalid_parse_snapshot_scope",
                                  "message": "Provide one explicit snapshot ID or null per selected paper",
                                  "retryable": False, "run_id": None, "details": None})
    timings["scope_validation_ms"] = round((time.perf_counter() - scope_started) * 1000, 1)
    all_chunks: list[dict] = []
    vectors: dict[str, list[float]] = {}
    dimensions_by_model: dict[str, int] = {}
    load_started = time.perf_counter()
    for version in versions:
        selected_snapshot_id = None
        if parse_snapshot_ids is not None:
            paper_position = paper_ids.index(version.paper_id)
            selected_snapshot_id = parse_snapshot_ids[paper_position]
        if selected_snapshot_id:
            from .parse_snapshot_store import _index_is_selectable
            selected_parse = db.get(ParseSnapshot, selected_snapshot_id)
            if (not selected_parse or selected_parse.document_version_id != version.id
                    or selected_parse.source_sha256 != version.sha256 or selected_parse.status != "ready"):
                raise HTTPException(422, {"code": "invalid_parse_snapshot_scope",
                                          "message": "Selected parse snapshot does not match the pinned PDF version",
                                          "retryable": False, "run_id": None,
                                          "details": {"paper_id": version.paper_id,
                                                      "document_version_id": version.id}})
            expected_model = providers.config_status()["embedding_model"]
            snapshot_index = db.scalars(select(ParseSnapshotIndex).where(
                ParseSnapshotIndex.parse_snapshot_id == selected_parse.id,
                ParseSnapshotIndex.embedding_model == expected_model,
                ParseSnapshotIndex.status == "ready").order_by(
                    ParseSnapshotIndex.created_at.desc(), ParseSnapshotIndex.id)).first()
            if (not snapshot_index or snapshot_index.document_version_id != version.id
                    or snapshot_index.dimensions <= 0 or not _index_is_selectable(db, snapshot_index)):
                raise HTTPException(409, {"code": "snapshot_index_not_ready",
                                          "message": "Selected parse snapshot has no complete compatible ready index",
                                          "retryable": False, "run_id": None,
                                          "details": {"parse_snapshot_id": selected_parse.id}})
            snapshot_chunks = db.scalars(select(ParseSnapshotChunk).where(
                ParseSnapshotChunk.parse_snapshot_id == selected_parse.id,
                ParseSnapshotChunk.chunker_generation == snapshot_index.chunker_generation).order_by(
                    ParseSnapshotChunk.ordinal)).all()
            links = db.scalars(select(ParseSnapshotChunkVector).where(
                ParseSnapshotChunkVector.embedding_model == snapshot_index.embedding_model)).all()
            link_by_chunk = {link.parse_snapshot_chunk_id: link for link in links}
            paper = db.get(Paper, version.paper_id)
            for chunk in snapshot_chunks:
                link = link_by_chunk.get(chunk.id)
                vector_row = db.get(ParseSnapshotVector, link.vector_id) if link else None
                if (not link or link.content_hash != chunk.content_hash or
                        link.dimensions != snapshot_index.dimensions or not vector_row or
                        vector_row.content_hash != chunk.content_hash or
                        vector_row.embedding_model != snapshot_index.embedding_model or
                        vector_row.dimensions != snapshot_index.dimensions or
                        not isinstance(vector_row.vector, list) or
                        len(vector_row.vector) != snapshot_index.dimensions):
                    raise HTTPException(409, {"code": "snapshot_vector_incomplete",
                                              "message": "Selected snapshot index contains an invalid vector mapping",
                                              "retryable": False, "run_id": None,
                                              "details": {"parse_snapshot_id": selected_parse.id}})
                row = {"id": chunk.id, "paper_id": paper.id, "title": paper.title,
                       "document_version_id": version.id, "source_version": version.source_version,
                       "sha256": version.sha256, "text": chunk.text, "ordinal": chunk.ordinal,
                       "section_path": chunk.section_path, "spans": chunk.spans,
                       "content_hash": chunk.content_hash, "parse_snapshot_id": selected_parse.id}
                all_chunks.append(row)
                vectors[chunk.id] = vector_row.vector
            dimensions_by_model[snapshot_index.embedding_model] = snapshot_index.dimensions
            continue
        elif document_version_ids is None:
            snapshot = await ensure_index(db, version)
        else:
            expected_model = providers.config_status()["embedding_model"]
            snapshot = db.get(SearchIndex, index_identifier(version, expected_model))
            if not snapshot or snapshot.status != "ready":
                raise HTTPException(409, {"code": "fixed_version_index_missing", "message": "Selected fixed version has no ready index for the configured embedding model", "retryable": False, "run_id": None, "details": {"document_version_id": version.id}})
        dimensions_by_model[snapshot.embedding_model] = snapshot.dimensions
        rows = db.execute(select(Chunk, ChunkVector.vector).join(IndexChunk, IndexChunk.chunk_id == Chunk.id)
                          .join(ChunkVector, ChunkVector.chunk_id == Chunk.id)
                          .where(IndexChunk.index_id == snapshot.id, ChunkVector.embedding_model == snapshot.embedding_model)
                          .order_by(Chunk.ordinal)).all()
        paper = db.get(Paper, version.paper_id)
        for chunk, vector in rows:
            row = {"id": chunk.id, "paper_id": paper.id, "title": paper.title,
                   "document_version_id": version.id, "source_version": version.source_version,
                   "sha256": version.sha256, "text": chunk.text, "ordinal": chunk.ordinal,
                   "section_path": chunk.section_path, "spans": chunk.spans, "content_hash": chunk.content_hash}
            all_chunks.append(row)
            vectors[chunk.id] = vector
    timings["index_and_chunk_load_ms"] = round((time.perf_counter() - load_started) * 1000, 1)
    if not all_chunks:
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
        return [], versions
    bm25_started = time.perf_counter()
    bm25 = bm25_scores(query, all_chunks)[:30]
    timings["bm25_ms"] = round((time.perf_counter() - bm25_started) * 1000, 1)
    dense = []
    if retrieval_mode == "hybrid":
        embedding_started = time.perf_counter()
        try:
            query_vectors, model = await providers.embed_texts([query])
            if model not in dimensions_by_model or model != providers.config_status()["embedding_model"]:
                raise RuntimeError("Query embedding identity does not match the selected index snapshot")
            if dimensions_by_model[model] != len(query_vectors[0]):
                raise RuntimeError("Query embedding dimensions do not match the selected index snapshot")
            if any(chunk["id"] not in vectors for chunk in all_chunks):
                raise RuntimeError("Missing stored vectors")
        except Exception as exc:
            timings["query_embedding_ms"] = round((time.perf_counter() - embedding_started) * 1000, 1)
            timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
            timings["query_embedding_status"] = "failed_or_unknown"
            failure = ({**exc.diagnostics, "reason": exc.reason, "status_code": exc.status_code,
                        "outcome_unknown": exc.outcome_unknown}
                       if isinstance(exc, providers.EmbeddingProviderError) else {"reason": "internal_embedding_error"})
            failure["retrieval_timings_ms"] = dict(timings)
            retryable = exc.retryable if isinstance(exc, providers.EmbeddingProviderError) else False
            raise HTTPException(503, {"code": "query_embedding_failed", "message": "Could not embed the query", "retryable": retryable, "run_id": None, "details": failure}) from exc
        timings["query_embedding_ms"] = round((time.perf_counter() - embedding_started) * 1000, 1)
        timings["query_embedding_status"] = "succeeded"
        dense_started = time.perf_counter()
        dense = dense_scores(query_vectors[0], all_chunks, vectors)[:30]
        timings["dense_ms"] = round((time.perf_counter() - dense_started) * 1000, 1)
        fusion_started = time.perf_counter()
        fused = reciprocal_rank_fusion({"bm25": bm25, "dense": dense}, k=60, limit=min(20, len(all_chunks)))
    else:
        timings["query_embedding_ms"] = 0
        timings["query_embedding_status"] = "not_used_by_bm25_baseline"
        timings["dense_ms"] = 0
        fusion_started = time.perf_counter()
        fused = [{"index": idx, "rrf_score": score, "sources": {"bm25": score},
                  "ranks": {"bm25": rank}}
                 for rank, (idx, score) in enumerate(bm25, 1)]
    # Preserve representation for every selected paper before filling remaining slots globally.
    chosen: list[dict] = []
    remaining = fused[:]
    per_paper_max = max(1, top_k // len(versions))
    for version in versions:
        count = 0
        for item in remaining:
            chunk = all_chunks[item["index"]]
            if chunk["paper_id"] == version.paper_id and count < per_paper_max:
                chosen.append({**chunk, "rrf_score": item["rrf_score"], "ranks": item["ranks"], "sources": item["sources"]})
                count += 1
    chosen_ids = {item["id"] for item in chosen}
    for item in remaining:
        chunk = all_chunks[item["index"]]
        if chunk["id"] not in chosen_ids and len(chosen) < top_k:
            chosen.append({**chunk, "rrf_score": item["rrf_score"], "ranks": item["ranks"], "sources": item["sources"]})
    timings["ranked_candidates_top10"] = [
        {"rank": rank, "chunk_id": all_chunks[item["index"]]["id"],
         "paper_id": all_chunks[item["index"]]["paper_id"],
         "document_version_id": all_chunks[item["index"]]["document_version_id"],
         "bm25_rank": next((i for i, (idx, _) in enumerate(bm25, 1) if idx == item["index"]), None),
         "bm25_score": next((float(score) for idx, score in bm25 if idx == item["index"]), None),
         "dense_rank": next((i for i, (idx, _) in enumerate(dense, 1) if idx == item["index"]), None),
         "dense_score": next((float(score) for idx, score in dense if idx == item["index"]), None),
         "rrf_score": float(item["rrf_score"])}
        for rank, item in enumerate(fused[:10], 1)
    ]
    timings["fusion_and_scope_ms"] = round((time.perf_counter() - fusion_started) * 1000, 1)
    timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 1)
    return sorted(chosen, key=lambda item: (-item["rrf_score"], item["paper_id"], item["ordinal"])), versions


def normalize_quote(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    # PDF text extraction may preserve end-of-line word-division hyphens
    # (e.g. ``retriev-\nal``), while a model quotes the reconstructed word.
    # Join only a hyphen immediately before a line break and a lowercase
    # letter; ordinary in-line compounds remain distinct.
    value = re.sub(r"(?<=[a-z])[-\u00ad]\s*\r?\n\s*(?=[a-z])", "", value)
    value = value.replace("\u00ad", "")
    return " ".join(value.split())


def _persist_task_retrieval_snapshot(db: Session, task_id: str | None, timings: dict) -> None:
    """Persist bounded rank metadata before any Chat call can fail.

    Candidate text is intentionally excluded. This is a child detail of the
    active phase entry, so existing phase-trace consumers remain compatible.
    """
    if not task_id:
        return
    task = db.get(OperationTask, task_id)
    if not task:
        return
    trace = list(task.phase_trace or [])
    phase_index = next((index for index in range(len(trace) - 1, -1, -1)
                        if trace[index].get("phase") == "retrieving"
                        and trace[index].get("ended_at") is None), None)
    if phase_index is None:
        return
    phase = dict(trace[phase_index])
    phase["retrieval_snapshot"] = {
        "status": "completed",
        "retrieval_mode": timings.get("retrieval_mode", "hybrid"),
        "elapsed_ms": timings.get("total_ms"),
        "query_embedding_ms": timings.get("query_embedding_ms"),
        "ranked_candidates_top10": list(timings.get("ranked_candidates_top10") or []),
    }
    trace[phase_index] = phase
    task.phase_trace = trace
    task.updated_at = _utcnow()
    db.commit()


def validate_citations(result: dict, retrieved: list[dict]) -> list[dict]:
    allowed = {item["evidence_id"]: item for item in retrieved}
    claims = result.get("claims")
    if not isinstance(claims, list):
        raise HTTPException(502, {"code": "invalid_model_response", "message": "Model response claims must be an array", "retryable": False, "run_id": None, "details": None})
    validated = []
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not isinstance(claim.get("quote"), str) or not isinstance(claim.get("evidence_ids"), list):
            raise HTTPException(502, {"code": "invalid_model_response", "message": "Each claim requires text, quote, and evidence_ids", "retryable": False, "run_id": None, "details": None})
        ids = claim["evidence_ids"]
        if not ids or any(evidence_id not in allowed for evidence_id in ids):
            raise HTTPException(502, {"code": "citation_validation_failed", "message": "A claim cites evidence outside the retrieved set", "retryable": False, "run_id": None, "details": None})
        quote = normalize_quote(claim["quote"])
        if not quote or not any(quote in normalize_quote(allowed[evidence_id]["text"]) for evidence_id in ids):
            raise HTTPException(502, {"code": "citation_validation_failed", "message": "A citation quote is not an exact normalized substring of its cited source", "retryable": False, "run_id": None, "details": None})
        validated.append({"text": claim["text"], "evidence_ids": ids, "quote": claim["quote"], "quote_valid": True,
                          "location_valid": all(bool(allowed[evidence_id]["spans"]) for evidence_id in ids),
                          "support_status": "pending"})
    return validated


def apply_support_results(claims: list[dict], result: dict) -> list[dict]:
    """Strictly validate verifier output; malformed/omitted judgments stay unverified."""
    allowed = {"supported", "partially_supported", "unsupported", "conflicting"}
    indexed = {item.get("claim_index"): item for item in result.get("results", [])
               if isinstance(item, dict) and isinstance(item.get("claim_index"), int)} if isinstance(result, dict) else {}
    output = []
    for index, claim in enumerate(claims):
        judgment = indexed.get(index)
        valid = (isinstance(judgment, dict) and judgment.get("status") in allowed
                 and isinstance(judgment.get("reason"), str)
                 and isinstance(judgment.get("evidence_ids"), list)
                 and set(judgment["evidence_ids"]).issubset(set(claim["evidence_ids"])))
        output.append({**claim,
                       "support_status": judgment["status"] if valid else "unverified",
                       "support_verification": "automated" if valid else "unverified",
                       "support_reason": judgment["reason"] if valid else "Verifier output missing or invalid",
                       "support_evidence_ids": judgment["evidence_ids"] if valid else []})
    return output


def should_repair_claims(claims: list[dict], repair_attempts: int) -> bool:
    return repair_attempts < 1 and any(c.get("support_status") in {"partially_supported", "unsupported", "conflicting"} for c in claims)


def should_repair_completeness(completeness: dict, claims: list[dict], revision_attempts: int) -> bool:
    """Revise a structural omission only when every current claim is supported and budget remains."""
    return (revision_attempts < 1 and completeness.get("status") == "incomplete" and bool(claims)
            and all(claim.get("support_status") == "supported" for claim in claims))


def repair_percentage_point_units(question: str, claims: list[dict]) -> tuple[list[dict], list[dict]]:
    """Correct a mislabeled absolute difference when cited claims jointly prove it.

    The operands and the reported margin can occur in separate claims: table
    values are often one claim while the paper's prose margin is another. We
    therefore require a unique operand pair in the selected evidence set and
    require both operands to appear in cited claim text before changing a unit.
    """
    if not any(term in question.casefold() for term in ("百分点", "percentage point")):
        return claims, []
    percent = re.compile(r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)\s*%")
    numeric = re.compile(r"(?<![\w.])(?P<value>\d+(?:\.\d+)?)(?:\s*%)?")
    difference_pattern = r"差|相差|差值|差额|提高|提升|增加|下降|高出|difference|gap|margin|spread|delta|improv|increase|decreas|higher|lower"
    output = []
    repairs = []
    cited_pairs: set[tuple[float, float]] = set()
    operand_pairs_in_claims: set[tuple[float, float]] = set()
    for item in claims:
        text_values = [float(match.group("value")) for match in numeric.finditer(item.get("text", ""))]
        for i, left in enumerate(text_values):
            for right in text_values[i + 1:]:
                operand_pairs_in_claims.add(tuple(sorted((round(left, 8), round(right, 8)))))
    for item in claims:
        quote_values = [float(match.group("value")) for match in numeric.finditer(item.get("quote", ""))]
        for i, left in enumerate(quote_values):
            for right in quote_values[i + 1:]:
                if round(abs(left - right), 8) in {
                    round(float(match.group("value")), 8)
                    for claim in claims
                    if re.search(difference_pattern, claim.get("text", ""), re.I)
                    for match in percent.finditer(claim.get("text", ""))
                }:
                    cited_pairs.add(tuple(sorted((round(left, 8), round(right, 8)))))
    for index, claim in enumerate(claims):
        text = claim.get("text", "")
        quote = claim.get("quote", "")
        if not re.search(difference_pattern, text, re.I):
            output.append(claim)
            continue
        text_values = [float(match.group("value")) for match in percent.finditer(text)]
        replacements = []
        for match in percent.finditer(text):
            stated = float(match.group("value"))
            matching_pairs = [pair for pair in cited_pairs
                              if round(abs(pair[0] - pair[1]), 8) == round(stated, 8)
                              and pair in operand_pairs_in_claims]
            if len(matching_pairs) != 1:
                continue
            suffix = text[match.end():]
            if re.match(r"\s*(个百分点|percentage\s+points?)", suffix, re.I):
                continue
            replacements.append((match.start(), match.end(), match.group("value") + " 个百分点"))
        corrected = text
        for start, end, replacement in reversed(replacements):
            corrected = corrected[:start] + replacement + corrected[end:]
        output.append({**claim, "text": corrected})
        if corrected != text:
            repairs.append({"claim_index": index, "before": text, "after": corrected,
                            "evidence_ids": list(claim.get("evidence_ids", [])),
                            "reason": "A unique operand pair in the cited evidence set proves the absolute difference."})
    return output, repairs


def render_claims_answer(claims: list[dict]) -> str:
    return "\n".join(f"{claim['text']} " + " ".join(f"[{item}]" for item in claim["evidence_ids"])
                     for claim in claims) if claims else "在所选论文中未找到可用于回答的原文证据。"


def retain_verified_claims(claims: list[dict]) -> list[dict]:
    retained = []
    for claim in claims:
        if claim.get("support_status") in {"unsupported", "conflicting"}:
            continue
        if claim.get("support_status") in {"partially_supported", "unverified"}:
            claim = {**claim, "text": claim["text"] + "（证据不足，未能核实完整结论）"}
        retained.append(claim)
    return retained


def assess_question_completeness(question: str, answer: str) -> dict:
    """Conservative structural check for explicit decisions; does not assess evidence support or correctness."""
    q = question.casefold()
    asks_decision = any(term in q for term in (
        "能据此", "是否", "能否", "可以断言", "可否", "can we", "can this", "does this support", "is it possible"
    ))
    if not asks_decision:
        return {"status": "not_assessed", "check": "decision_and_reason_presence"}
    decision = any(term in answer.casefold() for term in (
        "不能", "不可以", "无法", "不可", "不是", "并非", "并不是", "并未", "未获胜", "不支持",
        "不能据此", "否", "可以", "能够", "cannot", "not directly", "no,", "yes,"
    ))
    lowered_answer = answer.casefold()
    process_sequence = bool(re.search(r"(?:先|首先).{0,100}(?:再|然后|接着)", lowered_answer))
    reason = process_sequence or any(term in lowered_answer for term in (
        "因为", "由于", "原因", "指标", "口径", "数据集", "实验设置", "不同", "而是",
        "顺序", "because", "metric", "dataset", "setting", "rather than"
    ))
    gaps = []
    requirements = []
    if not decision:
        gaps.append("explicit_decision")
        requirements.append("Give a direct, explicit answer to the original question. For every yes/no or comparison part, state the conclusion or say that the evidence does not justify one.")
    if not reason:
        gaps.append("explanation")
        requirements.append("Explain the conclusion using the supplied evidence and name the relevant limitation or reason.")
    return {"status": "complete" if not gaps else "incomplete", "check": "decision_and_reason_presence",
            "missing_parts": gaps, "missing_requirements": requirements}


@contextmanager
def session():
    with Session(engine) as db:
        yield db


def _request_fingerprint(payload: dict) -> str:
    packed = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _schedule_operation(task_id: str) -> None:
    current = ACTIVE_TASKS.get(task_id)
    if current and not current.done():
        return
    runner = asyncio.create_task(_run_operation_task(task_id), name=f"papertrail-{task_id}")
    ACTIVE_TASKS[task_id] = runner
    runner.add_done_callback(lambda _task, key=task_id: ACTIVE_TASKS.pop(key, None))


async def _enqueue_operation(kind: str, payload: dict, idempotency_key: str) -> dict:
    if not idempotency_key or len(idempotency_key) > 120:
        raise HTTPException(400, {"code": "invalid_idempotency_key", "message": "Idempotency-Key must contain 1-120 characters",
                                  "retryable": False, "run_id": None, "details": None})
    digest = _request_fingerprint(payload)
    with Session(engine) as db:
        existing = db.scalars(select(OperationTask).where(
            OperationTask.kind == kind, OperationTask.idempotency_key == idempotency_key)).first()
        if existing:
            if existing.request_hash != digest:
                raise HTTPException(409, {"code": "idempotency_key_reused", "message": "This key was already used for a different request",
                                          "retryable": False, "run_id": existing.id, "details": None})
            task = existing
        else:
            created_at = _utcnow()
            task = OperationTask(id="task_" + uuid.uuid4().hex, kind=kind,
                                 idempotency_key=idempotency_key, request_hash=digest,
                                 request_payload=payload, status="waiting", phase="waiting",
                                 created_at=created_at, updated_at=created_at,
                                 phase_trace=[{"phase": "waiting", "started_at": created_at.isoformat(),
                                               "ended_at": None, "elapsed_ms": None}])
            db.add(task)
            try:
                db.commit()
            except Exception:
                db.rollback()
                # A concurrent duplicate may have won the unique-key race.
                existing = db.scalars(select(OperationTask).where(
                    OperationTask.kind == kind, OperationTask.idempotency_key == idempotency_key)).first()
                if not existing:
                    raise
                if existing.request_hash != digest:
                    raise HTTPException(409, {"code": "idempotency_key_reused", "message": "This key was already used for a different request",
                                              "retryable": False, "run_id": existing.id, "details": None})
                task = existing
        data = _task_data(task, db)
    if data["status"] == "waiting":
        _schedule_operation(data["task_id"])
    return data


async def _run_operation_task(task_id: str) -> None:
    try:
        async with TASK_SEMAPHORE:
            with Session(engine) as db:
                task = db.get(OperationTask, task_id)
                if not task or task.status != "waiting":
                    return
                if task.cancel_requested:
                    _update_task(db, task_id, status="cancelled", phase="cancelled", finished=True)
                    return
                task.kind  # load before commit/refresh boundaries
                kind = task.kind
                payload = dict(task.request_payload)
                committed_result_id = payload.get("document_version_id") if kind == "document_ingestion" else None
                _update_task(db, task_id, status="running", phase="validating",
                             result_id=committed_result_id, started=True)
            if kind == "qa":
                result = await _execute_qa(AskRequest.model_validate(payload), task_id=task_id)
                result_id = result["run_id"]
            elif kind == "comparison_report":
                result = await _execute_comparison_report(ComparisonReportCreateRequest.model_validate(payload), task_id=task_id)
                result_id = result["report_id"]
            elif kind == "document_ingestion":
                result = await _execute_document_ingestion(payload, task_id=task_id)
                result_id = result["document_version_id"]
            else:
                raise RuntimeError("Unknown persisted task kind")
            with Session(engine) as db:
                _update_task(db, task_id, status="succeeded", phase="completed", result_id=result_id, finished=True)
    except asyncio.CancelledError:
        with Session(engine) as db:
            task = db.get(OperationTask, task_id)
            if task and task.status in {"waiting", "running"}:
                prior_phase = task.phase
                if _task_result_exists(db, task):
                    _update_task(db, task_id, status="succeeded", phase="completed", finished=True, error=None)
                    return
                if task.status == "waiting" or not task.external_call_started:
                    status = "cancelled" if task.cancel_requested else "interrupted"
                    message = "任务已在外部请求前停止；没有自动重试。" if status == "cancelled" else "服务停止前任务未开始；没有自动重试。"
                else:
                    status = "outcome_unknown"
                    message = "任务停止时外部请求可能已被处理；没有自动重试。请先检查历史记录。"
                detail = {"recovery": "manual_review_required" if status == "outcome_unknown" else "safe_to_resubmit",
                          "previous_phase": prior_phase}
                _update_task(db, task_id, status=status, phase="interrupted", finished=True,
                             error={"code": "task_interrupted", "message": message, "retryable": False,
                                    "details": detail})
                _set_ingestion_terminal(db, task, status)
                db.commit()
        return
    except Exception as exc:
        terminal, error = _task_error_from_exception(exc)
        with Session(engine) as db:
            task = db.get(OperationTask, task_id)
            if not task:
                return
            failed_phase = task.phase
            if _task_result_exists(db, task):
                _update_task(db, task_id, status="succeeded", phase="completed", finished=True, error=None)
            else:
                if task.external_call_started and terminal == "failed" and not isinstance(exc, HTTPException):
                    terminal = "outcome_unknown"
                    error = {"code": "operation_result_uncertain",
                             "message": "任务在外部请求后发生内部错误，模型服务可能已处理请求；系统未自动重试。请先检查历史记录。",
                             "retryable": False, "details": {"exception_type": type(exc).__name__,
                                                               "recovery": "manual_review_required",
                                                               "failed_phase": failed_phase}}
                else:
                    error.setdefault("details", {})["failed_phase"] = failed_phase
                _update_task(db, task_id, status=terminal, phase=failed_phase, error=error, finished=True)
                _set_ingestion_terminal(db, task, terminal)
                db.commit()


def _operation_task(task_id: str) -> OperationTask:
    with Session(engine) as db:
        task = db.get(OperationTask, task_id)
        if not task:
            raise HTTPException(404, {"code": "task_not_found", "message": "Task not found",
                                      "retryable": False, "run_id": None, "details": None})
        db.expunge(task)
        return task


@app.get("/api/v1/health")
def health_check():
    with Session(engine) as db:
        db.execute(select(1)).scalar_one()
        pending = db.scalar(select(func.count()).select_from(OperationTask).where(
            OperationTask.status.in_(("waiting", "running")))) or 0
    return {"status": "ok", "database": "ok", "active_tasks": pending,
            "version": app.version, "schema_version": CURRENT_SCHEMA_VERSION,
            "build_id": os.getenv("PAPERTRAIL_API_BUILD_ID", "unmanaged")}


@app.get("/api/v1/version")
def runtime_version():
    """Safe build/schema identity for local launcher and frontend proxy checks."""
    return {"application": "PaperTrail API", "version": app.version,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "build_id": os.getenv("PAPERTRAIL_API_BUILD_ID", "unmanaged")}


@app.get("/api/v1/tasks")
def list_operation_tasks(limit: int = Query(20, ge=1, le=100)):
    with Session(engine) as db:
        rows = db.scalars(select(OperationTask).order_by(OperationTask.created_at.desc()).limit(limit)).all()
        return {"items": [_task_data(row, db) for row in rows]}


@app.get("/api/v1/tasks/by-idempotency/{kind}/{idempotency_key}")
def get_operation_by_idempotency(kind: str, idempotency_key: str):
    if kind not in {"qa", "comparison_report", "document_ingestion"}:
        raise HTTPException(404, "Task kind not found")
    with Session(engine) as db:
        task = db.scalars(select(OperationTask).where(
            OperationTask.kind == kind, OperationTask.idempotency_key == idempotency_key)).first()
        if not task:
            raise HTTPException(404, {"code": "task_not_found", "message": "Task not found",
                                      "retryable": False, "run_id": None, "details": None})
        return _task_data(task, db)


@app.get("/api/v1/tasks/{task_id}")
def get_operation_task(task_id: str):
    with Session(engine) as db:
        task = db.get(OperationTask, task_id)
        if not task:
            raise HTTPException(404, {"code": "task_not_found", "message": "Task not found",
                                      "retryable": False, "run_id": None, "details": None})
        return _task_data(task, db)


@app.post("/api/v1/tasks/{task_id}/cancel")
def cancel_operation_task(task_id: str):
    with Session(engine) as db:
        task = db.get(OperationTask, task_id)
        if not task:
            raise HTTPException(404, {"code": "task_not_found", "message": "Task not found",
                                      "retryable": False, "run_id": None, "details": None})
        if task.status not in {"waiting", "running"}:
            return _task_data(task, db)
        task.cancel_requested = True
        runner = ACTIVE_TASKS.get(task_id)
        if task.status == "waiting" and not task.external_call_started:
            task.status = "cancelled"
            task.phase = "cancelled"
            task.finished_at = _utcnow()
            _set_ingestion_terminal(db, task, "cancelled")
        task.updated_at = _utcnow()
        db.commit()
        result = _task_data(task, db)
    if runner and not runner.done():
        runner.cancel()
    return result


def version_data(v: DocumentVersion) -> dict:
    try:
        file_size_bytes = Path(v.path).stat().st_size
    except OSError:
        file_size_bytes = None
    return {"id": v.id, "paper_id": v.paper_id, "source_version": v.source_version, "source_url": v.source_url,
            "sha256": v.sha256, "status": v.status, "parser_version": v.parser_version,
            "page_count": v.page_count, "block_count": v.block_count, "quality": v.quality, "error": v.error,
            "file_size_bytes": file_size_bytes, "created_at": v.created_at.isoformat()}


def paper_data(p: Paper) -> dict:
    return {"id": p.id, "title": p.title, "authors": p.authors, "arxiv_id": p.arxiv_id,
            "source_url": p.source_url, "source": p.source, "original_filename": p.original_filename,
            "status": p.status, "versions": [version_data(v) for v in p.versions]}


PARSER_VERSION = f"pymupdf-{fitz.VersionBind}-section-v3"
NUMBER = re.compile(r"^([1-9]\d?(?:\.[1-9]\d?){0,3})$", re.I)
NAMED_HEADING = re.compile(r"^(abstract|references|appendix)$", re.I)


def can_reuse_parsed_blocks(version: DocumentVersion, stored_blocks: int, target_parser: str) -> bool:
    quality = version.quality if isinstance(version.quality, dict) else {}
    complete = quality.get("parse_complete")
    completed_parse = (
        isinstance(complete, dict)
        and complete.get("parser_version") == target_parser
        and complete.get("page_count") == version.page_count
        and complete.get("block_count") == version.block_count
    ) or isinstance(quality.get("indexing_attempt"), dict)
    return (version.parser_version == target_parser
            and version.page_count > 0
            and version.block_count > 0
            and stored_blocks == version.block_count
            and completed_parse)


def heading_of(raw: str) -> str | None:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) == 1 and NAMED_HEADING.fullmatch(lines[0]):
        return lines[0]
    if (len(lines) == 2 and NUMBER.fullmatch(lines[0]) and re.match(r"^[A-Za-z]", lines[1])
            and 4 <= len(lines[1]) < 90 and not any(mark in lines[1] for mark in ("=", "_", "{", "}"))):
        words = re.findall(r"[A-Za-z]+", lines[1])
        stopwords = {"and", "of", "to", "the", "in", "for", "with", "on", "a", "an"}
        if all(word[0].isupper() or word.lower() in stopwords for word in words):
            return f"{lines[0]} {lines[1]}"
    return None


def parse_pdf(db: Session, version: DocumentVersion, task_id: str | None = None) -> None:
    parse_started = time.perf_counter()
    previous_indexing = (version.quality or {}).get("indexing") if isinstance(version.quality, dict) else None
    native_extract_ms = 0
    block_persist_ms = 0
    commit_ms = 0
    version.status = "parsing"
    version.block_count = 0
    version.page_count = 0
    version.quality = {}
    db.commit()
    try:
        empty_pages = []
        table_pages = []
        ocr_pages = []
        ocr_page_details = {}
        section: list[str] = []
        with fitz.open(version.path) as doc:
            version.page_count = len(doc)
            for page_index, page in enumerate(doc):
                phase_started = time.perf_counter()
                native_blocks = [b for b in page.get_text("blocks") if len(b) >= 7 and b[6] == 0 and b[4].strip()]
                native_extract_ms += (time.perf_counter() - phase_started) * 1000
                blocks = [{"text": b[4], "bbox": b[:4], "kind": "text"} for b in native_blocks]
                if not blocks and ocr.enabled():
                    if len(ocr_page_details) >= ocr.max_pages():
                        raise ocr.OCRProcessingError("OCR page limit exceeded")
                    if task_id:
                        _update_task(db, task_id, phase="ocr")
                    recognized, detail = ocr.extract_page(page)
                    ocr_page_details[str(page_index)] = detail
                    if recognized:
                        ocr_pages.append(page_index)
                        blocks = [{"text": line["text"], "bbox": line["bbox"], "kind": "ocr_text"}
                                  for line in recognized]
                if not blocks:
                    empty_pages.append(page_index)
                # Preserve PyMuPDF's source order; geometric y sorting interleaves two columns.
                if any(re.search(r"\btable\s+\d+\b", b["text"], re.I) for b in blocks):
                    table_pages.append(page_index)
                phase_started = time.perf_counter()
                for ordinal, b in enumerate(blocks):
                    raw = b["text"]
                    heading = heading_of(raw)
                    if heading:
                        match = re.match(r"^(\d+)(?:\.(\d+))?", heading)
                        if match and match.group(2) and section and section[0].startswith(match.group(1) + " "):
                            section = section[:1] + [heading]
                        else:
                            section = [heading]
                    box = [round(float(x), 2) for x in b["bbox"]]
                    digest = hashlib.sha256(f"{version.id}:{page_index}:{ordinal}:{raw}".encode()).hexdigest()[:24]
                    db.add(ParsedBlock(id=f"blk_{digest}", document_version_id=version.id,
                                       pdf_page_index=page_index, ordinal=ordinal, text=raw, bbox=box,
                                       page_width=float(page.rect.width), page_height=float(page.rect.height),
                                       section_path=section.copy(), kind=b["kind"]))
                    version.block_count += 1
                block_persist_ms += (time.perf_counter() - phase_started) * 1000
            quality = {"empty_text_pages": empty_pages, "possible_table_pages": table_pages,
                       "limits": ["Table cell and row relationships are not reconstructed.",
                                  "Reading order may be wrong in multi-column pages.",
                                  "Section headings are heuristic; unknown paths remain empty."]}
            if ocr.enabled():
                quality.update({"ocr_engine": ocr.OCR_VERSION, "ocr_pages": ocr_pages,
                                "ocr_page_details": ocr_page_details})
                if ocr_pages:
                    quality["limits"].append("OCR text and line boxes are machine recognized, not verified source text.")
                quality["ocr_timing_ms"] = {
                    "render": sum(int(x.get("render_ms", 0)) for x in ocr_page_details.values()),
                    "engine_initialization": sum(int(x.get("engine_init_ms") or 0) for x in ocr_page_details.values()),
                    "inference": sum(int(x.get("ocr_ms", 0)) for x in ocr_page_details.values()),
                }
            quality["parse_timing_ms"] = {
                "native_text_extraction": round(native_extract_ms),
                "block_construction_and_orm_add": round(block_persist_ms),
                "total_before_commit": round((time.perf_counter() - parse_started) * 1000),
            }
            quality["parse_complete"] = {"parser_version": version.parser_version,
                                          "page_count": version.page_count,
                                          "block_count": version.block_count}
            configured_model = providers.config_status().get("embedding_model")
            if (isinstance(previous_indexing, dict) and configured_model
                    and previous_indexing.get("index_id") == index_identifier(version, configured_model)):
                quality["indexing"] = previous_indexing
            version.quality = quality
            version.status = "quality_warning" if empty_pages or ocr_pages else "ready"
            phase_started = time.perf_counter()
            db.commit()
            commit_ms = round((time.perf_counter() - phase_started) * 1000)
            quality = dict(version.quality or {})
            # JSON columns are not mutable-tracked; copy the nested timing map
            # before editing so SQLAlchemy sees the post-commit fields as dirty.
            quality["parse_timing_ms"] = dict(quality.get("parse_timing_ms") or {})
            quality["parse_timing_ms"]["final_commit"] = commit_ms
            quality["parse_timing_ms"]["total"] = round((time.perf_counter() - parse_started) * 1000)
            version.quality = quality
            db.commit()
    except Exception as exc:
        db.rollback()
        version = db.get(DocumentVersion, version.id)
        version.status = "failed"
        version.error = str(exc)
        db.commit()
        raise


def _parse_uploaded_version(version_id: str, task_id: str | None = None) -> None:
    # OCR can take minutes; keep the API event loop free for task polling.
    with Session(engine) as db:
        version = db.get(DocumentVersion, version_id)
        if version is None:
            raise RuntimeError("Uploaded document disappeared before parsing")
        parse_pdf(db, version, task_id=task_id)


async def _execute_document_ingestion(payload: dict, task_id: str) -> dict:
    """Parse and index one immutable user-uploaded PDF; never scans directories."""
    version_id = payload.get("document_version_id", "")
    paper_id = payload.get("paper_id", "")
    with session() as db:
        version = db.get(DocumentVersion, version_id)
        paper = db.get(Paper, paper_id)
        if not version or not paper or paper.source != "user_upload" or version.paper_id != paper_id:
            raise HTTPException(404, {"code": "uploaded_document_not_found", "message": "Uploaded document is unavailable",
                                      "retryable": False, "run_id": None, "details": None})
        if version.sha256 != payload.get("sha256"):
            raise HTTPException(409, {"code": "upload_identity_changed", "message": "Uploaded document identity changed",
                                      "retryable": False, "run_id": None, "details": None})
        path = Path(version.path)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != version.sha256:
            raise HTTPException(409, {"code": "stored_pdf_integrity_failed", "message": "Stored PDF failed its SHA-256 check",
                                      "retryable": False, "run_id": None, "details": None})
        latest_version_id = db.scalar(select(DocumentVersion.id).where(
            DocumentVersion.paper_id == paper.id).order_by(
                DocumentVersion.created_at.desc(), DocumentVersion.id.desc()).limit(1))
        is_latest = latest_version_id == version.id
        if is_latest:
            paper.status = "processing"
        target_parser = ocr.parser_identity(PARSER_VERSION)
        stored_blocks = db.scalar(select(func.count()).select_from(ParsedBlock).where(
            ParsedBlock.document_version_id == version.id,
            func.length(func.trim(ParsedBlock.text)) > 0)) or 0
        reusable_parse = can_reuse_parsed_blocks(version, stored_blocks, target_parser)
        version.error = None
        if reusable_parse:
            version.status = "indexing"
            if is_latest:
                paper.status = "processing"
            db.commit()
            _update_task(db, task_id, phase="indexing")
        else:
            version.status = "parsing"
            db.query(ParsedBlock).filter_by(document_version_id=version.id).delete()
            version.block_count = 0
            version.page_count = 0
            version.quality = {}
            version.parser_version = target_parser
            db.commit()
            _update_task(db, task_id, phase="parsing")
            try:
                await asyncio.to_thread(_parse_uploaded_version, version_id, task_id)
            except Exception as exc:
                db.expire_all()
                version = db.get(DocumentVersion, version_id)
                paper = db.get(Paper, paper_id)
                version.status = "failed"
                version.error = f"parse_failed:{type(exc).__name__}"
                if is_latest:
                    paper.status = "failed"
                db.commit()
                if isinstance(exc, ocr.OCRUnavailable):
                    raise HTTPException(503, {"code": "ocr_unavailable", "message": "Local OCR is enabled but its runtime or models are unavailable",
                                              "retryable": False, "run_id": None,
                                              "details": {"exception_type": type(exc).__name__}}) from exc
                raise HTTPException(422, {"code": "pdf_parse_failed", "message": "PDF could not be parsed",
                                          "retryable": False, "run_id": None,
                                          "details": {"exception_type": type(exc).__name__}}) from exc
        db.expire_all()
        version = db.get(DocumentVersion, version_id)
        paper = db.get(Paper, paper_id)
        text_blocks = db.scalar(select(func.count()).select_from(ParsedBlock).where(
            ParsedBlock.document_version_id == version_id, func.length(func.trim(ParsedBlock.text)) > 0)) or 0
        if version.page_count <= 0 or not text_blocks:
            version.status = "failed"
            version.error = "no_extractable_text"
            if is_latest:
                paper.status = "failed"
            db.commit()
            message = ("PDF has no native text and OCR is disabled" if not ocr.enabled()
                       else "OCR did not recover sufficiently reliable text from this PDF")
            raise HTTPException(422, {"code": "no_extractable_text", "message": message,
                                      "retryable": False, "run_id": None,
                                      "details": {"page_count": version.page_count,
                                                  "ocr_status": "disabled" if not ocr.enabled() else "low_quality"}})
        version.status = "indexing"
        if is_latest:
            paper.status = "processing"
        db.commit()
        # User PDFs use smaller provider batches than fixed-manifest imports.
        # Chunk text, model identity, and vector dimensions remain unchanged.
        index = await ensure_index(db, version, task_id=task_id, embedding_batch_size=5)
        _update_task(db, task_id, phase="persisting")
        version = db.get(DocumentVersion, version_id)
        paper = db.get(Paper, paper_id)
        # Parsing can report quality_warning for pages without text; indexing still
        # succeeds only when at least one non-empty chunk was committed.
        version.status = "quality_warning" if (version.quality.get("empty_text_pages") or
                                                version.quality.get("ocr_pages")) else "ready"
        latest_version_id = db.scalar(select(DocumentVersion.id).where(
            DocumentVersion.paper_id == paper.id).order_by(
                DocumentVersion.created_at.desc(), DocumentVersion.id.desc()).limit(1))
        if latest_version_id == version.id:
            paper.status = version.status
        version.error = None
        db.commit()
        return {"document_version_id": version.id, "paper_id": paper.id,
                "index_id": index.id, "status": version.status}


def import_paper(paper_id: str, content: bytes | None = None) -> dict:
    item = manifest_papers().get(paper_id)
    if not item:
        raise HTTPException(404, "Paper not listed in manifest")
    source = (ROOT / item["pdf_path"]).resolve()
    if not source.is_relative_to((ROOT / "data/samples/papers").resolve()):
        raise HTTPException(400, "Invalid manifest path")
    content = content if content is not None else source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != item["sha256"] or not content.startswith(b"%PDF-"):
        raise HTTPException(422, "PDF does not match the immutable manifest version")
    vid = f"doc_{paper_id}_{digest[:16]}"
    with session() as db:
        existing = db.get(DocumentVersion, vid)
        if existing:
            if existing.status in ("failed", "parsing") or existing.parser_version != PARSER_VERSION:
                db.query(ParsedBlock).filter_by(document_version_id=vid).delete()
                existing.block_count = 0
                existing.error = None
                existing.parser_version = PARSER_VERSION
                db.commit()
                parse_pdf(db, existing)
            return {"deduplicated": True, "version": version_data(existing)}
        paper = db.get(Paper, paper_id)
        if not paper:
            paper = Paper(id=paper_id, title=item["title"], authors=item["authors"], arxiv_id=item["arxiv_id"], source_url=item["abstract_url"])
            db.add(paper)
        stored = DATA_DIR / "pdf" / f"{digest}.pdf"
        stored.parent.mkdir(parents=True, exist_ok=True)
        if not stored.exists():
            stored.write_bytes(content)
        version = DocumentVersion(id=vid, paper_id=paper_id, source_version=item["version"],
                                  source_url=item["pdf_url"], sha256=digest, path=str(stored),
                                  status="queued", parser_version=PARSER_VERSION)
        db.add(version)
        db.commit()
        parse_pdf(db, version)
        return {"deduplicated": False, "version": version_data(version)}


def _upload_display_filename(value: str | None) -> str:
    raw = unicodedata.normalize("NFKC", (value or "document.pdf").replace("\\", "/").split("/")[-1])
    raw = "".join(ch for ch in raw if ch >= " " and ch not in '<>:"|?*')
    raw = raw.strip(" .")[:255]
    if not raw or not raw.lower().endswith(".pdf"):
        raise HTTPException(422, {"code": "invalid_filename", "message": "Upload must have a .pdf filename",
                                  "retryable": False, "run_id": None, "details": None})
    return raw


def _find_ingestion_task(db: Session, version_id: str) -> OperationTask | None:
    return db.scalars(select(OperationTask).where(
        OperationTask.kind == "document_ingestion",
        OperationTask.request_payload["document_version_id"].as_string() == version_id
    ).order_by(OperationTask.created_at.desc())).first()


def _enqueue_payload_for_version(version: DocumentVersion) -> dict:
    return {"paper_id": version.paper_id, "document_version_id": version.id, "sha256": version.sha256}


async def _register_user_pdf(filename: str, content: bytes, idempotency_key: str | None) -> dict:
    digest = hashlib.sha256(content).hexdigest()
    display_name = _upload_display_filename(filename)
    stem = Path(display_name).stem.strip()[:240] or "uploaded-paper"
    with Session(engine) as db:
        version = db.scalars(select(DocumentVersion).join(Paper).where(
            DocumentVersion.sha256 == digest, Paper.source == "user_upload")).first()
        if version:
            paper = db.get(Paper, version.paper_id)
            deduplicated = True
        else:
            identity = hashlib.sha256(unicodedata.normalize("NFKC", stem).casefold().encode("utf-8")).hexdigest()[:24]
            paper_id = f"user_{identity}"
            paper = db.get(Paper, paper_id)
            if paper and paper.source != "user_upload":
                raise HTTPException(409, {"code": "paper_identity_collision", "message": "Paper identity conflicts with a fixed manifest entry",
                                          "retryable": False, "run_id": None, "details": None})
            count = db.scalar(select(func.count()).select_from(DocumentVersion).where(
                DocumentVersion.paper_id == paper_id)) or 0
            version_id = f"doc_{paper_id}_{digest[:20]}"
            deduplicated = False
            payload = {"paper_id": paper_id, "document_version_id": version_id, "sha256": digest}
        if version:
            payload = _enqueue_payload_for_version(version)
        key = idempotency_key or f"upload-{digest}"
        if any(ord(ch) < 33 for ch in key) or len(key) > 120:
            raise HTTPException(400, {"code": "invalid_idempotency_key", "message": "Idempotency-Key must contain 1-120 visible characters",
                                      "retryable": False, "run_id": None, "details": None})
        existing_key = db.scalars(select(OperationTask).where(
            OperationTask.kind == "document_ingestion", OperationTask.idempotency_key == key)).first()
        if existing_key and existing_key.request_hash != _request_fingerprint(payload):
            raise HTTPException(409, {"code": "idempotency_key_reused", "message": "This key was already used for a different upload",
                                      "retryable": False, "run_id": existing_key.id, "details": None})
        if not version:
            if not paper:
                paper = Paper(id=paper_id, title=stem, authors=[], arxiv_id="", source_url="",
                              source="user_upload", original_filename=display_name, status="queued")
                db.add(paper)
            storage = DATA_DIR / "user_uploads" / f"{digest}.pdf"
            storage.parent.mkdir(parents=True, exist_ok=True)
            if storage.exists():
                if hashlib.sha256(storage.read_bytes()).hexdigest() != digest:
                    raise HTTPException(409, {"code": "stored_pdf_integrity_failed", "message": "Stored upload failed its SHA-256 check",
                                              "retryable": False, "run_id": None, "details": None})
            else:
                temporary = storage.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(content)
                    os.replace(temporary, storage)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            version = DocumentVersion(id=version_id, paper_id=paper_id, source_version=f"v{count + 1}",
                                      source_url="", sha256=digest, path=str(storage), status="queued",
                                      parser_version=ocr.parser_identity(PARSER_VERSION))
            db.add(version)
            paper.status = "queued"
            paper.original_filename = display_name
            db.commit()
        existing_task = _find_ingestion_task(db, version.id)
        paper_snapshot = paper_data(paper)
        version_snapshot = version_data(version)
        existing_task_id = existing_task.id if existing_task else None
    if existing_task_id:
        with Session(engine) as db:
            task_data = _task_data(db.get(OperationTask, existing_task_id), db)
    else:
        task_data = await _enqueue_operation("document_ingestion", payload, key)
    return {"deduplicated": deduplicated, "paper": paper_snapshot,
            "version": version_snapshot, "task": task_data}


@app.get("/api/v1/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/manifest")
def get_manifest():
    return [{"paper_id": p["paper_id"], "title": p["title"], "source_version": p["version"]} for p in manifest_papers().values()]


@app.post("/api/v1/papers/import/{paper_id}")
def import_from_manifest(paper_id: str):
    return import_paper(paper_id)


@app.post("/api/v1/papers/upload")
async def upload(file: Annotated[UploadFile, File()]):
    content = await file.read()
    if len(content) > 40 * 1024 * 1024:
        raise HTTPException(413, "PDF too large")
    digest = hashlib.sha256(content).hexdigest()
    matches = [p for p in manifest_papers().values() if p["sha256"] == digest]
    if not matches:
        raise HTTPException(422, "Only registered manifest PDF versions can be imported")
    return import_paper(matches[0]["paper_id"], content)


@app.post("/api/v1/user-papers/upload", status_code=202)
async def upload_user_pdf(file: Annotated[UploadFile, File()],
                          idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    filename = _upload_display_filename(file.filename)
    if file.content_type not in {None, "", "application/pdf", "application/x-pdf", "application/octet-stream"}:
        raise HTTPException(415, {"code": "unsupported_media_type", "message": "Expected a PDF upload",
                                  "retryable": False, "run_id": None, "details": None})
    content = await file.read(40 * 1024 * 1024 + 1)
    if len(content) > 40 * 1024 * 1024:
        raise HTTPException(413, {"code": "pdf_too_large", "message": "PDF exceeds the 40 MiB upload limit",
                                  "retryable": False, "run_id": None, "details": None})
    if not content.startswith(b"%PDF-"):
        raise HTTPException(415, {"code": "invalid_pdf_content", "message": "File content is not a PDF",
                                  "retryable": False, "run_id": None, "details": None})
    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            if document.needs_pass or not 1 <= document.page_count <= 5000:
                raise ValueError("invalid page count or encrypted PDF")
    except Exception as exc:
        raise HTTPException(422, {"code": "invalid_pdf", "message": "PDF is invalid, encrypted, or has an unsupported page count",
                                  "retryable": False, "run_id": None, "details": {"exception_type": type(exc).__name__}}) from exc
    return await _register_user_pdf(filename, content, idempotency_key)


@app.post("/api/v1/user-papers/{paper_id}/versions/{version_id}/retry", status_code=202)
async def retry_user_pdf_processing(paper_id: str, version_id: str,
                                    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
                                    confirm_unknown: Annotated[bool, Header(alias="X-Confirm-Outcome-Unknown-Retry")] = False):
    with Session(engine) as db:
        paper = db.get(Paper, paper_id)
        version = db.get(DocumentVersion, version_id)
        if not paper or paper.source != "user_upload" or not version or version.paper_id != paper_id:
            raise HTTPException(404, {"code": "uploaded_document_not_found", "message": "Uploaded document is unavailable",
                                      "retryable": False, "run_id": None, "details": None})
        prior = _find_ingestion_task(db, version_id)
        if prior and prior.status in {"waiting", "running"}:
            raise HTTPException(409, {"code": "processing_in_progress", "message": "A processing task is already active",
                                      "retryable": False, "run_id": prior.id, "details": None})
        if version.status in {"ready", "quality_warning"}:
            raise HTTPException(409, {"code": "document_already_ready", "message": "This document is already indexed",
                                      "retryable": False, "run_id": None, "details": None})
        if prior and prior.status == "outcome_unknown" and not confirm_unknown:
            raise HTTPException(409, {"code": "outcome_unknown_confirmation_required",
                                      "message": "The previous embedding request may have been processed; confirm an explicit retry",
                                      "retryable": False, "run_id": prior.id,
                                      "details": {"header": "X-Confirm-Outcome-Unknown-Retry: true"}})
        if not idempotency_key or len(idempotency_key) > 120 or any(ord(ch) < 33 for ch in idempotency_key):
            raise HTTPException(400, {"code": "invalid_idempotency_key", "message": "Idempotency-Key must contain 1-120 visible characters",
                                      "retryable": False, "run_id": None, "details": None})
        payload = _enqueue_payload_for_version(version)
        existing_key = db.scalars(select(OperationTask).where(
            OperationTask.kind == "document_ingestion",
            OperationTask.idempotency_key == idempotency_key)).first()
        if existing_key:
            if existing_key.request_hash != _request_fingerprint(payload):
                raise HTTPException(409, {"code": "idempotency_key_reused",
                                          "message": "This key was already used for a different processing request",
                                          "retryable": False, "run_id": existing_key.id, "details": None})
            return _task_data(existing_key, db)
        version.status = "queued"
        version.error = None
        latest_version_id = db.scalar(select(DocumentVersion.id).where(
            DocumentVersion.paper_id == paper.id).order_by(
                DocumentVersion.created_at.desc(), DocumentVersion.id.desc()).limit(1))
        if latest_version_id == version.id:
            paper.status = "queued"
        db.commit()
    return await _enqueue_operation("document_ingestion", payload, idempotency_key)


@app.get("/api/v1/papers")
def list_papers():
    with session() as db:
        return [paper_data(p) for p in db.scalars(select(Paper).order_by(Paper.id)).all()]


@app.get("/api/v1/papers/{paper_id}")
def get_paper(paper_id: str):
    with session() as db:
        p = db.get(Paper, paper_id)
        if not p:
            raise HTTPException(404, "Paper not found")
        return paper_data(p)


@app.get("/api/v1/documents/{version_id}/parse-snapshots")
def list_parse_snapshots(version_id: str):
    with session() as db:
        if not db.get(DocumentVersion, version_id):
            raise HTTPException(404, "Version not found")
        rows = db.scalars(select(ParseSnapshot).where(ParseSnapshot.document_version_id == version_id)
                          .order_by(ParseSnapshot.created_at.desc(), ParseSnapshot.id)).all()
        return [{"parse_snapshot_id": row.id, "document_version_id": row.document_version_id,
                 "source_sha256": row.source_sha256, "parser_identity": row.parser_identity,
                 "protocol_version": row.protocol_version, "status": row.status,
                 "page_count": row.page_count, "block_count": row.block_count,
                 "quality": row.quality, "created_at": row.created_at.isoformat()} for row in rows]


@app.get("/api/v1/documents/{version_id}/parse-snapshots/{parse_snapshot_id}")
def get_parse_snapshot(version_id: str, parse_snapshot_id: str):
    with session() as db:
        snapshot = db.get(ParseSnapshot, parse_snapshot_id)
        version = db.get(DocumentVersion, version_id)
        if (not snapshot or not version or snapshot.document_version_id != version_id
                or snapshot.source_sha256 != version.sha256 or snapshot.status != "ready"):
            raise HTTPException(404, "Parse snapshot not found for this document version")
        return {"parse_snapshot_id": snapshot.id, "document_version_id": snapshot.document_version_id,
                "source_sha256": snapshot.source_sha256, "parser_identity": snapshot.parser_identity,
                "protocol_version": snapshot.protocol_version, "status": snapshot.status,
                "page_count": snapshot.page_count, "block_count": snapshot.block_count,
                "quality": snapshot.quality, **snapshot.structure_data}


@app.get("/api/v1/documents/{version_id}/parse-snapshots/{parse_snapshot_id}/indexes")
def list_parse_snapshot_indexes(version_id: str, parse_snapshot_id: str):
    with session() as db:
        snapshot = db.get(ParseSnapshot, parse_snapshot_id)
        if not snapshot or snapshot.document_version_id != version_id:
            raise HTTPException(404, "Parse snapshot not found for this document version")
        rows = db.scalars(select(ParseSnapshotIndex).where(
            ParseSnapshotIndex.parse_snapshot_id == parse_snapshot_id).order_by(
            ParseSnapshotIndex.created_at.desc(), ParseSnapshotIndex.id)).all()
        selection = db.get(DocumentIndexSelection, version_id)
        return [{"index_id": row.id, "document_version_id": row.document_version_id,
                 "parse_snapshot_id": row.parse_snapshot_id, "chunker_generation": row.chunker_generation,
                 "embedding_model": row.embedding_model, "dimensions": row.dimensions,
                 "chunk_count": row.chunk_count, "status": row.status,
                 "is_active": bool(selection and selection.active_snapshot_index_id == row.id),
                 "created_at": row.created_at.isoformat()} for row in rows]


@app.get("/api/v1/documents/{version_id}/blocks")
def get_blocks(version_id: str, page: int | None = Query(None, ge=0), limit: int = Query(200, ge=1, le=1000),
               offset: int = Query(0, ge=0), parse_snapshot_id: str | None = None):
    with session() as db:
        version = db.get(DocumentVersion, version_id)
        if not version:
            raise HTTPException(404, "Version not found")
        if parse_snapshot_id is not None:
            snapshot = db.get(ParseSnapshot, parse_snapshot_id)
            if (not snapshot or snapshot.document_version_id != version_id or snapshot.status != "ready"
                    or snapshot.source_sha256 != version.sha256):
                raise HTTPException(404, "Parse snapshot not found for this document version")
            statement = select(ParseSnapshotBlock).where(ParseSnapshotBlock.parse_snapshot_id == snapshot.id)
            if page is not None:
                statement = statement.where(ParseSnapshotBlock.pdf_page_index == page)
            blocks = db.scalars(statement.order_by(ParseSnapshotBlock.pdf_page_index,
                                                   ParseSnapshotBlock.ordinal).offset(offset).limit(limit)).all()
            return [{"block_id": block.source_block_id, "document_version_id": version_id,
                     "parse_snapshot_id": snapshot.id, "parser_identity": snapshot.parser_identity,
                     "pdf_page_index": block.pdf_page_index, "ordinal": block.ordinal,
                     "text": block.text, "bbox": block.bbox, "page_width": block.page_width,
                     "page_height": block.page_height, "section_path": block.section_path,
                     "kind": block.kind, "role": block.role, "reading_order": block.reading_order,
                     "source": block.source, "quality_flags": block.quality_flags} for block in blocks]
        statement = select(ParsedBlock).where(ParsedBlock.document_version_id == version_id)
        if page is not None:
            statement = statement.where(ParsedBlock.pdf_page_index == page)
        blocks = db.scalars(statement.order_by(ParsedBlock.pdf_page_index, ParsedBlock.ordinal).offset(offset).limit(limit)).all()
        return [{"block_id": b.id, "document_version_id": b.document_version_id, "pdf_page_index": b.pdf_page_index,
                 "ordinal": b.ordinal, "text": b.text, "bbox": b.bbox, "page_width": b.page_width,
                 "page_height": b.page_height, "section_path": b.section_path, "kind": b.kind} for b in blocks]


@app.get("/api/v1/documents/{version_id}/pdf")
def get_pdf(version_id: str):
    with session() as db:
        v = db.get(DocumentVersion, version_id)
        if not v:
            raise HTTPException(404, "Version not found")
        return FileResponse(v.path, media_type="application/pdf", filename=f"{version_id}.pdf", content_disposition_type="inline")


@app.get("/api/v1/config/status")
def get_config_status():
    return {**providers.config_status(), "ocr_enabled": ocr.enabled(),
            "ocr_runtime_present": ocr.OCR_PACKAGES.is_dir() and all(
                (ocr.OCR_PACKAGES / "rapidocr" / "models" / name).is_file()
                for name in ocr.MODEL_NAMES.values())}


@app.post("/api/v1/documents/{version_id}/index")
async def build_index(version_id: str):
    with session() as db:
        version = db.get(DocumentVersion, version_id)
        if not version:
            raise HTTPException(404, "Version not found")
        if version.status not in ("ready", "quality_warning"):
            raise HTTPException(409, "Document is not parsed")
        config = providers.config_status()
        will_deduplicate = bool(config["embedding_ready"] and
                                 (existing := db.get(SearchIndex, index_identifier(version, config["embedding_model"]))) and
                                 existing.status == "ready")
        try:
            result = await ensure_index(db, version)
        except HTTPException:
            raise
        return {"index_id": result.id, "document_version_id": result.document_version_id,
                "embedding_model": result.embedding_model, "dimensions": result.dimensions,
                "chunker_version": result.chunker_version, "chunk_count": result.chunk_count,
                "status": result.status, "deduplicated": will_deduplicate}


@app.get("/api/v1/documents/{version_id}/index")
def get_index_status(version_id: str):
    with session() as db:
        if not db.get(DocumentVersion, version_id):
            raise HTTPException(404, "Version not found")
        indexes = db.scalars(select(SearchIndex).where(SearchIndex.document_version_id == version_id)
                             .order_by(SearchIndex.created_at.desc())).all()
        return [{"index_id": item.id, "embedding_model": item.embedding_model,
                 "chunker_version": item.chunker_version, "dimensions": item.dimensions,
                 "chunk_count": item.chunk_count, "status": item.status,
                 "created_at": item.created_at.isoformat()} for item in indexes]


def search_result(chunk: dict, query_id: str) -> dict:
    evidence_id = "ev_" + hashlib.sha256(f"{query_id}:{chunk['id']}".encode()).hexdigest()[:24]
    return {"evidence_id": evidence_id, "chunk_id": chunk["id"], "paper_id": chunk["paper_id"],
            "title": chunk["title"], "document_version_id": chunk["document_version_id"],
            "source_version": chunk["source_version"], "sha256": chunk["sha256"],
            "text": chunk["text"], "section_path": chunk["section_path"], "spans": chunk["spans"],
            "parse_snapshot_id": chunk.get("parse_snapshot_id"),
            "retrieval": {"rrf_score": chunk["rrf_score"], "sources": chunk["sources"], "ranks": chunk["ranks"]}}


@app.post("/api/v1/search")
async def search_evidence(request: SearchRequest):
    with session() as db:
        retrieval_query = request.retrieval_query or request.query
        chunks, versions = await retrieve(db, retrieval_query, request.paper_ids, request.top_k,
                                          parse_snapshot_ids=request.parse_snapshot_ids)
        request_id = uuid.uuid4().hex
        results = [search_result(chunk, request_id) for chunk in chunks]
        return {"query": request.query, "retrieval_query": retrieval_query, "paper_ids": request.paper_ids,
                "document_version_ids": [version.id for version in versions],
                "parse_snapshot_ids": request.parse_snapshot_ids,
        "embedding_model": providers.config_status()["embedding_model"], "results": results}


@app.post("/api/v1/qa")
async def ask_papers(request: AskRequest, prefer: Annotated[str | None, Header()] = None,
                     idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    if (prefer and "respond-async" in prefer.lower()) or idempotency_key:
        if not idempotency_key:
            raise HTTPException(400, {"code": "idempotency_key_required", "message": "Async QA requires Idempotency-Key",
                                      "retryable": False, "run_id": None, "details": None})
        task = await _enqueue_operation("qa", request.model_dump(mode="json"), idempotency_key)
        return JSONResponse(status_code=202, content=task)
    return await _execute_qa(request)


async def _timed_provider_call(call_timings: list[dict], purpose: str, awaitable):
    started = time.perf_counter()
    state = "succeeded"
    try:
        return await awaitable
    except BaseException:
        state = "failed_or_cancelled"
        raise
    finally:
        call_timings.append({"purpose": purpose, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                             "status": state})


async def _execute_qa(request: AskRequest, task_id: str | None = None):
    request_started = time.perf_counter()
    request_id = uuid.uuid4().hex
    with session() as db:
        retrieval_query = request.retrieval_query or request.query
        retrieval_timings: dict = {}
        chat_call_timings: list[dict] = []
        if task_id:
            _update_task(db, task_id, phase="retrieving", external_call_started=True)
        chunks, versions = await retrieve(db, retrieval_query, request.paper_ids, request.top_k,
                                          request.document_version_ids, timings=retrieval_timings,
                                          parse_snapshot_ids=request.parse_snapshot_ids,
                                          retrieval_mode=request.retrieval_mode)
        # Commit the actual bounded ordering before generation/validation. A
        # later provider or citation-protocol failure must not erase retrieval
        # evidence from the persisted task record.
        _persist_task_retrieval_snapshot(db, task_id, retrieval_timings)
        if not chunks:
            raise HTTPException(404, {"code": "no_evidence_found", "message": "No matching text was found in the selected papers", "retryable": False, "run_id": None, "details": None})
        run_id = "qrun_" + uuid.uuid4().hex
        if task_id:
            _update_task(db, task_id, phase="generating", result_id=run_id, external_call_started=True)
        candidates = [search_result(chunk, run_id) for chunk in chunks]
        generation_started = time.perf_counter()
        try:
            model_result, chat_model = await _timed_provider_call(
                chat_call_timings, "initial_generation",
                providers.generate_answer(request.query, candidates, request_id=task_id or request_id))
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "Configure PAPERTRAIL_LLM_API_KEY" in str(exc):
                raise HTTPException(503, {"code": "llm_not_configured", "message": "Configure PAPERTRAIL_LLM_API_KEY and PAPERTRAIL_LLM_MODEL to enable generated answers", "retryable": False, "run_id": run_id, "details": None}) from exc
            elapsed_ms = (time.perf_counter() - generation_started) * 1000
            details = providers.chat_failure_diagnostics(exc, elapsed_ms, request_id)
            retryable = exc.retryable if isinstance(exc, providers.ChatProviderError) else False
            logger.warning("qa_provider_failure request_id=%s phase=%s elapsed_ms=%s reason=%s exception_chain=%s endpoint_host=%s",
                           request_id, details["phase"], details["elapsed_ms"], details["reason"],
                           ",".join(details["exception_chain"]), details["endpoint_host"])
            raise HTTPException(502, {"code": "llm_request_failed", "message": "Chat provider request failed", "retryable": retryable, "run_id": run_id, "details": details}) from exc
        claims = validate_citations(model_result, candidates)
        baseline_answer = render_claims_answer(claims)
        trace = {"enabled": request.verify_support, "verification_type": "automated_model_judgment",
                 "repair_retrievals": 0, "rewrite_calls": 0, "verification_calls": 0,
                 "verification_attempts": 0, "model_calls": 1, "chat_requests": 1,
                 "embedding_requests": 1 if request.retrieval_mode == "hybrid" else 0,
                 "answer_revisions": 0, "completeness_repair": None, "deterministic_repairs": [],
                 "repair_queries": [], "repair_reasons": [],
                 "added_chunk_ids": [], "added_evidence": [], "verification_results": [], "events": [],
                 "retrieval_timings_ms": retrieval_timings,
                 # Preserve the complete bounded candidate ordering and source ranks;
                 # the separate context snapshot records the exact answer input.
                 "retrieval_candidates": [
                     {"rank": rank, "evidence_id": item["evidence_id"], "chunk_id": item["chunk_id"],
                      "paper_id": item["paper_id"], "document_version_id": item["document_version_id"],
                      "parse_snapshot_id": item.get("parse_snapshot_id"),
                      "retrieval": item["retrieval"]}
                     for rank, item in enumerate(candidates, start=1)
                 ],
                 # Isolated evaluation runs may need an auditable snapshot of
                 # the exact bounded context sent to the answer generator.
                 "input_context_snapshot": [
                     {"evidence_id": item["evidence_id"], "chunk_id": item["chunk_id"],
                      "paper_id": item["paper_id"], "document_version_id": item["document_version_id"],
                      "source_version": item["source_version"],
                      "pdf_page_index": (item.get("spans") or [{}])[0].get("pdf_page_index"),
                      "section_path": item["section_path"], "text": item["text"]}
                     for item in candidates
                 ],
                 "chat_call_timings": chat_call_timings,
                 "generation_input": {"candidate_chunks": len(candidates),
                                      "context_chars": sum(len(item.get("text", "")) for item in candidates),
                                      "query_chars": len(request.query)},
                 "generation_elapsed_ms": round((time.perf_counter() - generation_started) * 1000, 1)}
        trace["baseline_answer_chars"] = len(baseline_answer)
        trace["baseline_claims"] = [dict(c) for c in claims]
        claims, unit_repairs = repair_percentage_point_units(request.query, claims)
        trace["deterministic_repairs"].extend(unit_repairs)
        if unit_repairs:
            trace["events"].append({"step": "percentage_point_unit_check", "status": "corrected",
                                    "repair_count": len(unit_repairs)})
        initial_evidence_ids = {evidence_id for claim in claims for evidence_id in claim["evidence_ids"]}
        initial_quote_by_evidence = {}
        for claim in claims:
            for evidence_id in claim["evidence_ids"]:
                initial_quote_by_evidence.setdefault(evidence_id, claim["quote"])
        trace["baseline_evidence"] = [
            {"evidence_id": item["evidence_id"], "chunk_id": item["chunk_id"],
             "document_version_id": item["document_version_id"], "paper_id": item["paper_id"],
             "parse_snapshot_id": item.get("parse_snapshot_id"),
             "text": item["text"], "exact_quote": initial_quote_by_evidence[item["evidence_id"]],
             "spans": item["spans"]}
            for item in candidates if item["evidence_id"] in initial_evidence_ids
        ]
        support_started = time.perf_counter()
        final_candidates = list(candidates)
        if request.verify_support and claims:
            if task_id:
                _update_task(db, task_id, phase="verifying", external_call_started=True)
            by_id = {item["evidence_id"]: item for item in final_candidates}
            linked = [{"evidence_id": item["evidence_id"], "text": item["text"]}
                      for claim in claims for item in [by_id[eid] for eid in claim["evidence_ids"]]]
            try:
                trace["verification_attempts"] += 1
                trace["model_calls"] += 1
                trace["chat_requests"] += 1
                judged, _ = await _timed_provider_call(
                    chat_call_timings, "initial_support_check",
                    providers.verify_claim_support(
                        [{"text": c["text"], "evidence_ids": c["evidence_ids"]} for c in claims], linked))
                claims = apply_support_results(claims, judged)
                trace["verification_calls"] += 1
                trace["verification_results"].append({"round": 1, "claims": [dict(c) for c in claims]})
                trace["events"].append({"step": "initial_support_check", "status": "completed"})
            except Exception as exc:
                claims = [{**c, "support_status": "unverified", "support_verification": "unverified",
                           "support_reason": type(exc).__name__, "support_evidence_ids": []} for c in claims]
                trace["verification_results"].append({"round": 1, "claims": [dict(c) for c in claims]})
                trace["events"].append({"step": "initial_support_check", "status": "failed", "reason": type(exc).__name__})
            deficient = [c for c in claims if c.get("support_status") in {"partially_supported", "unsupported", "conflicting"}]
            if deficient and should_repair_claims(claims, trace["repair_retrievals"]):
                if task_id:
                    _update_task(db, task_id, phase="repairing", external_call_started=True)
                trace["repair_reasons"] = [{"claim": c["text"], "status": c["support_status"],
                                             "reason": c.get("support_reason", "")} for c in deficient]
                deficient_linked = [{"evidence_id": eid, "text": by_id[eid]["text"]}
                                    for c in deficient for eid in c["evidence_ids"] if eid in by_id]
                try:
                    trace["rewrite_calls"] += 1
                    trace["model_calls"] += 1
                    trace["chat_requests"] += 1
                    repair_query, _ = await _timed_provider_call(
                        chat_call_timings, "repair_query_generation",
                        providers.make_repair_query(
                            [{"text": c["text"], "support_status": c["support_status"], "reason": c.get("support_reason")}
                             for c in deficient], deficient_linked))
                    # Exactly one bounded supplementary retrieval call.
                    added_by_chunk = {}
                    if repair_query.strip():
                        trace["repair_queries"].append(repair_query)
                        # Count the attempt before awaiting so failed repair embeddings remain visible.
                        trace["embedding_requests"] += 1
                        repair_retrieval_timings: dict = {}
                        more_chunks, more_versions = await retrieve(
                            db, repair_query, request.paper_ids, request.top_k,
                            request.document_version_ids, timings=repair_retrieval_timings,
                            parse_snapshot_ids=request.parse_snapshot_ids)
                        trace.setdefault("repair_retrieval_timings_ms", []).append(repair_retrieval_timings)
                        if {v.id for v in more_versions} != {v.id for v in versions}:
                            more_chunks = []
                        for chunk in more_chunks:
                            if chunk["id"] not in {item["chunk_id"] for item in final_candidates}:
                                added_by_chunk[chunk["id"]] = chunk
                        trace["repair_retrievals"] = 1
                    additions = [search_result(chunk, run_id) for chunk in added_by_chunk.values()]
                    final_candidates.extend(additions)
                    trace["added_chunk_ids"] = [item["chunk_id"] for item in additions]
                    trace["added_evidence"] = [{"evidence_id": item["evidence_id"], "chunk_id": item["chunk_id"],
                                                 "document_version_id": item["document_version_id"],
                                                 "parse_snapshot_id": item.get("parse_snapshot_id"),
                                                 "pdf_pages": sorted({span["pdf_page_index"] + 1 for span in item["spans"]
                                                                      if isinstance(span.get("pdf_page_index"), int)}),
                                                 "text": item["text"]} for item in additions]
                    if additions:
                        try:
                            trace["rewrite_calls"] += 1
                            trace["answer_revisions"] += 1
                            trace["model_calls"] += 1
                            trace["chat_requests"] += 1
                            revised, _ = await _timed_provider_call(
                                chat_call_timings, "evidence_repair_revision",
                                providers.revise_answer(request.query, baseline_answer, claims, final_candidates))
                            claims = validate_citations(revised, final_candidates)
                            claims, unit_repairs = repair_percentage_point_units(request.query, claims)
                            trace["deterministic_repairs"].extend(unit_repairs)
                            checked_map = {item["evidence_id"]: item for item in final_candidates}
                            linked2 = [{"evidence_id": checked_map[eid]["evidence_id"], "text": checked_map[eid]["text"]}
                                       for c in claims for eid in c["evidence_ids"]]
                            trace["verification_attempts"] += 1
                            trace["model_calls"] += 1
                            trace["chat_requests"] += 1
                            judged2, _ = await _timed_provider_call(
                                chat_call_timings, "revised_support_recheck",
                                providers.verify_claim_support(
                                    [{"text": c["text"], "evidence_ids": c["evidence_ids"]} for c in claims], linked2))
                            trace["verification_calls"] += 1
                            claims = apply_support_results(claims, judged2)
                            trace["verification_results"].append({"round": 2, "claims": [dict(c) for c in claims]})
                            trace["events"].append({"step": "single_repair_and_recheck", "status": "completed"})
                        except Exception as exc:
                            claims = [{**c, "support_status": "unverified", "support_verification": "unverified",
                                       "support_reason": type(exc).__name__, "support_evidence_ids": []} for c in claims]
                            trace["verification_results"].append({"round": 2, "claims": [dict(c) for c in claims]})
                            trace["events"].append({"step": "single_repair_and_recheck", "status": "failed", "reason": type(exc).__name__})
                except Exception as exc:
                    trace["events"].append({"step": "repair_query", "status": "failed", "reason": type(exc).__name__})
            # Unsupported/conflicting facts are removed; partial/unverified claims remain explicitly qualified.
            claims = retain_verified_claims(claims)
            before_completeness = assess_question_completeness(request.query, render_claims_answer(claims))
            trace["baseline_completeness"] = before_completeness
            if should_repair_completeness(before_completeness, claims, trace["answer_revisions"]):
                if task_id:
                    _update_task(db, task_id, phase="revising", external_call_started=True)
                cited = {evidence_id for claim in claims for evidence_id in claim["evidence_ids"]}
                reusable = [item for item in final_candidates if item["evidence_id"] in cited]
                trace["completeness_repair"] = {
                    "status": "started", "missing_parts": before_completeness.get("missing_parts", []),
                    "missing_requirements": before_completeness.get("missing_requirements", []),
                    "reason": "Answer structure is incomplete while all current claims are supported; reuse cited evidence without retrieval.",
                    "evidence_ids": sorted(cited), "retrievals": 0,
                }
                try:
                    trace["rewrite_calls"] += 1
                    trace["answer_revisions"] += 1
                    trace["model_calls"] += 1
                    trace["chat_requests"] += 1
                    revised, _ = await _timed_provider_call(
                        chat_call_timings, "completeness_revision",
                        providers.revise_answer(
                            request.query, render_claims_answer(claims), claims, reusable,
                            completeness_check=before_completeness))
                    revised_claims = validate_citations(revised, reusable)
                    revised_claims, unit_repairs = repair_percentage_point_units(request.query, revised_claims)
                    trace["deterministic_repairs"].extend(unit_repairs)
                    revised_by_id = {item["evidence_id"]: item for item in reusable}
                    revised_linked = [{"evidence_id": revised_by_id[eid]["evidence_id"],
                                       "text": revised_by_id[eid]["text"]}
                                      for claim in revised_claims for eid in claim["evidence_ids"]]
                    trace["verification_attempts"] += 1
                    trace["model_calls"] += 1
                    trace["chat_requests"] += 1
                    judged, _ = await _timed_provider_call(
                        chat_call_timings, "completeness_support_recheck",
                        providers.verify_claim_support(
                            [{"text": claim["text"], "evidence_ids": claim["evidence_ids"]}
                             for claim in revised_claims], revised_linked))
                    revised_claims = apply_support_results(revised_claims, judged)
                    trace["verification_calls"] += 1
                    trace["verification_results"].append({"round": len(trace["verification_results"]) + 1,
                                                           "purpose": "completeness_repair_recheck",
                                                           "claims": [dict(c) for c in revised_claims]})
                    claims = retain_verified_claims(revised_claims)
                    trace["completeness_repair"]["status"] = "completed"
                    trace["events"].append({"step": "completeness_repair_and_recheck", "status": "completed"})
                except Exception as exc:
                    trace["completeness_repair"]["status"] = "failed"
                    trace["completeness_repair"]["reason_detail"] = type(exc).__name__
                    trace["events"].append({"step": "completeness_repair_and_recheck", "status": "failed",
                                            "reason": type(exc).__name__})
                trace["completeness_repair"]["final_status"] = assess_question_completeness(
                    request.query, render_claims_answer(claims)).get("status")
        trace["support_elapsed_ms"] = round((time.perf_counter() - support_started) * 1000, 1)
        trace["total_elapsed_ms"] = round((time.perf_counter() - request_started) * 1000, 1)
        cited_ids = {evidence_id for claim in claims for evidence_id in claim["evidence_ids"]}
        selected_candidates = [item for item in final_candidates if item["evidence_id"] in cited_ids]
        status = "answered" if claims else "insufficient_evidence"
        # Assemble the displayed response only from claims whose IDs and verbatim quotes passed validation.
        answer = render_claims_answer(claims)
        trace["answer_completeness"] = assess_question_completeness(request.query, answer)
        if task_id:
            _update_task(db, task_id, phase="persisting", external_call_started=True)
        run = QaRun(id=run_id, query=request.query, retrieval_query=retrieval_query,
                    paper_ids=request.paper_ids,
                    document_version_ids=[version.id for version in versions],
                    parse_snapshot_ids=request.parse_snapshot_ids,
                    answer=answer, baseline_answer=baseline_answer, support_trace=trace,
                    claims=claims, chat_model=chat_model,
                    embedding_model=providers.config_status()["embedding_model"], status=status)
        db.add(run)
        db.flush()
        citation_quote = {}
        for claim in claims:
            for evidence_id in claim["evidence_ids"]:
                citation_quote.setdefault(evidence_id, claim["quote"])
        support_by_evidence = {}
        for claim in claims:
            for evidence_id in claim["evidence_ids"]:
                evidence_support = claim.get("support_status", "pending")
                previous = support_by_evidence.get(evidence_id)
                support_by_evidence[evidence_id] = evidence_support if previous in (None, evidence_support) else "partially_supported"
        for item in selected_candidates:
            item["exact_quote"] = citation_quote[item["evidence_id"]]
            normalized_quote = normalize_quote(item["exact_quote"])
            matching_spans = [span for span in item["spans"] if normalized_quote in normalize_quote(span.get("source_text", ""))]
            if matching_spans:
                matched_ids = {span["block_id"] for span in matching_spans}
                item["spans"] = matching_spans + [span for span in item["spans"] if span["block_id"] not in matched_ids]
            if item.get("parse_snapshot_id"):
                db.add(ParseSnapshotEvidence(
                    id=item["evidence_id"], qa_run_id=run_id,
                    parse_snapshot_id=item["parse_snapshot_id"], parse_snapshot_chunk_id=item["chunk_id"],
                    document_version_id=item["document_version_id"],
                    exact_quote=citation_quote[item["evidence_id"]], spans=item["spans"],
                    retrieval_details=item["retrieval"], location_valid=bool(item["spans"]),
                    quote_valid=True, support_status=support_by_evidence.get(item["evidence_id"], "pending")))
            else:
                db.add(Evidence(id=item["evidence_id"], qa_run_id=run_id, chunk_id=item["chunk_id"],
                                document_version_id=item["document_version_id"],
                                exact_quote=citation_quote[item["evidence_id"]], spans=item["spans"],
                                retrieval_details=item["retrieval"], location_valid=bool(item["spans"]),
                                quote_valid=True, support_status=support_by_evidence.get(item["evidence_id"], "pending")))
        db.commit()
        citation_records = []
        for item in selected_candidates:
            if item.get("parse_snapshot_id"):
                saved = db.get(ParseSnapshotEvidence, item["evidence_id"])
                citation_records.append(snapshot_evidence_data(db, saved))
            else:
                saved = db.get(Evidence, item["evidence_id"])
                citation_records.append(evidence_data(db, saved))
        return {"run_id": run_id, "status": status, "query": request.query,
                "retrieval_query": retrieval_query,
                "answer": answer, "claims": claims_with_ids(run.id, claims),
                "citations": citation_records, "chat_model": chat_model,
                "embedding_model": run.embedding_model, "baseline_answer": baseline_answer,
                "support_trace": trace,
                "document_version_ids": run.document_version_ids,
                "parse_snapshot_ids": run.parse_snapshot_ids}


@app.get("/api/v1/qa/{run_id}")
def get_qa_run(run_id: str):
    with session() as db:
        run = db.get(QaRun, run_id)
        if not run:
            raise HTTPException(404, "QA run not found")
        evidence = db.scalars(select(Evidence).where(Evidence.qa_run_id == run_id)).all()
        snapshot_evidence = db.scalars(select(ParseSnapshotEvidence).where(
            ParseSnapshotEvidence.qa_run_id == run_id)).all()
        citations = [evidence_data(db, item) for item in evidence]
        citations.extend(snapshot_evidence_data(db, item) for item in snapshot_evidence)
        return {"run_id": run.id, "status": run.status, "query": run.query,
                "retrieval_query": run.retrieval_query or run.query,
                "paper_ids": run.paper_ids, "document_version_ids": run.document_version_ids,
                "parse_snapshot_ids": run.parse_snapshot_ids,
                "answer": run.answer, "claims": claims_with_ids(run.id, run.claims), "citations": citations,
                "baseline_answer": run.baseline_answer, "support_trace": run.support_trace,
                "chat_model": run.chat_model, "embedding_model": run.embedding_model,
                "created_at": run.created_at.isoformat()}


def claim_identifier(run_id: str, claim_index: int, claim: dict) -> str:
    signature = f"{run_id}:{claim_index}:{claim.get('text', '')}"
    return "clm_" + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]


def claims_with_ids(run_id: str, claims: list[dict]) -> list[dict]:
    return [{**claim, "claim_id": claim_identifier(run_id, index, claim)}
            for index, claim in enumerate(claims or [])]


def note_data(db: Session, note: EvidenceNote) -> dict:
    source_run = db.get(QaRun, note.source_qa_run_id) if note.source_qa_run_id else None
    citations = []
    for snapshot in note.citations_snapshot or []:
        version = db.get(DocumentVersion, snapshot.get("document_version_id"))
        evidence = db.get(Evidence, snapshot.get("evidence_id"))
        citations.append({**snapshot,
                          "source_available": bool(version and Path(version.path).is_file()),
                          "evidence_available": evidence is not None})
    return {"note_id": note.id, "title": note.title, "body": note.body,
            "source_qa_run_id": note.source_qa_run_id,
            "source_query_snapshot": note.source_query_snapshot,
            "source_qa_available": source_run is not None,
            "paper_ids": note.paper_ids_snapshot or [],
            "claims": note.claims_snapshot or [], "citations": citations,
            "created_at": note.created_at.isoformat(), "updated_at": note.updated_at.isoformat()}


def selected_note_claims(run: QaRun, claim_ids: list[str]) -> list[dict]:
    by_id = {claim_identifier(run.id, index, claim): (index, claim)
             for index, claim in enumerate(run.claims or [])}
    selected = []
    for claim_id in dict.fromkeys(claim_ids):
        if claim_id not in by_id:
            raise HTTPException(422, "Claim does not belong to the selected QA run")
        index, claim = by_id[claim_id]
        status = claim.get("support_status")
        if status not in {"supported", "partially_supported", "partial", "unsupported", "conflicting"}:
            status = "unverified"
        verification = claim.get("support_verification")
        if verification != "automated":
            verification = "unverified"
        selected.append({"claim_id": claim_id, "source_index": index,
                         "text": claim.get("text", ""), "quote": claim.get("quote", ""),
                         "evidence_ids": claim.get("evidence_ids", []),
                         "quote_valid": claim.get("quote_valid"),
                         "support_status": status,
                         "support_verification": verification,
                         "support_reason": claim.get("support_reason")})
    return selected


@app.get("/api/v1/notes")
def list_notes(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    with session() as db:
        total = db.scalar(select(func.count()).select_from(EvidenceNote)) or 0
        notes = db.scalars(select(EvidenceNote).order_by(EvidenceNote.updated_at.desc(), EvidenceNote.id.desc())
                           .offset(offset).limit(limit)).all()
        return {"items": [{"note_id": note.id, "title": note.title,
                            "source_qa_run_id": note.source_qa_run_id,
                            "paper_ids": note.paper_ids_snapshot or [],
                            "created_at": note.created_at.isoformat(),
                            "updated_at": note.updated_at.isoformat()} for note in notes],
                "total": total, "limit": limit, "offset": offset}


@app.post("/api/v1/notes")
def create_note(request: NoteCreateRequest):
    title = request.title.strip()
    if not title:
        raise HTTPException(422, "Note title is required")
    if not request.source_qa_run_id and (request.claim_ids or request.evidence_ids):
        raise HTTPException(422, "Claims and evidence require a source QA run")
    with session() as db:
        requested_paper_ids = validate_note_paper_ids(db, request.paper_ids) if request.paper_ids is not None else None
        run = db.get(QaRun, request.source_qa_run_id) if request.source_qa_run_id else None
        if request.source_qa_run_id and not run:
            raise HTTPException(404, "Source QA run not found")
        claims = selected_note_claims(run, request.claim_ids) if run else []
        requested_evidence_ids = list(dict.fromkeys(request.evidence_ids))
        if requested_evidence_ids and not run:
            raise HTTPException(422, "Evidence requires a source QA run")
        rows = (db.scalars(select(Evidence).where(Evidence.qa_run_id == run.id,
                                                   Evidence.id.in_(requested_evidence_ids))).all()
                if run and requested_evidence_ids else [])
        snapshot_rows = (db.scalars(select(ParseSnapshotEvidence).where(
            ParseSnapshotEvidence.qa_run_id == run.id,
            ParseSnapshotEvidence.id.in_(requested_evidence_ids))).all()
            if run and requested_evidence_ids else [])
        by_id = {row.id: row for row in [*rows, *snapshot_rows]}
        if set(by_id) != set(requested_evidence_ids):
            raise HTTPException(422, "Citation does not belong to the selected QA run")
        citation_snapshots = [snapshot_evidence_data(db, by_id[evidence_id])
                              if isinstance(by_id[evidence_id], ParseSnapshotEvidence)
                              else evidence_data(db, by_id[evidence_id])
                              for evidence_id in requested_evidence_ids]
        for citation in citation_snapshots:
            saved_status = citation.get("verification", {}).get("support_status")
            if saved_status not in {"supported", "partially_supported", "partial", "unsupported", "conflicting"}:
                citation["verification"]["support_status"] = "unverified"
                citation["support_verification"] = "unverified"
            else:
                citation["support_verification"] = "automated" if (run.support_trace or {}).get("enabled") else "unverified"
        now = datetime.now(timezone.utc)
        saved_paper_ids = (requested_paper_ids if requested_paper_ids is not None else
                           list(dict.fromkeys((run.paper_ids if run else []) +
                                              [citation.get("paper_id", "") for citation in citation_snapshots])))
        note = EvidenceNote(id="note_" + uuid.uuid4().hex, title=title, body=request.body,
                            source_qa_run_id=run.id if run else None,
                            source_query_snapshot=run.query if run else "",
                            claims_snapshot=claims, citations_snapshot=citation_snapshots,
                            paper_ids_snapshot=[paper_id for paper_id in saved_paper_ids if paper_id],
                            created_at=now, updated_at=now)
        db.add(note)
        db.commit()
        db.refresh(note)
        return note_data(db, note)


@app.get("/api/v1/notes/{note_id}")
def get_note(note_id: str):
    with session() as db:
        note = db.get(EvidenceNote, note_id)
        if not note:
            raise HTTPException(404, "Note not found")
        return note_data(db, note)


@app.put("/api/v1/notes/{note_id}")
def update_note(note_id: str, request: NoteUpdateRequest):
    title = request.title.strip()
    if not title:
        raise HTTPException(422, "Note title is required")
    with session() as db:
        note = db.get(EvidenceNote, note_id)
        if not note:
            raise HTTPException(404, "Note not found")
        note.title = title
        note.body = request.body
        if request.paper_ids is not None:
            note.paper_ids_snapshot = validate_note_paper_ids(db, request.paper_ids)
        note.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(note)
        return note_data(db, note)


@app.delete("/api/v1/notes/{note_id}", status_code=204)
def delete_note(note_id: str):
    with session() as db:
        note = db.get(EvidenceNote, note_id)
        if not note:
            raise HTTPException(404, "Note not found")
        db.delete(note)
        db.commit()
        return Response(status_code=204)


def note_markdown(note: dict) -> str:
    lines = [f"# {note['title']}", "", "## 我的笔记", "", note.get("body", "") or "（空）", "",
             "## 来源问答", "", f"- 问题：{note.get('source_query_snapshot') or '未保存'}",
             f"- QA run ID：{note.get('source_qa_run_id') or '未关联'}",
             f"- 关联论文 ID：{', '.join(note.get('paper_ids') or []) or '未关联'}", "", "## 保存的 claims", ""]
    claims = note.get("claims") or []
    if not claims:
        lines.append("- 未保存 claim")
    for claim in claims:
        lines.extend([f"### {claim.get('text') or '未保存 claim 文本'}", "",
                      f"- Claim ID：{claim.get('claim_id', '未记录')}",
                      f"- 保存时核验状态：{claim.get('support_status') or '未核验'}",
                      f"- 核验方式：{claim.get('support_verification') or '未核验'}"])
        if claim.get("quote"):
            lines.extend(["", f"> {claim['quote']}"])
        lines.append("")
    lines.extend(["## 保存的论文引用", ""])
    citations = note.get("citations") or []
    if not citations:
        lines.append("- 未保存引用")
    for citation in citations:
        lines.extend([f"### {citation.get('title') or '论文标题未记录'}", "",
                      f"- 论文 ID：{citation.get('paper_id') or '未记录'}",
                      f"- 文档版本 ID：{citation.get('document_version_id') or '未记录'}",
                      f"- 来源版本：{citation.get('source_version') or '未记录'}",
                      f"- SHA-256：{citation.get('sha256') or '未记录'}",
                      f"- Evidence ID：{citation.get('evidence_id') or '未记录'}",
                      f"- 保存时核验状态：{citation.get('verification', {}).get('support_status') or '未核验'}",
                      f"- 核验方式：{citation.get('support_verification') or '未核验'}",
                      f"- 引用原文页：{_markdown_pages(citation.get('quote_location_pdf_page_indexes'), citation.get('verification', {}).get('location_valid'))}",
                      f"- 分块覆盖页：{_markdown_pages(citation.get('chunk_pdf_page_indexes'), True)}",
                      f"- 定位精度：{_citation_precision(citation)}", "",
                      citation.get("exact_quote") or "（未保存引文）", ""])
    return "\n".join(lines).rstrip() + "\n"


def _markdown_pages(indexes: list[int] | None, valid: bool) -> str:
    if not indexes:
        return "未记录（无可靠页码）"
    pages = sorted({index + 1 for index in indexes if isinstance(index, int) and index >= 0})
    if not pages or not valid:
        return "未记录（无可靠页码）"
    return ", ".join(str(page) for page in pages) + "（PDF 显示页码；API 页索引从 0 起）"


def _citation_precision(citation: dict) -> str:
    if not citation.get("verification", {}).get("location_valid"):
        return "无可靠引句位置；分块覆盖页不代表引句位置。"
    if any(span.get("bbox") for span in citation.get("spans", [])):
        return "原文块 bbox 范围，非引句精确矩形。"
    return "仅有原文页码，没有可用 bbox。"


def markdown_filename(title: str) -> str:
    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:100].strip(" .")
    if not safe_name:
        safe_name = "papertrail-note"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if safe_name.split(".", 1)[0].upper() in reserved:
        safe_name = "_" + safe_name
    return f"{safe_name}.md"


@app.get("/api/v1/notes/{note_id}/markdown")
def export_note_markdown(note_id: str):
    with session() as db:
        note = db.get(EvidenceNote, note_id)
        if not note:
            raise HTTPException(404, "Note not found")
        payload = note_data(db, note)
    filename = markdown_filename(payload["title"])
    return Response(note_markdown(payload), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{url_quote(filename)}"})


def comparison_report_data(report: ComparisonReport) -> dict:
    return {"report_id": report.id, "title": report.title, "paper_ids": report.paper_ids,
            "document_version_ids": report.document_version_ids, "report_data": report.report_data,
            "chat_model": report.chat_model, "embedding_model": report.embedding_model,
            "created_at": report.created_at.isoformat()}


def comparison_revision_data(source: ComparisonReport, revision: ComparisonReportRevision) -> dict:
    payload = comparison_report_data(source)
    payload.update({"revision_id": revision.id, "revision_number": revision.revision_number,
                    "source_report_id": revision.source_report_id, "revision_created_at": revision.created_at.isoformat(),
                    "changes": revision.changes, "report_data": revision.report_data})
    return payload


@app.get("/api/v1/comparison-reports")
def list_comparison_reports(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    with session() as db:
        total = db.scalar(select(func.count()).select_from(ComparisonReport)) or 0
        reports = db.scalars(select(ComparisonReport).order_by(ComparisonReport.created_at.desc(), ComparisonReport.id.desc())
                             .offset(offset).limit(limit)).all()
        return {"items": [{"report_id": row.id, "title": row.title, "paper_ids": row.paper_ids,
                            "created_at": row.created_at.isoformat()} for row in reports],
                "total": total, "limit": limit, "offset": offset}


@app.post("/api/v1/comparison-reports")
async def create_comparison_report(request: ComparisonReportCreateRequest,
                                   prefer: Annotated[str | None, Header()] = None,
                                   idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    if (prefer and "respond-async" in prefer.lower()) or idempotency_key:
        if not idempotency_key:
            raise HTTPException(400, {"code": "idempotency_key_required", "message": "Async report creation requires Idempotency-Key",
                                      "retryable": False, "run_id": None, "details": None})
        task = await _enqueue_operation("comparison_report", request.model_dump(mode="json"), idempotency_key)
        return JSONResponse(status_code=202, content=task)
    return await _execute_comparison_report(request)


async def _execute_comparison_report(request: ComparisonReportCreateRequest, task_id: str | None = None):
    started = time.perf_counter()
    title = request.title.strip()
    if not title or len(title) > 200:
        raise HTTPException(422, "Report title must contain 1-200 characters")
    if not 2 <= len(request.paper_ids) <= 3:
        raise HTTPException(422, "Select two or three papers for comparison")
    if len(request.document_version_ids) != len(request.paper_ids):
        raise HTTPException(422, "Choose one fixed document version for each selected paper")
    if len(request.note_ids) > 20:
        raise HTTPException(422, "Select no more than 20 supplemental notes")
    dimensions = [str(value).strip() for value in request.dimensions]
    if not dimensions or len(dimensions) > 8 or any(not value or len(value) > 120 for value in dimensions) or len(set(dimensions)) != len(dimensions):
        raise HTTPException(422, "Provide 1-8 distinct comparison dimensions, each 1-120 characters")
    report_id = "rpt_" + uuid.uuid4().hex
    if task_id:
        with Session(engine) as task_db:
            _update_task(task_db, task_id, phase="retrieving", result_id=report_id, external_call_started=True)
    query = comparison_query(dimensions)
    with session() as db:
        require_query_scope("comparison report", request.paper_ids, db)
        try:
            chunks, versions = await retrieve(db, query, request.paper_ids, min(18, 6 * len(request.paper_ids)),
                                               document_version_ids=request.document_version_ids)
        except HTTPException:
            raise
        if not chunks:
            raise HTTPException(404, "No indexed paper excerpts were found for this comparison")
        by_id = {item["evidence_id"]: item for item in (search_result(chunk, report_id) for chunk in chunks)}
        evidence = [{"evidence_id": item["evidence_id"], "paper_id": item["paper_id"], "title": item["title"],
                     "document_version_id": item["document_version_id"], "source_version": item["source_version"],
                     "text": item["text"], "section_path": item["section_path"]} for item in by_id.values()]
        supplemental_notes = []
        for note_id in dict.fromkeys(request.note_ids):
            note = db.get(EvidenceNote, note_id)
            if not note:
                raise HTTPException(422, {"code": "invalid_supplemental_note", "message": "Selected user note is unavailable", "retryable": False, "run_id": None, "details": {"note_id": note_id}})
            supplemental_notes.append({"note_id": note.id, "title": note.title, "body": note.body,
                                       "source_qa_run_id": note.source_qa_run_id,
                                       "source_query_snapshot": note.source_query_snapshot,
                                       "claims_snapshot": note.claims_snapshot,
                                       "citations_snapshot": note.citations_snapshot,
                                       "label": "用户笔记补充材料（非论文原文）"})
        payload = {"requested_dimensions": dimensions,
                   "papers": [{"paper_id": version.paper_id, "title": db.get(Paper, version.paper_id).title,
                               "document_version_id": version.id, "source_version": version.source_version,
                               "sha256": version.sha256,
                               "evidence": [item for item in evidence if item["paper_id"] == version.paper_id]}
                              for version in versions]}
        if task_id:
            _update_task(db, task_id, phase="generating", result_id=report_id, external_call_started=True)
        try:
            generated, chat_model = await providers._chat_json(COMPARISON_SYSTEM_PROMPT, payload,
                                                                request_id=task_id or report_id)
            report_dimensions, flat_claims, excluded_claims = normalize_generated_report(
                generated, dimensions, request.paper_ids, by_id, report_id)
        except InvalidReport as exc:
            raise HTTPException(502, {"code": "invalid_comparison_report", "message": "Generated report failed citation validation", "retryable": False, "run_id": report_id, "details": {"reason": str(exc)}}) from exc
        except Exception as exc:
            details = providers.chat_failure_diagnostics(exc, (time.perf_counter() - started) * 1000)
            retryable = exc.retryable if isinstance(exc, providers.ChatProviderError) else False
            logger.warning("comparison_provider_failure request_id=%s phase=%s elapsed_ms=%s reason=%s exception_chain=%s endpoint_host=%s provider_request_id=%s finish_reason=%s",
                           details.get("request_id"), details["phase"], details["elapsed_ms"], details["reason"],
                           ",".join(details["exception_chain"]), details["endpoint_host"],
                           details.get("provider_request_id"), details.get("finish_reason"))
            raise HTTPException(502, {"code": "comparison_generation_failed", "message": "Could not generate the comparison report", "retryable": retryable, "run_id": report_id, "details": details}) from exc

        # Existing verifier receives only report claims and their cited source excerpts.
        support_started = time.perf_counter()
        verification_calls = 0
        if flat_claims:
            if task_id:
                _update_task(db, task_id, phase="verifying", external_call_started=True)
            linked_ids = {evidence_id for claim in flat_claims for evidence_id in claim["evidence_ids"]}
            verifier_evidence = [{"evidence_id": evidence_id, "text": by_id[evidence_id]["text"]}
                                 for evidence_id in linked_ids]
            verification_calls = 1  # Count attempted calls, including transport or response failures.
            try:
                judged, _ = await providers.verify_claim_support(
                    [{"text": claim["text"], "evidence_ids": claim["evidence_ids"]} for claim in flat_claims],
                    verifier_evidence)
                flat_claims = apply_support_results(flat_claims, judged)
                judgments_by_id = {claim["claim_id"]: claim for claim in flat_claims}
                for dimension in report_dimensions:
                    for claim in [*sum((cell["claims"] for cell in dimension["cells"]), []), *dimension["comparisons"]]:
                        claim.update(judgments_by_id.get(claim["claim_id"], {}))
            except Exception as exc:
                flat_claims = [{**claim, "support_status": "unverified", "support_verification": "unverified",
                                "support_reason": type(exc).__name__, "support_evidence_ids": []} for claim in flat_claims]
                for dimension in report_dimensions:
                    for claim in [*sum((cell["claims"] for cell in dimension["cells"]), []), *dimension["comparisons"]]:
                        claim.update(next((item for item in flat_claims if item["claim_id"] == claim["claim_id"]), {}))
        trace = {"chat_calls": 1 + verification_calls, "embedding_requests": 1,
                 "support_check": "automated_model_judgment" if verification_calls else ("failed" if flat_claims else "no_claims"),
                 "support_elapsed_ms": round((time.perf_counter() - support_started) * 1000, 1),
                 "total_elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                 "excluded_unsupported_absence_claims": excluded_claims["unsupported_absence"],
                 "excluded_unsafe_numeric_comparisons": excluded_claims["unsafe_numeric_comparison"],
                 "coverage_rule": "Cell coverage indicates retrieved cited content only; empty cells are evidence insufficient, not not-reported."}
        # Use a strict citation-to-current retrieval mapping and preserve the fixed version snapshots.
        support_status_by_evidence = {}
        for claim in flat_claims:
            for evidence_id in claim["evidence_ids"]:
                status = claim.get("support_status", "unverified")
                previous = support_status_by_evidence.get(evidence_id)
                support_status_by_evidence[evidence_id] = status if previous in (None, status) else "partially_supported"
        def location_builder(source: dict, quote_text: str, claim: dict) -> dict:
            spans = quote_location_spans(quote_text, source.get("spans", []))
            return {"evidence_id": source["evidence_id"], "chunk_id": source["chunk_id"],
                    "paper_id": source["paper_id"], "title": source["title"],
                    "document_version_id": source["document_version_id"], "source_version": source["source_version"],
                    "sha256": source["sha256"], "exact_quote": quote_text, "chunk_text": source["text"],
                    "section_path": source["section_path"], "spans": spans,
                    "chunk_pdf_page_indexes": sorted({s.get("pdf_page_index") for s in source["spans"] if isinstance(s.get("pdf_page_index"), int)}),
                    "quote_location_pdf_page_indexes": sorted({s["pdf_page_index"] for s in spans}),
                    "page_number_basis": "pdf_page_index (zero-based); display page = index + 1",
                    "location_precision": "原文块 bbox 范围，非引句精确矩形。" if spans else "暂无法精确定位",
                    "verification": {"location_valid": bool(spans), "quote_valid": True,
                                      "support_status": support_status_by_evidence.get(source["evidence_id"], "unverified")}}
        attach_citations(report_dimensions, by_id, location_builder)
        paper_snapshots = [{"paper_id": version.paper_id, "title": db.get(Paper, version.paper_id).title,
                            "document_version_id": version.id, "source_version": version.source_version,
                            "sha256": version.sha256} for version in versions]
        stored_data = {"paper_snapshots": paper_snapshots, "dimensions": report_dimensions,
                       "supplemental_notes": supplemental_notes, "support_trace": trace,
                       "coverage_status": "per_cell", "retrieval_evidence_count": len(evidence)}
        row = ComparisonReport(id=report_id, title=title, paper_ids=request.paper_ids,
                               document_version_ids=[v.id for v in versions], report_data=stored_data,
                               chat_model=chat_model, embedding_model=providers.config_status()["embedding_model"])
        if task_id:
            _update_task(db, task_id, phase="persisting", external_call_started=True)
        db.add(row); db.commit(); db.refresh(row)
        return comparison_report_data(row)


@app.get("/api/v1/comparison-reports/{report_id}")
def get_comparison_report(report_id: str):
    with session() as db:
        report = db.get(ComparisonReport, report_id)
        if not report:
            raise HTTPException(404, "Comparison report not found")
        return comparison_report_data(report)


@app.get("/api/v1/comparison-reports/{report_id}/revisions")
def list_comparison_report_revisions(report_id: str):
    with session() as db:
        if not db.get(ComparisonReport, report_id):
            raise HTTPException(404, "Comparison report not found")
        rows = db.scalars(select(ComparisonReportRevision).where(
            ComparisonReportRevision.source_report_id == report_id
        ).order_by(ComparisonReportRevision.revision_number)).all()
        return {"items": [{"revision_id": row.id, "revision_number": row.revision_number,
                           "created_at": row.created_at.isoformat(), "changes": row.changes}
                          for row in rows]}


@app.post("/api/v1/comparison-reports/{report_id}/revisions")
def create_comparison_report_revision(report_id: str, request: ComparisonReportRevisionRequest):
    if not request.changes or len(request.changes) > 100:
        raise HTTPException(422, "Provide 1-100 report cell changes")
    if len({item.claim_id for item in request.changes}) != len(request.changes):
        raise HTTPException(422, "A claim may be edited only once per revision")
    if any(not item.text.strip() or len(item.text) > 8000 or not item.reason.strip() or len(item.reason) > 2000
           for item in request.changes):
        raise HTTPException(422, "Each edit requires claim text and a reason within the size limit")
    with session() as db:
        source = db.get(ComparisonReport, report_id)
        if not source:
            raise HTTPException(404, "Comparison report not found")
        snapshot = json.loads(json.dumps(source.report_data, ensure_ascii=False))
        claims_by_id = {}
        for dimension in snapshot.get("dimensions", []):
            claims = [*sum((cell.get("claims", []) for cell in dimension.get("cells", [])), []),
                      *dimension.get("comparisons", [])]
            for claim in claims:
                claims_by_id[claim.get("claim_id")] = claim
        changes = []
        for edit in request.changes:
            claim = claims_by_id.get(edit.claim_id)
            if not claim:
                raise HTTPException(422, {"code": "invalid_report_claim", "message": "Claim is not in the source report", "retryable": False, "run_id": report_id, "details": {"claim_id": edit.claim_id}})
            old_status = claim.get("support_status", "unverified")
            change = {"claim_id": edit.claim_id, "original_text": claim["text"], "revised_text": edit.text.strip(),
                      "reason": edit.reason.strip(), "original_support_status": old_status,
                      "revised_support_status": "edited_unverified"}
            claim["text"] = edit.text.strip()
            claim["revision_edit"] = {"status": "edited_unverified", "reason": edit.reason.strip(),
                                      "original_text": change["original_text"],
                                      "original_support_status": old_status}
            claim["support_status"] = "unverified"
            claim["support_verification"] = "unverified"
            claim["support_reason"] = "已编辑，未重新核验"
            claim["support_evidence_ids"] = []
            # Citations and source locations are copied unchanged; their support display belongs to the original text.
            for citation in claim.get("citations", []):
                citation.setdefault("verification", {})["support_status"] = "unverified"
            changes.append(change)
        previous = db.scalar(select(func.max(ComparisonReportRevision.revision_number)).where(
            ComparisonReportRevision.source_report_id == report_id)) or 0
        revision = ComparisonReportRevision(id="rrev_" + uuid.uuid4().hex, source_report_id=report_id,
                                             revision_number=previous + 1, report_data=snapshot, changes=changes)
        db.add(revision)
        db.commit()
        db.refresh(revision)
        return comparison_revision_data(source, revision)


@app.get("/api/v1/comparison-reports/{report_id}/revisions/{revision_id}")
def get_comparison_report_revision(report_id: str, revision_id: str):
    with session() as db:
        source = db.get(ComparisonReport, report_id)
        revision = db.get(ComparisonReportRevision, revision_id)
        if not source or not revision or revision.source_report_id != report_id:
            raise HTTPException(404, "Comparison report revision not found")
        return comparison_revision_data(source, revision)


@app.get("/api/v1/comparison-reports/{report_id}/revisions/{revision_id}/markdown")
def export_comparison_report_revision(report_id: str, revision_id: str):
    with session() as db:
        source = db.get(ComparisonReport, report_id)
        revision = db.get(ComparisonReportRevision, revision_id)
        if not source or not revision or revision.source_report_id != report_id:
            raise HTTPException(404, "Comparison report revision not found")
        payload = comparison_revision_data(source, revision)
    filename = report_filename(payload["title"] + f"-修订{revision.revision_number}")
    return Response(report_markdown(payload), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{url_quote(filename)}"})


@app.get("/api/v1/comparison-reports/{report_id}/markdown")
def export_comparison_report(report_id: str):
    with session() as db:
        report = db.get(ComparisonReport, report_id)
        if not report:
            raise HTTPException(404, "Comparison report not found")
        payload = comparison_report_data(report)
    filename = report_filename(payload["title"])
    return Response(report_markdown(payload), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{url_quote(filename)}"})


@app.get("/api/v1/qa")
def list_qa_runs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    """Return a bounded page of persisted QA history; this endpoint never runs retrieval or models."""
    with session() as db:
        total = db.scalar(select(func.count()).select_from(QaRun)) or 0
        runs = db.scalars(select(QaRun).order_by(QaRun.created_at.desc(), QaRun.id.desc())
                          .offset(offset).limit(limit)).all()
        return {"items": [{"run_id": run.id, "query": run.query, "paper_ids": run.paper_ids,
                           "status": run.status, "created_at": run.created_at.isoformat(),
                           "chat_model": run.chat_model}
                          for run in runs], "total": total, "limit": limit, "offset": offset}


def quote_location_spans(exact_quote: str, spans: list[dict]) -> list[dict]:
    normalized_quote = normalize_quote(exact_quote)
    if not normalized_quote:
        return []
    valid = [span for span in spans
             if isinstance(span.get("pdf_page_index"), int)
             and isinstance(span.get("bbox"), list) and len(span["bbox"]) == 4]
    exact = [span for span in valid
             if normalized_quote in normalize_quote(str(span.get("source_text") or ""))]
    if exact:
        return exact

    # Structured parsers can split one quoted algorithm/table row across
    # adjacent lines. Accept only a contiguous same-page source-block match;
    # returned boxes remain coarse block ranges, never quote-level rectangles.
    for start, first in enumerate(spans):
        page_index = first.get("pdf_page_index")
        if not isinstance(page_index, int):
            continue
        pieces: list[tuple[int, int, dict]] = []
        joined = ""
        for span in spans[start:]:
            if span.get("pdf_page_index") != page_index:
                break
            source = normalize_quote(str(span.get("source_text") or ""))
            if not source:
                continue
            separator = " " if joined else ""
            piece_start = len(joined) + len(separator)
            joined += separator + source
            pieces.append((piece_start, len(joined), span))
            quote_start = joined.find(normalized_quote)
            if quote_start < 0:
                continue
            quote_end = quote_start + len(normalized_quote)
            matched = [item for piece_start, piece_end, item in pieces
                       if piece_start < quote_end and piece_end > quote_start]
            if matched and all(item in valid for item in matched):
                return matched
    return []


def evidence_data(db: Session, evidence: Evidence) -> dict:
    chunk = db.get(Chunk, evidence.chunk_id)
    version = db.get(DocumentVersion, evidence.document_version_id)
    paper = db.get(Paper, version.paper_id) if version else None
    if not chunk or not version or not paper:
        raise HTTPException(409, "Evidence source record is incomplete")
    # Stored spans may cover the entire chunk. Only spans whose own source text
    # contains this exact quote are quote locations; other spans remain chunk coverage.
    quote_spans = quote_location_spans(evidence.exact_quote, evidence.spans or [])
    quote_location_valid = bool(quote_spans)
    return {"evidence_id": evidence.id, "chunk_id": chunk.id, "paper_id": paper.id,
            "title": paper.title, "document_version_id": version.id,
            "source_version": version.source_version, "sha256": version.sha256,
            "exact_quote": evidence.exact_quote, "chunk_text": chunk.text,
            "section_path": chunk.section_path, "spans": quote_spans,
            "chunk_pdf_page_indexes": sorted({span.get("pdf_page_index") for span in (chunk.spans or [])
                                               if isinstance(span.get("pdf_page_index"), int)}),
            "quote_location_pdf_page_indexes": sorted({span["pdf_page_index"] for span in quote_spans}),
            "page_number_basis": "pdf_page_index (zero-based); display page = index + 1",
            "retrieval": evidence.retrieval_details,
            "verification": {"location_valid": quote_location_valid,
                             "quote_valid": evidence.quote_valid,
                             "support_status": evidence.support_status}}


def snapshot_evidence_data(db: Session, evidence: ParseSnapshotEvidence) -> dict:
    chunk = db.get(ParseSnapshotChunk, evidence.parse_snapshot_chunk_id)
    version = db.get(DocumentVersion, evidence.document_version_id)
    snapshot = db.get(ParseSnapshot, evidence.parse_snapshot_id)
    paper = db.get(Paper, version.paper_id) if version else None
    if (not chunk or not version or not paper or not snapshot or
            chunk.parse_snapshot_id != snapshot.id or snapshot.document_version_id != version.id or
            snapshot.source_sha256 != version.sha256):
        raise HTTPException(409, "Experimental evidence source record is incomplete")
    quote_spans = quote_location_spans(evidence.exact_quote, evidence.spans or [])
    return {"evidence_id": evidence.id, "chunk_id": chunk.id, "parse_snapshot_id": snapshot.id,
            "parser_identity": snapshot.parser_identity, "paper_id": paper.id, "title": paper.title,
            "document_version_id": version.id, "source_version": version.source_version,
            "sha256": version.sha256, "exact_quote": evidence.exact_quote, "chunk_text": chunk.text,
            "section_path": chunk.section_path, "spans": quote_spans,
            "chunk_pdf_page_indexes": sorted({span.get("pdf_page_index") for span in (chunk.spans or [])
                                                if isinstance(span.get("pdf_page_index"), int)}),
            "quote_location_pdf_page_indexes": sorted({span["pdf_page_index"] for span in quote_spans}),
            "page_number_basis": "pdf_page_index (zero-based); display page = index + 1",
            "retrieval": evidence.retrieval_details,
            "verification": {"location_valid": bool(quote_spans), "quote_valid": evidence.quote_valid,
                             "support_status": evidence.support_status}}


@app.get("/api/v1/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    with session() as db:
        evidence = db.get(Evidence, evidence_id)
        if evidence:
            return evidence_data(db, evidence)
        snapshot_evidence = db.get(ParseSnapshotEvidence, evidence_id)
        if not snapshot_evidence:
            raise HTTPException(404, "Evidence not found")
        return snapshot_evidence_data(db, snapshot_evidence)
