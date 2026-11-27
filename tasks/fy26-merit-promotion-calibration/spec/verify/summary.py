"""Parsing of Executive Calibration Summary .docx files in the submission."""
import datetime as _dt
import os
import re
import zipfile
from pathlib import Path

import docx
from docx.opc.exceptions import PackageNotFoundError

from . import model
from .workbook import norm

CORRUPT_DOCX = (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError, TypeError,
                AttributeError, IndexError)

MONTHS = {m.casefold(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
DATE_PATTERNS = [
    (re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", re.I), ("m", "d", "y")),
    (re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(\d{4})", re.I), ("d", "m", "y")),
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), ("y", "mn", "d")),
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), ("mn", "d", "y")),
]

TEMPLATE_PLACEHOLDERS = [
    "[TBD]",
    "[Eligible headcount, total merit spend vs. company merit budget]",
    "[Departments over pool / approvals requested]",
    "[Promotion pool position]",
    "[Lump-sum conversions]",
    "[Number of promotions, promotion pool budget, recurring promotion spend, remaining]",
    "[List each approval required under Guidelines §8 and the approver]",
    "[Update if roster changes before the Dec 1 lock.]",
]
PLACEHOLDER_RES = [re.compile(r"\bTBD\b"), re.compile(r"update if (the )?roster changes", re.I)]
DRAFT_RE = re.compile(r"\bdraft\b", re.I)
MONEY_RE = re.compile(r"(\(?)\s*([-−–]?)\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(\)?)")


def dates_in(text):
    found = []
    for pat, order in DATE_PATTERNS:
        for m in pat.finditer(text):
            g = dict(zip(order, m.groups()))
            try:
                mon = MONTHS[g["m"].casefold().rstrip(".")[:4] if g["m"].casefold().startswith("sept")
                            else g["m"].casefold().rstrip(".")[:3]] if "m" in g else int(g["mn"])
                found.append((m.start(), _dt.date(int(g["y"]), mon, int(g["d"]))))
            except (KeyError, ValueError):
                continue
    return [d for _, d in sorted(found)]


def money(text):
    """First money figure in a cell; parentheses or a leading minus mean negative."""
    if text is None:
        return None
    m = MONEY_RE.search(str(text))
    if not m:
        return None
    v = float(m.group(3).replace(",", ""))
    if m.group(2) or (m.group(1) and m.group(4)):
        v = -v
    return v


def list_docx(workspace, exclude_names=()):
    """All .docx files anywhere under the workspace except shipped reference documents (by name)."""
    out = []

    def onerr(e):
        raise e
    for root, dirs, files in os.walk(workspace, onerror=onerr):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.lower().endswith(".docx") and not f.startswith(("~$", ".~lock")) and f not in exclude_names:
                out.append(Path(root) / f)
    return out


def looks_like_summary(info):
    """A .docx is a summary candidate if its name or title block says 'summary'."""
    if "summary" in Path(info["path"]).name.lower():
        return True
    return any("summary" in t.lower() for t in info.get("title_block", []) + [info.get("core_title") or ""])


def list_summaries(workspace):
    """All .docx files with 'summary' in the name anywhere under the workspace."""
    out = []

    def onerr(e):
        raise e
    for root, dirs, files in os.walk(workspace, onerror=onerr):
        dirs.sort()
        for f in sorted(files):
            if f.lower().endswith(".docx") and "summary" in f.lower() and not f.startswith(("~$", ".~lock")):
                out.append(Path(root) / f)
    return out


def _hf_paragraphs(section):
    out = []
    for attr in ("header", "first_page_header", "even_page_header",
                 "footer", "first_page_footer", "even_page_footer"):
        part = getattr(section, attr, None)
        if part is None or part.is_linked_to_previous and attr != "header":
            continue
        out.extend(p.text for p in part.paragraphs)
        for t in part.tables:
            for row in t.rows:
                out.extend(c.text for c in row.cells)
    return [t for t in out if t and t.strip()]


def parse_summary(path, workspace, template=None):
    """template: trusted template texts (see template_texts) used to keep shell text out of excerpts."""
    rel = str(path.relative_to(workspace))
    info = {"path": rel, "name_draft": bool(DRAFT_RE.search(path.stem.replace("_", " "))),
            "name_final": "final" in path.stem.lower(), "error": None}
    try:
        d = docx.Document(str(path))
    except FileNotFoundError:
        info["error"] = "missing"
        return info
    except (IsADirectoryError, PermissionError) as e:
        info["error"] = f"not accessible: {e}"
        return info
    except CORRUPT_DOCX as e:
        info["error"] = f"corrupt docx: {type(e).__name__}: {e}"
        return info
    hf = []
    for s in d.sections:
        hf.extend(_hf_paragraphs(s))
    paras = [p for p in d.paragraphs]
    block = []
    for i, p in enumerate(paras):
        st = (p.style.name if p.style is not None else "") or ""
        if i > 0 and (st.startswith("Heading 2") or re.match(r"^\s*1\.\s+\S", p.text)):
            break
        if i >= 15:
            break
        if p.text.strip():
            block.append(p.text)
    title = d.core_properties.title or ""
    info["header_footer_text"] = hf
    info["title_block"] = block
    info["core_title"] = title
    marking_src = [t for t in hf + block + [title] if DRAFT_RE.search(t)]
    info["draft_marking"] = marking_src
    # date: prefer an explicit "Date:" field in title block / header
    date_field = None
    for t in block + hf:
        for line in t.splitlines():
            m = re.search(r"\bdate[d]?\s*[:\-]\s*(.+)", line, re.I)
            if m:
                ds = dates_in(m.group(1))
                if ds:
                    date_field = (line.strip(), ds[0])
                    break
        if date_field:
            break
    if date_field is None:
        for t in block + hf:
            ds = dates_in(t)
            if ds:
                date_field = (t.strip()[:120], ds[0])
                break
    info["date_line"] = date_field[0] if date_field else None
    info["date"] = date_field[1].isoformat() if date_field else None
    # full text for placeholder search
    texts = [p.text for p in paras] + hf
    for t in d.tables:
        for row in t.rows:
            texts.extend(c.text for c in row.cells)
    full = "\n".join(texts)
    ph = [p for p in TEMPLATE_PLACEHOLDERS if p in full]
    n_tbd = len(PLACEHOLDER_RES[0].findall(full))
    if n_tbd:
        ph.append(f"'TBD' x{n_tbd}")
    ph.extend(sorted({m.group(0) for m in PLACEHOLDER_RES[1].finditer(full)}))
    info["placeholders"] = ph
    # every date carried by the title block, header/footer or date line (issue-date identification)
    iss = set()
    for t in block + hf:
        iss.update(x.isoformat() for x in dates_in(t))
    if info["date"]:
        iss.add(info["date"])
    info["issue_dates"] = sorted(iss)
    # content units for stage-work markers: body paragraphs and table rows outside the title block,
    # with unchanged shell text dropped
    tpl = template or set()
    units = []
    for i, p in enumerate(paras):
        t = p.text.strip()
        if t and t not in tpl and i >= len(block) and not any(t == b.strip() for b in block):
            units.append(t)
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                units.append(" | ".join(cells))
    info["content_units"] = units
    info["dept_table"], info["dept_table_error"] = _dept_table(d)
    info["lump_table"], info["lump_table_error"], info["product_comparison_tables"] = _lump_tables(d)
    info["sections"] = _sections(d, template)
    return info


def _header_cols(row_cells):
    return {i: norm(c.text) for i, c in enumerate(row_cells)}


def _dept_table(d):
    names = {norm(x): x for x in model.DEPTS}
    for t in d.tables:
        if not t.rows:
            continue
        h = _header_cols(t.rows[0].cells)
        if not any(v == "department" for v in h.values()) or not any("budget" in v for v in h.values()):
            continue
        cols = {}
        for k, pred in (("budget", lambda v: "budget" in v), ("spend", lambda v: "spend" in v),
                        ("remaining", lambda v: "remaining" in v or re.search(r"\bover", v) is not None),
                        ("status", lambda v: "status" in v)):
            c = [i for i, v in h.items() if pred(v)]
            if c:
                cols[k] = c[0]
        rows = {}
        for row in t.rows[1:]:
            cells = row.cells
            lab = norm(cells[0].text)
            key = names.get(lab) or ("Company Total" if "total" in lab else None)
            if not key or key in rows:
                continue
            rows[key] = {k: cells[c].text.strip() if c < len(cells) else None for k, c in cols.items()}
        return {"columns": {k: h[c] for k, c in cols.items()}, "rows": rows}, None
    return None, "no table with 'Department' and budget headers"


PRODUCT_CMP_WORDS = re.compile(r"before|after|original|revised|reduced|adjusted|factor|matrix|pre-?decision|post-?decision")


def _caption(d, tbl_el):
    prev = tbl_el.getprevious()
    while prev is not None:
        if prev.tag.endswith("}p"):
            t = "".join(x.text or "" for x in prev.iter() if x.tag.endswith("}t")).strip()
            if t:
                return t
        elif prev.tag.endswith("}tbl"):
            return ""
        prev = prev.getprevious()
    return ""


def _lump_tables(d):
    """Company lump-sum list = the Employee+Lump table with the most employee rows that is not a
    Product before/after comparison; comparison tables are reported separately."""
    cands, product_cmp = [], []
    for ti, t in enumerate(d.tables):
        if not t.rows:
            continue
        h = _header_cols(t.rows[0].cells)
        lc = [i for i, v in h.items() if "lump" in v and "approv" not in v]
        if not lc or not any(("employee" in v or v in ("id", "emp id")) for v in h.values()):
            continue
        cap = _caption(d, t._tbl)
        rows, total = [], None
        for row in t.rows[1:]:
            txt = [c.text.strip() for c in row.cells]
            ids = [m.group(0) for x in txt for m in [re.search(r"\bE\d{4}\b", x)] if m]
            amt = money(txt[lc[0]]) if lc[0] < len(txt) else None
            if ids:
                rows.append({"id": ids[0], "amount_text": txt[lc[0]] if lc[0] < len(txt) else None,
                             "amount": amt, "cells": txt})
            elif any("total" in norm(x) for x in txt) and amt is not None:
                total = amt
        entry = {"table_index": ti, "caption": cap, "header": [c.text.strip() for c in t.rows[0].cells],
                 "rows": rows, "total_row": total}
        hdr = " ".join(h.values())
        if PRODUCT_CMP_WORDS.search(hdr) or ("product" in norm(cap) and "all" not in norm(cap)):
            product_cmp.append(entry)
        else:
            cands.append(entry)
    if not cands:
        return None, "no table with Employee and Lump Sum headers (other than a Product comparison)", product_cmp
    best = max(cands, key=lambda e: (len(e["rows"]), -e["table_index"]))
    return best, None, product_cmp


TOPIC_RULES = [
    ("promotion_pool", re.compile(r"promotion")),
    ("approvals_status", re.compile(r"approv|status")),
    ("basis_of_preparation", re.compile(r"basis|preparation")),
    ("product_exception", re.compile(r"product|cfo decision|exception decision")),
    ("data_exceptions_notes", re.compile(r"exception|note|data|issue|flag|below|minimum|open item")),
    ("lump_sums", re.compile(r"lump")),
    ("department_pools", re.compile(r"department|merit pool")),
    ("overview", re.compile(r"summary|overview|highlight")),
]


def template_texts(code_dir):
    """Paragraph texts of the trusted summary shell (trusted material: errors propagate)."""
    d = docx.Document(str(Path(code_dir) / model.TRUSTED_SUMMARY))
    return {p.text.strip() for p in d.paragraphs if p.text.strip()}


def _is_heading(p):
    st = (p.style.name if p.style is not None else "") or ""
    t = p.text.strip()
    return st.startswith(("Heading", "Title")) or (bool(re.match(r"^\d+(\.\d+)*\.?\s+\S", t)) and len(t) < 90
                                                   and "\n" not in t)


def _sections(d, template):
    """Body in document order, split by headings; each section keeps paragraph and table excerpts
    with their indices. Unchanged shell text is dropped unless it is a bracketed placeholder."""
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    template = template or set()
    secs = [{"heading": "(before first heading)", "heading_paragraph_index": None, "topics": [],
             "paragraphs": [], "tables": [], "placeholders": []}]
    pi = ti = 0
    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            p = Paragraph(el, d)
            t = p.text.strip()
            idx = pi
            pi += 1
            if not t:
                continue
            if _is_heading(p) and idx > 0:
                h = norm(t)
                secs.append({"heading": t, "heading_paragraph_index": idx,
                             "topics": [k for k, rx in TOPIC_RULES if rx.search(h)],
                             "paragraphs": [], "tables": [], "placeholders": []})
                continue
            if t in template:
                if "[" in t or re.search(r"\bTBD\b", t):
                    secs[-1]["placeholders"].append({"paragraph_index": idx, "text": t})
                continue
            secs[-1]["paragraphs"].append({"paragraph_index": idx, "text": t})
        elif el.tag.endswith("}tbl"):
            tb = Table(el, d)
            rows = [[c.text.strip() for c in r.cells] for r in tb.rows]
            secs[-1]["tables"].append({"table_index": ti, "caption": _caption(d, el), "rows": rows})
            ti += 1
    topic_index = {}
    for i, sct in enumerate(secs):
        for k in sct["topics"]:
            topic_index.setdefault(k, []).append(i)
    return {"sections": secs, "topic_index": topic_index,
            "note": "topics are labels from heading text only; no content verdict"}


def is_draft(info):
    return bool(info.get("draft_marking")) or info["name_draft"]


def summary_overview(info):
    keep = ("path", "error", "name_draft", "name_final", "draft_marking", "core_title", "date",
            "date_line", "issue_dates", "placeholders", "title_block", "header_footer_text", "dept_table", "dept_table_error",
            "lump_table", "lump_table_error", "product_comparison_tables", "sections")
    return {k: info.get(k) for k in keep}
