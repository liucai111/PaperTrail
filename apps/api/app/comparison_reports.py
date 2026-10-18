"""Pure helpers for evidence-grounded cross-paper comparison reports."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import quote
from typing import Any


DEFAULT_DIMENSIONS = [
    "研究问题",
    "检索机制",
    "纠错/自评机制",
    "实验设置与局限",
]

SYSTEM_PROMPT = """
Create a comparative research report using only the supplied fixed-version paper excerpts.
Return one JSON object: {dimensions:[{name,cells:[{paper_id,claims:[{text,citations:[{evidence_id,quote}]}]}],comparisons:[{text,citations:[{evidence_id,quote}]}]}]}.
Include every requested dimension and one cell for every supplied paper. Empty claims mean the excerpts are insufficient; the application will label that cell “证据不足”.
Every substantive claim must have one or more citations, each with an exact verbatim substring from that exact evidence item. Cell claims may cite only evidence from their own paper. A comparison claim must cite evidence from at least two selected papers.
Do not infer that a paper does not report something merely because retrieval found no excerpt. State that something was not reported only when a cited excerpt explicitly says so; otherwise leave the cell claims empty.
Keep each number attached to its dataset, metric, model group and experimental setting. Do not directly rank or compare values from different datasets, metrics or settings. If comparability is not established by the excerpts, say the values are not directly comparable and cite the evidence that identifies their contexts.
Do not use outside knowledge, invent citation IDs, quotes, page numbers, version facts or conclusions. Do not treat user notes as paper evidence; user notes are not included in this prompt.
Keep the response compact: at most one concise claim per paper cell and at most one cross-paper conclusion per dimension. Each claim must be one sentence. Use only the shortest complete verbatim sentence needed as each quote (up to two citations for a cross-paper conclusion, from different papers). Do not repeat the same finding in cell claims and cross-paper conclusions. Preserve every requested dimension and every paper cell; use empty claims when evidence is insufficient. These limits keep the complete structured result within the configured response budget.
""".strip()

_ABSENCE_ASSERTION = re.compile(
    r"\b(?:the paper|this paper|the authors?|the study)\s+(?:does not|doesn't|did not|didn't|do not|don't|has not|have not)\s+"
    r"(?:report|mention|describe|specify|state|evaluate)\b|"
    r"\bnot\s+(?:reported|mentioned|described|specified|evaluated)\b|"
    r"\bno\b.{0,60}\b(?:reported|mentioned|described|specified|evaluated)\b|"
    r"(?:论文|作者|该研究).{0,24}(?:未报告|没有报告|未提及|没有提及|未说明|没有说明|未描述|没有描述)",
    re.IGNORECASE,
)
_NUMERIC_COMPARATIVE_LANGUAGE = re.compile(
    r"\b(?:higher|lower|better|worse|outperform(?:s|ed)?|superior|inferior|exceed(?:s|ed)?|rank(?:s|ed)?|vs\.?|versus)\b|"
    r"(?:更高|更低|更好|更差|优于|勝过|胜过|超过|领先|排名|提升|提高|相比|对比|比较)", re.IGNORECASE,
)
_NUMBER = re.compile(r"(?:\d+(?:\.\d+)?%?|\d+\.\d+)")


class InvalidReport(ValueError):
    pass


def normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def retrieval_query(dimensions: list[str]) -> str:
    common_terms = "research question problem method retrieval mechanism correction self-reflection experiment dataset metric limitation"
    return (" ".join([*dimensions, common_terms]))[:1800]


def _checked_citations(raw: Any, evidence_by_id: dict[str, dict], required_paper: str | None,
                       allow_cross_paper: bool) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise InvalidReport("Every report claim must cite at least one retrieved paper excerpt")
    citations: list[dict[str, str]] = []
    paper_ids = set()
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidReport("Citation must be an object")
        evidence_id, quote_text = item.get("evidence_id"), item.get("quote")
        source = evidence_by_id.get(evidence_id) if isinstance(evidence_id, str) else None
        if not source or not isinstance(quote_text, str) or not quote_text.strip():
            raise InvalidReport("Citation ID or quote is missing or outside the retrieved set")
        if required_paper and source.get("paper_id") != required_paper:
            raise InvalidReport("A paper cell cites evidence from another paper")
        if normalized_text(quote_text) not in normalized_text(source.get("text", "")):
            raise InvalidReport("Citation quote is not a source-text substring")
        paper_ids.add(source["paper_id"])
        citations.append({"evidence_id": evidence_id, "quote": quote_text})
    if not allow_cross_paper and len(paper_ids) != 1:
        raise InvalidReport("A paper cell claim must stay within one paper")
    if allow_cross_paper and len(paper_ids) < 2:
        raise InvalidReport("A cross-paper conclusion must cite at least two selected papers")
    return citations


def _unsupported_absence_claim(text: str, citations: list[dict[str, str]]) -> bool:
    return bool(_ABSENCE_ASSERTION.search(text)) and not any(
        _ABSENCE_ASSERTION.search(citation["quote"]) for citation in citations
    )


def _unsafe_numeric_ranking(text: str) -> bool:
    return bool(_NUMBER.search(text) and _NUMERIC_COMPARATIVE_LANGUAGE.search(text))


def normalize_generated_report(raw: dict[str, Any], dimensions: list[str], paper_ids: list[str],
                               evidence_by_id: dict[str, dict], report_id: str) -> tuple[list[dict], list[dict], int]:
    raw_dimensions = raw.get("dimensions") if isinstance(raw, dict) else None
    if not isinstance(raw_dimensions, list):
        raise InvalidReport("Report response must contain a dimensions array")
    by_name = {}
    for item in raw_dimensions:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise InvalidReport("Each report dimension must have a name")
        if item["name"] not in dimensions or item["name"] in by_name:
            raise InvalidReport("Report returned a duplicate or unrequested dimension")
        by_name[item["name"]] = item

    flat_claims: list[dict] = []
    report_dimensions: list[dict] = []
    excluded_absence_claims = 0
    excluded_unsafe_numeric_comparisons = 0
    for dimension in dimensions:
        raw_dimension = by_name.get(dimension, {})
        raw_cells = raw_dimension.get("cells", [])
        if not isinstance(raw_cells, list):
            raise InvalidReport("Dimension cells must be an array")
        cells_by_paper = {}
        for cell in raw_cells:
            if not isinstance(cell, dict) or cell.get("paper_id") not in paper_ids or cell["paper_id"] in cells_by_paper:
                raise InvalidReport("Dimension contains an unknown or duplicate paper cell")
            cells_by_paper[cell["paper_id"]] = cell
        cells = []
        for paper_id in paper_ids:
            raw_claims = cells_by_paper.get(paper_id, {}).get("claims", [])
            if not isinstance(raw_claims, list):
                raise InvalidReport("Cell claims must be an array")
            claims = []
            for raw_claim in raw_claims:
                if not isinstance(raw_claim, dict) or not isinstance(raw_claim.get("text"), str) or not raw_claim["text"].strip():
                    raise InvalidReport("Each report claim requires text")
                citations = _checked_citations(raw_claim.get("citations"), evidence_by_id, paper_id, False)
                if _unsupported_absence_claim(raw_claim["text"], citations):
                    excluded_absence_claims += 1
                    continue
                claim = {"text": raw_claim["text"].strip(), "evidence_ids": list(dict.fromkeys(c["evidence_id"] for c in citations)),
                         "citations": citations, "support_status": "unverified", "support_verification": "unverified",
                         "support_reason": "Support check pending", "support_evidence_ids": []}
                flat_claims.append(claim)
                claims.append(claim)
            cells.append({"paper_id": paper_id, "coverage_status": "has_cited_content" if claims else "insufficient_evidence",
                          "claims": claims})
        raw_comparisons = raw_dimension.get("comparisons", [])
        if not isinstance(raw_comparisons, list):
            raise InvalidReport("Dimension comparisons must be an array")
        comparisons = []
        for raw_claim in raw_comparisons:
            if not isinstance(raw_claim, dict) or not isinstance(raw_claim.get("text"), str) or not raw_claim["text"].strip():
                raise InvalidReport("Each comparison conclusion requires text")
            citations = _checked_citations(raw_claim.get("citations"), evidence_by_id, None, True)
            if _unsupported_absence_claim(raw_claim["text"], citations):
                excluded_absence_claims += 1
                continue
            if _unsafe_numeric_ranking(raw_claim["text"]):
                excluded_unsafe_numeric_comparisons += 1
                continue
            claim = {"text": raw_claim["text"].strip(), "evidence_ids": list(dict.fromkeys(c["evidence_id"] for c in citations)),
                     "citations": citations, "support_status": "unverified", "support_verification": "unverified",
                     "support_reason": "Support check pending", "support_evidence_ids": []}
            flat_claims.append(claim)
            comparisons.append(claim)
        report_dimensions.append({"name": dimension, "cells": cells, "comparisons": comparisons})

    for index, claim in enumerate(flat_claims):
        claim["claim_id"] = "rclm_" + hashlib.sha256(f"{report_id}:{index}:{claim['text']}".encode("utf-8")).hexdigest()[:24]
    return report_dimensions, flat_claims, {"unsupported_absence": excluded_absence_claims,
                                             "unsafe_numeric_comparison": excluded_unsafe_numeric_comparisons}


def attach_citations(report_dimensions: list[dict], evidence_by_id: dict[str, dict], location_builder) -> None:
    for dimension in report_dimensions:
        for claim in [*sum((cell["claims"] for cell in dimension["cells"]), []), *dimension["comparisons"]]:
            claim["citations"] = [location_builder(evidence_by_id[item["evidence_id"]], item["quote"], claim)
                                  for item in claim["citations"]]


def _cell_text(claims: list[dict]) -> str:
    if not claims:
        return "证据不足"
    lines = []
    for claim in claims:
        labels = {"supported": "模型辅助支持", "partially_supported": "部分支持", "unsupported": "不支持",
                  "conflicting": "证据冲突", "unverified": "未核验"}
        label = "已编辑，未重新核验" if claim.get("revision_edit") else labels.get(claim.get("support_status"), "未核验")
        refs = " ".join(f"[{item['evidence_id']}]" for item in claim.get("citations", []))
        lines.append(f"- {claim['text']}（{label}） {refs}")
    return "<br>".join(lines)


def _display_pages(indexes: list[int] | None, reliable: bool) -> str:
    if not reliable or not indexes:
        return "未记录（无可靠引句页）"
    pages = sorted({page + 1 for page in indexes if isinstance(page, int) and page >= 0})
    return ", ".join(map(str, pages)) + "（PDF 显示页码）" if pages else "未记录（无可靠引句页）"


def report_markdown(report: dict[str, Any]) -> str:
    data = report["report_data"]
    papers = data.get("paper_snapshots", [])
    paper_ids = report.get("paper_ids", [])
    paper_by_id = {paper["paper_id"]: paper for paper in papers}
    paper_headers = [paper_by_id.get(paper_id, {"title": paper_id}).get("title", paper_id) for paper_id in paper_ids]
    lines = [f"# {report['title']}", "", f"- Report ID：{report['report_id']}",
             f"- 创建时间：{report.get('created_at', '未记录')}",
             f"- Chat 模型：{report.get('chat_model', '未记录')}",
             f"- Embedding 索引：{report.get('embedding_model', '未记录')}", "",
             "## 固定论文版本", ""]
    if report.get("revision_id"):
        lines.extend(["## 修订信息", "", f"- 来源报告：`{report.get('source_report_id', report['report_id'])}`",
                      f"- 修订版本：{report.get('revision_number', '未记录')} · 修订 ID：`{report['revision_id']}`",
                      f"- 修订时间：{report.get('revision_created_at', '未记录')}",
                      "- 修订后的内容：已编辑，未重新核验；原版模型核验状态仅供查看，不适用于修订文本。",
                      "- 修订阶段模型调用：0。", ""])
        for change in report.get("changes", []):
            lines.extend([f"### 修改 {change.get('claim_id', '')}", "",
                          f"- 修改原因：{change.get('reason', '未记录')}",
                          f"- 原文：{change.get('original_text', '')}",
                          f"- 修订：{change.get('revised_text', '')}",
                          f"- 原版核验：{change.get('original_support_status', 'unverified')} · 修订状态：已编辑，未重新核验", ""])
    for paper_id in paper_ids:
        paper = paper_by_id.get(paper_id, {})
        lines.append(f"- {paper.get('title', paper_id)}（{paper_id}），版本 {paper.get('source_version', '未记录')}，文档版本 ID `{paper.get('document_version_id', '未记录')}`，SHA-256 `{paper.get('sha256', '未记录')}`")
    lines.extend(["", "## 对比表", "", "| 维度 | " + " | ".join(paper_headers) + " |", "|---|" + "|".join(["---"] * len(paper_headers)) + "|"])
    for dimension in data.get("dimensions", []):
        cell_by_paper = {cell["paper_id"]: cell for cell in dimension.get("cells", [])}
        cells = [_cell_text(cell_by_paper.get(pid, {}).get("claims", [])) for pid in paper_ids]
        lines.append("| " + " | ".join([dimension["name"], *[cell.replace("|", "&#124;") for cell in cells]]) + " |")
    for dimension in data.get("dimensions", []):
        if dimension.get("comparisons"):
            lines.extend(["", f"### 跨论文结论：{dimension['name']}", ""])
            for claim in dimension["comparisons"]:
                status = "已编辑，未重新核验" if claim.get("revision_edit") else claim.get("support_status", "unverified")
                lines.append(f"- {claim['text']}（{status}） " + " ".join(f"[{c['evidence_id']}]" for c in claim.get("citations", [])))
    lines.extend(["", "## 引用与定位", ""])
    citation_rows = []
    for dimension in data.get("dimensions", []):
        for claim in [*sum((cell["claims"] for cell in dimension.get("cells", [])), []), *dimension.get("comparisons", [])]:
            citation_rows.extend((dimension["name"], claim) for claim in claim.get("citations", []))
    seen = set()
    for dimension_name, citation in citation_rows:
        key = (citation.get("evidence_id"), citation.get("exact_quote"))
        if key in seen:
            continue
        seen.add(key)
        lines.extend([f"### [{citation.get('evidence_id', 'ev_missing')}] {citation.get('title', '论文标题未记录')} · {dimension_name}", "",
                      f"- Paper ID：{citation.get('paper_id', '未记录')}",
                      f"- 文档版本 ID：`{citation.get('document_version_id', '未记录')}` · 版本：{citation.get('source_version', '未记录')}",
                      f"- 保存时支持状态：{citation.get('verification', {}).get('support_status', 'unverified')}（模型辅助核验，不是人工确认）",
                      f"- 引句原文页：{_display_pages(citation.get('quote_location_pdf_page_indexes'), citation.get('verification', {}).get('location_valid', False))}",
                      f"- 分块覆盖页：{_display_pages(citation.get('chunk_pdf_page_indexes'), True)}",
                      f"- 定位精度：{citation.get('location_precision', '暂无法精确定位')}", "",
                      "> " + (citation.get("exact_quote") or "（未保存原文引句）"), ""])
    notes = data.get("supplemental_notes", [])
    if notes:
        lines.extend(["## 用户笔记补充材料（非论文原文，不参与证据支持核验）", ""])
        for note in notes:
            lines.extend([f"### {note.get('title', '未命名笔记')}", "", note.get("body", "") or "（空）",
                          "", f"来源 QA run ID：`{note.get('source_qa_run_id', '未记录')}`", ""])
    trace = data.get("support_trace", {})
    lines.extend(["## 自动处理记录", "", f"- 证据支持状态：模型辅助核验；报告覆盖由每个单元格是否有引用内容独立表示。",
                  f"- Chat 调用次数：{trace.get('chat_calls', '未记录')} · 查询 Embedding 请求：{trace.get('embedding_requests', '未记录')}",
                  f"- 总耗时：{trace.get('total_elapsed_ms', '未记录')} ms"])
    return "\n".join(lines).rstrip() + "\n"


def report_filename(title: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip(" .")[:100].strip(" .") or "papertrail-comparison"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if safe.split(".", 1)[0].upper() in reserved:
        safe = "_" + safe
    return f"{safe}.md"
