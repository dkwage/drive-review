from io import BytesIO
import unittest
from zipfile import ZipFile

from openpyxl import Workbook

from drive_review.preview import PreviewKind, preview_kind
from drive_review.preview_text import TEXT_LIMIT, office_text, plain_text


class PreviewTests(unittest.TestCase):
    def test_type_dispatch(self):
        cases = {
            PreviewKind.IMAGE: ("photo.JPG", "raw.CR3", "modern.heic", "animation.webp", "scan.tiff"),
            PreviewKind.VIDEO: ("video.MP4", "movie.mkv"),
            PreviewKind.AUDIO: ("sound.m4a", "recording.WAV"),
            PreviewKind.PDF: ("report.PDF",),
            PreviewKind.TEXT: ("README", ".gitignore", "data.csv", "data.json", "source.py"),
            PreviewKind.OFFICE: ("letter.docx", "old.doc", "slides.pptx", "sheet.xlsx", "note.rtf"),
            PreviewKind.OTHER: ("archive.zip", "unknown.dat"),
        }
        for kind, names in cases.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(preview_kind(name), kind)

    def test_plain_text_encoding_and_limit(self):
        self.assertEqual(plain_text(BytesIO("Sample text".encode("utf-16"))), "Sample text")
        result = plain_text(BytesIO(b"a" * (TEXT_LIMIT - 1) + "é".encode()))
        self.assertEqual(result, "a" * (TEXT_LIMIT - 1) + "\n\n[Preview truncated]")

    def test_docx_text_extracts_paragraphs_tabs_and_breaks(self):
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr("word/document.xml", """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>First</w:t><w:tab/><w:t>paragraph</w:t></w:r></w:p>
                    <w:p><w:r><w:t>Next</w:t><w:br/><w:t>line</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """)
        self.assertEqual(office_text(stream, "document.docx"), "First\tparagraph\nNext\nline")

    def test_pptx_extracts_slide_text_in_numeric_order(self):
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            for number, text in ((10, "Later"), (2, "Earlier")):
                archive.writestr(f"ppt/slides/slide{number}.xml", f"""
                    <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                           xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                      <a:p><a:r><a:t>{text}</a:t></a:r><a:r><a:t> slide</a:t></a:r></a:p>
                    </p:sld>
                """)
            archive.writestr("ppt/notesSlides/notesSlide2.xml", "<notes>Skip these notes</notes>")
        self.assertEqual(office_text(stream, "slides.pptx"), "Slide 2\nEarlier slide\n\nSlide 10\nLater slide")
        with ZipFile(stream, "a") as archive:
            archive.writestr("ppt/presentation.xml", """
                <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                                xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <p:sldIdLst><p:sldId r:id="rId10"/><p:sldId r:id="rId2"/></p:sldIdLst>
                </p:presentation>
            """)
            archive.writestr("ppt/_rels/presentation.xml.rels", """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId10" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
                                Target="slides/slide10.xml"/>
                  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
                                Target="slides/slide2.xml"/>
                </Relationships>
            """)
        self.assertEqual(office_text(stream, "slides.pptx"), "Slide 1\nLater slide\n\nSlide 2\nEarlier slide")

    def test_xlsx_extracts_worksheet_names_inline_strings_and_cell_gaps(self):
        stream = BytesIO()
        book = Workbook()
        book.active.title = "First sheet"
        book.active["A1"] = "Item"
        book.active["C1"] = 42
        book.create_sheet("Second sheet").append([True, "Value"])
        book.save(stream)
        book.close()
        self.assertEqual(
            office_text(stream, "table.xlsx"),
            "Sheet: First sheet\nItem\t\t42\n\nSheet: Second sheet\nTRUE\tValue",
        )
