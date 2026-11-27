import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createEvidenceTools } from "./tools.mjs";

const REPO_PY = fileURLToPath(new URL("../../.venv/bin/python", import.meta.url));

test("evidence tools restrict reads to visible root and block path traversal or escaping symlinks", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-tools-test-"));
  try {
    const visible = path.join(root, "visible");
    await mkdir(visible);
    await writeFile(path.join(visible, "answer.json"), '{"amount":47}');
    await writeFile(path.join(root, "hidden.txt"), "hidden rubric");
    await symlink(path.join(root, "hidden.txt"), path.join(visible, "escape.txt"));
    const tools = createEvidenceTools(visible, REPO_PY, false);
    assert.deepEqual(tools.map(t => t.name), ["ls", "read", "read_json", "read_excel", "read_docx", "read_pdf", "read_pptx"]);
    const read = tools.find(t => t.name === "read");
    const result = await read.execute("1", { path: "answer.json" });
    assert.equal(result.content[0].text, '{"amount":47}');
    await writeFile(path.join(visible, "gateway.log"), "accepted M-0006\nack M-0014\n");
    const receipt = await read.execute("receipt", { path: "gateway.log", offset: 2, limit: 1 });
    assert.equal(receipt.content[0].text.split("\n")[0], "ack M-0014");
    for (const filename of ["../hidden.txt", path.join(root, "hidden.txt"), "escape.txt"]) {
      await assert.rejects(read.execute("2", { path: filename }), /outside this role's visible workspace/);
    }
    assert.equal(await readFile(path.join(visible, "answer.json"), "utf8"), '{"amount":47}');
    assert.deepEqual(createEvidenceTools(null, "python3", false), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("read_json preserves false/null, unescapes pointers, and flags oversized objects without returning body", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-json-test-"));
  try {
    const value = { observations: { "a/b~c": [false, null, 47], history: "x".repeat(20000) } };
    const filename = path.join(root, "evidence.json");
    await writeFile(filename, JSON.stringify(value));
    const tool = createEvidenceTools(root, "python3", false).find(t => t.name === "read_json");
    const read = async args => JSON.parse((await tool.execute("1", { path: "evidence.json", ...args })).content[0].text);
    assert.deepEqual((await read({})).keys, ["observations"]);
    assert.equal((await read({ pointer: "/observations/a~1b~0c/0" })).value, false);
    assert.equal((await read({ pointer: "/observations/a~1b~0c/1" })).value, null);
    assert.equal((await read({ pointer: "/observations/a~1b~0c/2" })).value, 47);
    const large = await read({ pointer: "/observations" });
    assert.equal(large.too_large, true);
    assert.ok(!Object.hasOwn(large, "value"));
    for (const pointer of ["observations", "/observations/~2", "/observations/a~1b~0c/length", "/missing", "/toString"]) {
      await assert.rejects(read({ pointer }));
    }
    await assert.rejects(tool.execute("2", { path: "../hidden.json" }));
    assert.deepEqual(JSON.parse(await readFile(filename, "utf8")), value);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("read_conversation paginates long messages by index while preserving content, attachments, and bounds", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-conversation-test-"));
  try {
    await mkdir(path.join(root, "evaluation"));
    const original = [
      { index: 0, role: "user", content: "earlier stage" },
      { index: 1, role: "user", content: "stage request" },
      { index: 2, role: "assistant", content: "long reply ".repeat(10000), attachments: [{ path: "answer.xlsx", kind: "file" }] },
      { index: 3, role: "user", content: "confirmed" },
      { index: 4, role: "assistant", content: "next stage" },
    ];
    const filename = path.join(root, "evaluation/conversation.jsonl");
    const raw = original.map(message => JSON.stringify(message)).join("\n") + "\n";
    await writeFile(filename, raw);
    const tool = createEvidenceTools(root, "python3", false, "reviewer").find(t => t.name === "read_conversation");
    const read = async args => JSON.parse((await tool.execute("1", args)).content[0].text);
    const collected = new Map();
    let args = { start_index: 1, end_index: 3, max_messages: 1 };
    let pages = 0;
    do {
      const result = await read(args);
      assert.equal(result.has_more, result.next !== null);
      for (const message of result.messages) {
        assert.ok(message.index >= 1 && message.index <= 3);
        const previous = collected.get(message.index)?.content ?? "";
        assert.equal(message.content_offset, previous.length);
        collected.set(message.index, { ...message, content: previous + message.content });
      }
      args = result.next;
      pages += 1;
    } while (args !== null);
    assert.ok(pages > 3);
    assert.deepEqual([...collected.keys()], [1, 2, 3]);
    assert.equal(collected.get(2).content, original[2].content);
    assert.deepEqual(collected.get(2).attachments, original[2].attachments);
    assert.equal(collected.get(2).role, "assistant");
    assert.ok([...collected.values()].every(message => message.content_complete));
    assert.deepEqual((await read({ start_index: 9 })).messages, []);
    for (const invalid of [{ start_index: 3, end_index: 1 }, { max_messages: 0 }, { start_index: 2, offset: 999999 }]) {
      await assert.rejects(read(invalid));
    }
    assert.equal(await readFile(filename, "utf8"), raw);
    assert.ok(!createEvidenceTools(root, "python3", false, "user").some(t => t.name === "read_conversation"));
    assert.deepEqual(createEvidenceTools(null, "python3", false, "reviewer"), []);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("read_conversation blocks symlinks escaping visible root and surfaces malformed JSONL", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-conversation-boundary-"));
  try {
    const visible = path.join(root, "visible");
    await mkdir(path.join(visible, "evaluation"), { recursive: true });
    const hidden = path.join(root, "hidden.jsonl");
    await writeFile(hidden, '{"index":0,"role":"user","content":"hidden"}\n');
    const filename = path.join(visible, "evaluation/conversation.jsonl");
    await symlink(hidden, filename);
    const tool = createEvidenceTools(visible, "python3", false, "reviewer").find(t => t.name === "read_conversation");
    await assert.rejects(tool.execute("1", {}), /outside this role's visible workspace/);
    await rm(filename);
    await writeFile(filename, "not json\n");
    await assert.rejects(tool.execute("2", {}), SyntaxError);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("large text files paginate by line and read_json handles files larger than 8MB while blocking binary formats", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-large-test-"));
  try {
    await writeFile(path.join(root, "large.txt"), "early line\n".repeat(1500000) + "final line");
    await writeFile(path.join(root, "office.json"), JSON.stringify({
      unrelated: "x".repeat(9 * 1024 * 1024),
      "current/output/memo.docx": { paragraphs: ["target body"] },
    }));
    const tools = createEvidenceTools(root, "python3", false);
    const read = tools.find(t => t.name === "read");
    const first = await read.execute("1", { path: "large.txt", limit: 2 });
    assert.match(first.content[0].text, /early line/);
    assert.match(first.content[0].text, /offset=3/);
    const last = await read.execute("2", { path: "large.txt", offset: 1500001 });
    assert.equal(last.content[0].text, "final line");
    const json = tools.find(t => t.name === "read_json");
    const result = await json.execute("3", { path: "office.json", pointer: "/current~1output~1memo.docx/paragraphs" });
    assert.deepEqual(JSON.parse(result.content[0].text).value, ["target body"]);
    // Native read must not expand permissions or misread binary Office files as plain text.
    await writeFile(path.join(root, "memo.docx"), "not plain text");
    await assert.rejects(read.execute("4", { path: "memo.docx" }), /No plain-text read tool/);
    await assert.rejects(tools.find(t => t.name === "ls").execute("5", { path: ".." }), /outside this role's visible workspace/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("single-line JSON long string fields paginate completely without shell or line tools", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-json-pages-"));
  try {
    const original = 'body😀\n"quote"'.repeat(15000);
    await writeFile(path.join(root, "single-line.json"), JSON.stringify({ body: original }));
    const tool = createEvidenceTools(root, "python3", false).find(t => t.name === "read_json");
    let args = { path: "single-line.json", pointer: "/body" };
    let collected = "";
    do {
      const result = JSON.parse((await tool.execute("1", args)).content[0].text);
      assert.equal(result.offset, collected.length);
      assert.equal(result.total_length, original.length);
      assert.ok(result.value.length <= 16000);
      assert.equal(result.has_more, result.next !== null);
      collected += result.value;
      args = result.next;
    } while (args !== null);
    assert.equal(collected, original);
    await assert.rejects(tool.execute("2", { path: "single-line.json", pointer: "/body", offset: original.length + 1 }), /offset/);
    await assert.rejects(tool.execute("3", { path: "single-line.json", pointer: "", offset: 1 }), /string/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("conversation files over 8MB remain readable by index range without dropping boundary messages", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "pi-conversation-large-"));
  try {
    await mkdir(path.join(root, "evaluation"));
    const messages = Array.from({ length: 7000 }, (_, index) => ({ index, role: "assistant", content: "x".repeat(1500) }));
    const raw = messages.map(m => JSON.stringify(m)).join("\n");
    assert.ok(Buffer.byteLength(raw) > 8 * 1024 * 1024);
    await writeFile(path.join(root, "evaluation/conversation.jsonl"), raw);
    const tool = createEvidenceTools(root, "python3", false, "reviewer").find(t => t.name === "read_conversation");
    const read = async args => JSON.parse((await tool.execute("1", args)).content[0].text);
    const first = await read({ start_index: 6997, end_index: 6998, max_messages: 1 });
    assert.equal(first.last_index, 6999);
    assert.equal(first.messages[0].index, 6997);
    assert.equal(first.messages[0].content, messages[6997].content);
    const second = await read(first.next);
    assert.equal(second.messages[0].index, 6998);
    assert.equal(second.messages[0].content, messages[6998].content);
    assert.equal(second.next, null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("read_docx, read_pdf, and read_pptx extract structured Office/PDF content, paginate, and enforce boundaries", async () => {
  const outer = await mkdtemp(path.join(os.tmpdir(), "pi-office-tools-"));
  try {
    const root = path.join(outer, "visible");
    await mkdir(root);
    await writeFile(path.join(outer, "escape.docx"), "outside");
    execFileSync(REPO_PY, ["-c", `
import zipfile
from pathlib import Path
from pypdf import PdfWriter

root = Path(${JSON.stringify(root)})

# 1. Construct a rich .docx with header, heading, interleaved paragraphs, table, SDT control, deleted revision, comment, and footer.
with zipfile.ZipFile(root / "report.docx", "w") as zf:
    zf.writestr("word/header1.xml", '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>CONFIDENTIAL HEADER</w:t></w:r></w:p></w:hdr>')
    zf.writestr("word/footer1.xml", '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Page Footer v2</w:t></w:r></w:p></w:ftr>')
    zf.writestr("word/comments.xml", '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:id="1" w:author="Auditor" w:date="2026-11-24"><w:p><w:r><w:t>Verify $4,800 cap</w:t></w:r></w:p></w:comment></w:comments>')
    zf.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Credit Memo Request</w:t></w:r></w:p>
        <w:p><w:r><w:t>Approved amount: $2,400</w:t></w:r><w:del><w:r><w:delText>OLD $9,999</w:delText></w:r></w:del></w:p>
        <w:tbl>
          <w:tr><w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
          <w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr>
          <w:tr><w:tc><w:p><w:r><w:t>Account</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>ARC-10482</w:t></w:r></w:p></w:tc></w:tr>
        </w:tbl>
        <w:sdt><w:sdtContent><w:p><w:r><w:t>Control Block Sign-off</w:t></w:r></w:p></w:sdtContent></w:sdt>
      </w:body>
    </w:document>''')

# 2. Construct a valid 2-page PDF with text via minimal PDF syntax + pypdf validation.
pdf_bytes = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 48 >> stream
BT /F1 12 Tf 20 100 Td (Page One Tariff 112125) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
6 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
7 0 obj << /Length 48 >> stream
BT /F1 12 Tf 20 100 Td (Page Two Cutoff Nov19) Tj ET
endstream endobj
xref
0 8
0000000000 65535 f\\x20
0000000009 00000 n\\x20
0000000058 00000 n\\x20
0000000121 00000 n\\x20
0000000247 00000 n\\x20
0000000345 00000 n\\x20
0000000415 00000 n\\x20
0000000541 00000 n\\x20
trailer << /Size 8 /Root 1 0 R >>
startxref
639
%%EOF
"""
(root / "schedule.pdf").write_bytes(pdf_bytes)

# 3. Construct a 2-slide .pptx with presentation order [slide2, slide1], table, and speaker notes.
with zipfile.ZipFile(root / "deck.pptx", "w") as zf:
    zf.writestr("ppt/presentation.xml", '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:sldIdLst><p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/></p:sldIdLst>
    </p:presentation>''')
    zf.writestr("ppt/_rels/presentation.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="slide" Target="slides/slide1.xml"/>
      <Relationship Id="rId2" Type="slide" Target="slides/slide2.xml"/>
    </Relationships>''')
    zf.writestr("ppt/slides/slide2.xml", '''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Executive Slide First</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
    </p:sld>''')
    zf.writestr("ppt/slides/_rels/slide2.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/>
    </Relationships>''')
    zf.writestr("ppt/notesSlides/notesSlide1.xml", '''<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Speaker note: mention Dock C</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
    </p:notes>''')
    zf.writestr("ppt/slides/slide1.xml", '''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
      <p:cSld><p:spTree><p:graphicFrame><a:graphic><a:graphicData><a:tbl>
        <a:tr><a:tc><a:txBody><a:p><a:r><a:t>Metric</a:t></a:r></a:p></a:txBody></a:tc><a:tc><a:txBody><a:p><a:r><a:t>99.61%</a:t></a:r></a:p></a:txBody></a:tc></a:tr>
      </a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld>
    </p:sld>''')

# 4. Construct a single-slide .pptx with 150 chars body + 250 chars speaker notes + an .xlsx workbook.
with zipfile.ZipFile(root / "long_slide.pptx", "w") as zf:
    zf.writestr("ppt/presentation.xml", '''<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>
    </p:presentation>''')
    zf.writestr("ppt/_rels/presentation.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="slide" Target="slides/slide1.xml"/>
    </Relationships>''')
    zf.writestr("ppt/slides/slide1.xml", '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>' + ("B" * 150) + '</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    zf.writestr("ppt/slides/_rels/slide1.xml.rels", '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide" Target="../notesSlides/notesSlide1.xml"/>
    </Relationships>''')
    zf.writestr("ppt/notesSlides/notesSlide1.xml", '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>' + ("N" * 250) + '</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>')

from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.title = "Summary"
ws["A1"] = 42
wb.save(root / "book.xlsx")
`]);

    const tools = createEvidenceTools(root, REPO_PY, false, "reviewer");
    const readExcel = tools.find(t => t.name === "read_excel");
    const readDocx = tools.find(t => t.name === "read_docx");
    const readPdf = tools.find(t => t.name === "read_pdf");
    const readPptx = tools.find(t => t.name === "read_pptx");

    // Verify Excel empty-string sheet lists worksheets while misspelled sheet still errors
    const sheetsByOmit = JSON.parse((await readExcel.execute("e1", { path: "book.xlsx" })).content[0].text);
    const sheetsByEmpty = JSON.parse((await readExcel.execute("e2", { path: "book.xlsx", sheet: "" })).content[0].text);
    assert.deepEqual(sheetsByEmpty, sheetsByOmit);
    assert.equal(sheetsByEmpty.sheets[0].name, "Summary");
    await assert.rejects(readExcel.execute("e-bad", { path: "book.xlsx", sheet: "Missing" }), /not found/);

    // Verify DOCX extraction, original 1-based table row numbering across blank rows, & pagination
    const docxFull = JSON.parse((await readDocx.execute("d1", { path: "report.docx" })).content[0].text);
    assert.match(docxFull.content, /CONFIDENTIAL HEADER/);
    assert.match(docxFull.content, /\[Heading1\] Credit Memo Request/);
    assert.match(docxFull.content, /Approved amount: \$2,400/);
    assert.doesNotMatch(docxFull.content, /OLD \$9,999/);
    assert.match(docxFull.content, /\[Table 1: 3 rows x 2 cols\]/);
    assert.match(docxFull.content, /Row 1: Field \| Value/);
    assert.doesNotMatch(docxFull.content, /Row 2:/);
    assert.match(docxFull.content, /Row 3: Account \| ARC-10482/);
    assert.match(docxFull.content, /Control Block Sign-off/);
    assert.match(docxFull.content, /Auditor.*Verify \$4,800 cap/);
    assert.match(docxFull.content, /Page Footer v2/);

    let docxCollected = "";
    let docxArgs = { path: "report.docx", max_chars: 40 };
    do {
      const page = JSON.parse((await readDocx.execute("dp", docxArgs)).content[0].text);
      docxCollected += page.content;
      docxArgs = page.next;
    } while (docxArgs !== null);
    assert.equal(docxCollected, docxFull.content);
    await assert.rejects(readDocx.execute("d-bad", { path: "schedule.pdf" }), /only supports \.docx/);
    await assert.rejects(readDocx.execute("d-esc", { path: "../escape.docx" }), /outside this role's visible workspace/);

    // Verify PDF page extraction & pagination
    const pdfP1 = JSON.parse((await readPdf.execute("p1", { path: "schedule.pdf", start_page: 1, max_pages: 1 })).content[0].text);
    assert.equal(pdfP1.total_pages, 2);
    assert.equal(pdfP1.has_more, true);
    assert.match(pdfP1.pages[0].text, /Page One Tariff 112125/);
    const pdfP2 = JSON.parse((await readPdf.execute("p2", pdfP1.next)).content[0].text);
    assert.equal(pdfP2.has_more, false);
    assert.match(pdfP2.pages[0].text, /Page Two Cutoff Nov19/);

    // Verify PPTX presentation-ordered extraction, table, and speaker notes
    const ppt1 = JSON.parse((await readPptx.execute("s1", { path: "deck.pptx", start_slide: 1, max_slides: 1 })).content[0].text);
    assert.equal(ppt1.total_slides, 2);
    assert.equal(ppt1.has_more, true);
    assert.match(ppt1.slides[0].text, /Executive Slide First/);
    assert.match(ppt1.slides[0].notes, /Speaker note: mention Dock C/);
    const ppt2 = JSON.parse((await readPptx.execute("s2", ppt1.next)).content[0].text);
    assert.equal(ppt2.has_more, false);
    assert.match(ppt2.slides[0].text, /Metric \| 99\.61%/);

    // Verify PPTX intra-slide offset pagination with shared body + speaker notes budget
    let bodyAcc = "";
    let notesAcc = "";
    let pptArgs = { path: "long_slide.pptx", max_chars: 100 };
    let pages = 0;
    do {
      const chunk = JSON.parse((await readPptx.execute(`lp-${pages}`, pptArgs)).content[0].text);
      const s = chunk.slides[0];
      assert.ok(s.text.length + (s.notes ?? "").length <= 100);
      bodyAcc += s.text;
      notesAcc += s.notes ?? "";
      pages += 1;
      pptArgs = chunk.next;
    } while (pptArgs !== null);
    assert.equal(pages, 4);
    assert.equal(bodyAcc, "B".repeat(150));
    assert.equal(notesAcc, "N".repeat(250));
  } finally {
    await rm(outer, { recursive: true, force: true });
  }
});

test("reviewer sandbox bash mounts review root read-only, writes only to scratch, blocks network, and is never exposed to user", async (t) => {
  let sandboxImage = null;
  const candidates = [process.env.MULTI_ROUND_TEST_SANDBOX_IMAGE, "python:3.11-slim", "python:3.12-slim"].filter(Boolean);
  for (const candidate of candidates) {
    try {
      execFileSync("docker", ["image", "inspect", candidate], { stdio: "ignore" });
      sandboxImage = candidate;
      break;
    } catch {}
  }
  if (!sandboxImage) {
    t.skip("No local Docker image available for Reviewer sandbox test");
    return;
  }

  const outer = await mkdtemp(path.join(os.tmpdir(), "pi-sandbox-bash-"));
  try {
    const root = path.join(outer, "review");
    await mkdir(path.join(root, "submission"), { recursive: true });
    await mkdir(path.join(root, "evaluation"), { recursive: true });
    await mkdir(path.join(root, "scratch"), { recursive: true });
    await writeFile(path.join(root, "submission/ledger.csv"), "item,amount\nA,120\nB,80\n");
    await writeFile(path.join(root, "evaluation/conversation.jsonl"), '{"index":0,"role":"user","content":"hi"}\n');

    // User NEVER gets bash even if sandbox config is passed.
    const userTools = createEvidenceTools(root, REPO_PY, false, "user", { image: sandboxImage });
    assert.ok(!userTools.some(tool => tool.name === "bash"));
    assert.deepEqual(createEvidenceTools(null, REPO_PY, false, "reviewer", { image: sandboxImage }), []);

    const revTools = createEvidenceTools(root, REPO_PY, false, "reviewer", { image: sandboxImage, defaultTimeoutSeconds: 30 });
    const bash = revTools.find(tool => tool.name === "bash");
    assert.ok(bash);

    // 1. Read-only enforcement on submission/, evaluation/, and /workspace; writable scratch/; offline network.
    const checkCmd = [
      "set -e",
      "if echo x >> submission/ledger.csv 2>/dev/null; then echo SUB_WRITABLE; exit 1; fi",
      "if echo x >> evaluation/conversation.jsonl 2>/dev/null; then echo EVAL_WRITABLE; exit 1; fi",
      "if echo x > /workspace/root_file.txt 2>/dev/null; then echo ROOT_WRITABLE; exit 1; fi",
      "cat <<'PY' > scratch/recalc.py",
      "import csv, socket",
      "with open('submission/ledger.csv') as f:",
      "    total = sum(int(r['amount']) for r in csv.DictReader(f))",
      "net_blocked = False",
      "try:",
      "    s = socket.create_connection(('8.8.8.8', 53), timeout=1)",
      "    s.close()",
      "except OSError:",
      "    net_blocked = True",
      "print(f'TOTAL={total} NET_BLOCKED={net_blocked}')",
      "PY",
      "python3 scratch/recalc.py",
    ].join("\n");
    const res = await bash.execute("b1", { command: checkCmd });
    assert.match(res.content[0].text, /TOTAL=200 NET_BLOCKED=True/);
    assert.equal(await readFile(path.join(root, "submission/ledger.csv"), "utf8"), "item,amount\nA,120\nB,80\n");
    assert.ok((await readFile(path.join(root, "scratch/recalc.py"), "utf8")).includes("csv.DictReader"));

    // 2. Truncated >50KB output is relocated into scratch/ so read can inspect it.
    const bigRes = await bash.execute("b2", {
      command: "python3 -c \"for i in range(4000): print(f'LINE_{i:04d}_' + 'X'*20)\"",
    });
    assert.match(bigRes.content[0].text, /Full output: scratch\/bash-output-/);
    assert.match(bigRes.details.fullOutputPath, /^scratch\/bash-output-/);
    const readTool = revTools.find(tool => tool.name === "read");
    const headPage = await readTool.execute("r-big", { path: bigRes.details.fullOutputPath, offset: 1, limit: 3 });
    assert.match(headPage.content[0].text, /LINE_0000_/);

    // 3. Escaping symlinks inside review workspace are blocked before container mount.
    await writeFile(path.join(outer, "host-secret.txt"), "TOP_SECRET");
    await symlink(path.join(outer, "host-secret.txt"), path.join(root, "submission/escape.txt"));
    await assert.rejects(bash.execute("b3", { command: "ls" }), /Symlink escapes review workspace boundary/);
    await rm(path.join(root, "submission/escape.txt"));

    // 4. Per-command timeout kills container cleanly without leaving orphans.
    await assert.rejects(bash.execute("b4", { command: "sleep 30", timeout: 1 }), /Command timed out after 1 seconds/);
  } finally {
    await rm(outer, { recursive: true, force: true });
  }
});
