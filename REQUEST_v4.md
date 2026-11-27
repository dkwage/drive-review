# REQUEST v4: RAW+JPG pairs shown as one tile, per-pair upload choice

Context: REQUEST_v1..v3, README.md, CODEX_REPORT.md. v3 added capture dates (`drive_review/taken.py`).
Keys today: Z Upload, X Delete, C Clear, Space Quick Look, K play/pause. Keep all tests passing.
Do NOT run git commands that write (.git is read-only in your sandbox). Work autonomously until done.

## Pairing (new module `drive_review/pairs.py`, pure logic, unit-tested)
- A pair = one RAW file (RAW extensions from `items.CATEGORIES["Image"]`: arw cr2 cr3 nef dng raf ...)
  + one JPG/JPEG/HEIC/HEIF in the **same folder** (same ZIP folder for entries; loose files never pair
  with ZIP entries).
- BOTH conditions are required (no name-only and no metadata-only pairing):
  1. Same base name, case-insensitive (`IMG_0177.CR2` + `IMG_0177.JPG`).
  2. Same capture metadata: both files have a capture time (from `taken.py`) and it is equal to the second;
     if both also have SubSecTimeOriginal (0x9291) it must match; if both have camera Make/Model
     (0x010F/0x0110) they must match. Extend `taken.py` to read SubSec and Make/Model into the cached result
     (migrate the cache).
- Either file lacking a capture time -> do not pair (show separately). This avoids pairing e.g. two cameras'
  `IMG_0001` files in one folder.
- If a base name has several JPGs or several RAWs, do not pair (keep them separate).
- Pairs can only be formed once capture dates are read; tiles merge as the background date pass completes
  (no flicker loops; preserve selection and flags when tiles merge).

## Grid / Quick Look
- A pair is ONE tile. Thumbnail and Quick Look always show the JPG. The tile shows a clear badge
  `RAW+JPG` and the current upload choice in words: `↑ JPG`, `↑ RAW`, or `↑ RAW+JPG` (only when flagged
  Upload). Tooltip lists both file names, sizes, and the choice.
- Upload choice per pair: default **JPG**. Key **R** cycles JPG → RAW → RAW+JPG → JPG for the selected
  pair(s) (works in grid and Quick Look; multi-select applies to all selected pairs). Pressing R on an
  unflagged pair also flags it Upload. Quick Look footer shows a segmented control `Upload: [JPG] [RAW]
  [RAW+JPG]` for pairs (no focus stealing, like the existing flag buttons) and a hint `R choose RAW/JPG`.
- Z on a pair = Upload with the pair's current choice: the chosen file(s) get `green`, the other file is
  left **unflagged** (not deleted). X on a pair = both files `red` (deleting the photo). C clears both.
- Filters/counts: a pair counts once in the grid count; status-bar sizes count the actual files flagged.
  Category filter Image includes pairs. Search matches either name.
- Toggle in View menu: "Group RAW+JPG pairs" (on by default, remembered). Off = previous behavior.

## Persistence
- flags.csv stays one row per file (rel_path, zip_entry, flag ...) so RS Uploader / "Copy green files"
  work unchanged. Store the pair choice too: add a column `pair_choice` (jpg|raw|both, empty for non-pairs)
  at the END of the CSV header; reading must accept old files without it. On reload, a pair whose files'
  flags imply a choice (only RAW green -> raw, both green -> both) restores it even without the column.
- Duplicate auto-flags must not split a pair silently: if dupes would flag one file of a pair red, keep the
  suggestion but show the pair tile with a duplicate badge; do not change the pair choice.

## Tests
Pairing requires name AND metadata (case variants, ZIP folders, loose vs ZIP never pair), same name but
different capture time/model -> not paired, missing date on either -> not paired, subsec mismatch -> not
paired, several-JPG ambiguity, tiles merging after the date pass keeps selection/flags, R cycling and multi-select, Z/X/C effects on
both files, flags.csv round-trip incl. old-format files and choice inference, counts, toggle off, offscreen
UI: one tile for a pair, badge text, Quick Look shows the JPG and the segmented control.

## Done
README (English): document pairs, the R key, and the CSV column. Add a v4 section to CODEX_REPORT.md.
Final message: changed files + test summary.
