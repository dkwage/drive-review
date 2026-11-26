"""Content comparison and conservative duplicate flag suggestions."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import os
from zlib import crc32

from .drives import identify_drive
from .index import Record
from .scan import is_junk
from .zipsource import ItemReader, open_item, validate_item

EDGE = 64 * 1024
CHUNK = 1024 * 1024


def new_digest():
    return hashlib.blake2b(digest_size=32)


def check_cancel(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError("작업이 취소되었습니다.")


def partial_hash(root, item, stop):
    check_cancel(stop)
    digest = new_digest()
    with open_item(root, item) as source:
        digest.update(source.read(EDGE))
        source.seek(max(0, item.size - EDGE))
        digest.update(source.read(EDGE))
    return digest.hexdigest()


def hash_stream(source, stop=None, destination=None):
    digest = new_digest()
    checksum = 0
    while True:
        check_cancel(stop)
        chunk = source.read(CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        checksum = crc32(chunk, checksum)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest(), checksum


def same_bytes(left, right, stop, reader):
    with reader.open(left.root, left.item) as a, reader.open(right.root, right.item) as b:
        while True:
            check_cancel(stop)
            first = a.read(CHUNK)
            second = b.read(CHUNK)
            if first != second:
                return False
            if not first:
                return True


def keeper_order(record):
    item = record.item
    in_junk = is_junk(item.rel_path) or is_junk(item.zip_entry)
    depth = item.rel_path.count("/") + (1 + item.zip_entry.count("/") if item.zip_entry else 0)
    return (in_junk, bool(item.zip_entry), depth, item.mtime_ns, len(item.name),
            item.identity.casefold(), record.drive_id)


@dataclass(slots=True)
class Group:
    records: list[Record]
    keeper: Record | None
    suggestions: list[tuple[str, str]] = field(default_factory=list)
    unverified: set[str] = field(default_factory=set)


@dataclass(slots=True)
class DuplicateResult:
    groups: list[Group]
    hashes: dict
    errors: list[str]


def find_duplicates(drive, items, flags, index, stop=None, progress=lambda count, total, step: None,
                    verify=False):
    with ItemReader() as reader:
        return _find_duplicates(drive, items, flags, index, reader, stop, progress, verify)


def _physical_path(root, item):
    return os.path.join(root, *item.rel_path.split("/")).casefold(), item.zip_entry


def _find_duplicates(drive, items, flags, index, reader, stop, progress, verify):
    current = []
    buckets = defaultdict(list)
    cached = index.cached(drive.id)
    for item in items:
        check_cancel(stop)
        flag = flags.get(item.key)
        record = Record(item, drive.id, drive.label, drive.root,
                        flag=flag.flag if flag and flag.matches(item) else "")
        saved = cached.get(item.key)
        if saved and saved[0] == (item.size, item.mtime_ns, item.crc, item.archive_stamp):
            record.partial, record.digest, record.content_crc = saved[1:]
        current.append(record)
        buckets[item.size].append(record)
    local_paths = {_physical_path(drive.root, item) for item in items}
    for record in index.previous(drive, buckets):
        check_cancel(stop)
        if _physical_path(record.root, record.item) not in local_paths:
            buckets[record.item.size].append(record)

    errors = []
    changed = []

    def save(record):
        changed.append(record)
        if len(changed) >= 32:
            index.update_hashes(changed)
            changed.clear()

    eligible = []
    hashes = {}
    try:
        for count, record in enumerate(current, 1):
            check_cancel(stop)
            try:
                if not record.item.zip_entry and not record.partial:
                    record.partial = partial_hash(record.root, record.item, stop)
                    save(record)
                eligible.append(record)
            except InterruptedError:
                raise
            except Exception as error:
                errors.append(f"{record.item.identity}: {error}")
            if count % 64 == 0 or count == len(current):
                progress(count, len(current), "부분 해시")

        # Within size buckets, prioritize matching loose-file edge hashes and ZIP CRCs.
        # A loose file with unknown CRC must remain a candidate for an equal-sized ZIP entry.
        signatures = dict()
        for size, records in buckets.items():
            check_cancel(stop)
            edges = Counter(record.partial for record in records if record.partial)
            zips = Counter(record.item.crc for record in records if record.item.zip_entry)
            loose = Counter(record.content_crc for record in records if not record.item.zip_entry)
            signatures[size] = edges, zips, loose

        def possible(record):
            edges, zips, loose = signatures[record.item.size]
            if record.item.zip_entry:
                crc = record.item.crc
                return zips[crc] > 1 or loose[crc] > 0 or loose[None] > 0
            crc = record.content_crc
            return edges[record.partial] > 1 or (bool(zips) if crc is None else zips[crc] > 0)

        eligible.sort(key=lambda record: not possible(record))
        for count, record in enumerate(eligible, 1):
            check_cancel(stop)
            try:
                if not record.digest:
                    with reader.open(record.root, record.item) as source:
                        digest, checksum = hash_stream(source, stop)
                    record.digest, record.content_crc = digest, checksum
                    save(record)
                else:
                    validate_item(record.root, record.item)
                flag = flags.get(record.item.key)
                if flag and flag.hash and flag.hash != record.digest:
                    record.flag = ""
                hashes[record.item.key] = record.digest
            except InterruptedError:
                raise
            except Exception as error:
                errors.append(f"{record.item.identity}: {error}")
            if count % 16 == 0 or count == len(eligible):
                progress(count, len(eligible), "전체 해시")
    finally:
        if changed:
            index.update_hashes(changed)

    matches = defaultdict(list)
    for records in buckets.values():
        check_cancel(stop)
        for record in records:
            if (record.digest and possible(record)
                    and (record.drive_id != drive.id or record.item.key in hashes)):
                matches[record.item.size, record.digest].append(record)

    groups = []
    available = {}
    candidates = [records for records in matches.values() if len(records) > 1
                  and any(r.drive_id == drive.id for r in records)]
    for count, records in enumerate(candidates, 1):
        check_cancel(stop)
        records.sort(key=keeper_order)
        local = [r for r in records if r.drive_id == drive.id]
        unverified = set()
        if verify:
            reference = next((r for r in local if r.flag != "red"), local[0])
            confirmed = [reference]
            for record in records:
                check_cancel(stop)
                if record is reference:
                    continue
                try:
                    if record.drive_id != drive.id:
                        if record.drive_id not in available:
                            available[record.drive_id] = identify_drive(record.root).id == record.drive_id
                        if not available[record.drive_id]:
                            raise OSError("드라이브를 사용할 수 없습니다.")
                    if same_bytes(reference, record, stop, reader):
                        confirmed.append(record)
                except InterruptedError:
                    raise
                except Exception as error:
                    if record.drive_id != drive.id:
                        unverified.add(record.drive_id)
                        continue
                    errors.append(f"{record.item.identity}: {error}")
            confirmed_ids = {id(r) for r in confirmed}
            records = [r for r in records if id(r) in confirmed_ids or r.drive_id in unverified]
            eligible_keepers = confirmed
        else:
            confirmed_ids = {id(r) for r in records}
            eligible_keepers = records
        if len(records) > 1:
            keeper = min((r for r in eligible_keepers if r.flag != "red"
                          and (r.drive_id == drive.id or r.flag == "green")),
                         key=keeper_order, default=None)
            suggestions = [r.item.key for r in records if keeper is not None and r is not keeper
                           and r.drive_id == drive.id and not r.flag and id(r) in confirmed_ids]
            groups.append(Group(records, keeper, suggestions, unverified))
        if verify and (count % 8 == 0 or count == len(candidates)):
            progress(count, len(candidates), "바이트 비교")
    return DuplicateResult(groups, hashes, errors)
