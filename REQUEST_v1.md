# REQUEST v1: build Drive Review (Windows desktop app)

You are building a new app from scratch in this repo. Work autonomously until it is done, tests pass,
and the checklist at the bottom is satisfied. Do not ask questions; pick the simplest reasonable option
and note it in README "Decisions".

## Context
GGGI has ~10 old external drives (photos, videos, RAW, docs, big zips; ~4.5 TB, ~70k files). A reviewer
on **Windows** connects a drive, looks at thumbnails, and flags each file like Lightroom:
**green = upload** to ResourceSpace, **red = delete**. The drive is then physically handed to the owner,
who loads the green list into a separate app (rs-uploader, NOT part of this task) and later executes reds.
From an earlier inventory (by name+size only): ~2,969 duplicate groups, 1,970 span different drives,
~307 GB redundant. So duplicate detection, including across drives, matters.

`reference_usb_file_inventory.py` is the previous single-purpose tool (Tkinter, scan USB drive ->
Excel sheet). Reuse its ideas (USB drive detection on Windows, category by extension incl. RAW and
Adobe formats, Excel sheet naming/limits) as the **secondary** "Inventory export" feature. Delete that
reference file once its logic is ported.

## Stack
- Python 3.12, **PySide6** (QListView icon mode with a model; must stay smooth with 30k+ items:
  lazy thumbnails in a thread pool, cache), openpyxl for Excel. Standard library otherwise where possible.
- Target: Windows 10/11, packaged with PyInstaller (one-folder is fine). Dev happens on macOS, so every
  Windows-only piece sits behind a small interface with a fallback, and the app + tests run on macOS too.
- Thumbnails on Windows: **Windows Shell thumbnail API** (`IShellItemImageFactory::GetImage` via
  `comtypes` or raw `ctypes` COM) so photos, RAW (if codec installed), videos, PDF and Office files look
  like in Explorer. Fallback chain: Pillow for common images -> generic icon by category. On macOS dev,
  `qlmanage -t` may be used as the fallback provider. Thumbnail cache on local disk
  (`%LOCALAPPDATA%\DriveReview\thumbs`, keyed by drive id + rel path + size + mtime), never on the drive.

## Features
1. **Open a drive/folder**: list connected removable/external drives (port USB detection) or pick any
   folder. Scan recursively in a background thread with progress + cancel. Hide system junk by default
   (`$RECYCLE.BIN`, `System Volume Information`, `.Trashes`, `.Spotlight-V100`, `.fseventsd`, `._*`,
   `.DS_Store`, `desktop.ini`, `Thumbs.db`, autorun/Seagate installer files) with a toggle to show.
2. **Review grid** (main screen): thumbnails with name, size, flag badge. Left panel: folder tree (zip
   files appear as expandable folders). Filters: category (Video/Image/Docs/Music/Compressed/Design/Etc),
   flag (none/green/red/duplicate), text search. Keys: `P` green, `X` red, `U` clear, arrows move,
   `Space` large preview (image/thumbnail at high res), `Enter`/double-click open with default app
   (for zip entries: extract that entry to a temp dir first). Multi-select + flag applies to all.
   Status bar: counts and total bytes per flag. Sort by name/size/date/folder.
3. **Zip browsing without extraction**: read the central directory (`zipfile`, Zip64; 44 GB zips exist)
   so entries are listed instantly. Thumbnail of an entry: images/PDF read that single entry into memory or
   temp; videos only on demand (explicit action), since extraction can be GBs. Entries can be flagged; their
   identity is `zip_rel_path::entry_path`. Corrupt/encrypted zips: show as plain file with a warning.
4. **Duplicates (content, not name)**: size groups -> partial hash (first+last 64 KiB) -> full hash
   (BLAKE2b, streamed, chunked; show progress, cancellable, resumable via cache) -> optional
   byte-by-byte compare toggle. Zip entries participate using their CRC32+size as a prefilter and a full
   hash when needed. Persist hashes in a local SQLite **cross-drive index**
   (`%LOCALAPPDATA%\DriveReview\index.sqlite`: drive label/serial, rel path, size, mtime, hash) so files
   already seen on a previous drive are marked "seen on <drive>". Duplicate view lists groups; one file
   per group is auto-suggested as keeper (rule order: not in trash/system folders > not inside a zip >
   shallower path > older mtime > shorter name); the others get a **red flag with reason "duplicate"**
   which the reviewer can override. Never auto-flag something whose only copy is on another drive that
   might be red too: cross-drive duplicates are flagged red only when the keeper is on a drive whose
   flags are recorded as kept (non-red) in the index; otherwise just badge them.
5. **Flags persistence** (most important contract): autosave (debounced, atomic write via temp+rename) to
   `<drive root>\_drive_review\flags.csv`, UTF-8 with BOM, columns:
   `rel_path,zip_entry,flag,reason,size,mtime_iso,hash,category,reviewer,flagged_at_iso`
   - `rel_path` uses `/` separators, relative to drive root; `zip_entry` empty unless inside a zip.
   - `flag` in {green, red}; unflagged files are not written. `reason` e.g. `duplicate`, `manual`.
   Also keep the same flags in the local index. On open, load existing flags.csv so review can resume.
   If the drive is read-only, save next to the local index and tell the user.
6. **Execute reds** (owner side, behind a confirm dialog with count + bytes): move red files (not zip
   entries) into `<drive root>\_drive_review\quarantine\<original rel path>` and log to
   `quarantine_log.csv`; an "Undo quarantine" action moves them back. Never hard-delete.
7. **Export green list** for uploading: button "Prepare upload set" copies/extracts green items
   (including zip entries) into a chosen local folder preserving rel paths and writes `manifest.csv`
   (dest path, source rel_path, zip_entry, hash). Also available: **Inventory export** to Excel
   (ported from the reference tool) with an extra Flag + Hash column.
8. Settings: reviewer name (stored, written in CSV), show hidden toggle, thumbnail size slider.

## Quality
- Package layout: `drive_review/` (scan.py, zipsource.py, thumbs.py + thumbs_windows.py, dupes.py,
  index.py, flags.py, quarantine.py, export.py, ui/ ...), `main.py` entry.
- File access errors (permissions, drive unplugged mid-scan) are reported, never crash. Long paths on
  Windows (`\\?\` prefix) handled.
- Unit tests (`python -m unittest discover -s tests`) for: scan filters, zip listing incl. Zip64 test zip
  generated in a tmp dir, duplicate pipeline (incl. same content different names, same size different
  content, zip entry vs loose file), keeper rule, flags.csv round-trip + atomic write, quarantine +
  undo, cross-drive index. UI smoke test with `QT_QPA_PLATFORM=offscreen` that builds the main window on a
  generated folder of ~2,000 files and checks it does not block.
- `.github/workflows/build.yml`: windows-latest, Python 3.12, run tests, PyInstaller build, upload
  artifact `DriveReview-windows`. `build.bat` for local builds. `requirements.txt`, `.gitignore`.
- README in Korean (users are Korean staff): what it does, reviewer workflow (P/X/U keys), owner workflow
  (execute reds, prepare upload set), flags.csv format, "Decisions" section.

## Constraints
- Never modify or delete files on the source drive except: writing `_drive_review/` and the quarantine
  move/undo. Opening/previewing must be read-only.
- Do not push to any remote. Make small local git commits as you go (conventional commit messages).

## Done checklist (write results to `CODEX_REPORT.md`)
- [ ] all tests pass on macOS (paste the output summary)
- [ ] app launches offscreen without errors
- [ ] list what is untested on real Windows (Shell thumbnail COM, USB detection) and why
- [ ] known limitations / follow-ups
