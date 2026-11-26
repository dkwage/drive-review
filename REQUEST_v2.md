# REQUEST v2: English UI, clearer owner actions, Quick Look-style preview

Read REQUEST_v1.md and CODEX_REPORT.md for context; the v1 app in this repo works and all tests pass.
Work autonomously until done. Do NOT run git commands that write (.git is read-only in your sandbox);
the maintainer commits. Keep the existing safety guarantees (read-only source except `_drive_review/`,
no overwrite, write-ahead quarantine log, path validation).

## 1. English everywhere
- All UI strings, dialogs, notices, error messages raised to the UI, README, and code comments in English.
  README.md fully rewritten in English (keep the download badge pointing to
  `https://github.com/dkwage/drive-review/releases/latest/download/DriveReview-Windows.zip`).
- Flag names in the UI: green = "Upload", red = "Delete". The flags.csv format is unchanged.

## 2. Clear owner actions (put them in an "Owner" menu/toolbar group, each with a tooltip that explains it)
- "Copy green files for upload…" (was 업로드 세트 준비): copies green files and extracts green ZIP entries
  into a chosen local folder + manifest.csv, for dragging into RS Uploader. Tooltip says exactly that.
- "Move red files to quarantine…" (was 빨간 파일 격리): moves red files into
  `_drive_review/quarantine/` on the same drive. Confirm dialog explains it is reversible.
- "Restore quarantined files…" (was 격리 실행 취소).
- NEW "Delete quarantine permanently…": permanently deletes files currently in the quarantine folder that
  the log records as `quarantined` (re-check size+mtime before each delete; skip mismatches), marks rows
  `deleted` in quarantine_log.csv, removes now-empty quarantine dirs. Confirm dialog shows count and bytes
  and requires typing the number of files. Tests for it.
- Main-window status line shows: Upload N (size) · Delete N (size) · In quarantine N (size).

## 3–5. Quick Look-style preview (like macOS Finder Space bar)
Replace the current static preview with a Quick Look panel:
- Space toggles it for the current item; Esc also closes. While open, arrow keys move the grid selection
  and the panel follows; P / X / U still flag the current item and the panel shows the flag. Panel shows
  file name, size, date, path, index "12 / 340". Large, resizable, remembers size; fit-to-window.
- Content by type (loaded asynchronously, never blocking the UI; cancel when the user moves on):
  - Images: full resolution, zoom (wheel / +/-), fit. JPEG/PNG/TIFF/GIF/WebP via Qt/Pillow; HEIC via
    pillow-heif if available; RAW and other formats via the Windows Shell thumbnail API at a large size
    (e.g. 2048px) as fallback. Respect EXIF orientation.
  - Video and audio: play inline with QtMultimedia (QMediaPlayer + QVideoWidget): play/pause (K or the
    button; Space must still close the panel), seek bar, time, volume/mute. Autoplay muted is fine. ZIP
    entries: extract to a temp file with a progress bar first, then play; clean temp files on close.
  - PDF: QtPdf viewer (QPdfDocument + QPdfView) with scrolling pages and page count.
  - Office and others (docx, doc, xlsx, pptx, rtf, etc.): host the **Windows Preview Handler** for the
    extension (the same one Explorer's preview pane uses: look up the IPreviewHandler CLSID via the
    shellex {8895b1c6-b41f-4c1c-a562-0d564250836f} registry key / AssocQueryString, create it out of process,
    initialize with IInitializeWithFile (or IInitializeWithStream), SetWindow on a native child QWidget,
    SetRect on resize, Unload on change). Isolate this in `drive_review/preview_windows.py` behind an
    interface; if it fails or no handler is registered, fall back to extracted text: docx/pptx/xlsx by
    reading their XML parts with zipfile (paragraphs / slide text / first sheets as a simple table), and
    plain text/CSV/JSON/MD/code shown in a read-only monospace view (first ~2 MB).
  - Anything else: large icon/thumbnail + metadata + "Open with default app" button.
- Files inside ZIPs use the same viewers (extract the single entry to temp when a file path is needed).
- On macOS dev, the preview handler path is simply unavailable (text fallback is used).

## Dependencies / build
- Switch to full `PySide6` (QtMultimedia, QtPdf live in the Addons package) in requirements.txt;
  add `pillow-heif` only if it installs cleanly on Windows/py3.12 wheels. Make sure PyInstaller bundles
  the Qt multimedia FFmpeg backend and QtPdf plugins on Windows (add hidden imports / collect args in
  build.bat and in `.github/workflows/build.yml`; keep the release job as is).
- Tests: keep all existing tests passing (update string assertions to English). Add tests for permanent
  delete, docx/pptx/xlsx text extraction, preview type dispatch, and an offscreen smoke test that opens the
  Quick Look panel on an image, a PDF (generate a tiny one), a text file and moves with arrow keys.

## Done
Update CODEX_REPORT.md (replace the v1 content with v2 results: test output summary, what still needs
real-Windows verification, limitations). Final message: a short list of changed files.
