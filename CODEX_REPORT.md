# Drive Review v1 verification

- [x] All tests passed on macOS 27.0.1 arm64 using Python 3.12.16 in the local `.venv`.
- [x] The Python entry point and macOS PyInstaller one-folder bundle launched offscreen. Each indexed a loose file, a ZIP, and a ZIP member with empty stderr.
- [x] Native Windows coverage gaps and the reason are recorded below.
- [x] Known limitations and follow-ups are recorded below.

## macOS test output

Command `QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests`

```text
.................
----------------------------------------------------------------------
Ran 17 tests in 2.100s

OK
```

The offscreen smoke tests exercised scanning while the Qt event loop was running, ZIP tree navigation, multi-select P/X/U, portable flag autosave and reload, automatic duplicate flags, upload staging, quarantine, and undo. A separate offscreen trial loaded 30,001 generated items and a visible image thumbnail in 0.47 s. It recorded a 70 ms maximum sampled event-loop timer gap during load and a 6 ms filter update on that fixture.

## Native Windows checks pending

The available host was macOS. Native testing remains for Windows Shell thumbnail COM and its installed codecs, removable and fixed USB drive detection, extended-length paths, read-only volumes, and quarantine/undo on NTFS and exFAT. Windows PyInstaller packaging and the `DriveReview-windows` CI artifact also await a Windows runner. The macOS package was built and launched locally.

## Known limitations and follow-ups

- The first duplicate pass streams all reviewable items to build a cross-drive baseline. An offline drive's cached match reflects its last scan. With byte comparison enabled, an unavailable drive is ineligible to supply the keeper for an automatic red flag.
- ZIP listing validates the central directory. Damage confined to the compressed payload can surface later when the affected entry is read. Automated ZIP thumbnails are limited to 64 MiB per member, and ZIP video thumbnail extraction requires an explicit preview.
- Opening a file in its default app uses a local temporary copy. Large files need corresponding local free space. The on-disk thumbnail cache has no automatic eviction.
- The workspace marks `.git` read-only. The changes are in the workspace, and the conventional commits were made in a writable local clone.
