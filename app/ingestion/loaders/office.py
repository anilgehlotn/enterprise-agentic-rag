"""Local DOCX/PPTX text extraction using python-docx and python-pptx.

Both libraries are pure-Python and already ship in requirements-prod.txt, so
Office uploads work inside the Docker image without the heavy `unstructured`
dependency tree.
"""

from pathlib import Path

import logfire


def _docx_table_text(table) -> list[str]:
    """Flatten a DOCX table to one ' | '-joined line per row (nested tables included)."""
    lines: list[str] = []
    for row in table.rows:
        cells: list[str] = []
        seen_cells = set()
        for cell in row.cells:
            # Horizontally merged cells are returned once per grid column; skip repeats.
            if cell._tc in seen_cells:
                continue
            seen_cells.add(cell._tc)
            text = "\n".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
            if text:
                cells.append(text)
            for nested in cell.tables:
                lines.extend(_docx_table_text(nested))
        if cells:
            lines.append(" | ".join(cells))
    return lines


def _parse_docx(file_path: str) -> str:
    from docx import Document
    from docx.table import Table

    document = Document(file_path)
    parts: list[str] = []

    # iter_inner_content() yields paragraphs and tables in document order.
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            parts.extend(_docx_table_text(block))
        elif block.text.strip():
            parts.append(block.text.strip())

    return "\n".join(parts)


def _pptx_shape_text(shape) -> list[str]:
    """Collect text from a slide shape, recursing into groups and reading tables."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    lines: list[str] = []
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        for child in shape.shapes:
            lines.extend(_pptx_shape_text(child))
        return lines

    if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
        lines.append(shape.text_frame.text.strip())

    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                lines.append(" | ".join(cells))

    return lines


def _parse_pptx(file_path: str) -> str:
    from pptx import Presentation

    presentation = Presentation(file_path)
    parts: list[str] = []
    for slide in presentation.slides:
        for shape in slide.shapes:
            parts.extend(_pptx_shape_text(shape))
    return "\n".join(parts)


def parse_office(file_path: str) -> str:
    """
    Parses Office documents (.docx, .pptx) locally.
    DOCX: paragraphs and tables, in document order.
    PPTX: text from every slide shape (text frames, tables, grouped shapes).
    """
    with logfire.span("📄 Office Document Parsing", filename=file_path):
        extension = Path(file_path).suffix.lower()
        try:
            if extension == ".docx":
                full_text = _parse_docx(file_path)
            elif extension == ".pptx":
                full_text = _parse_pptx(file_path)
            else:
                raise ValueError(f"Unsupported Office file type: {extension or '(none)'}")

            if not full_text.strip():
                logfire.warning(f"⚠️ Office parser returned empty text for {file_path}")
            else:
                logfire.info(f"✅ Successfully parsed {len(full_text)} characters")

            return full_text
        except Exception as e:
            logfire.error(f"❌ Office Parse Failed: {e}")
            raise
