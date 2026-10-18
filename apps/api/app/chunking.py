"""Stable, location-preserving text chunks built only from parsed PDF blocks."""
from __future__ import annotations

import hashlib
import re
from typing import Any


CHUNKER_VERSION = "papertrail-word-window-v2"
WORD_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]")


def build_chunks(blocks: list[dict[str, Any]], *, target_words: int = 500, overlap_words: int = 60) -> list[dict[str, Any]]:
    if target_words <= 0 or overlap_words < 0 or overlap_words >= target_words:
        raise ValueError("Require target_words > overlap_words >= 0")
    # Keep parsed block text byte-for-byte intact; chunks join original block strings with a newline.
    output: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    count = 0

    def emit(items: list[dict[str, Any]]) -> None:
        if not items:
            return
        text = "\n".join(item["text"] for item in items).strip()
        if not text:
            return
        unique_spans = []
        seen = set()
        for item in items:
            key = item["block_id"]
            if key in seen:
                continue
            seen.add(key)
            unique_spans.append({"block_id": key, "pdf_page_index": item["pdf_page_index"],
                                 "bbox": item["bbox"], "page_width": item["page_width"],
                                 "page_height": item["page_height"], "source_text": item["text"]})
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        output.append({"ordinal": len(output), "text": text, "token_count": len(WORD_RE.findall(text)),
                       "content_hash": digest, "section_path": items[0]["section_path"], "spans": unique_spans})

    def fragment(block: dict[str, Any], start_word: int, end_word: int) -> dict[str, Any]:
        matches = list(WORD_RE.finditer(block["text"]))
        piece = dict(block)
        piece["text"] = block["text"][matches[start_word].start():matches[end_word - 1].end()]
        return piece

    for block in blocks:
        text = block["text"].strip()
        words = WORD_RE.findall(text)
        if not words:
            continue
        if current and block["section_path"] != current[-1]["section_path"]:
            emit(current)
            current, count = [], 0
        if len(words) > target_words:
            emit(current)
            current, count = [], 0
            # Long source blocks are split into word windows. The same source bbox is retained
            # for each fragment, so citation highlighting remains honest but coarse.
            for start in range(0, len(words), target_words - overlap_words):
                end = min(len(words), start + target_words)
                if end <= start:
                    break
                emit([fragment(block, start, end)])
                if start + target_words >= len(words):
                    break
            continue
        if current and count + len(words) > target_words:
            emit(current)
            overlap = []
            remaining = overlap_words
            for item in reversed(current):
                item_words = WORD_RE.findall(item["text"])
                if len(item_words) <= remaining:
                    overlap.insert(0, item)
                    remaining -= len(item_words)
                else:
                    if remaining:
                        overlap.insert(0, fragment(item, len(item_words) - remaining, len(item_words)))
                    break
            current = overlap
            count = sum(len(WORD_RE.findall(item["text"])) for item in current)
        current.append(block)
        count += len(words)
    emit(current)
    return output


def stable_chunk_id(document_version_id: str, ordinal: int, content_hash: str, namespace: str = "") -> str:
    key = f"{document_version_id}:{namespace}:{ordinal}:{content_hash}".encode("utf-8")
    return "ch_" + hashlib.sha256(key).hexdigest()[:24]
