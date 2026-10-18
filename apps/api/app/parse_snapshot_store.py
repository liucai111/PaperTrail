"""Persistence helpers for version-pinned experimental parse snapshots.

The legacy ParsedBlock/Chunk/SearchIndex path remains untouched. This module
stores Stage 15 parse snapshots in isolated tables and never calls providers.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chunking import CHUNKER_VERSION, build_chunks, stable_chunk_id
from .main import (Chunk, ChunkVector, DocumentIndexSelection, DocumentVersion, IndexChunk,
                   ParseSnapshot, ParseSnapshotBlock, ParseSnapshotChunk,
                   ParseSnapshotChunkVector, ParseSnapshotIndex,
                   ParseSnapshotIndexChunk, ParseSnapshotVector, SearchIndex)


def _stable_id(prefix: str, value: str, length: int = 32) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _valid_bbox(raw: Any, page: dict[str, Any]) -> list[float] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("bbox must contain four coordinates or be null")
    bbox = [float(value) for value in raw]
    if not all(math.isfinite(value) for value in bbox):
        raise ValueError("bbox coordinates must be finite")
    width, height = float(page["width"]), float(page["height"])
    if bbox[0] < -1 or bbox[1] < -1 or bbox[2] > width + 1 or bbox[3] > height + 1:
        raise ValueError("bbox falls outside its source page")
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        raise ValueError("bbox coordinates are inverted")
    return bbox


def _validate_structure_references(payload: dict[str, Any], block_ids: set[str]) -> None:
    id_sets = {
        "target_block_id": block_ids,
        "caption_block_id": block_ids,
        "source_block_id": block_ids,
        "target_table_id": {row.get("table_id") for row in payload.get("tables", [])},
        "target_figure_id": {row.get("figure_id") for row in payload.get("figures", [])},
        "target_formula_id": {row.get("formula_id") for row in payload.get("formulas", [])},
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in id_sets and child is not None and child not in id_sets[key]:
                    raise ValueError(f"sidecar contains a dangling {key} reference")
                if key == "source_block_ids":
                    if not isinstance(child, list) or any(item not in block_ids for item in child):
                        raise ValueError("sidecar contains dangling source_block_ids")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit({key: payload.get(key) for key in ("pages", "blocks", "tables", "figures", "formulas")})


def store_parse_snapshot(db: Session, version: DocumentVersion, payload: dict[str, Any]) -> tuple[ParseSnapshot, bool]:
    """Validate and insert one immutable sidecar snapshot; return (row, created)."""
    source_sha = payload.get("source_sha256")
    parser_identity = payload.get("parser_identity")
    protocol_version = payload.get("protocol_version")
    page_number_basis = payload.get("page_number_basis")
    pages = payload.get("pages")
    blocks = payload.get("blocks")
    if source_sha != version.sha256:
        raise ValueError("sidecar source SHA-256 does not match document version")
    if not all(isinstance(value, str) and value for value in (parser_identity, protocol_version)):
        raise ValueError("sidecar parser identity and protocol version are required")
    if page_number_basis != "pdf_page_index zero-based; display page is index + 1":
        raise ValueError("sidecar page number basis is missing or unsupported")
    if not isinstance(pages, list) or len(pages) != version.page_count:
        raise ValueError("sidecar page count does not match document version")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("sidecar must contain parsed blocks")
    page_by_index = {}
    for page in pages:
        index = page.get("pdf_page_index")
        if not isinstance(index, int) or index < 0 or index >= version.page_count:
            raise ValueError("sidecar page index is outside the document")
        if index in page_by_index:
            raise ValueError("sidecar contains duplicate page indices")
        width, height = float(page.get("width", 0)), float(page.get("height", 0))
        if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
            raise ValueError("sidecar page dimensions must be positive")
        page_by_index[index] = page
    if set(page_by_index) != set(range(version.page_count)):
        raise ValueError("sidecar pages must cover the document using zero-based indices")

    normalized = []
    seen_ids: set[str] = set()
    per_page_ordinal: dict[int, int] = {}
    for block in blocks:
        source_block_id = block.get("block_id")
        text = block.get("text")
        page_index = block.get("pdf_page_index")
        if not isinstance(source_block_id, str) or not source_block_id or source_block_id in seen_ids:
            raise ValueError("sidecar block IDs must be non-empty and unique")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("sidecar block text must be non-empty")
        if not isinstance(page_index, int) or page_index not in page_by_index:
            raise ValueError("sidecar block references an unknown zero-based page")
        seen_ids.add(source_block_id)
        page = page_by_index[page_index]
        bbox = _valid_bbox(block.get("bbox"), page)
        ordinal = per_page_ordinal.get(page_index, 0)
        per_page_ordinal[page_index] = ordinal + 1
        normalized.append((block, source_block_id, text, page_index, ordinal, bbox, page))

    _validate_structure_references(payload, seen_ids)

    signature = f"{version.id}:{source_sha}:{parser_identity}:{protocol_version}"
    snapshot_id = _stable_id("psnap_", signature)
    existing = db.get(ParseSnapshot, snapshot_id)
    if existing:
        if existing.block_count != len(normalized) or existing.status != "ready":
            raise ValueError("existing parse snapshot is incomplete or inconsistent")
        return existing, False

    snapshot = ParseSnapshot(
        id=snapshot_id,
        document_version_id=version.id,
        source_sha256=source_sha,
        parser_identity=parser_identity,
        protocol_version=protocol_version,
        status="building",
        page_count=len(pages),
        block_count=0,
        quality=payload.get("quality_summary") or {},
        structure_data={key: payload.get(key) for key in ("pages", "tables", "figures", "formulas")},
    )
    db.add(snapshot)
    db.flush()
    for block, source_block_id, text, page_index, ordinal, bbox, page in normalized:
        row_id = _stable_id("psblk_", f"{snapshot_id}:{source_block_id}")
        db.add(ParseSnapshotBlock(
            id=row_id,
            parse_snapshot_id=snapshot_id,
            source_block_id=source_block_id,
            pdf_page_index=page_index,
            ordinal=ordinal,
            text=text,
            bbox=bbox,
            page_width=float(page["width"]),
            page_height=float(page["height"]),
            section_path=block.get("section_path") if isinstance(block.get("section_path"), list) else [],
            kind=str(block.get("kind") or "text"),
            role=block.get("role"),
            reading_order=block.get("reading_order") if isinstance(block.get("reading_order"), int) else None,
            source=block.get("source"),
            quality_flags=block.get("quality_flags") if isinstance(block.get("quality_flags"), list) else [],
            structure_data={key: block.get(key) for key in ("relations", "confidence", "source_ordinal")},
        ))
    snapshot.block_count = len(normalized)
    snapshot.status = "ready"
    db.flush()
    return snapshot, True


def build_snapshot_chunks(db: Session, snapshot_id: str, chunker_generation: str = CHUNKER_VERSION) -> list[ParseSnapshotChunk]:
    """Build immutable chunks in the snapshot namespace, preserving source block spans."""
    snapshot = db.get(ParseSnapshot, snapshot_id)
    if not snapshot or snapshot.status != "ready":
        raise ValueError("only a ready parse snapshot can be chunked")
    blocks = db.scalars(select(ParseSnapshotBlock).where(
        ParseSnapshotBlock.parse_snapshot_id == snapshot_id).order_by(
        ParseSnapshotBlock.pdf_page_index, ParseSnapshotBlock.ordinal)).all()
    raw = [{"block_id": block.source_block_id, "pdf_page_index": block.pdf_page_index,
            "ordinal": block.ordinal, "text": block.text, "bbox": block.bbox,
            "page_width": block.page_width, "page_height": block.page_height,
            "section_path": block.section_path} for block in blocks]
    pieces = build_chunks(raw)
    if not pieces:
        raise ValueError("snapshot has no indexable text")
    saved = []
    for piece in pieces:
        chunk_id = stable_chunk_id(snapshot.document_version_id, piece["ordinal"], piece["content_hash"],
                                   namespace=f"{snapshot.id}:{chunker_generation}")
        row = db.get(ParseSnapshotChunk, chunk_id)
        source_block_ids = list(dict.fromkeys(span["block_id"] for span in piece["spans"]))
        if row:
            if row.parse_snapshot_id != snapshot_id or row.content_hash != piece["content_hash"]:
                raise ValueError("snapshot chunk ID collision detected")
            saved.append(row)
            continue
        row = ParseSnapshotChunk(id=chunk_id, parse_snapshot_id=snapshot_id,
                                 document_version_id=snapshot.document_version_id,
                                 chunker_generation=chunker_generation, ordinal=piece["ordinal"],
                                 text=piece["text"], content_hash=piece["content_hash"],
                                 token_count=piece["token_count"], section_path=piece["section_path"],
                                 spans=piece["spans"], source_block_ids=source_block_ids)
        db.add(row)
        saved.append(row)
    db.flush()
    return saved


def reuse_legacy_snapshot_vectors(db: Session, snapshot_id: str, embedding_model: str,
                                  dimensions: int) -> dict[str, int]:
    """Reuse only exact content-hash/model/dimension matches; issue no provider calls."""
    chunks = db.scalars(select(ParseSnapshotChunk).where(
        ParseSnapshotChunk.parse_snapshot_id == snapshot_id)).all()
    reused = 0
    already_cached = 0
    for chunk in chunks:
        link = db.scalar(select(ParseSnapshotChunkVector).where(
            ParseSnapshotChunkVector.parse_snapshot_chunk_id == chunk.id,
            ParseSnapshotChunkVector.embedding_model == embedding_model))
        if link:
            if link.content_hash == chunk.content_hash and link.dimensions == dimensions:
                already_cached += 1
            continue
        vector_row = db.scalar(select(ParseSnapshotVector).where(
            ParseSnapshotVector.embedding_model == embedding_model,
            ParseSnapshotVector.dimensions == dimensions,
            ParseSnapshotVector.content_hash == chunk.content_hash))
        if vector_row is None:
            cached = db.execute(select(ChunkVector.vector).join(
                Chunk, Chunk.id == ChunkVector.chunk_id).where(
                    Chunk.content_hash == chunk.content_hash,
                    ChunkVector.embedding_model == embedding_model,
                    ChunkVector.dimensions == dimensions).limit(1)).first()
            if cached is None:
                continue
            if not isinstance(cached[0], list) or len(cached[0]) != dimensions:
                continue
            vector_id = _stable_id("psvec_", f"{embedding_model}:{dimensions}:{chunk.content_hash}")
            vector_row = ParseSnapshotVector(id=vector_id, embedding_model=embedding_model,
                                             dimensions=dimensions, content_hash=chunk.content_hash,
                                             vector=cached[0])
            db.add(vector_row)
            db.flush()
        link_id = _stable_id("pscv_", f"{chunk.id}:{embedding_model}")
        db.add(ParseSnapshotChunkVector(id=link_id, parse_snapshot_chunk_id=chunk.id,
                                        embedding_model=embedding_model, dimensions=dimensions,
                                        content_hash=chunk.content_hash, vector_id=vector_row.id))
        reused += 1
    db.flush()
    return {"reused": reused, "already_cached": already_cached, "chunks": len(chunks)}


def _snapshot_index_vectors_complete(db: Session, index: ParseSnapshotIndex) -> bool:
    chunks = db.scalars(select(ParseSnapshotChunk).where(
        ParseSnapshotChunk.parse_snapshot_id == index.parse_snapshot_id,
        ParseSnapshotChunk.chunker_generation == index.chunker_generation)).all()
    if len(chunks) != index.chunk_count or not chunks:
        return False
    index_links = db.scalars(select(ParseSnapshotIndexChunk).where(
        ParseSnapshotIndexChunk.index_id == index.id)).all()
    if {row.parse_snapshot_chunk_id for row in index_links} != {row.id for row in chunks}:
        return False
    for chunk in chunks:
        link = db.scalar(select(ParseSnapshotChunkVector).where(
            ParseSnapshotChunkVector.parse_snapshot_chunk_id == chunk.id,
            ParseSnapshotChunkVector.embedding_model == index.embedding_model))
        vector = db.get(ParseSnapshotVector, link.vector_id) if link else None
        if (not link or link.content_hash != chunk.content_hash or link.dimensions != index.dimensions
                or not vector or vector.content_hash != chunk.content_hash
                or vector.embedding_model != index.embedding_model or vector.dimensions != index.dimensions
                or not isinstance(vector.vector, list) or len(vector.vector) != index.dimensions):
            return False
    return True


def create_snapshot_index(db: Session, snapshot_id: str, chunker_generation: str,
                          embedding_model: str, dimensions: int) -> ParseSnapshotIndex:
    """Create an isolated index generation; it is ready only with every matching vector."""
    snapshot = db.get(ParseSnapshot, snapshot_id)
    if not snapshot or snapshot.status != "ready":
        raise ValueError("only a ready parse snapshot can be indexed")
    chunks = db.scalars(select(ParseSnapshotChunk).where(
        ParseSnapshotChunk.parse_snapshot_id == snapshot_id,
        ParseSnapshotChunk.chunker_generation == chunker_generation).order_by(
        ParseSnapshotChunk.ordinal)).all()
    if not chunks:
        raise ValueError("snapshot chunks must be built before an index")
    index_id = _stable_id("psidx_", f"{snapshot_id}:{chunker_generation}:{embedding_model}:{dimensions}")
    index = db.get(ParseSnapshotIndex, index_id)
    if index is None:
        index = ParseSnapshotIndex(id=index_id, document_version_id=snapshot.document_version_id,
                                   parse_snapshot_id=snapshot_id, chunker_generation=chunker_generation,
                                   embedding_model=embedding_model, dimensions=dimensions,
                                   chunk_count=len(chunks), status="building")
        db.add(index)
        for chunk in chunks:
            db.add(ParseSnapshotIndexChunk(index_id=index_id, parse_snapshot_chunk_id=chunk.id))
        db.flush()
    elif index.chunk_count != len(chunks) or index.status == "failed":
        raise ValueError("existing snapshot index metadata is inconsistent")
    links = db.scalars(select(ParseSnapshotIndexChunk).where(
        ParseSnapshotIndexChunk.index_id == index.id)).all()
    linked_ids = {row.parse_snapshot_chunk_id for row in links}
    expected_ids = {row.id for row in chunks}
    if linked_ids and linked_ids != expected_ids:
        raise ValueError("snapshot index chunk mapping is inconsistent")
    if not linked_ids:
        for chunk in chunks:
            db.add(ParseSnapshotIndexChunk(index_id=index.id, parse_snapshot_chunk_id=chunk.id))
        db.flush()
    index.status = "ready" if _snapshot_index_vectors_complete(db, index) else "building"
    db.flush()
    return index


def _index_is_selectable(db: Session, index: ParseSnapshotIndex) -> bool:
    snapshot = db.get(ParseSnapshot, index.parse_snapshot_id)
    version = db.get(DocumentVersion, index.document_version_id)
    return bool(snapshot and version and snapshot.status == "ready"
                and snapshot.document_version_id == version.id
                and snapshot.source_sha256 == version.sha256
                and index.status == "ready" and _snapshot_index_vectors_complete(db, index))


def activate_snapshot_index(db: Session, index_id: str) -> DocumentIndexSelection:
    """Atomically switch the opt-in pointer to a fully ready snapshot index."""
    index = db.get(ParseSnapshotIndex, index_id)
    if not index or not _index_is_selectable(db, index):
        raise ValueError("snapshot index is not complete and ready")
    selection = db.get(DocumentIndexSelection, index.document_version_id)
    if selection is None:
        legacy = db.scalars(select(SearchIndex).where(
            SearchIndex.document_version_id == index.document_version_id,
            SearchIndex.status == "ready",
            SearchIndex.embedding_model == index.embedding_model,
            SearchIndex.dimensions == index.dimensions
        ).order_by(SearchIndex.created_at.desc(), SearchIndex.id)).first()
        if legacy is None:
            selection = DocumentIndexSelection(document_version_id=index.document_version_id,
                                               active_legacy_index_id=None,
                                               active_snapshot_index_id=index.id)
            db.add(selection)
            db.flush()
            return selection
        selection = DocumentIndexSelection(document_version_id=index.document_version_id,
                                           active_legacy_index_id=legacy.id,
                                           active_snapshot_index_id=None)
        db.add(selection)
        db.flush()
    selection.previous_legacy_index_id = selection.active_legacy_index_id
    selection.previous_snapshot_index_id = selection.active_snapshot_index_id
    selection.active_legacy_index_id = None
    selection.active_snapshot_index_id = index.id
    selection.updated_at = datetime.now(timezone.utc)
    db.flush()
    return selection


def rollback_snapshot_index(db: Session, document_version_id: str, failed_index_id: str) -> bool:
    """Restore the prior ready generation, deleting the pointer to return to legacy default if absent."""
    selection = db.get(DocumentIndexSelection, document_version_id)
    if not selection or selection.active_snapshot_index_id != failed_index_id:
        raise ValueError("the requested snapshot index is not the active generation")
    if selection.previous_legacy_index_id:
        prior = db.get(SearchIndex, selection.previous_legacy_index_id)
        if not prior or prior.document_version_id != document_version_id or prior.status != "ready":
            raise ValueError("previous legacy index is not ready; pointer was not changed")
    elif selection.previous_snapshot_index_id:
        prior = db.get(ParseSnapshotIndex, selection.previous_snapshot_index_id)
        if not prior or prior.document_version_id != document_version_id or not _index_is_selectable(db, prior):
            raise ValueError("previous snapshot index is not ready; pointer was not changed")
    else:
        db.delete(selection)
        db.flush()
        return True
    selection.active_legacy_index_id = selection.previous_legacy_index_id
    selection.active_snapshot_index_id = selection.previous_snapshot_index_id
    selection.previous_legacy_index_id = None
    selection.previous_snapshot_index_id = None
    db.flush()
    return True
