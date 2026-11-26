"""Create a local upload set or write a drive inventory workbook."""

from pathlib import Path, PurePosixPath
import os
import re
import tempfile

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE, WriteOnlyCell
from openpyxl.styles import Font
from openpyxl.utils.cell import get_column_letter

from .dupes import check_cancel, hash_stream
from .flags import atomic_csv
from .items import CATEGORY_LABELS, iso_mtime
from .paths import move_no_replace, outside_source, safe_path
from .zipsource import ItemReader

MANIFEST = ("dest_path", "source_rel_path", "zip_entry", "hash")
HEADERS = ("분류", "파일명", "확장자", "상대 경로", "크기(바이트)", "수정 시간", "Flag", "Hash")
EXCEL_ROWS = 1_048_575


def _upload_paths(selected):
    """Keep loose paths at the export root and give archive members a separate directory."""
    reserved = {item.rel_path.split("/", 1)[0].casefold() for item, _flag in selected if not item.zip_entry}

    def namespace(base):
        name = base
        number = 2
        while name.casefold() in reserved:
            name = f"{base}_{number}"
            number += 1
        return name

    archives = namespace("zip_entries")
    sources = namespace("_drive_review_sources")
    for item, flag in selected:
        if item.zip_entry:
            relative = f"{archives}/{item.rel_path}/{item.zip_entry}"
        elif item.rel_path.split("/", 1)[0].casefold() == "manifest.csv":
            relative = f"{sources}/{item.rel_path}"
        else:
            relative = item.rel_path
        yield item, flag, relative


def prepare_upload(root, destination, items, flags, stop=None, progress=lambda count, total: None):
    destination = outside_source(root, destination)
    rows = []
    selected = [(item, flag) for item in items if (flag := flags.get(item.key))
                and flag.flag == "green" and flag.matches(item)]
    if not selected:
        raise ValueError("업로드 플래그가 지정된 파일이 없습니다.")
    paths = list(_upload_paths(selected))
    with tempfile.TemporaryDirectory(prefix=".drive-review-", dir=destination) as directory, ItemReader() as reader:
        staging = Path(directory)
        top_level = {relative.split("/", 1)[0] for _item, _flag, relative in paths} | {"manifest.csv"}
        for name in top_level:
            if os.path.lexists(destination / name):
                raise FileExistsError(f"이미 존재하는 업로드 세트 경로: {destination / name}")
        for count, (item, flag, relative) in enumerate(paths, 1):
            check_cancel(stop)
            output = safe_path(staging, relative, create_parents=True)
            with reader.open(root, item) as source, output.open("xb") as target:
                digest, _ = hash_stream(source, stop, target)
            if flag.hash and digest != flag.hash:
                raise OSError(f"검토한 내용과 다른 파일입니다: {item.identity}")
            rows.append(dict(zip(MANIFEST, (relative, item.rel_path, item.zip_entry, digest))))
            if count % 16 == 0 or count == len(selected):
                progress(count, len(selected))
        atomic_csv(staging / "manifest.csv", MANIFEST, rows)
        check_cancel(stop)
        published = []
        names = sorted(child.name for child in staging.iterdir() if child.name != "manifest.csv")
        names.append("manifest.csv")
        try:
            for name in names:
                move_no_replace(staging / name, destination / name)
                published.append(name)
        except OSError:
            for name in reversed(published):
                move_no_replace(destination / name, staging / name)
            raise
    return destination / "manifest.csv"


def sheet_title(label, existing):
    base = re.sub(r"[\\/*?:\[\]\x00-\x1f]", "_", label).strip(" '") or "드라이브"
    if base.casefold() == "history":
        base += "_"
    titles = {title.casefold() for title in existing}
    title = base[:31].rstrip(" '")
    number = 2
    while title.casefold() in titles:
        suffix = f" ({number})"
        title = base[:31 - len(suffix)].rstrip(" '") + suffix
        number += 1
    return title


def text_cell(value):
    return ILLEGAL_CHARACTERS_RE.sub("", value)[:32767]


def inventory_excel(root, destination, label, items, flags, hashes, stop=None,
                    progress=lambda count, total: None):
    destination = outside_source(root, destination)
    if destination.suffix.lower() != ".xlsx":
        raise ValueError(".xlsx 파일을 선택하세요.")
    streaming = not destination.exists()
    book = Workbook(write_only=True) if streaming else load_workbook(destination)
    temp = None
    sheet = None
    try:
        for offset in range(0, max(1, len(items)), EXCEL_ROWS):
            sheet = book.create_sheet(sheet_title(label, book.sheetnames))
            sheet.freeze_panes = "A2"
            for column, width in enumerate((15, 35, 12, 65, 18, 31, 12, 67), 1):
                sheet.column_dimensions[get_column_letter(column)].width = width

            if streaming:
                headers = [WriteOnlyCell(sheet, value=value) for value in HEADERS]
                for cell in headers:
                    cell.font = Font(bold=True)
                sheet.append(headers)
            else:
                sheet.append(HEADERS)
                for cell in sheet[1]:
                    cell.font = Font(bold=True)

            chunk = items[offset:offset + EXCEL_ROWS]
            # Once a write-only sheet starts, finish its XML even if the operation is cancelled.
            try:
                for count, item in enumerate(chunk, 1):
                    check_cancel(stop)
                    flag = flags.get(item.key)
                    known_hash = hashes.get(item.key, "")
                    status = flag.flag if flag and flag.matches(item) and (
                        not known_hash or flag.hash in ("", known_hash)) else ""
                    values = (CATEGORY_LABELS[item.category], item.name, PurePosixPath(item.name).suffix.lower(),
                              item.identity, item.size, iso_mtime(item.mtime_ns), status,
                              known_hash or (flag.hash if status else ""))
                    values = [text_cell(value) if isinstance(value, str) else value for value in values]
                    if streaming:
                        cells = [WriteOnlyCell(sheet, value=value) for value in values]
                        for cell, value in zip(cells, values):
                            if isinstance(value, str):
                                cell.data_type = "s"
                        sheet.append(cells)
                    else:
                        sheet.append(values)
                        for cell in sheet[count + 1]:
                            if cell.data_type == "f":
                                cell.data_type = "s"
                    if count % 256 == 0 or offset + count == len(items):
                        progress(offset + count, len(items))
                sheet.auto_filter.ref = f"A1:H{len(chunk) + 1}"
            finally:
                if streaming:
                    sheet.close()

        check_cancel(stop)
        with tempfile.NamedTemporaryFile(suffix=".xlsx", dir=destination.parent, delete=False) as output:
            temp = Path(output.name)
        book.save(temp)
        check_cancel(stop)
        os.replace(temp, destination)
    finally:
        if streaming:
            for tab in book.worksheets:
                # openpyxl leaves the temporary sheet XML behind if save() did not consume it.
                if tab._writer and os.path.exists(tab._writer.out):
                    tab._writer.cleanup()
        book.close()
        if temp:
            temp.unlink(missing_ok=True)
    return destination
