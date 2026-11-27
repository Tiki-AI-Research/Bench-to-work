"""Fixed read-only parser for .docx, .pdf, and .pptx; path boundaries are validated by the caller and no arbitrary code is accepted."""

import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile

MAX_OOXML_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_CHARS = 16000
MAX_CHARS_LIMIT = 32000

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def _validate_char_budget(offset: int, max_chars: int) -> None:
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(max_chars) is not int or not 1 <= max_chars <= MAX_CHARS_LIMIT:
        raise ValueError(f"max_chars must be an integer between 1 and {MAX_CHARS_LIMIT}")


def _open_ooxml_zip(path: Path, expected_entry: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid or corrupted OOXML archive: {path.name}") from exc
    total = sum(info.file_size for info in zf.infolist())
    if total > MAX_OOXML_UNCOMPRESSED_BYTES:
        zf.close()
        raise ValueError(
            f"Archive uncompressed size ({total} bytes) exceeds {MAX_OOXML_UNCOMPRESSED_BYTES} byte safety limit"
        )
    if expected_entry not in zf.namelist():
        zf.close()
        raise ValueError(f"Missing required OOXML entry {expected_entry!r} in {path.name}")
    return zf


def _natural_key(name: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


# ---------------- DOCX ----------------


def _docx_inline_text(elem: ET.Element) -> str:
    parts: list[str] = []
    for child in elem:
        tag = child.tag
        if tag == f"{W_NS}del":
            # Skip deleted Track Changes runs so active text reflects current document content.
            continue
        if tag == f"{W_NS}t":
            if child.text:
                parts.append(child.text)
        elif tag == f"{W_NS}tab":
            parts.append("\t")
        elif tag in {f"{W_NS}br", f"{W_NS}cr"}:
            parts.append("\n")
        else:
            parts.append(_docx_inline_text(child))
    return "".join(parts)


def _docx_paragraph_line(p: ET.Element) -> str:
    text = _docx_inline_text(p).strip()
    if not text:
        return ""
    style = p.find(f"./{W_NS}pPr/{W_NS}pStyle")
    style_val = style.get(f"{W_NS}val", "") if style is not None else ""
    if re.match(r"^(Heading|Title|Subtitle)", style_val, re.IGNORECASE):
        return f"[{style_val}] {text}"
    return text


def _docx_table_lines(tbl: ET.Element, table_index: int) -> list[str]:
    rows_out: list[tuple[int, list[str]]] = []
    max_cols = 0
    all_trs = tbl.findall(f"./{W_NS}tr")
    total_rows = len(all_trs)
    for orig_r_idx, tr in enumerate(all_trs, start=1):
        row_cells: list[str] = []
        for tc in tr.findall(f"./{W_NS}tc"):
            cell_parts: list[str] = []
            for block in _docx_collect_blocks(tc, counter=None):
                cleaned = " ".join(line.strip() for line in block.splitlines() if line.strip())
                if cleaned:
                    cell_parts.append(cleaned)
            row_cells.append(" / ".join(cell_parts).replace("|", "\\|"))
        max_cols = max(max_cols, len(row_cells))
        if any(row_cells):
            rows_out.append((orig_r_idx, row_cells))
    header = f"[Table {table_index}: {total_rows} rows x {max_cols} cols]"
    lines = [header]
    for orig_r_idx, cells in rows_out:
        lines.append(f"  Row {orig_r_idx}: " + " | ".join(cells))
    return lines


def _docx_collect_blocks(container: ET.Element, counter: dict[str, int] | None) -> list[str]:
    blocks: list[str] = []
    for child in container:
        tag = child.tag
        if tag == f"{W_NS}p":
            line = _docx_paragraph_line(child)
            if line:
                if counter is not None:
                    counter["paragraphs"] += 1
                blocks.append(line)
        elif tag == f"{W_NS}tbl":
            if counter is not None:
                counter["tables"] += 1
                idx = counter["tables"]
            else:
                idx = 1
            blocks.append("\n".join(_docx_table_lines(child, idx)))
        elif tag == f"{W_NS}sdt":
            sdt_content = child.find(f"./{W_NS}sdtContent")
            if sdt_content is not None:
                blocks.extend(_docx_collect_blocks(sdt_content, counter))
    return blocks


def _docx_part_texts(zf: zipfile.ZipFile, pattern: str) -> list[str]:
    seen: list[str] = []
    for name in sorted((n for n in zf.namelist() if re.fullmatch(pattern, n)), key=_natural_key):
        root = ET.fromstring(zf.read(name))
        text = "\n".join(_docx_collect_blocks(root, counter=None)).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _docx_comments(zf: zipfile.ZipFile) -> list[str]:
    if "word/comments.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("word/comments.xml"))
    out: list[str] = []
    for c in root.findall(f"./{W_NS}comment"):
        cid = c.get(f"{W_NS}id", "")
        author = c.get(f"{W_NS}author", "")
        date = c.get(f"{W_NS}date", "")
        body = " ".join(
            _docx_paragraph_line(p) for p in c.findall(f".//{W_NS}p") if _docx_paragraph_line(p)
        ).strip()
        if body:
            meta = ", ".join(x for x in (f"id={cid}" if cid else "", author, date) if x)
            out.append(f"({meta}) {body}" if meta else body)
    return out


def _docx_notes(zf: zipfile.ZipFile, entry: str, item_tag: str) -> list[str]:
    if entry not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(entry))
    out: list[str] = []
    for note in root.findall(f"./{W_NS}{item_tag}"):
        nid = note.get(f"{W_NS}id", "")
        ntype = note.get(f"{W_NS}type", "")
        if ntype in {"separator", "continuationSeparator"} or nid in {"-1", "0"}:
            continue
        body = " ".join(
            _docx_paragraph_line(p) for p in note.findall(f".//{W_NS}p") if _docx_paragraph_line(p)
        ).strip()
        if body:
            out.append(f"[{nid}] {body}")
    return out


def read_docx(path: Path, *, client_path: str, offset: int = 0, max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    _validate_char_budget(offset, max_chars)
    with _open_ooxml_zip(path, "word/document.xml") as zf:
        doc_root = ET.fromstring(zf.read("word/document.xml"))
        body = doc_root.find(f"./{W_NS}body")
        counts = {"paragraphs": 0, "tables": 0, "headers": 0, "footers": 0, "comments": 0}
        body_blocks = _docx_collect_blocks(body, counts) if body is not None else []
        headers = _docx_part_texts(zf, r"word/header\d*\.xml")
        footers = _docx_part_texts(zf, r"word/footer\d*\.xml")
        comments = _docx_comments(zf)
        footnotes = _docx_notes(zf, "word/footnotes.xml", "footnote")
        endnotes = _docx_notes(zf, "word/endnotes.xml", "endnote")
        counts["headers"] = len(headers)
        counts["footers"] = len(footers)
        counts["comments"] = len(comments)

    sections: list[str] = []
    if headers:
        sections.append("=== Headers ===\n" + "\n---\n".join(headers))
    if body_blocks:
        sections.append("\n\n".join(body_blocks))
    if comments:
        sections.append("=== Comments ===\n" + "\n".join(comments))
    if footnotes:
        sections.append("=== Footnotes ===\n" + "\n".join(footnotes))
    if endnotes:
        sections.append("=== Endnotes ===\n" + "\n".join(endnotes))
    if footers:
        sections.append("=== Footers ===\n" + "\n---\n".join(footers))

    full_text = "\n\n".join(sections)
    if offset > len(full_text):
        raise ValueError(f"offset {offset} exceeds document text length {len(full_text)}")
    chunk = full_text[offset : offset + max_chars]
    has_more = offset + len(chunk) < len(full_text)
    return {
        "format": "docx",
        "total_length": len(full_text),
        "offset": offset,
        "content": chunk,
        "counts": counts,
        "has_more": has_more,
        "next": (
            {"path": client_path, "offset": offset + len(chunk), "max_chars": max_chars}
            if has_more
            else None
        ),
    }


# ---------------- PDF ----------------


def _extract_pdf_pages_pypdf(path: Path) -> list[tuple[str, int]]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception as exc:
            raise ValueError(f"Encrypted PDF cannot be opened without password: {path.name}") from exc
        if not decrypted:
            raise ValueError(f"Encrypted PDF requires a password: {path.name}")
    pages: list[tuple[str, int]] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        img_count = 0
        if not text:
            try:
                img_count = len(page.images)
            except Exception:
                img_count = 0
        pages.append((text, img_count))
    return pages


def _extract_pdf_pages_poppler(path: Path) -> list[tuple[str, int]]:
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise RuntimeError("Neither pypdf nor pdftotext is available to read PDF files")
    proc = subprocess.run(
        [pdftotext, "-layout", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    raw_pages = proc.stdout.split("\f")
    # pdftotext emits a trailing form-feed after the final page.
    if raw_pages and raw_pages[-1] == "":
        raw_pages.pop()
    return [(p.strip(), 0) for p in raw_pages]


def read_pdf(
    path: Path,
    *,
    client_path: str,
    start_page: int = 1,
    max_pages: int = 10,
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    if type(start_page) is not int or start_page < 1:
        raise ValueError("start_page must be a positive 1-based integer")
    if type(max_pages) is not int or not 1 <= max_pages <= 50:
        raise ValueError("max_pages must be an integer between 1 and 50")
    _validate_char_budget(offset, max_chars)

    try:
        import pypdf  # noqa: F401
    except ImportError:
        all_pages = _extract_pdf_pages_poppler(path)
    else:
        all_pages = _extract_pdf_pages_pypdf(path)

    total_pages = len(all_pages)
    if total_pages == 0:
        return {
            "format": "pdf",
            "total_pages": 0,
            "start_page": start_page,
            "end_page": 0,
            "pages": [],
            "has_more": False,
            "next": None,
        }
    if start_page > total_pages:
        raise ValueError(f"start_page {start_page} exceeds total_pages {total_pages}")

    limit_page = min(total_pages, start_page + max_pages - 1)
    pages_out: list[dict] = []
    used_chars = 0
    next_args: dict | None = None

    for page_num in range(start_page, limit_page + 1):
        raw_text, img_count = all_pages[page_num - 1]
        display_text = (
            raw_text
            if raw_text
            else (
                f"[No extractable text on page {page_num}; contains {img_count} embedded image(s)]"
                if img_count
                else ""
            )
        )
        page_offset = offset if page_num == start_page else 0
        if page_offset > len(display_text):
            raise ValueError(
                f"offset {page_offset} exceeds page {page_num} text length {len(display_text)}"
            )
        remaining_budget = max_chars - used_chars
        if remaining_budget <= 0:
            next_args = {"path": client_path, "start_page": page_num, "max_pages": max_pages, "max_chars": max_chars}
            break
        sliced = display_text[page_offset : page_offset + remaining_budget]
        partial = page_offset + len(sliced) < len(display_text)
        entry: dict = {"page": page_num, "text": sliced}
        if page_offset:
            entry["offset"] = page_offset
        if not raw_text and img_count:
            entry["image_only"] = True
        pages_out.append(entry)
        used_chars += len(sliced)
        if partial:
            next_args = {
                "path": client_path,
                "start_page": page_num,
                "max_pages": max_pages,
                "offset": page_offset + len(sliced),
                "max_chars": max_chars,
            }
            break

    end_page = pages_out[-1]["page"] if pages_out else start_page
    if next_args is None and end_page < total_pages:
        next_args = {
            "path": client_path,
            "start_page": end_page + 1,
            "max_pages": max_pages,
            "max_chars": max_chars,
        }

    return {
        "format": "pdf",
        "total_pages": total_pages,
        "start_page": start_page,
        "end_page": end_page,
        "pages": pages_out,
        "has_more": next_args is not None,
        "next": next_args,
    }


# ---------------- PPTX ----------------


def _resolve_rel_target(base_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = str(PurePosixPath(base_part).parent)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _pptx_ordered_slides(zf: zipfile.ZipFile) -> list[str]:
    names = set(zf.namelist())
    rels_part = "ppt/_rels/presentation.xml.rels"
    if "ppt/presentation.xml" in names and rels_part in names:
        rels_root = ET.fromstring(zf.read(rels_part))
        rel_map = {
            rel.get("Id"): _resolve_rel_target("ppt/presentation.xml", rel.get("Target", ""))
            for rel in rels_root.findall(f"./{REL_NS}Relationship")
            if rel.get("Id") and rel.get("Target")
        }
        pres_root = ET.fromstring(zf.read("ppt/presentation.xml"))
        ordered = [
            rel_map[rid]
            for sld in pres_root.findall(f".//{P_NS}sldIdLst/{P_NS}sldId")
            if (rid := sld.get(f"{R_NS}id")) in rel_map and rel_map[rid] in names
        ]
        if ordered:
            return ordered
    return sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural_key)


def _pptx_text_body(elem: ET.Element) -> list[str]:
    lines: list[str] = []
    for p in elem.findall(f".//{A_NS}p"):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == f"{A_NS}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{A_NS}br":
                parts.append("\n")
            elif node.tag == f"{A_NS}tab":
                parts.append("\t")
        text = "".join(parts).strip()
        if text:
            lines.append(text)
    return lines


def _pptx_slide_content(zf: zipfile.ZipFile, slide_part: str) -> dict:
    root = ET.fromstring(zf.read(slide_part))
    blocks: list[str] = []
    tables_count = 0
    sp_tree = root.find(f".//{P_NS}cSld/{P_NS}spTree")
    candidates = list(sp_tree) if sp_tree is not None else [root]
    for child in candidates:
        for tbl in child.findall(f".//{A_NS}tbl"):
            tables_count += 1
            t_lines: list[str] = [f"[Table {tables_count}]"]
            for r_idx, tr in enumerate(tbl.findall(f"./{A_NS}tr"), start=1):
                cells = [
                    " / ".join(_pptx_text_body(tc)).replace("|", "\\|")
                    for tc in tr.findall(f"./{A_NS}tc")
                ]
                if any(cells):
                    t_lines.append(f"  Row {r_idx}: " + " | ".join(cells))
            blocks.append("\n".join(t_lines))
        if not child.findall(f".//{A_NS}tbl"):
            text_lines = _pptx_text_body(child)
            if text_lines:
                blocks.append("\n".join(text_lines))

    notes_text = ""
    slide_Path = PurePosixPath(slide_part)
    slide_rels = f"{slide_Path.parent}/_rels/{slide_Path.name}.rels"
    if slide_rels in zf.namelist():
        rels_root = ET.fromstring(zf.read(slide_rels))
        for rel in rels_root.findall(f"./{REL_NS}Relationship"):
            if "notesSlide" in rel.get("Type", ""):
                notes_part = _resolve_rel_target(slide_part, rel.get("Target", ""))
                if notes_part in zf.namelist():
                    notes_root = ET.fromstring(zf.read(notes_part))
                    notes_lines = _pptx_text_body(notes_root)
                    notes_text = "\n".join(notes_lines).strip()
                break

    out: dict = {"text": "\n\n".join(blocks)}
    if notes_text:
        out["notes"] = notes_text
    return out


def read_pptx(
    path: Path,
    *,
    client_path: str,
    start_slide: int = 1,
    max_slides: int = 20,
    offset: int = 0,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    if type(start_slide) is not int or start_slide < 1:
        raise ValueError("start_slide must be a positive 1-based integer")
    if type(max_slides) is not int or not 1 <= max_slides <= 50:
        raise ValueError("max_slides must be an integer between 1 and 50")
    _validate_char_budget(offset, max_chars)

    with _open_ooxml_zip(path, "ppt/presentation.xml") as zf:
        slide_parts = _pptx_ordered_slides(zf)
        total_slides = len(slide_parts)
        if total_slides == 0:
            return {
                "format": "pptx",
                "total_slides": 0,
                "start_slide": start_slide,
                "end_slide": 0,
                "slides": [],
                "has_more": False,
                "next": None,
            }
        if start_slide > total_slides:
            raise ValueError(f"start_slide {start_slide} exceeds total_slides {total_slides}")

        limit_slide = min(total_slides, start_slide + max_slides - 1)
        slides_out: list[dict] = []
        used_chars = 0
        next_args: dict | None = None

        for s_num in range(start_slide, limit_slide + 1):
            parsed = _pptx_slide_content(zf, slide_parts[s_num - 1])
            body_text = parsed["text"]
            notes_text = parsed.get("notes", "")
            body_len = len(body_text)
            total_len = body_len + len(notes_text)
            slide_offset = offset if s_num == start_slide else 0
            if slide_offset > total_len:
                raise ValueError(
                    f"offset {slide_offset} exceeds slide {s_num} content length {total_len}"
                )
            remaining_budget = max_chars - used_chars
            if remaining_budget <= 0 or (slides_out and slide_offset == 0 and total_len > remaining_budget):
                next_args = {
                    "path": client_path,
                    "start_slide": s_num,
                    "max_slides": max_slides,
                    "max_chars": max_chars,
                }
                break

            end_pos = min(total_len, slide_offset + remaining_budget)
            text_slice = body_text[min(slide_offset, body_len) : min(end_pos, body_len)]
            notes_slice = notes_text[max(0, slide_offset - body_len) : max(0, end_pos - body_len)]
            partial = end_pos < total_len

            entry: dict = {"slide": s_num, "text": text_slice}
            if notes_slice:
                entry["notes"] = notes_slice
            if slide_offset:
                entry["offset"] = slide_offset
            if partial:
                entry["truncated"] = True
            slides_out.append(entry)
            used_chars += end_pos - slide_offset

            if partial:
                next_args = {
                    "path": client_path,
                    "start_slide": s_num,
                    "max_slides": max_slides,
                    "offset": end_pos,
                    "max_chars": max_chars,
                }
                break

    end_slide = slides_out[-1]["slide"] if slides_out else start_slide
    if next_args is None and end_slide < total_slides:
        next_args = {
            "path": client_path,
            "start_slide": end_slide + 1,
            "max_slides": max_slides,
            "max_chars": max_chars,
        }
    return {
        "format": "pptx",
        "total_slides": total_slides,
        "start_slide": start_slide,
        "end_slide": end_slide,
        "slides": slides_out,
        "has_more": next_args is not None,
        "next": next_args,
    }


def main() -> None:
    args = json.load(sys.stdin)
    fmt = args["format"]
    path = Path(args["path"])
    client_path = args.get("client_path") or path.name
    if fmt == "docx":
        result = read_docx(
            path,
            client_path=client_path,
            offset=args.get("offset", 0),
            max_chars=args.get("max_chars", DEFAULT_MAX_CHARS),
        )
    elif fmt == "pdf":
        result = read_pdf(
            path,
            client_path=client_path,
            start_page=args.get("start_page", 1),
            max_pages=args.get("max_pages", 10),
            offset=args.get("offset", 0),
            max_chars=args.get("max_chars", DEFAULT_MAX_CHARS),
        )
    elif fmt == "pptx":
        result = read_pptx(
            path,
            client_path=client_path,
            start_slide=args.get("start_slide", 1),
            max_slides=args.get("max_slides", 20),
            offset=args.get("offset", 0),
            max_chars=args.get("max_chars", DEFAULT_MAX_CHARS),
        )
    else:
        raise ValueError(f"Unsupported office document format: {fmt!r}")
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
