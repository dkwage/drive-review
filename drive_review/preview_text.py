"""Bounded text previews for plain text and Office Open XML documents."""

from codecs import BOM_UTF16_BE, BOM_UTF16_LE, BOM_UTF32_BE, BOM_UTF32_LE
from pathlib import PurePosixPath
import posixpath
import re
from xml.etree import ElementTree
from zipfile import ZipFile

from .dupes import check_cancel

TEXT_LIMIT = 2 * 1024**2
XML_LIMIT = 16 * 1024**2
TRUNCATED = "\n\n[Preview truncated]"
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
PRESENTATION = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
STRICT_NAMESPACES = {
    "{http://purl.oclc.org/ooxml/wordprocessingml/main}": WORD,
    "{http://purl.oclc.org/ooxml/drawingml/main}": DRAWING,
    "{http://purl.oclc.org/ooxml/presentationml/main}": PRESENTATION,
    "{http://purl.oclc.org/ooxml/spreadsheetml/main}": SHEET,
}
STRICT_REL_ID = "{http://purl.oclc.org/ooxml/officeDocument/relationships}id"
WORD_BREAKS = {WORD + "tab": "\t", WORD + "br": "\n", WORD + "cr": "\n"}


def plain_text(stream):
    data = stream.read(TEXT_LIMIT + 1)
    truncated = len(data) > TEXT_LIMIT
    data = data[:TEXT_LIMIT]
    if data.startswith((BOM_UTF32_LE, BOM_UTF32_BE)):
        text = data.decode("utf-32", errors="replace")
    elif data.startswith((BOM_UTF16_LE, BOM_UTF16_BE)):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            if truncated and error.end == len(data) and error.reason == "unexpected end of data":
                text = data[:error.start].decode("utf-8-sig")
            else:
                text = data.decode("cp1252", errors="replace")
    return text + (TRUNCATED if truncated else "")


def xml_elements(archive, part, tag, stop):
    if archive.getinfo(part).file_size > XML_LIMIT:
        raise ValueError(f"The Office document part is too large for text preview: {part}")
    with archive.open(part) as source:
        for _event, element in ElementTree.iterparse(source, events=("end",)):
            check_cancel(stop)
            namespace, separator, local = element.tag.partition("}")
            if separator and (normal := STRICT_NAMESPACES.get(namespace + separator)):
                element.tag = normal + local
            if element.tag == tag:
                yield element
                element.clear()


def related_parts(archive, base, rels_file, kind, stop):
    targets = {}
    for link in xml_elements(archive, rels_file, PACKAGE_REL + "Relationship", stop):
        if link.get("Type", "").endswith(f"/{kind}") and link.get("TargetMode") != "External":
            target = link.get("Target", "")
            name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else base + target)
            if name.startswith(base):
                targets[link.get("Id")] = name
    return targets


def document_lines(archive, stop):
    for paragraph in xml_elements(archive, "word/document.xml", WORD + "p", stop):
        parts = [child.text or "" if child.tag == WORD + "t" else WORD_BREAKS.get(child.tag, "")
                 for child in paragraph.iter()]
        yield "".join(parts)


def slide_lines(archive, stop):
    names = set(archive.namelist())
    if {"ppt/presentation.xml", "ppt/_rels/presentation.xml.rels"} <= names:
        targets = related_parts(archive, "ppt/", "ppt/_rels/presentation.xml.rels", "slide", stop)
        slide_ids = xml_elements(archive, "ppt/presentation.xml", PRESENTATION + "sldId", stop)
        slides = [(number, targets.get(slide.get(OFFICE_REL + "id") or slide.get(STRICT_REL_ID)))
                  for number, slide in enumerate(slide_ids, 1)]
    else:
        slides = []
        for name in names:
            check_cancel(stop)
            match = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
            if match:
                slides.append((int(match[1]), name))
        slides.sort()
    for number, name in slides:
        if name not in names:
            continue
        yield f"Slide {number}"
        for paragraph in xml_elements(archive, name, DRAWING + "p", stop):
            yield "".join(node.text or "" for node in paragraph.iter(DRAWING + "t"))
        yield ""


def sheet_lines(archive, stop):
    shared = []
    if "xl/sharedStrings.xml" in archive.namelist():
        for item in xml_elements(archive, "xl/sharedStrings.xml", SHEET + "si", stop):
            shared.append("".join(node.text or "" for node in item.iter(SHEET + "t")))
    targets = related_parts(archive, "xl/", "xl/_rels/workbook.xml.rels", "worksheet", stop)
    sheet_number = 0
    for sheet in xml_elements(archive, "xl/workbook.xml", SHEET + "sheet", stop):
        part = targets.get(sheet.get(OFFICE_REL + "id") or sheet.get(STRICT_REL_ID))
        if not part:
            continue
        sheet_number += 1
        if sheet_number > 3:
            yield "[Additional sheets omitted]"
            break
        yield f"Sheet: {sheet.get('name', str(sheet_number))}"
        for number, row in enumerate(xml_elements(archive, part, SHEET + "row", stop), 1):
            if number > 200:
                yield "[Additional rows omitted]"
                break
            cells = []
            for cell in row.iterfind(SHEET + "c"):
                value = cell.findtext(SHEET + "v", default="")
                kind = cell.get("t")
                if kind == "s" and value.isdecimal() and int(value) < len(shared):
                    value = shared[int(value)]
                elif kind == "inlineStr":
                    value = "".join(node.text or "" for node in cell.iter(SHEET + "t"))
                elif kind == "b":
                    value = "TRUE" if value == "1" else "FALSE"
                column = 0
                for letter in cell.get("r", ""):
                    if not "A" <= letter <= "Z":
                        break
                    column = column * 26 + ord(letter) - ord("A") + 1
                column = column or len(cells) + 1
                if column > 80:
                    cells.append("[Additional columns omitted]")
                    break
                cells.extend([""] * max(0, column - len(cells) - 1))
                cells.append(value.replace("\t", " ").replace("\r", " ").replace("\n", " "))
            yield "\t".join(cells)
        yield ""


OFFICE_EXTRACTORS = {
    ".docx": document_lines, ".docm": document_lines, ".dotx": document_lines, ".dotm": document_lines,
    ".pptx": slide_lines, ".pptm": slide_lines, ".ppsx": slide_lines, ".ppsm": slide_lines,
    ".potx": slide_lines, ".potm": slide_lines, ".xlsx": sheet_lines, ".xlsm": sheet_lines,
    ".xltx": sheet_lines, ".xltm": sheet_lines,
}


def office_text(stream, name, stop=None):
    extract = OFFICE_EXTRACTORS[PurePosixPath(name).suffix.lower()]
    lines = []
    remaining = TEXT_LIMIT
    with ZipFile(stream) as archive:
        for line in extract(archive, stop):
            if len(line) + 1 > remaining:
                lines.extend((line[:remaining], TRUNCATED))
                break
            lines.append(line)
            remaining -= len(line) + 1
    return "\n".join(lines).strip()
