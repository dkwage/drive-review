# Drive Review

Drive Review is a Windows 10/11 desktop app for reviewing photos, videos, documents, and ZIP entries on external drives. Reviewers flag files for upload or deletion. The drive owner stages upload files and manages quarantine from the **Owner** menu.

[![Download for Windows](https://img.shields.io/badge/Download-Windows-0078D6?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/dkwage/drive-review/releases/latest/download/DriveReview-Windows.zip)

Extract `DriveReview-Windows.zip` and run `DriveReview.exe` from the extracted `DriveReview` folder. Keep the rest of the folder beside the executable.

## Review a drive

1. Select an external drive or click **Open folder**. Use **Refresh** to list newly connected drives.
2. Enter your reviewer name. Use the folder tree to browse, including the contents of ZIP archives.
3. Select a file in the grid. Hold Shift or Ctrl to select multiple files on Windows. Use Command for individual selections on macOS.

| Key | Action |
| --- | --- |
| Z | Flag for **Upload** (green) |
| X | Flag for **Delete** (red) |
| C | Clear the flag |
| Arrow keys | Move the grid selection |
| Space | Open or close Quick Look |
| Esc | Close Quick Look |
| Enter or double-click | Open a local temporary copy in the default app |

Search by name or path, filter by category or flag, and sort by name, size, date, or folder. **Show system items** includes files normally hidden from the review. The status bar shows the file count and total size for **Upload**, **Delete**, and **In quarantine** across the open source.

### Quick Look

Press Space on a selected file. The panel shows the name, size, creation and modification dates, path, flag, and position in the current grid. It remembers its size. The arrow keys move the grid selection while the panel is open. Z, X, and C apply to the current file; the same actions are buttons at the bottom of the panel.

- Images open at full resolution where a decoder is available and fit the window by default. Use the mouse wheel or **+** and **−** to zoom. Click **Fit** to reset the zoom. The Windows Shell can supply a large thumbnail for RAW and other formats with an installed codec.
- Play video and audio inline. Press **K** or click **Play** or **Pause**. The controls include seeking, elapsed time, volume, and mute.
- Scroll through PDFs and see the current page and page count.
- Office and other supported formats use the preview handler registered with Windows Explorer. When a handler is unavailable, Quick Look can show text from Word, PowerPoint, and Excel Open XML files. Plain text, CSV, JSON, Markdown, and code open in a read-only monospaced view.
- When an inline viewer is unavailable, use **Open with default app**. ZIP entries use the same viewers as loose files, with a local temporary copy when a viewer needs a file path. Large entries need enough local free space.

### Find duplicates

**Find duplicates** checks file size, partial hashes or ZIP CRC32, and full BLAKE2b hashes. **Byte comparison** also compares matching files directly when they are accessible. The first pass reads files to build a cross-drive baseline. Restarting an interrupted pass reuses saved hashes.

Use **Duplicate groups** to review suggested keepers and matching drives. Automatic Delete flags have the reason `duplicate`. You can change them with Z, X, or C. When review is complete, close Drive Review to finish saving flags, then safely eject the drive.

## Owner actions

- **Copy green files for upload…** copies green files and extracts green ZIP entries into a local folder for RS Uploader. The export includes `manifest.csv` with `dest_path,source_rel_path,zip_entry,hash`. Loose files normally keep their relative paths. ZIP entries normally go under `zip_entries/<ZIP relative path>/<entry path>`.
- **Move red files to quarantine…** confirms the count and size, then moves flagged loose files into `_drive_review/quarantine/` on the same drive. The move is reversible. Individual ZIP entries stay inside their archive and retain their review flags.
- **Restore quarantined files…** returns files to their original locations. If a destination is occupied, the app reports the collision and keeps the quarantined file.
- **Delete quarantine permanently…** shows the eligible count and exact byte total. Type the displayed count to confirm. The app deletes only confirmed files recorded in the quarantine log whose size and modification time still match. It records successful deletions as `deleted` and removes directories emptied by deletion. Permanent deletion cannot be undone.

**Export Excel inventory…** adds a worksheet to a new or existing workbook with categories, file names, extensions, paths, sizes, timestamps, flags, and hashes.

## flags.csv

Drive Review autosaves flags to `_drive_review/flags.csv` under the selected root. It uses UTF-8 with a BOM and reloads the flags when you reopen that source.

```csv
rel_path,zip_entry,flag,reason,size,mtime_iso,hash,category,reviewer,flagged_at_iso
```

| Column | Value |
| --- | --- |
| `rel_path` | Path relative to the selected root, using `/` separators |
| `zip_entry` | Entry path inside the ZIP, or blank for loose files |
| `flag` | `green` or `red`. Unflagged files have no row. |
| `reason` | `manual` for reviewer decisions or `duplicate` for automatic flags |
| `size`, `mtime_iso` | Size in bytes and ISO 8601 modification time in UTC |
| `hash` | Hexadecimal BLAKE2b-256 digest, when calculated |
| `category` | `Video`, `Image`, `Docs`, `Music`, `Compressed`, `Design`, or `Etc` |
| `reviewer`, `flagged_at_iso` | Reviewer name and time the flag was set, in UTC |

On a read-only drive, the app saves flags locally and shows the CSV location. That copy is available on the same computer. Check that the drive's CSV is current before passing it to another computer. Review flags again after a file's size or modification time changes.

## Decisions

- The index and thumbnail cache live under `%LOCALAPPDATA%\DriveReview` on Windows and `~/Library/Application Support/DriveReview` on macOS. Drive identity uses the volume serial number or UUID where available, with the source path as a fallback.
- Keeper suggestions favor files outside system folders, loose files, shallower paths, older modification times, and shorter names, in that order. Manual decisions are preserved. An off-drive keeper qualifies for automatic deletion of a local copy only when the index records the keeper as flagged Upload. With byte comparison enabled, the keeper must also be readable.
- ZIP listing reads the central directory. Encrypted, damaged, and unsafe archives appear as files with a warning. Grid thumbnails skip ZIP videos and entries over 64 MiB. Quick Look loads an explicitly selected entry on demand.
- The upload manifest records actual output paths. If the ZIP entry directory name collides with a top-level file, Drive Review adds a numeric suffix. A source with the top-level name `manifest.csv` is staged under `_drive_review_sources/`.
- Moves and permanent deletions record intent ahead of file operations in `_drive_review/quarantine_log.csv`. Restore refuses to overwrite an existing file. Native preview handlers and default apps receive local copies to protect source files.

## Development and builds

Use Python 3.12. On macOS, run these commands from the repository root.

```sh
/opt/homebrew/bin/uv venv --python 3.12 .venv
/opt/homebrew/bin/uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python main.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

On Windows, run `build.bat` to install dependencies, run the tests, and package `dist\DriveReview\DriveReview.exe`. The GitHub Actions Windows job builds the same one-folder package.
