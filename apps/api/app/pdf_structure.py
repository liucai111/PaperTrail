"""Offline, versioned PDF structure extraction for Stage 15 sidecar experiments.

This module deliberately does not write PaperTrail's database, chunks, vectors,
or QA/Evidence rows. It uses native PyMuPDF text first and the existing optional
OCR runtime only for pages with no native text.
"""
from __future__ import annotations

import hashlib
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pymupdf

from . import ocr


PROTOCOL_VERSION = "papertrail-structure-v1"
LAYOUT_ALGORITHM_VERSION = "geometry-order-v13"
PARSER_BASE_IDENTITY = f"pymupdf-{pymupdf.VersionBind}-{PROTOCOL_VERSION}-{LAYOUT_ALGORITHM_VERSION}"
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+){0,4})\s+([A-Z][^.!?]{0,110})\s*$")
_NAMED_HEADINGS = {
    "abstract", "introduction", "related work", "background", "method", "methods",
    "methodology", "approach", "experiments", "experimental setup", "results",
    "discussion", "limitations", "conclusion", "conclusions", "acknowledgments",
    "acknowledgements", "references", "appendix",
}
_CAPTION = re.compile(r"^\s*(?:fig(?:ure)?|table)\s+[\w.-]+\s*[:.]?", re.I)
_FIGURE_CAPTION = re.compile(r"^\s*(?:fig(?:ure)?|fig\.)\s+\d+[A-Za-z]?\s*[:.\-–—]", re.I)
_TABLE_CAPTION = re.compile(r"^\s*(?:table|tab\.)\s+\d+[A-Za-z]?\s*[:.\-–—]", re.I)
_EXPLICIT_UNIT = re.compile(
    r"(?<!\w)(%|ms|milliseconds?|seconds?|tokens?|T?FLOPs?|time\s*\(\s*s\s*\)|percentage\s+points?)(?!\w)", re.I)
_PAGE_NUMBER = re.compile(r"^(?:\d{1,4}|[ivxlcdm]{1,8})$", re.I)
_TERMINAL = re.compile(r"[.!?。！？;；:]\s*[\"'”’)]*$")
_TOKEN = re.compile(r"\d+")


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _margin_signature(text: str) -> str:
    return _TOKEN.sub("#", _clean_text(text)).casefold()


def _box_key(box: tuple[float, ...] | list[float]) -> tuple[float, ...]:
    return tuple(round(float(value), 1) for value in box[:4])


def _style_by_box(page) -> dict[tuple[float, ...], dict[str, Any]]:
    result: dict[tuple[float, ...], dict[str, Any]] = {}
    try:
        data = page.get_text("dict")
    except Exception:
        return result
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        spans = [span for line in block.get("lines", []) for span in line.get("spans", []) if span.get("text", "").strip()]
        if not spans:
            continue
        chars = sum(len(span.get("text", "")) for span in spans) or 1
        bold_chars = sum(len(span.get("text", "")) for span in spans
                         if int(span.get("flags", 0)) & 16 or "bold" in str(span.get("font", "")).casefold())
        sizes = [float(span.get("size", 0)) for span in spans if math.isfinite(float(span.get("size", 0)))]
        result[_box_key(block.get("bbox", []))] = {
            "font_size": max(sizes, default=0.0), "bold_ratio": bold_chars / chars,
        }
    return result


def _native_page_blocks(page, page_index: int) -> list[dict[str, Any]]:
    styles = _style_by_box(page)
    output = []
    for source_ordinal, block in enumerate(page.get_text("blocks", sort=False)):
        if len(block) < 7 or block[6] != 0 or not str(block[4]).strip():
            continue
        box = [round(float(value), 2) for value in block[:4]]
        output.append({"text": str(block[4]), "bbox": box, "kind": "text", "source": "native",
                       "source_ordinal": source_ordinal, "pdf_page_index": page_index,
                       **styles.get(_box_key(box), {})})
    return output


def _column_split(blocks: list[dict[str, Any]], page_width: float, page_height: float) -> float | None:
    """Return a conservative x split only when both text columns have real support."""
    eligible = [item for item in blocks
                if item["bbox"][2] - item["bbox"][0] <= page_width * 0.66
                and len(_clean_text(item["text"])) >= 16]
    centers = sorted((item["bbox"][0] + item["bbox"][2]) / 2 for item in eligible)
    if len(centers) < 4:
        return None
    gaps = [(centers[index + 1] - centers[index], (centers[index + 1] + centers[index]) / 2)
            for index in range(len(centers) - 1)]
    gap, split = max(gaps, default=(0.0, 0.0))
    if gap < page_width * 0.12 or not page_width * 0.32 <= split <= page_width * 0.68:
        return None
    left = [item for item in eligible if (item["bbox"][0] + item["bbox"][2]) / 2 < split]
    right = [item for item in eligible if (item["bbox"][0] + item["bbox"][2]) / 2 >= split]
    # A page can expose only one large block per column (for example after
    # PyMuPDF groups line runs). Accept that sparse case only when both blocks
    # are substantial and their vertical extents overlap clearly.
    sparse_columns = (len(left) >= 1 and len(right) >= 1
                      and all(max(item["bbox"][2] - item["bbox"][0], 0) <= page_width * 0.50
                              and len(_clean_text(item["text"])) >= 120 for item in left + right))
    if (len(left) < 3 or len(right) < 3) and not sparse_columns:
        return None
    left_y = (min(item["bbox"][1] for item in left), max(item["bbox"][3] for item in left))
    right_y = (min(item["bbox"][1] for item in right), max(item["bbox"][3] for item in right))
    overlap = max(0.0, min(left_y[1], right_y[1]) - max(left_y[0], right_y[0]))
    if overlap < page_height * 0.18:
        return None
    if statistics.median(item["bbox"][2] - item["bbox"][0] for item in left + right) > page_width * 0.54:
        return None
    return split


def _reading_order(blocks: list[dict[str, Any]], width: float, height: float) -> tuple[list[dict[str, Any]], str, str]:
    if len(blocks) < 6:
        return list(blocks), "unresolved", "source_order_insufficient_blocks"
    split = _column_split(blocks, width, height)
    if split is None:
        wide = sum(item["bbox"][2] - item["bbox"][0] >= width * 0.64 for item in blocks)
        if wide >= max(3, math.ceil(len(blocks) * 0.45)):
            return list(blocks), "single_column", "source_order_preserved"
        return list(blocks), "unresolved", "source_order_preserved_layout_ambiguous"

    spanning = []
    columns = []
    for item in blocks:
        x0, _, x1, _ = item["bbox"]
        crosses_gutter = x0 < split - width * 0.05 and x1 > split + width * 0.05
        if (x1 - x0) >= width * 0.62 or crosses_gutter:
            spanning.append(item)
        else:
            columns.append(item)

    anchors = sorted(spanning, key=lambda item: (item["bbox"][1], item["bbox"][0], item["source_ordinal"]))
    # Merge vertically overlapping full-width blocks into a single layout
    # boundary. Treating each line of a centered title as a separate boundary
    # can otherwise place the right column before the left column.
    anchor_bands: list[tuple[float, float, list[dict[str, Any]]]] = []
    for anchor in anchors:
        top, bottom = anchor["bbox"][1], anchor["bbox"][3]
        if anchor_bands and top <= anchor_bands[-1][1] + height * 0.012:
            old_top, old_bottom, members = anchor_bands[-1]
            anchor_bands[-1] = (old_top, max(old_bottom, bottom), members + [anchor])
        else:
            anchor_bands.append((top, bottom, [anchor]))
    ordered: list[dict[str, Any]] = []
    lower_bound = float("-inf")
    for anchor_top, anchor_bottom, members in anchor_bands:
        band = [item for item in columns if lower_bound <= (item["bbox"][1] + item["bbox"][3]) / 2 < anchor_top]
        ordered.extend(sorted((item for item in band if (item["bbox"][0] + item["bbox"][2]) / 2 < split),
                              key=lambda item: (item["bbox"][1], item["bbox"][0], item["source_ordinal"])))
        ordered.extend(sorted((item for item in band if (item["bbox"][0] + item["bbox"][2]) / 2 >= split),
                              key=lambda item: (item["bbox"][1], item["bbox"][0], item["source_ordinal"])))
        ordered.extend(members)
        lower_bound = max(lower_bound, anchor_bottom)
    tail = [item for item in columns if (item["bbox"][1] + item["bbox"][3]) / 2 >= lower_bound]
    ordered.extend(sorted((item for item in tail if (item["bbox"][0] + item["bbox"][2]) / 2 < split),
                          key=lambda item: (item["bbox"][1], item["bbox"][0], item["source_ordinal"])))
    ordered.extend(sorted((item for item in tail if (item["bbox"][0] + item["bbox"][2]) / 2 >= split),
                          key=lambda item: (item["bbox"][1], item["bbox"][0], item["source_ordinal"])))
    ordered_ids = {id(item) for item in ordered}
    ordered.extend(item for item in blocks if id(item) not in ordered_ids)
    has_mixed_bands = bool(anchors)
    return ordered, "mixed" if has_mixed_bands else "two_column", "geometry_two_column"


def _is_page_number(text: str, box: list[float], width: float, height: float) -> bool:
    cleaned = _clean_text(text)
    in_margin = box[1] <= height * 0.10 or box[3] >= height * 0.90
    return in_margin and len(cleaned) <= 8 and bool(_PAGE_NUMBER.fullmatch(cleaned))


def _dense_vector_regions(page) -> list[dict[str, Any]]:
    """Return bounded vector-density zones as geometry-only figure candidates."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    page_area = max(float(page.rect.width) * float(page.rect.height), 1.0)
    shape_boxes = []
    for drawing in drawings:
        raw = drawing.get("rect")
        if not raw:
            continue
        box = [float(raw.x0), float(raw.y0), float(raw.x1), float(raw.y1)]
        area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
        if area > 0 and area <= page_area * 0.05:
            shape_boxes.append(box)
    if len(shape_boxes) < 12:
        return []
    tile_w, tile_h = 80.0, 60.0
    tile_counts: Counter[tuple[int, int]] = Counter()
    for x0, y0, x1, y1 in shape_boxes:
        for tx in range(max(0, int(x0 // tile_w)), max(0, int(max(x0, x1 - 0.01) // tile_w)) + 1):
            for ty in range(max(0, int(y0 // tile_h)), max(0, int(max(y0, y1 - 0.01) // tile_h)) + 1):
                tile_counts[(tx, ty)] += 1
    dense = {tile for tile, count in tile_counts.items() if count >= 4}
    # Form conservative 8-connected drawing-density zones. Dense zones are
    # only candidate figure regions; this does not claim image understanding.
    zones = []
    while dense:
        seed = dense.pop()
        component = {seed}
        queue = [seed]
        while queue:
            tx, ty = queue.pop()
            for nx in range(tx - 1, tx + 2):
                for ny in range(ty - 1, ty + 2):
                    neighbor = (nx, ny)
                    if neighbor in dense:
                        dense.remove(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
        if len(component) >= 3:
            box = [min(x for x, _ in component) * tile_w,
                   min(y for _, y in component) * tile_h,
                   (max(x for x, _ in component) + 1) * tile_w,
                   (max(y for _, y in component) + 1) * tile_h]
            if (box[2] - box[0]) * (box[3] - box[1]) <= page_area * 0.60:
                overlapping = sum(1 for sx0, sy0, sx1, sy1 in shape_boxes
                                  if max(0.0, min(box[2], sx1) - max(box[0], sx0))
                                  * max(0.0, min(box[3], sy1) - max(box[1], sy0)) > 0)
                zones.append({"bbox": [round(value, 2) for value in box],
                              "vector_shape_count": overlapping})
    return sorted(zones, key=lambda item: (item["bbox"][1], item["bbox"][0]))


def _mark_visual_text_candidates(page, blocks: list[dict[str, Any]]) -> int:
    """Flag text overlapping many small vector shapes; never discard text."""
    regions = _dense_vector_regions(page)
    if not regions:
        return 0
    try:
        drawings = page.get_drawings()
    except Exception:
        return 0
    page_area = max(float(page.rect.width) * float(page.rect.height), 1.0)
    shape_boxes = []
    for drawing in drawings:
        raw = drawing.get("rect")
        if not raw:
            continue
        box = [float(raw.x0), float(raw.y0), float(raw.x1), float(raw.y1)]
        area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
        if area > 0 and area <= page_area * 0.05:
            shape_boxes.append(box)
    count = 0
    for item in blocks:
        # Caption blocks remain prose anchors for explicit figure/table
        # relations even when vector strokes touch their bounding boxes.
        if _FIGURE_CAPTION.match(item.get("text", "")) or _TABLE_CAPTION.match(item.get("text", "")):
            continue
        x0, y0, x1, y1 = item["bbox"]
        area = max(1.0, (x1 - x0) * (y1 - y0))
        overlaps = 0
        for sx0, sy0, sx1, sy1 in shape_boxes:
            intersection = max(0.0, min(x1, sx1) - max(x0, sx0)) * max(0.0, min(y1, sy1) - max(y0, sy0))
            if intersection / area >= 0.05:
                overlaps += 1
        center_x, center_y = (x0 + x1) / 2, (y0 + y1) / 2
        in_dense_zone = any(zx0 - 40 <= center_x <= zx1 + 40
                            and zy0 - 30 <= center_y <= zy1 + 30
                            for zx0, zy0, zx1, zy1 in (region["bbox"] for region in regions))
        if overlaps >= 4 or (in_dense_zone and len(_clean_text(item["text"])) <= 180):
            item["role"] = "figure_text_candidate"
            item["quality_flags"].append("overlaps_dense_vector_region")
            count += 1
    return count


def _normalized_cell_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _table_caption_zone(page, caption: dict[str, Any], side: str):
    width, height = float(page.rect.width), float(page.rect.height)
    x0, y0, x1, y1 = caption["bbox"]
    if x1 - x0 >= width * 0.62:
        clip_x0, clip_x1 = 0.0, width
    else:
        # Keep a caption in its column. Expand slightly for table edges that
        # extend a few points beyond the caption text.
        clip_x0, clip_x1 = max(0.0, x0 - 14.0), min(width, x1 + 14.0)
    if side == "above":
        clip_y0, clip_y1 = max(0.0, y0 - height * 0.48), y0
    else:
        clip_y0, clip_y1 = y1, min(height, y1 + height * 0.48)
    if clip_y1 - clip_y0 < 18:
        return None
    return pymupdf.Rect(clip_x0, clip_y0, clip_x1, clip_y1)


def _table_candidate_score(table, caption: dict[str, Any], strategy: str,
                          page_height: float, clip_area: float) -> tuple[float, dict[str, Any]] | None:
    rows = table.extract() or []
    row_count, col_count = int(table.row_count), int(table.col_count)
    if row_count < 3 or col_count < 2 or col_count > 16 or len(rows) < 3:
        return None
    cells = [str(value or "").strip() for row in rows for value in row]
    nonempty = sum(bool(value) for value in cells)
    numeric = sum(bool(re.search(r"\d", value)) for value in cells)
    if not cells or nonempty / len(cells) < 0.20:
        return None
    caption_text = _clean_text(caption["text"]).casefold()
    caption_hints_textual_table = any(word in caption_text for word in ("case study", "prompt"))
    numeric_density = numeric / max(1, nonempty)
    if numeric < 2 and not caption_hints_textual_table:
        return None
    # Text-mode table finding can partition ordinary paragraphs into columns.
    # Numeric evaluation tables need at least modest numeric density; textual
    # prompt/case-study matrices remain candidates but are never resolved here.
    if numeric >= 3 and numeric_density < 0.15:
        return None
    bbox = [round(float(value), 2) for value in table.bbox]
    cb = caption["bbox"]
    x_overlap = max(0.0, min(bbox[2], cb[2]) - max(bbox[0], cb[0]))
    horizontal_fraction = x_overlap / max(1.0, min(bbox[2] - bbox[0], cb[2] - cb[0]))
    vertical_gap = max(0.0, cb[1] - bbox[3], bbox[1] - cb[3])
    if horizontal_fraction < 0.20 or vertical_gap > page_height * 0.28:
        return None
    line_bonus = 0.10 if strategy in {"lines", "text_lines"} else 0.0
    table_area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
    area_fraction = min(1.0, table_area / max(1.0, clip_area))
    # Caption-adjacent partial fragments often have denser cells than the
    # actual table. Prefer a candidate covering more of the bounded region,
    # while still keeping sparse/ambiguous candidates explicitly unresolved.
    score = ((nonempty / len(cells)) + min(numeric / max(1, len(cells)), 0.5)
             + line_bonus + 0.75 * area_fraction + 0.50 * numeric_density - vertical_gap / 1000)
    return score, {"table": table, "rows": rows, "row_count": row_count, "col_count": col_count,
                   "bbox": bbox, "cells": cells, "nonempty_fraction": nonempty / len(cells),
                   "numeric_cell_count": numeric, "strategy": strategy,
                   "numeric_density": numeric_density,
                   "area_fraction": area_fraction,
                   "horizontal_overlap_fraction": horizontal_fraction, "caption_gap": vertical_gap}


def _extract_tables(page, blocks: list[dict[str, Any]], page_index: int, identity: str,
                    source_sha256: str) -> list[dict[str, Any]]:
    """Extract caption-scoped table candidates; never infer missing cell text."""
    candidates = [item for item in blocks if _TABLE_CAPTION.match(item.get("text", ""))]
    structures: list[dict[str, Any]] = []
    used_bboxes: list[list[float]] = []
    page_height = float(page.rect.height)
    for caption in candidates:
        best: tuple[float, dict[str, Any]] | None = None
        strategies = (
            ("lines", {"strategy": "lines"}),
            ("text_lines", {"vertical_strategy": "text", "horizontal_strategy": "lines"}),
            ("text", {"strategy": "text"}),
        )
        for side in ("above", "below"):
            clip = _table_caption_zone(page, caption, side)
            if clip is None:
                continue
            for strategy_name, kwargs in strategies:
                try:
                    finder = page.find_tables(clip=clip, **kwargs)
                except Exception:
                    continue
                for table in finder.tables:
                    candidate = _table_candidate_score(
                        table, caption, strategy_name, page_height,
                        max(1.0, float(clip.width) * float(clip.height)))
                    if candidate is None:
                        continue
                    score, detail = candidate
                    detail["caption_side"] = side
                    if best is None or score > best[0]:
                        best = (score, detail)
        if best is None:
            continue
        score, detail = best
        bbox = detail["bbox"]
        if any(_bbox_iou(bbox, previous) >= 0.72 for previous in used_bboxes):
            continue
        used_bboxes.append(bbox)
        raw_rows = detail["rows"]
        selected_table = detail["table"]
        cell_rects = list(getattr(selected_table, "cells", []) or [])
        flat_rects = cell_rects if len(cell_rects) == detail["row_count"] * detail["col_count"] else []
        cells_out = []
        source_ids: set[str] = set()
        table_texts = []
        for row_index, row in enumerate(raw_rows):
            row_out = []
            for col_index in range(detail["col_count"]):
                text = str(row[col_index] or "").strip() if col_index < len(row) else ""
                flat_index = row_index * detail["col_count"] + col_index
                raw_rect = flat_rects[flat_index] if flat_rects else None
                cell_bbox = ([round(float(value), 2) for value in raw_rect]
                             if raw_rect is not None else None)
                source_block_id = None
                if text and len(_normalized_cell_text(text)) >= 3:
                    needle = _normalized_cell_text(text)
                    for block in blocks:
                        if block.get("source") != "native":
                            continue
                        normalized = _normalized_cell_text(block.get("text", ""))
                        block_box = block.get("bbox")
                        if needle in normalized and (cell_bbox is None or _bbox_iou(block_box, cell_bbox) > 0):
                            source_block_id = block["block_id"]
                            source_ids.add(source_block_id)
                            break
                flags = []
                if cell_bbox is None:
                    flags.append("cell_bbox_unresolved")
                if text and source_block_id is None:
                    flags.append("source_block_unmatched")
                row_out.append({"row_index": row_index, "column_index": col_index,
                                "text": text, "bbox": cell_bbox,
                                "source_block_id": source_block_id,
                                "quality_flags": flags})
                if text:
                    table_texts.append(text)
            cells_out.append(row_out)
        row0 = raw_rows[0] if raw_rows else []
        header_candidate = [str(value or "").strip() for value in row0]
        numeric = detail["numeric_cell_count"]
        header_confident = bool(header_candidate and any(value for value in header_candidate)
                                and sum(bool(re.search(r"\d", value)) for value in header_candidate)
                                <= max(1, len(header_candidate) // 3)
                                and numeric >= max(2, int(detail["row_count"] * detail["col_count"] * 0.08)))
        geometry_texts = [block["text"] for block in blocks
                          if block.get("source") == "native"
                          and (_bbox_iou(block.get("bbox"), bbox) > 0
                               or (bbox[0] <= (block["bbox"][0] + block["bbox"][2]) / 2 <= bbox[2]
                                   and bbox[1] <= (block["bbox"][1] + block["bbox"][3]) / 2 <= bbox[3])
                               or (_vertical_gap(block.get("bbox"), bbox) <= 24.0
                                   and _horizontal_overlap_fraction(block.get("bbox"), bbox) >= 0.20))]
        units = sorted({match.group(0) for text in [*table_texts, *geometry_texts, caption["text"]]
                        for match in _EXPLICIT_UNIT.finditer(text)}, key=str.casefold)
        flags = []
        if not flat_rects:
            flags.append("cell_geometry_unresolved")
        if not header_confident:
            flags.append("header_row_unresolved")
        if sum(cell["source_block_id"] is not None for row in cells_out for cell in row) < max(1, int(detail["row_count"] * detail["col_count"] * 0.25)):
            flags.append("cell_source_mapping_partial")
        if any(any(cell["quality_flags"] for cell in row) for row in cells_out):
            flags.append("some_cells_unresolved")
        table_id = "tbl_" + hashlib.sha256(
            f"{identity}:{source_sha256}:{page_index}:{bbox}:{caption['block_id']}".encode("utf-8")
        ).hexdigest()[:24]
        first_col_nonempty = sum(bool(row[0].get("text", "").strip()) for row in cells_out)
        other_col_nonempty = sum(bool(cell.get("text", "").strip())
                                 for row in cells_out for cell in row[1:])
        group_candidates = []
        for row_index, row in enumerate(cells_out):
            label = row[0]
            if label.get("text", "").strip() and not any(cell.get("text", "").strip() for cell in row[1:]):
                group_candidates.append({"row_index": row_index, "text": label["text"],
                                         "source_block_id": label["source_block_id"],
                                         "status": "candidate_unresolved"})
        caption["relations"].append({"type": "caption_for_table", "target_table_id": table_id})
        structures.append({
            "table_id": table_id, "pdf_page_index": page_index, "bbox": bbox,
            "source": "pymupdf_table_finder", "extraction_strategy": detail["strategy"],
            "row_count": detail["row_count"], "column_count": detail["col_count"],
            "caption_block_id": caption["block_id"], "caption_text": _clean_text(caption["text"]),
            "header_row_candidate_index": 0 if header_candidate else None,
            "header_status": "candidate" if header_confident else "unresolved",
            "row_label_column_candidate": 0 if first_col_nonempty >= other_col_nonempty else None,
            "experiment_group_candidates": group_candidates,
            "group_status": "candidate_unresolved" if group_candidates else "unresolved",
            "explicit_units": units, "cells": cells_out,
            "source_block_ids": sorted(source_ids),
            "status": "partial" if not flags else "unresolved",
            "quality_flags": flags,
            "diagnostics": {"nonempty_fraction": round(detail["nonempty_fraction"], 3),
                            "numeric_cell_count": detail["numeric_cell_count"],
                            "numeric_density": round(detail["numeric_density"], 3),
                            "caption_gap": round(detail["caption_gap"], 2),
                            "horizontal_overlap_fraction": round(detail["horizontal_overlap_fraction"], 3),
                            "area_fraction": round(detail["area_fraction"], 3),
                            "candidate_score": round(score, 3)},
        })
    return structures


def _vertical_gap(left: list[float], right: list[float]) -> float:
    return max(0.0, right[1] - left[3], left[1] - right[3])


def _horizontal_overlap_fraction(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    return overlap / max(1.0, min(left[2] - left[0], right[2] - right[0]))


def _overlap_fraction_of_left(left: list[float], right: list[float]) -> float:
    overlap = max(0.0, min(left[2], right[2]) - max(left[0], right[0]))
    overlap *= max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
    return overlap / max(1.0, (left[2] - left[0]) * (left[3] - left[1]))


def _extract_figures(page, blocks: list[dict[str, Any]], page_index: int, identity: str,
                     source_sha256: str) -> list[dict[str, Any]]:
    """Keep observed image/vector geometry and only link explicit nearby captions."""
    width, height = float(page.rect.width), float(page.rect.height)
    page_area = max(1.0, width * height)
    raw_regions: list[dict[str, Any]] = []
    try:
        image_infos = page.get_image_info(xrefs=True)
    except Exception:
        image_infos = []
    for info in image_infos:
        raw = info.get("bbox")
        if not raw or len(raw) < 4:
            continue
        bbox = [round(float(value), 2) for value in raw[:4]]
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area < page_area * 0.012 or area > page_area * 0.78:
            continue
        raw_regions.append({"bbox": bbox, "source": "native_image",
                            "xref": int(info.get("xref", 0)) or None})
    for region in _dense_vector_regions(page):
        bbox = region["bbox"]
        area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
        if area < page_area * 0.012:
            continue
        if any(_bbox_iou(bbox, existing["bbox"]) >= 0.72 for existing in raw_regions):
            continue
        raw_regions.append({"bbox": bbox, "source": "vector_density_candidate",
                            "vector_shape_count": region["vector_shape_count"]})

    captions = [item for item in blocks if _FIGURE_CAPTION.match(item.get("text", ""))]
    # Some vector figures are made from sparse paths and evade the density
    # detector. With an explicit Figure caption, retain a geometry-only
    # candidate from the nearby drawing paths; no nearby prose is absorbed.
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    for caption in captions:
        if any(_vertical_gap(region["bbox"], caption["bbox"]) <= height * 0.16
               and _horizontal_overlap_fraction(region["bbox"], caption["bbox"]) >= 0.12
               for region in raw_regions):
            continue
        side_candidates = []
        for side in ("above", "below"):
            selected = []
            for drawing in drawings:
                raw = drawing.get("rect")
                if not raw:
                    continue
                box = [float(raw.x0), float(raw.y0), float(raw.x1), float(raw.y1)]
                area = max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])
                if area > page_area * 0.30:
                    continue
                if side == "above" and (box[1] + box[3]) / 2 > caption["bbox"][1]:
                    continue
                if side == "below" and (box[1] + box[3]) / 2 < caption["bbox"][3]:
                    continue
                if (_vertical_gap(box, caption["bbox"]) <= height * 0.16
                        and _horizontal_overlap_fraction(box, caption["bbox"]) >= 0.12):
                    selected.append(box)
            if len(selected) < 4:
                continue
            bbox = [round(min(box[0] for box in selected), 2),
                    round(min(box[1] for box in selected), 2),
                    round(max(box[2] for box in selected), 2),
                    round(max(box[3] for box in selected), 2)]
            area = max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
            if (area >= page_area * 0.008 and area <= page_area * 0.60
                    and bbox[2] - bbox[0] >= width * 0.08):
                side_candidates.append({"bbox": bbox, "source": "caption_proximate_vector_candidate",
                                        "vector_shape_count": len(selected),
                                        "caption_block_id": caption["block_id"]})
        if side_candidates:
            side_candidates.sort(key=lambda item: (
                _vertical_gap(item["bbox"], caption["bbox"]), -item["vector_shape_count"], item["bbox"]))
            if (len(side_candidates) == 1 or
                    abs(_vertical_gap(side_candidates[0]["bbox"], caption["bbox"])
                        - _vertical_gap(side_candidates[1]["bbox"], caption["bbox"])) > height * 0.015):
                candidate = side_candidates[0]
                if not any(_bbox_iou(candidate["bbox"], existing["bbox"]) >= 0.72 for existing in raw_regions):
                    raw_regions.append(candidate)
    figures = []
    used_caption_ids: set[str] = set()
    for ordinal, region in enumerate(sorted(raw_regions, key=lambda item: (item["bbox"][1], item["bbox"][0]))):
        bbox = region["bbox"]
        matches = []
        for caption in captions:
            capbox = caption["bbox"]
            gap = _vertical_gap(bbox, capbox)
            horizontal = _horizontal_overlap_fraction(bbox, capbox)
            if gap <= height * 0.16 and horizontal >= 0.12:
                matches.append((gap, -horizontal, caption))
        matches.sort(key=lambda item: (item[0], item[1], item[2]["block_id"]))
        caption = None
        if region.get("caption_block_id"):
            caption = next((item for item in captions
                            if item["block_id"] == region["caption_block_id"]
                            and item["block_id"] not in used_caption_ids), None)
        if matches:
            # If two explicit captions are essentially equally close, do not
            # guess which graphic they describe.
            available_matches = [item for item in matches if item[2]["block_id"] not in used_caption_ids]
            if caption is None and available_matches:
                if len(available_matches) == 1 or available_matches[1][0] - available_matches[0][0] > height * 0.015:
                    caption = available_matches[0][2]
        source_blocks = []
        for block in blocks:
            if block.get("role") != "figure_text_candidate":
                continue
            if _overlap_fraction_of_left(block["bbox"], bbox) >= 0.35:
                source_blocks.append(block["block_id"])
        fig_id = "fig_" + hashlib.sha256(
            f"{identity}:{source_sha256}:{page_index}:{ordinal}:{bbox}:{region['source']}".encode("utf-8")
        ).hexdigest()[:24]
        flags = ["figure_semantics_not_inferred"]
        if region["source"] == "vector_density_candidate":
            flags.append("vector_region_is_geometry_candidate")
        if region["source"] == "caption_proximate_vector_candidate":
            flags.append("caption_proximity_vector_region_unverified")
        if caption is None:
            flags.append("caption_link_unresolved")
        else:
            caption["relations"].append({"type": "caption_for_figure", "target_figure_id": fig_id})
            used_caption_ids.add(caption["block_id"])
        figures.append({"figure_id": fig_id, "pdf_page_index": page_index,
                        "bbox": bbox, "source": region["source"],
                        "caption_block_id": caption["block_id"] if caption else None,
                        "caption_text": _clean_text(caption["text"]) if caption else None,
                        "source_block_ids": sorted(source_blocks),
                        "status": "caption_linked" if caption else "unresolved",
                        "quality_flags": flags})
    return figures


_EQUATION_NUMBER = re.compile(r"(?:\s|^)[([]\s*(\d{1,3}[a-z]?)\s*[)\]]\s*$", re.I)
_MATH_OPERATORS = re.compile(r"(?:=|≤|≥|≠|≈|∑|∏|∫|→|←|∈|∉|\b(?:arg\s*max|arg\s*min|sup|inf)\b)")


def _extract_formulas(blocks: list[dict[str, Any]], page_index: int,
                     identity: str, source_sha256: str, page_width: float) -> list[dict[str, Any]]:
    formulas = []
    for block in blocks:
        if block.get("source") != "native" or block.get("role") in {
                "caption", "heading", "page_number", "page_header_candidate", "page_footer_candidate",
                "figure_text_candidate"}:
            continue
        text = block.get("text", "").strip()
        clean = _clean_text(text)
        if not clean or len(clean) > 360 or not _MATH_OPERATORS.search(clean):
            continue
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) > 5:
            continue
        numbered = _EQUATION_NUMBER.search(clean)
        x_center = (block["bbox"][0] + block["bbox"][2]) / 2
        centered = abs(x_center - page_width / 2) <= page_width * 0.20
        word_like = re.findall(r"[A-Za-z]{3,}", clean)
        simple_expression = (len(clean) <= 90 and centered and len(word_like) <= 2
                             and not re.search(r"[.!?;]", clean))
        numbered_equation = bool(numbered and len(clean) <= 220 and "=" in clean)
        if not (numbered_equation or simple_expression):
            continue
        equation_number = numbered.group(1) if numbered else None
        formula_id = "eq_" + hashlib.sha256(
            f"{identity}:{source_sha256}:{page_index}:{block['block_id']}".encode("utf-8")
        ).hexdigest()[:24]
        block["relations"].append({"type": "formula_candidate", "target_formula_id": formula_id})
        flags = ["formula_structure_unresolved", "latex_not_inferred"]
        if equation_number is None:
            flags.append("equation_number_missing")
        formulas.append({"formula_id": formula_id, "pdf_page_index": page_index,
                         "source_block_id": block["block_id"], "bbox": list(block["bbox"]),
                         "source": "native_text_candidate", "raw_text": text,
                         "equation_number": equation_number, "latex": None,
                         "interpretation_inferred": False, "status": "candidate",
                         "quality_flags": flags})
    return formulas


def _bbox_iou(left: list[float] | tuple[float, ...] | None,
              right: list[float] | tuple[float, ...] | None) -> float:
    if left is None or right is None or len(left) < 4 or len(right) < 4:
        return 0.0
    ix0, iy0 = max(left[0], right[0]), max(left[1], right[1])
    ix1, iy1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    union += max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1]) - intersection
    return intersection / union if union > 0 else 0.0


def _heading(text: str, style: dict[str, Any], median_font: float) -> tuple[str, int] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    value = _clean_text(" ".join(lines))
    if len(lines) == 1 and value.casefold() in _NAMED_HEADINGS:
        return value, 1
    match = _NUMBERED_HEADING.fullmatch(value)
    if match and len(value) <= 115:
        has_code_symbols = any(symbol in value for symbol in ("=", "_", "{", "}", "[", "]", "->", "<-"))
        size = float(style.get("font_size") or 0)
        bold = float(style.get("bold_ratio") or 0)
        if not has_code_symbols and (bold >= 0.35 or (median_font and size >= median_font * 1.2)):
            return value, match.group(1).count(".") + 1
    if len(lines) != 1 or len(value) > 82 or _TERMINAL.search(value) or _CAPTION.match(value):
        return None
    words = value.split()
    if not 1 <= len(words) <= 10:
        return None
    size = float(style.get("font_size") or 0)
    bold = float(style.get("bold_ratio") or 0)
    title_case = all(word[:1].isupper() or word.casefold() in {"a", "an", "and", "of", "to", "the", "in", "for", "with"} for word in words)
    if len(words) >= 2 and ((median_font and size >= median_font * 1.25 and bold >= 0.35)
                           or (bold >= 0.8 and title_case)):
        return value, 1
    return None


def _classify_margins(pages: list[dict[str, Any]]) -> None:
    occurrences: dict[str, set[int]] = defaultdict(set)
    for page in pages:
        height = page["height"]
        for item in page["_blocks"]:
            if item["role"] == "figure_text_candidate":
                continue
            box = item["bbox"]
            if box[1] <= height * 0.10 or box[3] >= height * 0.90:
                signature = _margin_signature(item["text"])
                if 3 <= len(signature) <= 100:
                    occurrences[signature].add(page["pdf_page_index"])
    threshold = max(3, math.ceil(len(pages) * 0.30))
    repeated = {signature for signature, indexes in occurrences.items() if len(indexes) >= threshold}
    for page in pages:
        height = page["height"]
        for item in page["_blocks"]:
            if item["role"] == "figure_text_candidate":
                continue
            box = item["bbox"]
            margin = box[1] <= height * 0.10 or box[3] >= height * 0.90
            signature = _margin_signature(item["text"])
            if _is_page_number(item["text"], box, page["width"], height):
                item["role"] = "page_number"
                item["quality_flags"].append("page_number_candidate")
            elif margin and signature in repeated:
                item["role"] = "page_header_candidate" if box[1] <= height * 0.10 else "page_footer_candidate"
                item["quality_flags"].append("repeated_margin_text_candidate")


def _cross_page_candidates(pages: list[dict[str, Any]]) -> int:
    count = 0
    for previous, current in zip(pages, pages[1:]):
        prev_candidates = [item for item in previous["_blocks"] if item["role"] == "paragraph"
                           and "page_header_candidate" not in item["role"]
                           and item["bbox"][3] >= previous["height"] * 0.78]
        next_candidates = [item for item in current["_blocks"] if item["role"] == "paragraph"
                           and item["bbox"][1] <= current["height"] * 0.22]
        if not prev_candidates or not next_candidates:
            continue
        left = max(prev_candidates, key=lambda item: item["reading_order"])
        right = min(next_candidates, key=lambda item: item["reading_order"])
        if left["section_path"] != right["section_path"]:
            continue
        left_text, right_text = _clean_text(left["text"]), _clean_text(right["text"])
        if not left_text or not right_text or _TERMINAL.search(left_text):
            continue
        if right_text[:1].isupper() and not left_text.endswith(("-", "‑", "–")):
            continue
        left["relations"].append({"type": "possible_continuation_to", "target_block_id": right["block_id"]})
        right["relations"].append({"type": "possible_continuation_from", "target_block_id": left["block_id"]})
        left["quality_flags"].append("possible_cross_page_continuation")
        right["quality_flags"].append("possible_cross_page_continuation")
        count += 1
    return count


def parse_pdf_structured(pdf_path: str | Path | bytes, *, ocr_runtime=None) -> dict[str, Any]:
    """Extract a deterministic sidecar structure from one PDF without DB writes."""
    if isinstance(pdf_path, bytes):
        pdf_bytes = pdf_path
        source_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    else:
        path = Path(pdf_path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        source_sha256 = digest.hexdigest()
        document = pymupdf.open(path)
    runtime = ocr if ocr_runtime is None else ocr_runtime
    identity = runtime.parser_identity(PARSER_BASE_IDENTITY)
    pages: list[dict[str, Any]] = []
    with document as doc:
        for page_index, page in enumerate(doc):
            width, height = float(page.rect.width), float(page.rect.height)
            blocks = _native_page_blocks(page, page_index)
            if not blocks and runtime.enabled():
                recognized, detail = runtime.extract_page(page)
                blocks = [{"text": line["text"], "bbox": [round(float(x), 2) for x in line["bbox"]],
                           "kind": "ocr_text", "source": "ocr", "source_ordinal": index,
                           "pdf_page_index": page_index, "font_size": 0.0, "bold_ratio": 0.0,
                           "ocr_score": line.get("score")}
                          for index, line in enumerate(recognized)]
            else:
                detail = {"status": "native_text" if blocks else "no_text", "lines": 0, "chars": 0}
            for item in blocks:
                item["role"] = "paragraph"
                item["section_path"] = []
                item["relations"] = []
                item["quality_flags"] = []
            visual_candidates = _mark_visual_text_candidates(page, blocks) if blocks else 0
            prose_blocks = [item for item in blocks if item["role"] != "figure_text_candidate"]
            ordered_prose, layout, strategy = _reading_order(prose_blocks, width, height)
            ordered = list(ordered_prose)
            # Keep all source text in the sidecar, but visual labels do not
            # participate in prose ordering or heading/section inference.
            ordered.extend(item for item in blocks if item["role"] == "figure_text_candidate")
            order_by_id = {id(item): index for index, item in enumerate(ordered_prose)}
            for item in ordered:
                item["reading_order"] = order_by_id.get(id(item))
                if item["role"] == "figure_text_candidate":
                    item["quality_flags"].append("reading_order_not_inferred_visual_text")
                    item["section_path"] = []
                    item["relations"] = []
                    item["confidence"] = None
                    item["block_id"] = "blk_" + hashlib.sha256(
                        f"{identity}:{source_sha256}:{page_index}:{_box_key(item['bbox'])}:{item['text']}".encode("utf-8")
                    ).hexdigest()[:24]
                    continue
                item["role"] = "paragraph"
                item["section_path"] = []
                item["relations"] = []
                item["confidence"] = None
                if layout == "unresolved":
                    item["quality_flags"].append("reading_order_unresolved_source_order_preserved")
                if _CAPTION.match(item["text"]):
                    item["role"] = "caption"
                item["block_id"] = "blk_" + hashlib.sha256(
                    f"{identity}:{source_sha256}:{page_index}:{_box_key(item['bbox'])}:{item['text']}".encode("utf-8")
                ).hexdigest()[:24]
            tables = _extract_tables(page, ordered, page_index, identity, source_sha256)
            figures = _extract_figures(page, ordered, page_index, identity, source_sha256)
            pages.append({"pdf_page_index": page_index, "width": width, "height": height,
                          "layout": layout, "reading_order_strategy": strategy,
                          "quality_flags": (["reading_order_unresolved"] if layout == "unresolved" else []),
                          "visual_text_candidate_count": visual_candidates,
                          "table_ids": [item["table_id"] for item in tables],
                          "tables": tables,
                          "figure_ids": [item["figure_id"] for item in figures],
                          "figures": figures,
                          "ocr": detail, "_blocks": ordered})

    _classify_margins(pages)
    section_stack: list[str] = []
    font_sizes = [item.get("font_size", 0.0) for page in pages for item in page["_blocks"] if item.get("font_size", 0) > 0]
    median_font = statistics.median(font_sizes) if font_sizes else 0.0
    for page in pages:
        for item in page["_blocks"]:
            if item["role"] == "figure_text_candidate":
                item["section_path"] = []
                continue
            if item["role"] in {"page_number", "page_header_candidate", "page_footer_candidate", "caption"}:
                item["section_path"] = section_stack.copy()
                continue
            heading = _heading(item["text"], item, median_font)
            if heading:
                value, level = heading
                section_stack = section_stack[:max(0, level - 1)] + [value]
                item["role"] = "heading"
            item["section_path"] = section_stack.copy()
    continuation_count = _cross_page_candidates(pages)
    for page in pages:
        page["formulas"] = _extract_formulas(
            page["_blocks"], page["pdf_page_index"], identity, source_sha256, page["width"])
        page["formula_ids"] = [item["formula_id"] for item in page["formulas"]]
    public_pages = []
    public_blocks = []
    public_tables = []
    public_figures = []
    public_formulas = []
    for page in pages:
        public_pages.append({key: value for key, value in page.items() if key != "_blocks"})
        public_tables.extend(page.get("tables", []))
        public_figures.extend(page.get("figures", []))
        public_formulas.extend(page.get("formulas", []))
        for item in page["_blocks"]:
            public_blocks.append({key: value for key, value in item.items()
                                  if key not in {"font_size", "bold_ratio", "ocr_score"}})
    summary = {
        "page_count": len(public_pages), "block_count": len(public_blocks),
        "layout_pages": dict(Counter(page["layout"] for page in public_pages)),
        "unresolved_layout_pages": sum(page["layout"] == "unresolved" for page in public_pages),
        "margin_candidates": sum("repeated_margin_text_candidate" in item["quality_flags"] for item in public_blocks),
        "page_number_candidates": sum("page_number_candidate" in item["quality_flags"] for item in public_blocks),
        "possible_cross_page_continuations": continuation_count,
        "visual_text_candidates": sum(page["visual_text_candidate_count"] for page in public_pages),
        "table_candidates": len(public_tables),
        "table_candidates_with_cell_geometry": sum(
            "cell_geometry_unresolved" not in item["quality_flags"] for item in public_tables),
        "figure_candidates": len(public_figures),
        "figure_caption_links": sum(item["caption_block_id"] is not None for item in public_figures),
        "formula_candidates": len(public_formulas),
        "ocr_pages": sum(any(item["source"] == "ocr" for item in page["_blocks"]) for page in pages),
    }
    return {"protocol_version": PROTOCOL_VERSION, "parser_identity": identity,
            "layout_algorithm_version": LAYOUT_ALGORITHM_VERSION, "source_sha256": source_sha256,
            "page_number_basis": "pdf_page_index zero-based; display page is index + 1",
            "pages": public_pages, "blocks": public_blocks, "tables": public_tables,
            "figures": public_figures, "formulas": public_formulas,
            "quality_summary": summary}
