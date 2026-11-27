"""Find unambiguous RAW and rendered versions of the same capture."""

from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from .items import RAW_EXTENSIONS, Item

CHOICE_LABELS = {"jpg": "JPG", "raw": "RAW", "both": "RAW+JPG"}
CHOICES = tuple(CHOICE_LABELS)
RENDERED_EXTENSIONS = {"jpg", "jpeg", "heic", "heif"}


@dataclass(frozen=True, slots=True)
class PhotoPair:
    raw: Item
    jpg: Item

    @property
    def items(self):
        return self.raw, self.jpg

    @property
    def stamp(self):
        return (*self.raw.key, *self.jpg.key, self.raw.stamp, self.jpg.stamp)

    def matches(self, dates):
        raw, jpg = (dates.get(item.key) for item in self.items)
        if raw is None or jpg is None:
            return False
        if raw.offset and jpg.offset:
            same_second = raw.sort_key == jpg.sort_key
        else:
            same_second = raw.text == jpg.text
        return same_second and all(not left or not right or left == right for left, right in (
            (raw.subsec, jpg.subsec), (raw.make, jpg.make), (raw.model, jpg.model)))

    def uploads(self, choice):
        if choice == "both":
            return self.items
        return (self.raw if choice == "raw" else self.jpg,)


def pair_candidates(items):
    """Index both members of each possible pair before any metadata arrives."""
    groups = defaultdict(lambda: ([], []))
    for item in items:
        path = PurePosixPath(item.zip_entry or item.rel_path)
        extension = path.suffix.casefold().lstrip(".")
        if extension not in RAW_EXTENSIONS and extension not in RENDERED_EXTENSIONS:
            continue
        archive = item.rel_path if item.zip_entry else None
        raw, jpg = groups[archive, path.parent, path.stem.casefold()]
        (raw if extension in RAW_EXTENSIONS else jpg).append(item)
    candidates = {}
    for raw, jpg in groups.values():
        if len(raw) == len(jpg) == 1:
            pair = PhotoPair(raw[0], jpg[0])
            candidates.update({item.key: pair for item in pair.items})
    return candidates


def cycle_choice(choice):
    return CHOICES[(CHOICES.index(choice) + 1) % len(CHOICES)]


def restore_choice(raw_flag, jpg_flag, default="jpg"):
    # Green rows also reflect individual file edits made with grouping turned off.
    if raw_flag and raw_flag.flag == "green":
        return "both" if jpg_flag and jpg_flag.flag == "green" else "raw"
    if jpg_flag and jpg_flag.flag == "green":
        return "jpg"
    recorded = [flag for flag in (raw_flag, jpg_flag) if flag and flag.pair_choice]
    return max(recorded, key=lambda flag: flag.flagged_at_iso).pair_choice if recorded else default
