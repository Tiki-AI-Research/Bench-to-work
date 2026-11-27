"""Container image build preflight: verify Office/PDF libraries and public preview hooks."""
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pypdf import PdfReader


def minimal_pdf(text: str) -> bytes:
    content = b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        book = Workbook()
        book.active["A1"] = 3
        book.active["A2"] = "=A1+1"
        book.save(root / "check.xlsx")
        assert load_workbook(root / "check.xlsx").active["A2"].value == "=A1+1"
        document = Document()
        document.add_paragraph("Office preflight")
        document.save(root / "check.docx")
        assert Document(root / "check.docx").paragraphs[0].text == "Office preflight"
        slides = Presentation()
        slides.slides.add_slide(slides.slide_layouts[0])
        slides.save(root / "check.pptx")
        assert len(Presentation(root / "check.pptx").slides) == 1
        (root / "check.pdf").write_bytes(minimal_pdf("PDF preflight"))
        assert "PDF preflight" in PdfReader(root / "check.pdf").pages[0].extract_text()
        hooks = Path("/opt/task-public/hooks.py")
        if hooks.is_file():
            (root / "ws" / "output").mkdir(parents=True)
            subprocess.run(
                [sys.executable, str(hooks), "preview", "output"],
                input='{"workspace": "%s"}' % (root / "ws").as_posix(),
                text=True,
                check=True,
            )
            assert (root / "ws" / ".review" / "preview.md").is_file()
    print("worker environment ok")


if __name__ == "__main__":
    main()
