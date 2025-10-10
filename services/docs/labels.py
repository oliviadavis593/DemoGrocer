"""Markdown label rendering helpers backed by optional PDF generation."""
from __future__ import annotations

import html
import io
import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from packages.odoo_client import OdooClient

LOGGER = logging.getLogger("foodflow.docs")

DEFAULT_TEMPLATE = """<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      body {{ font-family: Helvetica, Arial, sans-serif; padding: 24px; }}
      .label {{
        border: 1px solid #222;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 24px;
        max-width: 320px;
      }}
      .title {{ font-size: 20px; font-weight: 600; margin: 0 0 8px 0; }}
      .sku {{ font-size: 14px; color: #333; margin-bottom: 6px; }}
      .meta {{ font-size: 12px; color: #444; margin-bottom: 4px; }}
      .description {{ font-size: 12px; color: #222; margin-top: 12px; line-height: 1.4; }}
      .barcode {{ font-size: 12px; color: #555; margin-bottom: 4px; }}
    </style>
  </head>
  <body>
    <div class="label">
      <div class="title">{product_name}</div>
      <div class="sku"><strong>SKU:</strong> {default_code}</div>
      {barcode_section}
      <div class="meta"><strong>Category:</strong> {category}</div>
      <div class="meta"><strong>Generated:</strong> {generated_at}</div>
      {description_section}
    </div>
  </body>
</html>
"""

COMBINED_TEMPLATE = """<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      body {{ font-family: Helvetica, Arial, sans-serif; padding: 24px; }}
      .labels-collection {{ display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start; }}
      .label {{
        border: 1px solid #222;
        border-radius: 8px;
        padding: 16px 20px;
        margin: 0;
        max-width: 320px;
        page-break-inside: avoid;
      }}
      .title {{ font-size: 20px; font-weight: 600; margin: 0 0 8px 0; }}
      .sku {{ font-size: 14px; color: #333; margin-bottom: 6px; }}
      .meta {{ font-size: 12px; color: #444; margin-bottom: 4px; }}
      .description {{ font-size: 12px; color: #222; margin-top: 12px; line-height: 1.4; }}
      .barcode {{ font-size: 12px; color: #555; margin-bottom: 4px; }}
    </style>
  </head>
  <body>
    <div class="labels-collection">
      {labels}
    </div>
  </body>
</html>
"""


@dataclass
class LabelDocument:
    """Details for a generated label."""

    default_code: str
    product_name: str
    category: Optional[str]
    description: Optional[str]
    barcode: Optional[str]
    pdf_path: Path
    generated_at: datetime
    found: bool
    html_content: Optional[str] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_code": self.default_code,
            "product": self.product_name,
            "category": self.category,
            "barcode": self.barcode,
            "path": str(self.pdf_path),
            "generated_at": self.generated_at.isoformat(),
            "found": self.found,
        }


class MarkdownLabelGenerator:
    """Render per-product labels as PDF files."""

    def __init__(
        self,
        client: OdooClient,
        *,
        output_dir: Path,
        template: str | None = None,
        renderer: Optional["PDFRenderer"] = None,
    ) -> None:
        self.client = client
        self.output_dir = output_dir
        self.template = template or DEFAULT_TEMPLATE
        self.renderer = renderer or PDFRenderer()

    def generate(self, default_codes: Sequence[str]) -> List[LabelDocument]:
        requested = _normalize_codes(default_codes)
        if not requested:
            return []
        self.output_dir.mkdir(parents=True, exist_ok=True)
        generated_at = datetime.now(timezone.utc)
        products = self._fetch_products(requested)
        documents: List[LabelDocument] = []
        for code in requested:
            product = products.get(code)
            context = self._build_context(code, product, generated_at)
            html_payload = self._render_html(context)
            filename = _sanitize_filename(code) + ".pdf"
            target_path = self.output_dir / filename
            self.renderer.render(html_payload, target_path)
            documents.append(
                LabelDocument(
                    default_code=context["default_code"],
                    product_name=context["product_name"],
                    category=context["category"],
                    description=context["description"],
                    barcode=context["barcode"],
                    pdf_path=target_path,
                    generated_at=generated_at,
                    found=product is not None,
                    html_content=html_payload,
                )
            )
        return documents

    def render_combined_pdf(self, documents: Sequence[LabelDocument]) -> bytes:
        fragments: List[str] = []
        for doc in documents:
            fragment = _extract_label_fragment(doc.html_content)
            if fragment:
                fragments.append(fragment)
        if not fragments:
            raise ValueError("no label content to combine")
        combined_html = _build_combined_html(fragments)
        return self.renderer.render_bytes(combined_html)

    def _fetch_products(self, default_codes: Iterable[str]) -> Dict[str, Mapping[str, Any]]:
        codes = list(default_codes)
        if not codes:
            return {}
        response = self.client.search_read(
            "product.product",
            domain=[["default_code", "in", codes]],
            fields=["id", "name", "default_code", "barcode", "categ_id", "description", "description_sale"],
        )
        products: Dict[str, Mapping[str, Any]] = {}
        for item in response:
            raw_code = item.get("default_code")
            if not raw_code:
                continue
            code = str(raw_code)
            products[code] = item
        return products

    def _build_context(
        self,
        default_code: str,
        product: Optional[Mapping[str, Any]],
        generated_at: datetime,
    ) -> Dict[str, Any]:
        product_name = str(product.get("name") if product else f"Product {default_code}")
        category = _resolve_relational_name(product.get("categ_id")) if product else None
        description_raw = ""
        if product:
            description_raw = str(
                product.get("description_sale")
                or product.get("description")
                or ""
            ).strip()
        barcode = str(product.get("barcode")).strip() if product and product.get("barcode") else None
        return {
            "default_code": default_code,
            "product_name": product_name,
            "category": category or "Uncategorized",
            "description": description_raw or None,
            "barcode": barcode,
            "generated_at": generated_at.isoformat(),
        }

    def _render_html(self, context: Mapping[str, Any]) -> str:
        escaped_context = dict(context)
        escaped_context["product_name"] = html.escape(str(context.get("product_name", "")))
        escaped_context["default_code"] = html.escape(str(context.get("default_code", "")))
        escaped_context["category"] = html.escape(str(context.get("category", "")))
        escaped_context["generated_at"] = html.escape(str(context.get("generated_at", "")))

        description = context.get("description")
        if description:
            description_html = _format_description(str(description))
            escaped_context["description_section"] = f'<div class="description">{description_html}</div>'
        else:
            escaped_context["description_section"] = ""

        barcode = context.get("barcode")
        if barcode:
            escaped_context["barcode_section"] = f'<div class="barcode"><strong>Barcode:</strong> {html.escape(str(barcode))}</div>'
        else:
            escaped_context["barcode_section"] = ""

        return self.template.format(**escaped_context)


_BODY_FRAGMENT_RE = re.compile(r"<body[^>]*>(?P<body>.*)</body>", re.IGNORECASE | re.DOTALL)


def _extract_label_fragment(html_content: Optional[str]) -> str:
    if not html_content:
        return ""
    match = _BODY_FRAGMENT_RE.search(html_content)
    if match:
        return match.group("body").strip()
    return html_content.strip()


def _build_combined_html(fragments: Sequence[str]) -> str:
    labels_markup = "\n".join(fragment for fragment in fragments if fragment)
    return COMBINED_TEMPLATE.format(labels=labels_markup)


class PDFRenderer:
    """PDF writer with WeasyPrint support and pure-Python fallback."""

    def __init__(self) -> None:
        try:  # pragma: no cover - optional dependency
            from weasyprint import HTML  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - optional dependency
            LOGGER.debug("WeasyPrint not installed; using fallback PDF renderer")
            self._html_cls = None
        except OSError as exc:  # pragma: no cover - missing native deps
            LOGGER.warning("WeasyPrint unavailable (%s); using fallback PDF renderer", exc)
            self._html_cls = None
        else:  # pragma: no branch - trivial
            self._html_cls = HTML

    def render(self, html_content: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self._html_cls is not None:  # pragma: no cover - requires optional dep
            self._html_cls(string=html_content).write_pdf(target=str(output_path))
            return
        text_content = _strip_html(html_content)
        _write_basic_pdf(text_content, output_path)

    def render_bytes(self, html_content: str) -> bytes:
        if self._html_cls is not None:  # pragma: no cover - requires optional dep
            buffer = io.BytesIO()
            self._html_cls(string=html_content).write_pdf(target=buffer)
            return buffer.getvalue()
        text_content = _strip_html(html_content)
        return _build_basic_pdf_bytes(text_content)


# Helper utilities -----------------------------------------------------------------
def _normalize_codes(default_codes: Sequence[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for code in default_codes:
        if not isinstance(code, str):
            continue
        normalized = code.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        output.append(normalized)
    return output


def _sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized or "label"


def _resolve_relational_name(value: Any) -> Optional[str]:
    if isinstance(value, (list, tuple)) and value:
        candidate = value[-1]
        return str(candidate)
    if value in (None, False):
        return None
    return str(value)


def _format_description(description: str) -> str:
    lines = [html.escape(line.strip()) for line in description.splitlines()]
    filtered = [line for line in lines if line]
    if not filtered:
        return ""
    return "<br/>".join(filtered)


def _strip_html(value: str) -> str:
    normalized = (
        value.replace("<br />", "\n")
        .replace("<br/>", "\n")
        .replace("<br>", "\n")
    )
    cleaned = re.sub(r"<[^>]+>", "", normalized)
    unescaped = html.unescape(cleaned)
    return "\n".join(line.rstrip() for line in unescaped.splitlines()).strip()


def _write_basic_pdf(text: str, output_path: Path) -> None:
    output_path.write_bytes(_build_basic_pdf_bytes(text))


def _build_basic_pdf_bytes(text: str) -> bytes:
    escaped_lines = [_escape_pdf_text(line) for line in text.splitlines() if line.strip()]
    if not escaped_lines:
        escaped_lines = [_escape_pdf_text(" ")]
    stream_lines = [
        "BT",
        "/F1 12 Tf",
        "14 TL",
        "72 720 Td",
    ]
    first = True
    for line in escaped_lines:
        if not first:
            stream_lines.append("T*")
        stream_lines.append(f"({line}) Tj")
        first = False
    stream_lines.append("ET")
    stream = "\n".join(stream_lines)
    stream_bytes = stream.encode("latin-1")
    objects = [
        "1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj",
        "2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj",
        "3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj",
        f"4 0 obj<< /Length {len(stream_bytes)} >>stream\n{stream}\nendstream\nendobj",
        "5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj",
    ]
    pdf = bytearray()
    pdf.extend(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(len(pdf))
        pdf.extend(obj.encode("latin-1"))
        pdf.extend(b"\n")
    xref_offset = len(pdf)
    count = len(objects) + 1
    pdf.extend(f"xref\n0 {count}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    pdf.extend(f"trailer<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("latin-1"))
    return bytes(pdf)


def _escape_pdf_text(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
    return escaped.replace("\r", "").replace("\n", "\\n")


__all__ = ["MarkdownLabelGenerator", "LabelDocument", "PDFRenderer"]
