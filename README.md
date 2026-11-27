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
| R | Cycle the upload choice for selected RAW+JPG pairs |
| M | Move the selected tiles to a folder in the open source |
| Delete (or Backspace on macOS) | Move selected loose files to quarantine immediately |
| Arrow keys | Move the grid selection |
| Space | Open or close Quick Look |
| Esc | Close Quick Look |
| Enter or double-click | Open a local temporary copy in the default app |

Search by name or path, filter by category or flag, and sort by name, size, date, or folder. **By taken** lists the newest capture dates first and puts files with an unknown capture date at the end. **Show system items** includes files normally hidden from the review. The status bar shows the file count and total size for **Upload**, **Delete**, and **In quarantine** across the open source.

### RAW+JPG pairs

**View → Group RAW+JPG pairs** is on by default and remembers your setting. Once the background capture-date pass reads both files, one RAW and one JPG, JPEG, HEIC, or HEIF can share a **RAW+JPG** tile. They must have the same base name, ignoring case, and the same capture time to the second. Subsecond time, camera make, and camera model must also agree wherever both files record the same field. Both files must be in the same loose folder or the same folder inside the same ZIP. Missing capture times or multiple candidates of either kind leave the files as separate tiles.

The tile and Quick Look show the JPG or HEIF image. Search matches either file name, and the **Photos** filter includes pairs. Each pair counts as one tile, and the status bar counts the individual flagged files and their sizes.

**Z** flags the current upload choice, which defaults to **JPG**. **R** cycles **JPG → RAW → RAW+JPG → JPG** and flags the chosen files for upload, including on an unflagged pair. It works on all selected pairs in the grid and Quick Look. The upload tile shows **↑ JPG**, **↑ RAW**, or **↑ RAW+JPG**. The other file stays unflagged when you choose just one. **X** flags both files for deletion. **C** clears both flags and keeps the upload choice.

### Organize files and folders

- Drag a folder or ZIP archive from the folder tree onto another real folder. You can drop onto **All files** to move to the source root. Right-click a folder for **New folder…**, **Rename…**, or **Move to…**. A ZIP can be renamed or moved as a whole while its entries stay inside.
- Drag selected grid tiles onto a tree folder, or choose **Organize → Move selected to folder…** with **M** or the grid's right-click menu. Both files in a RAW+JPG tile move together.
- **Organize → Move Upload files to folder…** and **Move Delete files to folder…** use the loose files flagged with that color across the source. The dialog shows the file count and size before you confirm. Files go directly into the chosen folder. Individual ZIP entries are reported as skipped.
- The folder picker stays within the selected source and includes a **New folder** button. **Organize → New folder…** also creates a folder in the current location. Name collisions add a numeric suffix such as `photo (2).jpg`. Flags follow renamed or moved files and folders, including flags on entries inside a moved ZIP.

Press **Delete** in the grid or Quick Look for immediate, reversible quarantine. **Backspace** does the same on macOS. Both files in a RAW+JPG tile are included and individual ZIP entries are skipped. The grid advances to the next tile when the current file moves. Use **Owner → Restore quarantined files…** to bring back the files.

### Quick Look

Press Space on a selected tile. The panel shows the name, size, **Taken** date when available, file creation and modification dates, path, flag, and position in the current grid. It remembers its size. The arrow keys move the grid selection while the panel is open. Z, X, and C apply to the current tile and have matching buttons. On a pair, the **Upload** control selects **JPG**, **RAW**, or **RAW+JPG** directly and flags that choice for upload.

**Taken** comes from embedded EXIF in photos and RAW files, or the QuickTime creation date or movie header in MP4, MOV, M4V, and 3GP videos. Drive Review displays the recorded time with its UTC offset when available and displays offset-free times as recorded. The grid tooltip and Excel inventory show the same value. Capture dates are read in the background after a scan and cached locally. For ZIP entries, metadata must be reachable within the first 4 MiB of the entry.

- Images open at full resolution where a decoder is available and fit the window by default. Use the mouse wheel or **+** and **−** to zoom. Click **Fit** to reset the zoom. The Windows Shell can supply a large thumbnail for RAW and other formats with an installed codec.
- Play video and audio inline. Press **K** or click **Play** or **Pause**. The controls include seeking, elapsed time, volume, and mute.
- Scroll through PDFs and see the current page and page count.
- Design files such as Photoshop, Illustrator, InDesign, and EPS can show embedded thumbnails in the grid and Quick Look. Drive Review reads embedded XMP JPEGs, renders the first page of PDF-compatible Illustrator files, and can use a TIFF preview from binary EPS files. Design files inside ZIPs use the same preview methods. Automatic grid thumbnails use the 64 MiB ZIP entry limit.
- Office and other supported formats use the preview handler registered with Windows Explorer. When a handler is unavailable, Quick Look can show text from Word, PowerPoint, and Excel Open XML files. Plain text, CSV, JSON, Markdown, and code open in a read-only monospaced view.
- When an inline viewer is unavailable, use **Open with default app**. ZIP entries use the same viewers as loose files, with a local temporary copy when a viewer needs a file path. Large entries need enough local free space.

### Find duplicates

**Find duplicates** checks file size, partial hashes or ZIP CRC32, and full BLAKE2b hashes. **Byte comparison** also compares matching files directly when they are accessible. The first pass reads files to build a cross-drive baseline. Restarting an interrupted pass reuses saved hashes.

Use **Duplicate groups** to review suggested keepers and matching drives. Automatic Delete flags have the reason `duplicate`. You can change them with Z, X, or C. When grouping is on, a confirmed pair keeps its upload choice and shows duplicate suggestions on the tile for review. When review is complete, close Drive Review to finish saving flags, then safely eject the drive.

## Owner actions

- **Copy green files for upload…** copies green files and extracts green ZIP entries into a local folder for RS Uploader. The export includes `manifest.csv` with `dest_path,source_rel_path,zip_entry,hash`. Loose files normally keep their relative paths. ZIP entries normally go under `zip_entries/<ZIP relative path>/<entry path>`.
- **Move red files to quarantine…** confirms the count and size, then moves flagged loose files into `_drive_review/quarantine/` on the same drive. The move is reversible. Individual ZIP entries stay inside their archive and retain their review flags.
- **Restore quarantined files…** returns files to their original locations. If a destination is occupied, the app reports the collision and keeps the quarantined file.
- **Delete quarantine permanently…** shows the eligible count and exact byte total. Type the displayed count to confirm. The app deletes only confirmed files recorded in the quarantine log whose size and modification time still match. It records successful deletions as `deleted` and removes directories emptied by deletion. Permanent deletion cannot be undone.

**Export Excel inventory…** adds a worksheet to a new or existing workbook with categories, file names, extensions, paths, sizes, timestamps, flags, and hashes. The **Taken** column appears after **Created** and is blank when the capture date is unknown.

## flags.csv

Drive Review autosaves flags to `_drive_review/flags.csv` under the selected root. It uses UTF-8 with a BOM and reloads the flags when you reopen that source.

```csv
rel_path,zip_entry,flag,reason,size,mtime_iso,hash,category,reviewer,flagged_at_iso,pair_choice
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
| `pair_choice` | `jpg`, `raw`, or `both` for a pair, or blank for individual files |

Each row identifies one file. **Copy green files for upload** uses the individual green rows. Drive Review also reads older CSV files and restores a pair's upload choice from its green flags when needed. After you clear a pair's flags, the app remembers its choice in the local index on the same computer.

On a read-only drive, the app saves flags locally and shows the CSV location. That copy is available on the same computer. Check that the drive's CSV is current before passing it to another computer. Review flags again after a file's size or modification time changes.

## Decisions

- The index and thumbnail cache live under `%LOCALAPPDATA%\DriveReview` on Windows and `~/Library/Application Support/DriveReview` on macOS. Drive identity uses the volume serial number or UUID where available, with the source path as a fallback.
- Keeper suggestions favor files outside system folders, loose files, shallower paths, older modification times, and shorter names, in that order. Manual decisions are preserved. An off-drive keeper qualifies for automatic deletion of a local copy only when the index records the keeper as flagged Upload. With byte comparison enabled, the keeper must also be readable.
- ZIP listing reads the central directory. Encrypted, damaged, and unsafe archives appear as files with a warning. Grid thumbnails skip ZIP videos and entries over 64 MiB. Quick Look loads an explicitly selected entry on demand.
- The upload manifest records actual output paths. If the ZIP entry directory name collides with a top-level file, Drive Review adds a numeric suffix. A source with the top-level name `manifest.csv` is staged under `_drive_review_sources/`.
- Quarantine moves and permanent deletions record intent ahead of file operations in `_drive_review/quarantine_log.csv`. Restore refuses to overwrite an existing file. Native preview handlers and default apps receive local copies to protect source files.

## Development and builds

Use Python 3.12. On macOS, run these commands from the repository root.

```sh
/opt/homebrew/bin/uv venv --python 3.12 .venv
/opt/homebrew/bin/uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python main.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
```

On Windows, run `build.bat` to install dependencies, run the tests, and package `dist\DriveReview\DriveReview.exe`. The GitHub Actions Windows job builds the same one-folder package.
