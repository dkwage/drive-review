# REQUEST v3: capture date ("Taken") from EXIF / video metadata

Context: REQUEST_v1.md, REQUEST_v2.md, README.md. Items already carry `mtime_ns` and `ctime_ns`
(see `drive_review/items.py`, `date_label`). Shortcuts are Z/X/C. All 29 tests pass now; keep them passing.
Do NOT run git commands that write (.git is read-only in your sandbox). Work autonomously until done.

## Goal
Show when a photo/video was actually taken, because Windows "Created" is usually the copy time.

## Reading (new module `drive_review/taken.py`, no new dependencies; Pillow/pillow-heif already present)
`read_taken(stream, name) -> Taken | None` where `Taken` holds the original text `YYYY-MM-DD HH:MM:SS`, an
optional UTC offset, and a sortable integer. Read only what is needed (headers/IFDs, seek, never the whole
file; hard cap ~4 MiB of reads per file). Must work on loose files and on ZIP entries (use the existing
ZIP reader; non-seekable streams may be buffered up to the cap).
- Tag priority: DateTimeOriginal (0x9003) > CreateDate/DateTimeDigitized (0x9004) > DateTime (0x0132).
  Use OffsetTimeOriginal (0x9011) / OffsetTime when present. No offset: keep as local wall-clock time
  (display as-is, no timezone conversion). Reject zero/blank dates like `0000:00:00 00:00:00`.
- Formats: JPEG (APP1 Exif), TIFF and TIFF-based RAW (CR2, NEF, NRW, ARW, SR2, DNG, ORF, RW2, PEF, SRW,
  RAF via its embedded JPEG), HEIC/HEIF/AVIF (ISOBMFF Exif item; pillow-heif or a small box parser), CR3
  (CMT1/CMT2 boxes), PNG (eXIf chunk), WebP (EXIF chunk). Write a small robust TIFF/IFD parser (both byte
  orders, bounds-checked, loop-safe) rather than relying on Pillow for RAW.
- Videos MP4/MOV/M4V/3GP: `moov/mvhd` creation_time (seconds since 1904-01-01 UTC, v0 and v1); prefer
  QuickTime `com.apple.quicktime.creationdate` (keys/ilst) when present since it has the local offset;
  ignore 0 / 1904 values. Other video formats: none.
- Any parse error -> None, never raise to the UI.

## Caching and background pass
- Cache results in the local SQLite index (new table, keyed by drive id + item key + item stamp, also
  caching "no date" so it is not retried). Migrate the schema safely for existing databases.
- After a scan, run a low-priority, cancellable background pass over Image and Video items only, cached
  ones first, nearest-to-visible first if easy; progress in the status bar ("Reading capture dates
  1,234 / 9,876"). Never block the UI; results update the model as they arrive (batched). Quick Look reads
  the current item immediately if not cached yet.

## UI / export
- Quick Look details: `Taken … · Created … · Modified …` (omit Taken when unknown).
- Grid tooltip: add `Taken …` line when known.
- Sort: add "By taken" (newest first, unknown last, then by name).
- Excel inventory export: add a "Taken" column after "Created". flags.csv format unchanged.
- README: document the Taken date and where it comes from, briefly.

## Tests
JPEG with EXIF (make with Pillow), offset handling, a minimal handcrafted little- and big-endian TIFF,
a malformed/looping IFD (returns None quickly), synthetic MP4 with mvhd v0 and v1, QuickTime creationdate
key, a ZIP entry JPEG, garbage file -> None, cache hit avoids re-reading, sort order with unknowns last,
offscreen UI check that Quick Look shows "Taken".

## Done
Update CODEX_REPORT.md (add a v3 section). Final message: short list of changed files and test summary.
