"""Optional, project-local OCR for PDF pages with no native text blocks.

The three ONNX models ship with the pinned RapidOCR wheel. Explicit model paths
prevent a processing task from downloading models at runtime.
"""
from __future__ import annotations

import math
import hashlib
import os
import re
import statistics
import sys
import time
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OCR_PACKAGES = ROOT / "var" / "ocr_packages"
MODEL_NAMES = {
    "Det": "PP-OCRv6_det_small.onnx",
    "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec": "PP-OCRv6_rec_small.onnx",
}
OCR_VERSION = "rapidocr-3.10.0-ppocrv6-small-v1"
MAX_RENDERED_PIXELS = 8_000_000
_last_engine_init_ms: int | None = None


class OCRUnavailable(RuntimeError):
    """The optional local OCR runtime or its bundled models are unavailable."""


class OCRProcessingError(RuntimeError):
    """OCR could not safely process a page or document."""


def enabled() -> bool:
    return os.getenv("PAPERTRAIL_OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def dpi() -> int:
    try:
        value = int(os.getenv("PAPERTRAIL_OCR_DPI", "150"))
    except ValueError as exc:
        raise OCRUnavailable("OCR DPI must be an integer") from exc
    if not 96 <= value <= 200:
        raise OCRUnavailable("OCR DPI must be between 96 and 200")
    return value


def max_pages() -> int:
    try:
        value = int(os.getenv("PAPERTRAIL_OCR_MAX_PAGES", "50"))
    except ValueError as exc:
        raise OCRUnavailable("OCR page limit must be an integer") from exc
    if not 1 <= value <= 500:
        raise OCRUnavailable("OCR page limit must be between 1 and 500")
    return value


def parser_identity(native_version: str) -> str:
    if not enabled():
        return native_version
    digest = hashlib.sha256(f"{OCR_VERSION}:dpi{dpi()}".encode()).hexdigest()[:8]
    return f"{native_version}-ocr-{digest}"


@lru_cache(maxsize=1)
def _engine():
    global _last_engine_init_ms
    started = time.monotonic()
    models = OCR_PACKAGES / "rapidocr" / "models"
    if not OCR_PACKAGES.is_dir() or any(not (models / name).is_file() for name in MODEL_NAMES.values()):
        raise OCRUnavailable("Project-local RapidOCR runtime or bundled models are missing")
    # These packages are installed only under var/; no system Python or PATH change.
    package_path = str(OCR_PACKAGES)
    if package_path not in sys.path:
        sys.path.insert(0, package_path)
    try:
        from rapidocr import RapidOCR
        engine = RapidOCR(params={
            **{f"{part}.model_path": str(models / name) for part, name in MODEL_NAMES.items()},
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            "Global.log_level": "error",
        })
        _last_engine_init_ms = int((time.monotonic() - started) * 1000)
        return engine
    except (ImportError, OSError) as exc:
        raise OCRUnavailable("Project-local RapidOCR runtime could not initialize") from exc


def _ordered_lines(lines: list[dict], page_width: float) -> list[dict]:
    """Keep simple two-column pages in column order without inventing headings."""
    by_y = sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    left = [line for line in by_y if line["bbox"][2] <= page_width * 0.53]
    right = [line for line in by_y if line["bbox"][0] >= page_width * 0.47]
    if len(left) < 5 or len(right) < 5:
        return by_y
    spans = [line for line in by_y if line not in left and line not in right]
    output: list[dict] = []
    remaining = by_y.copy()
    for span in spans:
        before = [line for line in remaining if line is not span and line["bbox"][1] < span["bbox"][1]]
        output.extend(line for line in before if line in left)
        output.extend(line for line in before if line in right)
        for line in before:
            remaining.remove(line)
        output.append(span)
        remaining.remove(span)
    output.extend(line for line in remaining if line in left)
    output.extend(line for line in remaining if line in right)
    return output


def _lines_from_result(result, page, pix) -> list[dict]:
    texts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    scores = getattr(result, "scores", None)
    if texts is None or boxes is None or scores is None:
        return []
    lines = []
    for raw, polygon, raw_score in zip(texts, boxes, scores):
        text = str(raw).strip()
        score = float(raw_score)
        if not text or not math.isfinite(score) or score < 0.65:
            continue
        points = [(float(point[0]), float(point[1])) for point in polygon]
        if len(points) != 4 or any(not math.isfinite(x) or not math.isfinite(y)
                                   or x < 0 or y < 0 or x > pix.width or y > pix.height
                                   for x, y in points):
            continue
        x0 = min(x for x, _ in points) * page.rect.width / pix.width + page.rect.x0
        y0 = min(y for _, y in points) * page.rect.height / pix.height + page.rect.y0
        x1 = max(x for x, _ in points) * page.rect.width / pix.width + page.rect.x0
        y1 = max(y for _, y in points) * page.rect.height / pix.height + page.rect.y0
        if x1 <= x0 or y1 <= y0:
            continue
        lines.append({"text": text, "bbox": [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                      "score": score})
    return _ordered_lines(lines, page.rect.width)


def extract_page(page) -> tuple[list[dict], dict]:
    """Return accepted OCR line blocks and non-sensitive page quality metadata."""
    if page.rotation:
        return [], {"status": "unsupported_rotation", "lines": 0, "chars": 0}
    render_dpi = dpi()
    pixels = math.ceil(page.rect.width * render_dpi / 72) * math.ceil(page.rect.height * render_dpi / 72)
    if pixels > MAX_RENDERED_PIXELS:
        return [], {"status": "page_too_large", "lines": 0, "chars": 0}
    started = time.monotonic()
    pix = page.get_pixmap(dpi=render_dpi, alpha=False)
    render_ms = int((time.monotonic() - started) * 1000)
    started = time.monotonic()
    try:
        cache = getattr(_engine, "cache_info", None)
        was_cached = bool(cache and cache().currsize)
        engine = _engine()
        engine_init_ms = _last_engine_init_ms if cache and not was_cached else None
        inference_started = time.monotonic()
        result = engine(pix.tobytes("png"))
    except OCRUnavailable:
        raise
    except Exception as exc:
        raise OCRProcessingError(f"OCR page inference failed ({type(exc).__name__})") from exc
    ocr_ms = int((time.monotonic() - inference_started) * 1000)
    lines = _lines_from_result(result, page, pix)
    combined = " ".join(line["text"] for line in lines)
    alnum = sum(char.isalnum() for char in combined)
    english_words = len(re.findall(r"[A-Za-z]{3,}", combined))
    han_chars = len(re.findall(r"[\u3400-\u9fff]", combined))
    median_score = statistics.median(line["score"] for line in lines) if lines else 0.0
    accepted = len(lines) >= 3 and alnum >= 80 and (english_words >= 12 or han_chars >= 20) and median_score >= 0.72
    diagnostics = {"status": "accepted" if accepted else "low_quality", "lines": len(lines),
                   "chars": len(combined), "median_score": round(median_score, 3),
                   "render_ms": render_ms, "engine_init_ms": engine_init_ms,
                   "ocr_ms": ocr_ms, "rendered_pixels": [pix.width, pix.height]}
    return (lines if accepted else []), diagnostics
