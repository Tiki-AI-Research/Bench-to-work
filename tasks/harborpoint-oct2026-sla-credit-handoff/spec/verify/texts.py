"""File inventory and readable text of new/modified Worker documents."""
import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from .reference import CODE_DIR, TRUSTED_FILES

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".cache", ".venv", "venv"}
OTHER_TEXT_EXT = {".txt", ".md", ".eml", ".html", ".htm"}
MAX_TEXT_CHARS = 200000

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def inventory(workspace):
    """Compare every submitted file with the trusted originals by bytes.
    Trusted originals are read from beside the code; failure there raises."""
    ws = Path(workspace)
    trusted = {rel: (CODE_DIR / rel).read_bytes() for rel in TRUSTED_FILES}
    inv = {"new": [], "modified": [], "unchanged": [], "trusted_not_in_submission": [],
           "unreadable": [], "walk_errors": []}
    seen = set()
    files = {}
    for dirpath, dirnames, filenames in os.walk(ws, onerror=lambda e: inv["walk_errors"].append(repr(e))):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.is_symlink():
                continue
            rel = p.relative_to(ws).as_posix()
            try:
                data = p.read_bytes()
            except (PermissionError, IsADirectoryError, FileNotFoundError) as exc:
                inv["unreadable"].append("%s (%s)" % (rel, type(exc).__name__))
                continue
            files[rel] = data
            if rel in trusted:
                seen.add(rel)
                (inv["unchanged"] if trusted[rel] == data else inv["modified"]).append(rel)
            else:
                inv["new"].append(rel)
    inv["trusted_not_in_submission"] = [r for r in TRUSTED_FILES if r not in seen]
    inv["sizes"] = {rel: len(files[rel]) for rel in inv["new"] + inv["modified"]}
    return inv, files


def _para_text(p_el):
    parts = []
    for el in p_el.iter():
        tag = el.tag
        if tag == W_NS + "t" and el.text:
            parts.append(el.text)
        elif tag == W_NS + "tab":
            parts.append("\t")
        elif tag in (W_NS + "br", W_NS + "cr"):
            parts.append("\n")
        elif tag == W_NS + "p" and el is not p_el:
            parts.append(" ")
    return "".join(parts).strip()


def _row_text(tr_el):
    cells = []
    for tc in tr_el.findall(W_NS + "tc"):
        paras = [_para_text(p) for p in tc.iter(W_NS + "p")]
        cells.append(" / ".join(t for t in paras if t))
    dedup = []
    for c in cells:
        if not dedup or dedup[-1] != c or c == "":
            dedup.append(c)
    return " | ".join(dedup)


def _walk_block(container, counters, lines):
    for child in container:
        tag = child.tag
        if tag == W_NS + "p":
            counters["p"] += 1
            t = _para_text(child)
            if t:
                lines.append("[P%d] %s" % (counters["p"], t))
        elif tag == W_NS + "tbl":
            counters["t"] += 1
            tno = counters["t"]
            for ri, tr in enumerate(child.findall(W_NS + "tr"), 1):
                lines.append("[Table %d R%d] %s" % (tno, ri, _row_text(tr)))
        elif tag == W_NS + "sdt":
            content = child.find(W_NS + "sdtContent")
            if content is not None:
                _walk_block(content, counters, lines)


def docx_text(path):
    import docx  # python-docx
    d = docx.Document(str(path))
    lines = []
    seen_parts = set()
    for si, sec in enumerate(d.sections, 1):
        for label, hf in (("Header", sec.header), ("Footer", sec.footer)):
            try:
                if hf.is_linked_to_previous:
                    continue
                part_id = id(hf._element)
            except Exception:
                continue
            if part_id in seen_parts:
                continue
            seen_parts.add(part_id)
            sub = []
            _walk_block(hf._element, {"p": 0, "t": 0}, sub)
            for line in sub:
                lines.append("[%s S%d] %s" % (label, si, line))
    counters = {"p": 0, "t": 0}
    _walk_block(d.element.body, counters, lines)
    return "\n".join(lines)


DOCX_ERRORS = (zipfile.BadZipFile, KeyError, ValueError, TypeError, AttributeError)


def _pdf_text(path):
    """Extract PDF text page by page via pypdf, falling back to /usr/bin/pdftotext."""
    try:
        import pypdf
    except ImportError:
        pdftotext = shutil.which("pdftotext") or (
            "/usr/bin/pdftotext" if os.path.isfile("/usr/bin/pdftotext") else None
        )
        if pdftotext is None:
            raise RuntimeError("neither pypdf nor pdftotext is available to read PDF files")
        try:
            proc = subprocess.run(
                [pdftotext, "-layout", str(path), "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("pdftotext timed out after %ss" % exc.timeout) from exc
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError("pdftotext rc=%d: %s" % (proc.returncode, err[:300]))
        raw = proc.stdout.decode("utf-8", errors="replace")
        pages = raw.split("\f")
        if pages and pages[-1] == "":
            pages = pages[:-1]
        return "\n".join("[Page %d]\n%s" % (i, pg) for i, pg in enumerate(pages, 1))

    r = pypdf.PdfReader(str(path))
    return "\n".join("[Page %d]\n%s" % (i, (pg.extract_text() or "")) for i, pg in enumerate(r.pages, 1))


def collect_texts(workspace, inv, files):
    ws = Path(workspace)
    docx_texts, other_texts, pdf_texts, errors = {}, {}, {}, {}
    for rel in inv["new"] + inv["modified"]:
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("~$") or name.startswith(".~lock"):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext == ".docx":
            try:
                txt = docx_text(ws / rel)
            except DOCX_ERRORS as exc:
                errors[rel] = "unreadable .docx: %s: %s" % (type(exc).__name__, exc)
                continue
            except Exception as exc:  # python-docx raises package-specific errors on corrupt files
                if type(exc).__module__.startswith(("docx", "lxml")):
                    errors[rel] = "unreadable .docx: %s: %s" % (type(exc).__name__, exc)
                    continue
                raise
            docx_texts[rel] = txt if txt else "(document contains no text)"
        elif ext in OTHER_TEXT_EXT:
            data = files[rel]
            txt = data.decode("utf-8", errors="replace")
            if len(txt) > MAX_TEXT_CHARS:
                txt = txt[:MAX_TEXT_CHARS] + "\n[truncated at %d chars]" % MAX_TEXT_CHARS
            other_texts[rel] = txt if txt.strip() else "(empty file)"
        elif ext == ".pdf":
            try:
                txt = _pdf_text(ws / rel)
            except (ValueError, OSError, EOFError) as exc:
                errors[rel] = "unreadable .pdf: %s: %s" % (type(exc).__name__, exc)
                continue
            except Exception as exc:
                if type(exc).__module__.startswith("pypdf"):
                    errors[rel] = "unreadable .pdf: %s: %s" % (type(exc).__name__, exc)
                    continue
                raise
            if len(txt) > MAX_TEXT_CHARS:
                txt = txt[:MAX_TEXT_CHARS] + "\n[truncated at %d chars]" % MAX_TEXT_CHARS
            pdf_texts[rel] = txt if txt.strip() else "(document contains no extractable text)"
    return docx_texts, other_texts, pdf_texts, errors
