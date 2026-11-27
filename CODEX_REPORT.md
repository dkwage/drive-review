# Drive Review v2 verification

Implemented the English UI and README, the Owner menu and quarantine totals, permanent deletion with typed confirmation, and the Quick Look panel. Existing `flags.csv` files keep the same columns and values.

## macOS results

Tested on macOS 27.0.1 arm64 with Python 3.12.16 in the existing uv-managed `.venv`.

```text
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
Ran 28 tests in 2.589s

OK
```

- The offscreen tests exercised a full-resolution EXIF-rotated image, image zoom and fit, a generated two-page PDF, plain text, grid navigation, flag shortcuts, and ZIP audio decoding and temporary-file cleanup. A simulated native handler failure exercised the Office text fallback and cleanup. A separate check decoded a generated HEIC image.
- Quarantine tests covered typed confirmation, byte and file totals, size and modification-time checks, changes after confirmation, path validation, log recovery after a failed final write, and cleanup of empty folders. The existing scan, export, duplicate, and reversible quarantine tests passed.
- The Python entry point and the macOS PyInstaller bundle built and launched offscreen. Each indexed one loose file, one ZIP archive, and one ZIP entry. Qt emitted informational font diagnostics in the launch checks. The offscreen audio test logged a device-property diagnostic while the FFmpeg backend decoded the fixture.
- The Windows x86-64 Python 3.12 dependency set resolved using published wheels only. The wheel contents include the Qt Multimedia FFmpeg plugin and five runtime DLLs, the Qt PDF libraries and image plugin, and a CPython 3.12 `pillow-heif` wheel. [PySide6 Addons](https://pypi.org/project/PySide6-Addons/6.12.0/#files), [PySide6 Essentials](https://pypi.org/project/PySide6-Essentials/6.12.0/#files), [PySide6 PDF](https://pypi.org/project/PySide6-Pdf/6.12.0.140/#files), [pillow-heif](https://pypi.org/project/pillow-heif/1.8.1/#files)

## Real Windows verification pending

- Run the Windows CI build and launch the resulting one-folder executable. Check PDF rendering, HEIC decoding, and actual audio and video playback, seeking, and volume through the bundled FFmpeg backend.
- Verify Explorer's installed Office and other preview handlers, COM surrogate activation, resize, shortcut forwarding, unloading, and fallback. Verify native Shell thumbnails and the RAW codecs installed on the target PC.
- Test removable USB discovery, extended-length paths, and quarantine, restore, and permanent deletion on NTFS and exFAT, including files held open by another process.

## Limitations and implementation decisions

- Native preview handlers and default apps receive local temporary copies to protect source files. Path-based viewers extract only the selected ZIP entry. Large copies need local free space. In-progress decodes and native calls may finish on their workers before cleanup can complete.
- Inline images display the first frame of animated formats. Office text fallback covers Open XML documents and displays up to three worksheet tabs, with up to 200 rows and 80 columns per tab. Legacy Office formats rely on an installed preview handler for inline content. Password-protected PDFs show an unavailable-preview message.
- The text viewer reads about 2 MiB from plain text files. Native Windows preview handlers and system-provided diagnostics can use the Windows display language.
- When a flag edit in Quick Look would change membership in the current flag filter, the panel retains the current grid until it closes or the filter is changed.
- The on-disk thumbnail cache still has no automatic eviction.

## v3 capture dates

Drive Review reads embedded capture dates for supported photos, RAW files, and videos. Quick Look, grid tooltips, and the Excel inventory show the recorded wall-clock text with the UTC offset when available. **By taken** compares offset-aware times in UTC and uses the recorded time directly when the offset is unknown. Unknown dates sort last.

The local SQLite index stores both known and unknown dates by drive, item key, and item stamp. The cancellable background pass reuses the cache and publishes batches. Quick Look requests its current item on a separate worker. Excel export can read remaining dates even after the background pass has been stopped.

```text
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
Ran 42 tests in 3.222s

OK
```

- The existing 29 tests passed on macOS arm64 with Python 3.12.16 in the existing uv-managed `.venv`. Added 13 tests for metadata parsing, the read cap, ZIP members, cache migration and invalidation, cancellation and resume, and offscreen Quick Look and sorting. The inventory test also checks the position and content of the **Taken** column.
- Spot checks returned capture dates for CR2, CR3, DNG, NEF, RAF, RW2, and MOV samples. The automated tests generate JPEG, PNG, HEIF, and, where the Pillow encoders are available, WebP and AVIF fixtures. [ExifTool sample files](https://github.com/exiftool/exiftool/tree/master/t/images)
- The 4 MiB read budget permits seeking past large payloads in loose files. For ZIP entries and other sequential streams, the reader can only reach metadata in the first 4 MiB. Native Windows verification remains pending.

## v4 RAW+JPG pairs

Drive Review groups an unambiguous RAW and JPG, JPEG, HEIC, or HEIF in the same folder after matching the case-insensitive base name and capture metadata. It compares UTC seconds when both offsets are available and recorded wall-clock seconds otherwise. Available subsecond and camera fields must agree when recorded by both files. The SQLite migration refreshes old capture records once to collect those fields.

Grouped tiles preserve selection and manual flags when capture dates arrive. They use the rendered image for both the thumbnail and Quick Look. **R** cycles upload choices for the selection, and Quick Look can set the current pair's choice directly. Duplicate searches resolve candidate metadata before applying automatic Delete flags. Confirmed pairs show those suggestions on the tile and retain their upload choice.

`flags.csv` appends `pair_choice` while keeping one row per flagged file. Older CSVs load and infer the choice from green flags. After **C** removes both rows, the local index remembers the pair's choice on the same computer.

```text
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
Ran 54 tests in 4.028s

OK
```

- All 42 baseline tests and 12 new tests passed on macOS 27.0.1 arm64 with Python 3.12.16 in the existing uv-managed `.venv`.
- Added coverage for pairing rules and ZIP boundaries, EXIF tags and SQLite migration, legacy and current CSVs, selection across merges, thumbnail and badge text, Quick Look, multi-selection shortcuts, filters and counts, remembered grouping, and duplicate and quarantine behavior while capture dates are pending.
- Native Windows UI and standalone RS Uploader verification remain pending.
