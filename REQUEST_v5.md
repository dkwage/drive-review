# REQUEST v5: Organize folders, move flagged files, Delete key, Adobe thumbnails

Context: REQUEST_v1..v4, README.md, CODEX_REPORT.md. Keep all tests passing.
Do NOT run git commands that write (.git is read-only in your sandbox). Work autonomously until done.

## Shared core: new module `drive_review/organize.py` (pure logic, no Qt, unit-tested)
- Move loose files and folders **within the selected source root only** (same volume), using
  `paths.move_no_replace` and `paths.safe_path` (no links/reparse points, never touch `_drive_review/`).
- Never overwrite: on a name collision, add a suffix `name (2).ext`, `name (3).ext` … (folders too).
- Reject moving a folder into itself or a descendant, moving to the same folder (no-op), moving ZIP entries
  (individual entries stay in their archive; report them as skipped), or files whose size/mtime changed
  since the scan.
- Return the moved count/size, errors, and an `old rel_path -> new rel_path` mapping (folder moves map a
  prefix). Provide `remap_flags(flags, mapping)` that rewrites `rel_path` of every flag under a moved
  path (incl. flags of ZIP entries inside a moved ZIP and of files inside a moved folder), keeping all
  other fields so `Flag.matches` still works after the rename (mtime is preserved by a rename).
- Create folder: `create_folder(root, parent_rel, name)` with Windows-safe name validation.

## 1. Organize folders (UI)
- Folder tree: drag a folder onto another folder to move it; right-click menu: **New folder…**,
  **Rename…**, **Move to…**. ZIP archives in the tree behave as files (can be moved, not dropped into).
  "All files" (root) cannot be moved but is a valid drop target.
- Grid: drag selected tiles onto a tree folder to move them. A RAW+JPG pair always moves both files.
- "Move to…" opens a small folder-picker dialog (tree of the source's real folders, plus a
  **New folder** button). Do not use a QFileDialog that can leave the source.
- After a move: remap flags, save, then refresh (rescanning is acceptable for moves; keep the current
  folder/selection where possible). Show a notice `Moved N files · size` plus errors via Details.

## 2. Move flagged files to another folder
- New top-level menu **Organize** with: **Move selected to folder… (M)**, **Move Upload files to
  folder…**, **Move Delete files to folder…**, **New folder…**. Flagged moves use all flags of that color
  across the source (use `scan.flagged_items`, loose files only; report ZIP entries as skipped).
- Files go **flat** into the chosen folder (name collisions get the suffix rule). Confirm with count and
  size before moving. Flags follow the files (stay green/red at the new path).
- Also add **Move selected to folder…** to a grid right-click menu.

## 3. Delete key = delete right away
- **Delete** (and **Backspace** on macOS) on the selection (grid or Quick Look) moves the selected loose
  files straight into the existing quarantine (`quarantine.quarantine_items`) with **no dialog** — it stays
  reversible via Owner → Restore. Pairs: both files. ZIP entries: skip with a notice.
- It must feel instant: update the model in place (remove the items, their flags/flag states, pairs,
  rows), bump the quarantine count/size in the status bar, keep the selection on the next tile, and save
  flags. Do NOT rescan the whole drive for each delete. Run the file move on a worker; if it fails for some
  items, keep those items and show the error.
- Show a notice `Moved N files to quarantine · Restore from Owner menu`. Add the keys to the hint label.

## 4. Adobe files (psd, psb, ai, indd, idml, eps, …) thumbnails
- Today: Design category exists; PSD/PSB thumbnails work via Pillow only; AI/INDD/EPS need a Windows shell
  thumbnail handler (usually absent), so they show a plain icon.
- Add a cross-platform fallback in `thumbs.py` (and Quick Look for Design files): extract the embedded
  XMP thumbnail (`xmpGImg:image`, base64 JPEG, `&#xA;` line breaks) found in AI, INDD, EPS, PSD, etc.
  Read a bounded amount (e.g. first and last 8 MiB, streaming search) so huge files stay fast.
  For AI files with PDF compatibility, also try rendering page 1 with QtPdf when no XMP thumbnail exists
  (worker-thread safe, or skip if not safe). For EPS, also try the DOS EPS binary header TIFF preview
  if easy. Quick Look should show the best available image for Design files instead of "no preview".
- Allow Design thumbnails for ZIP entries too, under the existing size limit.

## Tests
organize: file and folder moves, suffix on collision, into-itself rejection, `_drive_review` rejection,
symlink rejection, changed-file rejection, ZIP entries skipped, flag remap (folder prefix, ZIP inside moved
folder, unaffected paths), create_folder validation. UI offscreen: Delete key removes tile in place and
quarantines file (pair: both), quarantine count updated, no rescan; Move selected/flagged to folder moves
files and flags follow; tree context actions exist. XMP thumbnail extraction from a synthetic AI/INDD-like
file and a PSD; bounded read.

## Done
README (English): document Organize menu, drag-and-drop, M and Delete keys, Adobe thumbnails. Add a v5
section to CODEX_REPORT.md. Final message: changed files + test summary.
