"""Hole spectra, tape-blank material planning and punch orders.

Physical model
--------------
A SOLO version body is a sequence of bytes; one body byte occupies one
pitch (节距).  Bit ``c`` of the byte is channel (孔道) ``c``, bits are
numbered low to high as channels 0..7, and a set bit is a punched hole.
Switch instructions retained at the start or end of a body are ordinary
bytes too, so they occupy pitches exactly like visible characters.

A :class:`HoleSpectrum` pins one fixed revision version: every pitch can
be looked up as either a visible character (with its inner byte offset and
decode mode), a zero-character switch span, and a provenance descriptor
that follows the revision chain back to an original byte, a boundary
switch inserted by a revision batch, or newly encoded replacement text.

Material planning
-----------------
Each :class:`TapeOrder` asks for one continuous target spectrum and states
whether reverse mounting is allowed.  A :class:`TapeBlank` is a partly
punched tape; holes can only be added, never removed, so the blank holes
inside the chosen window must be a subset of the target holes.  A blank
may serve at most one order and may be cut at any continuous window; the
pitches outside the window are scrapped.  Reverse mounting reverses both
the pitch order and the eight channel order.

Batch objectives, strictly lexicographic:

1. minimise the number of brand-new (fully blank) tapes drawn;
2. then minimise the total number of punches;
3. then minimise the total scrapped blank pitches.

The optimum is solved exactly with a min-cost max-flow network (the
limits are 24 orders, 40 blanks and 2000 pitches per tape), so nothing
here is a heuristic.  Re-planning against a new batch of blanks only
reuses already built spectra: no version is decoded twice.
"""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .codec import (
    ESC,
    MODE_NAMES,
    MODE_PAIR,
    MODE_SINGLE,
    DecodedDocument,
    decode_document,
)
from .revision import BatchRecord, BoundaryAdjustment, PatchRecord, RevisionLibrary

__all__ = [
    "MAX_ORDERS",
    "MAX_BLANKS",
    "MAX_PITCHES",
    "ORIENT_FORWARD",
    "ORIENT_REVERSE",
    "SPECTRUM_FORMAT",
    "ORDER_FORMAT",
    "TapeError",
    "holes_of_byte",
    "byte_of_holes",
    "reverse_byte",
    "holes_string",
    "Segment",
    "TerminalOrigin",
    "TraceHop",
    "HoleSpectrum",
    "build_spectrum",
    "TapeBlank",
    "TapeOrder",
    "Placement",
    "best_placement",
    "enumerate_placements",
    "Allocation",
    "MachineOrder",
    "BatchPlan",
    "MaterialPlanner",
    "plan_batch",
]

MAX_ORDERS = 24
MAX_BLANKS = 40
MAX_PITCHES = 2000

ORIENT_FORWARD = "forward"
ORIENT_REVERSE = "reverse"
_ORIENTATIONS = (ORIENT_FORWARD, ORIENT_REVERSE)

SPECTRUM_FORMAT = "solo-hole-spectrum-v1"
ORDER_FORMAT = "solo-punch-order-v1"

# Cost weight separating punch count from scrap in the flow network.
# Maximum possible scrap in one edge is MAX_PITCHES, so 10**7 keeps the
# lexicographic order strict for every legal instance.
_PUNCH_WEIGHT = 10**7

# Reversed channel lookup table: channel c on the mounted target side is
# blank channel 7-c after flip mounting.
_REVERSE_BYTE = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))


class TapeError(Exception):
    """An invalid spectrum, blank, order, plan or machine-order operation."""


# ---------------------------------------------------------------------------
# Bit / channel geometry


def holes_of_byte(value: int) -> tuple[int, ...]:
    """Channels punched for one body byte, lowest channel first."""
    if not 0 <= value <= 0xFF:
        raise TapeError(f"byte out of range: {value!r}")
    return tuple(c for c in range(8) if value & (1 << c))


def byte_of_holes(channels: Iterable[int]) -> int:
    """Inverse of :func:`holes_of_byte`."""
    value = 0
    for c in channels:
        if not 0 <= c <= 7:
            raise TapeError(f"channel out of range: {c!r}")
        bit = 1 << c
        if value & bit:
            raise TapeError(f"duplicate channel {c}")
        value |= bit
    return value


def reverse_byte(value: int) -> int:
    """Reverse the eight channel bits (flip mounting keeps pitch geometry)."""
    return _REVERSE_BYTE[value]


def holes_string(value: int) -> str:
    """Compact fixed-width picture, channel 7 on the left, channel 0 right."""
    return "".join("1" if value & (1 << c) else "0" for c in range(7, -1, -1))


# ---------------------------------------------------------------------------
# Segments and provenance


@dataclass(frozen=True)
class Segment:
    """One decoded piece of the version body covering pitches [start, end).

    A character segment carries the decoded Unicode character, its index
    and the mode it was decoded in; a switch segment carries the mode
    transition and the raw switch bytes.  A two-byte switch occupies two
    pitches, both pointing at the same segment.
    """

    kind: str  # "char" or "switch"
    start: int
    end: int
    char_index: int | None = None
    char: str | None = None
    mode: int | None = None
    from_mode: int | None = None
    to_mode: int | None = None
    raw: bytes | None = None

    def describe(self) -> str:
        if self.kind == "char":
            return (
                f"char[{self.char_index}] {self.char!r} bytes[{self.start},{self.end}) "
                f"decoded in {MODE_NAMES[self.mode]}"
            )
        return (
            f"zero-char switch bytes[{self.start},{self.end}) "
            f"{MODE_NAMES[self.from_mode]}->{MODE_NAMES[self.to_mode]} raw={self.raw.hex()}"
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
        }
        if self.kind == "char":
            data.update(char_index=self.char_index, char=self.char, mode=self.mode)
        else:
            data.update(
                from_mode=self.from_mode,
                to_mode=self.to_mode,
                raw=self.raw.hex(),
            )
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Segment":
        kind = data["kind"]
        if kind == "char":
            return cls(
                kind=kind,
                start=int(data["start"]),
                end=int(data["end"]),
                char_index=int(data["char_index"]),
                char=data["char"],
                mode=int(data["mode"]),
            )
        if kind == "switch":
            return cls(
                kind=kind,
                start=int(data["start"]),
                end=int(data["end"]),
                from_mode=int(data["from_mode"]),
                to_mode=int(data["to_mode"]),
                raw=bytes.fromhex(data["raw"]),
            )
        raise TapeError(f"unknown segment kind {kind!r}")


@dataclass(frozen=True)
class TerminalOrigin:
    """Where a pitch ultimately comes from, after walking every revision.

    ``kind`` is one of:

    ``original``
        an untouched byte of the pinned original version.
    ``boundary``
        a zero-character switch inserted by a revision batch boundary
        adjustment (role splice/boundary/final).
    ``new_text``
        a body byte encoded for a replacement patch's inserted text.
    """

    kind: str
    version_id: int
    pitch: int
    batch_id: int | None = None
    role: str | None = None
    from_mode: int | None = None
    to_mode: int | None = None
    old_char_start: int | None = None
    old_char_end: int | None = None
    inserted_text: str | None = None
    inner_byte: int | None = None
    # Kept-byte hops from the owning version towards this terminal, as
    # (version_id, pitch) pairs, nearest first; empty at the terminal.
    via: tuple[tuple[int, int], ...] = ()

    def describe(self) -> str:
        if self.kind == "original":
            return f"original byte of v{self.version_id} at pitch {self.pitch}"
        if self.kind == "boundary":
            return (
                f"boundary switch inserted in v{self.version_id} batch {self.batch_id} "
                f"at pitch {self.pitch} ({MODE_NAMES[self.from_mode]}->"
                f"{MODE_NAMES[self.to_mode]}, role={self.role})"
            )
        return (
            f"new text encoded in v{self.version_id} batch {self.batch_id} at pitch "
            f"{self.pitch}: replacement chars[{self.old_char_start},{self.old_char_end}) "
            f"text={self.inserted_text!r}, inner byte {self.inner_byte}"
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "t": self.kind,
            "v": self.version_id,
            "p": self.pitch,
        }
        if self.kind == "boundary":
            data.update(
                batch=self.batch_id,
                role=self.role,
                from_mode=self.from_mode,
                to_mode=self.to_mode,
            )
        elif self.kind == "new_text":
            data.update(
                batch=self.batch_id,
                old_char_start=self.old_char_start,
                old_char_end=self.old_char_end,
                inserted_text=self.inserted_text,
                inner_byte=self.inner_byte,
            )
        if self.via:
            data["via"] = [list(hop) for hop in self.via]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TerminalOrigin":
        kind = data["t"]
        via = tuple((int(a), int(b)) for a, b in data.get("via", ()))
        common = dict(version_id=int(data["v"]), pitch=int(data["p"]), via=via)
        if kind == "original":
            return cls(kind=kind, **common)
        if kind == "boundary":
            return cls(
                kind=kind,
                batch_id=int(data["batch"]),
                role=data["role"],
                from_mode=int(data["from_mode"]),
                to_mode=int(data["to_mode"]),
                **common,
            )
        if kind == "new_text":
            return cls(
                kind=kind,
                batch_id=int(data["batch"]),
                old_char_start=int(data["old_char_start"]),
                old_char_end=int(data["old_char_end"]),
                inserted_text=data["inserted_text"],
                inner_byte=int(data["inner_byte"]),
                **common,
            )
        raise TapeError(f"unknown origin kind {kind!r}")


@dataclass(frozen=True)
class TraceHop:
    """One step of a per-pitch revision walk."""

    version_id: int
    pitch: int
    source: str  # "kept" | "original" | "boundary" | "new_text"
    detail: str


@dataclass(frozen=True)
class HoleSpectrum:
    """The continuous hole spectrum of one pinned revision version."""

    version_id: int
    body: bytes
    body_sha256: str
    text: str
    final_mode: int
    segments: tuple[Segment, ...]
    # Segment index owning each pitch; len == len(body).
    pitch_segment: tuple[int, ...]
    # Terminal provenance for each pitch; len == len(body).
    origins: tuple[TerminalOrigin, ...]

    def __post_init__(self) -> None:
        if hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise TapeError("spectrum body does not match its sha256 digest")
        if len(self.pitch_segment) != len(self.body) or len(self.origins) != len(self.body):
            raise TapeError("spectrum pitch tables do not cover the whole body")

    # -- geometry --------------------------------------------------------

    @property
    def length(self) -> int:
        return len(self.body)

    def byte_at(self, pitch: int) -> int:
        if not 0 <= pitch < self.length:
            raise TapeError(f"pitch {pitch} outside spectrum of length {self.length}")
        return self.body[pitch]

    def holes_at(self, pitch: int) -> tuple[int, ...]:
        return holes_of_byte(self.byte_at(pitch))

    def segment_at(self, pitch: int) -> Segment:
        return self.segments[self.pitch_segment[pitch]]

    def origin_at(self, pitch: int) -> TerminalOrigin:
        return self.origins[pitch]

    def lookup_pitch(self, pitch: int) -> dict[str, Any]:
        """Full record for one pitch: byte, holes, segment and provenance."""
        segment = self.segment_at(pitch)
        origin = self.origin_at(pitch)
        return {
            "version_id": self.version_id,
            "pitch": pitch,
            "byte": self.body[pitch],
            "holes": holes_of_byte(self.body[pitch]),
            "segment": segment,
            "segment_inner_offset": pitch - segment.start,
            "origin": origin,
        }

    def trace(self, pitch: int) -> tuple[TraceHop, ...]:
        """Walk the revision chain from this version's pitch to its origin."""
        if not 0 <= pitch < self.length:
            raise TapeError(f"pitch {pitch} outside spectrum of length {self.length}")
        origin = self.origins[pitch]
        hops: list[TraceHop] = []
        current_version = self.version_id
        current_pitch = pitch
        for via_version, via_pitch in origin.via:
            hops.append(
                TraceHop(
                    current_version,
                    current_pitch,
                    "kept",
                    f"byte copied verbatim from v{via_version} pitch {via_pitch}",
                )
            )
            current_version, current_pitch = via_version, via_pitch
        hops.append(
            TraceHop(
                origin.version_id,
                origin.pitch,
                origin.kind,
                origin.describe(),
            )
        )
        return tuple(hops)

    # -- (de)serialisation -----------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": SPECTRUM_FORMAT,
            "version_id": self.version_id,
            "body_sha256": self.body_sha256,
            "final_mode": self.final_mode,
            "text": self.text,
            "length": self.length,
            "segments": [s.to_dict() for s in self.segments],
            "pitch_segment": list(self.pitch_segment),
            "pitches": [
                {
                    "pitch": p,
                    "byte": f"{self.body[p]:02x}",
                    "holes": holes_of_byte(self.body[p]),
                    "origin": self.origins[p].to_dict(),
                }
                for p in range(self.length)
            ],
        }

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "HoleSpectrum":
        if data.get("format") != SPECTRUM_FORMAT:
            raise TapeError("not a SOLO hole spectrum file")
        body = bytes.fromhex("".join(str(p["byte"]) for p in data["pitches"]))
        digest = hashlib.sha256(body).hexdigest()
        if digest != data["body_sha256"]:
            raise TapeError("spectrum digest mismatch: original bytes were altered")
        length = int(data["length"])
        if len(body) != length or len(data["pitches"]) != length:
            raise TapeError("spectrum length fields disagree")
        for p, item in enumerate(data["pitches"]):
            if int(item["pitch"]) != p:
                raise TapeError(f"spectrum pitch {p} is out of order")
            stored_byte = int(item["byte"], 16)
            if stored_byte != body[p]:
                raise TapeError(f"spectrum pitch {p} byte disagrees with body")
            if tuple(item["holes"]) != holes_of_byte(body[p]):
                raise TapeError(f"spectrum pitch {p} hole list disagrees with byte")
        segments = tuple(Segment.from_dict(s) for s in data["segments"])
        pitch_segment = tuple(int(x) for x in data["pitch_segment"])
        # Segments must partition the body and every reference must be valid.
        covered = [False] * length
        for index, segment in enumerate(segments):
            if not (0 <= segment.start < segment.end <= length):
                raise TapeError(f"segment {index} has illegal bounds")
            for p in range(segment.start, segment.end):
                if covered[p]:
                    raise TapeError(f"pitch {p} belongs to two segments")
                covered[p] = True
        if length and not all(covered):
            raise TapeError("segments do not cover every pitch")
        if any(not 0 <= i < len(segments) for i in pitch_segment):
            raise TapeError("pitch-to-segment table references an unknown segment")
        origins = tuple(TerminalOrigin.from_dict(p["origin"]) for p in data["pitches"])
        return cls(
            version_id=int(data["version_id"]),
            body=body,
            body_sha256=digest,
            text=data["text"],
            final_mode=int(data["final_mode"]),
            segments=segments,
            pitch_segment=pitch_segment,
            origins=origins,
        )

    @classmethod
    def load(cls, path: str) -> "HoleSpectrum":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))


# ---------------------------------------------------------------------------
# Spectrum construction from a revision library


def _build_segments(
    body: bytes, doc: DecodedDocument
) -> tuple[tuple[Segment, ...], tuple[int, ...]]:
    """Split a decoded body into character and zero-character switch segments."""
    n = len(body)
    segments: list[Segment] = []
    pitch_segment = [-1] * n

    def add(segment: Segment) -> int:
        index = len(segments)
        segments.append(segment)
        for p in range(segment.start, segment.end):
            if pitch_segment[p] != -1:
                raise TapeError(f"internal: pitch {p} covered twice")
            pitch_segment[p] = index
        return index

    char_count = len(doc.text)
    for i in range(char_count):
        start, end = doc.byte_range(i)
        add(
            Segment(
                kind="char",
                start=start,
                end=end,
                char_index=i,
                char=doc.text[i],
                mode=doc.char_mode(i),
            )
        )

    # Gaps between characters (and the leading/trailing gaps) can only hold
    # complete zero-character ESC switches.
    gaps: list[tuple[int, int, int]] = []  # (start, end, mode at gap entry)
    if char_count:
        gaps.append((0, int(doc.starts[0]), MODE_SINGLE))
        for i in range(char_count - 1):
            gaps.append((int(doc.ends[i]), int(doc.starts[i + 1]), doc.char_mode(i)))
        gaps.append((int(doc.ends[char_count - 1]), n, doc.char_mode(char_count - 1)))
    elif n:
        gaps.append((0, n, MODE_SINGLE))

    for gap_start, gap_end, mode in gaps:
        p = gap_start
        while p < gap_end:
            if p + 1 >= gap_end or body[p] != ESC or body[p + 1] not in (0x00, 0x01):
                raise TapeError(
                    f"internal: non-switch bytes at [{p},{p + 1}) outside characters"
                )
            instruction = body[p + 1]
            to_mode = MODE_PAIR if instruction == 0x01 else MODE_SINGLE
            add(
                Segment(
                    kind="switch",
                    start=p,
                    end=p + 2,
                    from_mode=mode,
                    to_mode=to_mode,
                    raw=bytes((ESC, instruction)),
                )
            )
            mode = to_mode
            p += 2

    if n and any(i < 0 for i in pitch_segment):
        raise TapeError("internal: some pitches have no segment")
    return tuple(segments), tuple(pitch_segment)


def _batch_publishing(lib: RevisionLibrary, version_id: int) -> BatchRecord:
    for batch in lib.batches():
        if batch.new_version == version_id:
            return batch
    raise TapeError(f"version {version_id} has no revision batch record")


def build_spectrum(
    lib: RevisionLibrary,
    version_id: int,
    cache: dict[int, HoleSpectrum] | None = None,
) -> HoleSpectrum:
    """Build the pinned spectrum of a library version, with full provenance.

    Parent versions are built first and reused, so a revision chain is
    decoded once per version.  The returned spectrum is self-contained and
    survives being serialised and reloaded without the library.
    """
    if cache is not None and version_id in cache:
        return cache[version_id]
    owned_cache = cache is None
    if owned_cache:
        cache = {}

    version = lib.get(version_id)
    doc = decode_document(version.body)
    segments, pitch_segment = _build_segments(version.body, doc)

    if version.parent_version is None:
        origins = tuple(
            TerminalOrigin(
                kind="original", version_id=version_id, pitch=p
            )
            for p in range(version.body_length)
        )
    else:
        parent_spectrum = build_spectrum(lib, version.parent_version, cache)
        batch = _batch_publishing(lib, version_id)
        base = lib.get(version.parent_version)
        base_doc = decode_document(base.body)
        mapping: list[tuple[str, Any] | None] = [None] * version.body_length

        def mark(start: int, end: int, payload: tuple[str, Any]) -> None:
            for p in range(start, end):
                if mapping[p] is not None:
                    raise TapeError(f"internal: pitch {p} provenance assigned twice")
                mapping[p] = payload

        # Verbatim copied character runs.
        for old_char_0, _old_char_1, new_byte_0, new_byte_1 in batch.kept_runs:
            base_start = int(base_doc.starts[old_char_0])
            for p in range(new_byte_0, new_byte_1):
                base_pitch = base_start + (p - new_byte_0)
                mark(p, p + 1, ("kept", base_pitch))

        # Boundary-inserted zero-character switches.
        for adjustment in version.boundary_adjustments:
            raw_len = len(adjustment.raw)
            mark(
                adjustment.new_byte_offset,
                adjustment.new_byte_offset + raw_len,
                ("boundary", adjustment),
            )

        # Encoded replacement payloads (the leading splice switch, if any,
        # is recorded separately as a boundary adjustment).
        for record in batch.records:
            if record.kind != "replace":
                continue
            for p in range(record.new_byte_start, record.new_byte_end):
                mark(
                    p,
                    p + 1,
                    ("new_text", record, p - record.new_byte_start),
                )

        unresolved = [p for p, item in enumerate(mapping) if item is None]
        if unresolved:
            raise TapeError(
                f"internal: pitches without provenance: {unresolved[:8]}"
            )

        origins_list: list[TerminalOrigin] = []
        for p, item in enumerate(mapping):
            kind = item[0]
            if kind == "kept":
                base_pitch = item[1]
                parent_origin = parent_spectrum.origins[base_pitch]
                via = ((version.parent_version, base_pitch),) + parent_origin.via
                origins_list.append(
                    TerminalOrigin(
                        kind=parent_origin.kind,
                        version_id=parent_origin.version_id,
                        pitch=parent_origin.pitch,
                        batch_id=parent_origin.batch_id,
                        role=parent_origin.role,
                        from_mode=parent_origin.from_mode,
                        to_mode=parent_origin.to_mode,
                        old_char_start=parent_origin.old_char_start,
                        old_char_end=parent_origin.old_char_end,
                        inserted_text=parent_origin.inserted_text,
                        inner_byte=parent_origin.inner_byte,
                        via=via,
                    )
                )
            elif kind == "boundary":
                adjustment: BoundaryAdjustment = item[1]
                origins_list.append(
                    TerminalOrigin(
                        kind="boundary",
                        version_id=version_id,
                        pitch=p,
                        batch_id=batch.batch_id,
                        role=adjustment.role,
                        from_mode=adjustment.from_mode,
                        to_mode=adjustment.to_mode,
                    )
                )
            else:
                record: PatchRecord = item[1]
                inner_byte = item[2]
                origins_list.append(
                    TerminalOrigin(
                        kind="new_text",
                        version_id=version_id,
                        pitch=p,
                        batch_id=batch.batch_id,
                        old_char_start=record.old_char_start,
                        old_char_end=record.old_char_end,
                        inserted_text=record.inserted_text,
                        inner_byte=inner_byte,
                    )
                )
        origins = tuple(origins_list)

    spectrum = HoleSpectrum(
        version_id=version_id,
        body=bytes(version.body),
        body_sha256=version.body_sha256,
        text=version.text,
        final_mode=version.final_mode,
        segments=segments,
        pitch_segment=pitch_segment,
        origins=origins,
    )
    cache[version_id] = spectrum
    return spectrum


# ---------------------------------------------------------------------------
# Blanks, orders and placements


@dataclass(frozen=True)
class TapeBlank:
    """A physical partly punched tape blank on the shelf."""

    blank_id: str
    pitches: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.blank_id, str) or not self.blank_id:
            raise TapeError("blank id must be a non-empty string")
        if not isinstance(self.pitches, (bytes, bytearray)):
            raise TapeError("blank pitches must be bytes")
        if not 1 <= len(self.pitches) <= MAX_PITCHES:
            raise TapeError(
                f"blank {self.blank_id!r} length {len(self.pitches)} outside 1..{MAX_PITCHES}"
            )

    @property
    def length(self) -> int:
        return len(self.pitches)

    def holes(self) -> tuple[tuple[int, int], ...]:
        """All existing holes as ``(blank pitch, blank channel)`` pairs."""
        return tuple(
            (p, c)
            for p, value in enumerate(self.pitches)
            for c in holes_of_byte(value)
        )


@dataclass(frozen=True)
class TapeOrder:
    """One continuous target spectrum plus its mounting-direction policy."""

    order_id: str
    spectrum: HoleSpectrum
    allow_reverse: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.order_id, str) or not self.order_id:
            raise TapeError("order id must be a non-empty string")
        if not isinstance(self.spectrum, HoleSpectrum):
            raise TapeError("order target must be a HoleSpectrum")
        if not 1 <= self.spectrum.length <= MAX_PITCHES:
            raise TapeError(
                f"order {self.order_id!r} spectrum length {self.spectrum.length} "
                f"outside 1..{MAX_PITCHES}"
            )

    @property
    def length(self) -> int:
        return self.spectrum.length


@dataclass(frozen=True)
class Placement:
    """A feasible way to cut and mount one blank for one order.

    Punch and existing-hole coordinates are always the blank's original
    coordinates, so the punch machine can drive to them directly and its
    returned coordinates can be mapped back without re-deriving geometry.
    """

    orientation: str
    window_start: int
    added_holes: int
    scrap: int
    punches: tuple[tuple[int, int], ...]
    existing_holes: tuple[tuple[int, int], ...]


def _view_byte(blank: TapeBlank, orientation: str, window_start: int, target_pitch: int, length: int) -> int:
    """Byte the blank presents at target pitch after mounting."""
    if orientation == ORIENT_FORWARD:
        return blank.pitches[window_start + target_pitch]
    # Flip mounting: reverse pitch order and reverse channel order.
    blank_pitch = window_start + (length - 1 - target_pitch)
    return reverse_byte(blank.pitches[blank_pitch])


def _to_blank_coords(
    orientation: str, window_start: int, target_pitch: int, length: int, target_channel: int
) -> tuple[int, int]:
    if orientation == ORIENT_FORWARD:
        return window_start + target_pitch, target_channel
    blank_pitch = window_start + (length - 1 - target_pitch)
    return blank_pitch, 7 - target_channel


def enumerate_placements(blank: TapeBlank, order: TapeOrder) -> tuple[Placement, ...]:
    """Every feasible window/orientation, best first.

    Ordering is lexicographic on (added holes, scrap, orientation, window
    start); scrap is constant for a blank/order pair but is carried for
    the batch-level objective.
    """
    length = order.length
    if blank.length < length:
        return ()
    target = order.spectrum.body
    orientations = (
        (ORIENT_FORWARD, ORIENT_REVERSE) if order.allow_reverse else (ORIENT_FORWARD,)
    )
    placements: list[Placement] = []
    scrap = blank.length - length
    for orientation in orientations:
        for window_start in range(blank.length - length + 1):
            punches: list[tuple[int, int]] = []
            existing: list[tuple[int, int]] = []
            feasible = True
            for target_pitch in range(length):
                view = _view_byte(blank, orientation, window_start, target_pitch, length)
                wanted = target[target_pitch]
                if view & ~wanted:
                    feasible = False
                    break
                for channel in range(8):
                    if view & (1 << channel):
                        bp, bc = _to_blank_coords(
                            orientation, window_start, target_pitch, length, channel
                        )
                        existing.append((bp, bc))
                    elif wanted & (1 << channel):
                        bp, bc = _to_blank_coords(
                            orientation, window_start, target_pitch, length, channel
                        )
                        punches.append((bp, bc))
            if feasible:
                placements.append(
                    Placement(
                        orientation=orientation,
                        window_start=window_start,
                        added_holes=len(punches),
                        scrap=scrap,
                        punches=tuple(sorted(punches)),
                        existing_holes=tuple(sorted(existing)),
                    )
                )
    placements.sort(
        key=lambda pl: (
            pl.added_holes,
            pl.scrap,
            0 if pl.orientation == ORIENT_FORWARD else 1,
            pl.window_start,
        )
    )
    return tuple(placements)


def best_placement(blank: TapeBlank, order: TapeOrder) -> Placement | None:
    placements = enumerate_placements(blank, order)
    return placements[0] if placements else None


# ---------------------------------------------------------------------------
# Exact batch optimisation (min-cost max-flow)


class _FlowEdge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: int) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


def _min_cost_max_flow(
    graph: list[list[_FlowEdge]], source: int, sink: int
) -> tuple[int, int]:
    """Successive shortest augmenting path with Dijkstra and potentials."""
    n = len(graph)
    potential = [0] * n
    flow = 0
    cost = 0
    inf = 10**30
    while True:
        dist = [inf] * n
        prev_node = [-1] * n
        prev_edge = [-1] * n
        dist[source] = 0
        heap: list[tuple[int, int]] = [(0, source)]
        while heap:
            d, node = heapq.heappop(heap)
            if d != dist[node]:
                continue
            for edge_index, edge in enumerate(graph[node]):
                if edge.cap <= 0:
                    continue
                reduced = edge.cost + potential[node] - potential[edge.to]
                nd = d + reduced
                if nd < dist[edge.to]:
                    dist[edge.to] = nd
                    prev_node[edge.to] = node
                    prev_edge[edge.to] = edge_index
                    heapq.heappush(heap, (nd, edge.to))
        if dist[sink] == inf:
            return flow, cost
        for node in range(n):
            if dist[node] < inf:
                potential[node] += dist[node]
        # Every edge has capacity 1; one unit per augmentation.
        node = sink
        path_cost = 0
        while node != source:
            prev = prev_node[node]
            edge = graph[prev][prev_edge[node]]
            path_cost += edge.cost
            edge.cap -= 1
            graph[node][edge.rev].cap += 1
            node = prev
        flow += 1
        cost += path_cost


@dataclass(frozen=True)
class Allocation:
    """Where one order gets its physical tape."""

    order: TapeOrder
    blank: TapeBlank | None
    placement: Placement | None

    @property
    def uses_new_tape(self) -> bool:
        return self.blank is None

    @property
    def punch_count(self) -> int:
        if self.placement is None:
            return sum(
                bin(self.order.spectrum.body[p]).count("1")
                for p in range(self.order.length)
            )
        return self.placement.added_holes

    @property
    def scrap(self) -> int:
        return self.placement.scrap if self.placement is not None else 0


@dataclass(frozen=True)
class TargetLocation:
    """A machine-returned (pitch, channel) mapped back to the version."""

    target_pitch: int
    target_channel: int
    byte: int
    segment: Segment
    segment_inner_offset: int
    origin: TerminalOrigin


@dataclass(frozen=True)
class MachineOrder:
    """The self-contained punch card delivered for one physical tape."""

    order_id: str
    spectrum: HoleSpectrum
    allow_reverse: bool
    allocation: Allocation

    # -- coordinate mapping ----------------------------------------------

    def locate_returned(self, blank_pitch: int, blank_channel: int) -> TargetLocation:
        """Map a machine-returned blank coordinate back onto the version."""
        if not 0 <= blank_channel <= 7:
            raise TapeError(f"channel {blank_channel} out of range")
        length = self.spectrum.length
        placement = self.allocation.placement
        if placement is None:
            target_pitch, target_channel = blank_pitch, blank_channel
        elif placement.orientation == ORIENT_FORWARD:
            target_pitch = blank_pitch - placement.window_start
            target_channel = blank_channel
        else:
            target_pitch = placement.window_start + (length - 1) - blank_pitch
            target_channel = 7 - blank_channel
        if not 0 <= target_pitch < length or not 0 <= target_channel <= 7:
            raise TapeError(
                f"returned coordinate ({blank_pitch},{blank_channel}) is outside "
                f"the mounted window"
            )
        segment = self.spectrum.segment_at(target_pitch)
        return TargetLocation(
            target_pitch=target_pitch,
            target_channel=target_channel,
            byte=self.spectrum.body[target_pitch],
            segment=segment,
            segment_inner_offset=target_pitch - segment.start,
            origin=self.spectrum.origin_at(target_pitch),
        )

    # -- serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        placement = self.allocation.placement
        blank = self.allocation.blank
        if placement is None or blank is None:
            material: dict[str, Any] = {"type": "new"}
            existing: list[list[int]] = []
            punches = [
                [p, c]
                for p in range(self.spectrum.length)
                for c in holes_of_byte(self.spectrum.body[p])
            ]
        else:
            material = {
                "type": "old",
                "blank_id": blank.blank_id,
                "orientation": placement.orientation,
                "window_start": placement.window_start,
                "window_end": placement.window_start + self.spectrum.length,
                "blank_length": blank.length,
                "scrap_pitches": placement.scrap,
                "added_holes": placement.added_holes,
            }
            existing = [list(hole) for hole in placement.existing_holes]
            punches = [list(hole) for hole in placement.punches]
        return {
            "format": ORDER_FORMAT,
            "order_id": self.order_id,
            "version_id": self.spectrum.version_id,
            "body_sha256": self.spectrum.body_sha256,
            "body_hex": self.spectrum.body.hex(),
            "length": self.spectrum.length,
            "allow_reverse": self.allow_reverse,
            "material": material,
            "existing_holes": existing,
            "punches": punches,
            "target_holes": [
                [p, holes_of_byte(self.spectrum.body[p])]
                for p in range(self.spectrum.length)
            ],
        }

    def dump(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class BatchPlan:
    """The optimised material decision for one batch of orders."""

    orders: tuple[TapeOrder, ...]
    blanks: tuple[TapeBlank, ...]
    allocations: tuple[Allocation, ...]
    flow_cost: int

    def allocation_for(self, order_id: str) -> Allocation:
        for allocation in self.allocations:
            if allocation.order.order_id == order_id:
                return allocation
        raise TapeError(f"unknown order {order_id!r}")

    @property
    def new_tape_count(self) -> int:
        return sum(1 for a in self.allocations if a.uses_new_tape)

    @property
    def reused_blank_count(self) -> int:
        return sum(1 for a in self.allocations if not a.uses_new_tape)

    @property
    def total_punches(self) -> int:
        return sum(a.punch_count for a in self.allocations)

    @property
    def total_scrap(self) -> int:
        return sum(a.scrap for a in self.allocations)

    @property
    def objective(self) -> tuple[int, int, int]:
        return (self.new_tape_count, self.total_punches, self.total_scrap)

    def machine_orders(self) -> tuple[MachineOrder, ...]:
        return tuple(MachineOrder(a.order.order_id, a.order.spectrum, a.order.allow_reverse, a) for a in self.allocations)

    def summary(self) -> str:
        lines = [
            f"orders={len(self.orders)} blanks={len(self.blanks)} "
            f"new_tapes={self.new_tape_count} reused_blanks={self.reused_blank_count} "
            f"total_punches={self.total_punches} scrapped_pitches={self.total_scrap}"
        ]
        for allocation in self.allocations:
            order = allocation.order
            if allocation.uses_new_tape:
                lines.append(f"  {order.order_id}: NEW tape ({allocation.punch_count} punches)")
            else:
                placement = allocation.placement
                lines.append(
                    f"  {order.order_id}: blank {allocation.blank.blank_id} "
                    f"{placement.orientation} window[{placement.window_start},"
                    f"{placement.window_start + order.length}) "
                    f"+{placement.added_holes} holes scrap={placement.scrap}"
                )
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": {
                "new_tapes": self.new_tape_count,
                "total_punches": self.total_punches,
                "scrapped_pitches": self.total_scrap,
            },
            "allocations": [MachineOrder(a.order.order_id, a.order.spectrum, a.order.allow_reverse, a).to_dict() for a in self.allocations],
        }


def _validate_batch(orders: Sequence[TapeOrder], blanks: Sequence[TapeBlank]) -> None:
    if not orders:
        raise TapeError("batch has no orders")
    if len(orders) > MAX_ORDERS:
        raise TapeError(f"at most {MAX_ORDERS} orders per batch, got {len(orders)}")
    if len(blanks) > MAX_BLANKS:
        raise TapeError(f"at most {MAX_BLANKS} blanks per batch, got {len(blanks)}")
    order_ids = [o.order_id for o in orders]
    if len(set(order_ids)) != len(order_ids):
        raise TapeError("duplicate order ids")
    blank_ids = [b.blank_id for b in blanks]
    if len(set(blank_ids)) != len(blank_ids):
        raise TapeError("duplicate blank ids")


def plan_batch(orders: Sequence[TapeOrder], blanks: Sequence[TapeBlank]) -> BatchPlan:
    """Choose material for a whole batch, optimising the three objectives.

    The function is a pure function of the already-built spectra and the
    shelf blanks: swapping the blanks and calling again never decodes a
    version.
    """
    _validate_batch(orders, blanks)
    order_count = len(orders)
    blank_count = len(blanks)

    # One best placement per feasible (order, blank) pair.
    choices: dict[tuple[int, int], Placement] = {}
    for i, order in enumerate(orders):
        for j, blank in enumerate(blanks):
            placement = best_placement(blank, order)
            if placement is not None:
                choices[(i, j)] = placement

    source = 0
    order_base = 1
    blank_base = order_base + order_count
    sink = blank_base + blank_count
    graph: list[list[_FlowEdge]] = [[] for _ in range(sink + 1)]

    def add_edge(frm: int, to: int, cap: int, cost: int) -> None:
        forward = _FlowEdge(to, len(graph[to]), cap, cost)
        backward = _FlowEdge(frm, len(graph[frm]), 0, -cost)
        graph[frm].append(forward)
        graph[to].append(backward)

    for i in range(order_count):
        add_edge(source, order_base + i, 1, 0)
    for j in range(blank_count):
        add_edge(blank_base + j, sink, 1, 0)
    for (i, j), placement in choices.items():
        cost = _PUNCH_WEIGHT * placement.added_holes + placement.scrap
        add_edge(order_base + i, blank_base + j, 1, cost)

    flow, flow_cost = _min_cost_max_flow(graph, source, sink)

    used_blank: dict[int, int] = {}
    for i in range(order_count):
        for edge in graph[order_base + i]:
            if blank_base <= edge.to < sink and edge.cap == 0:
                j = edge.to - blank_base
                used_blank[i] = j

    allocations: list[Allocation] = []
    for i, order in enumerate(orders):
        if i in used_blank:
            j = used_blank[i]
            blank = blanks[j]
            allocations.append(Allocation(order, blank, choices[(i, j)]))
        else:
            allocations.append(Allocation(order, None, None))

    return BatchPlan(
        orders=tuple(orders),
        blanks=tuple(blanks),
        allocations=tuple(allocations),
        flow_cost=flow_cost,
    )


# ---------------------------------------------------------------------------
# Persistent planner: spectra survive stock changes


class MaterialPlanner:
    """Caches pinned spectra so re-stocking never triggers a re-decode."""

    def __init__(self, library: RevisionLibrary) -> None:
        self._library = library
        self._cache: dict[int, HoleSpectrum] = {}

    @property
    def library(self) -> RevisionLibrary:
        return self._library

    def spectrum(self, version_id: int) -> HoleSpectrum:
        if version_id not in self._cache:
            self._cache[version_id] = build_spectrum(
                self._library, version_id, self._cache
            )
        return self._cache[version_id]

    def make_order(
        self, order_id: str, version_id: int, allow_reverse: bool = False
    ) -> TapeOrder:
        return TapeOrder(order_id, self.spectrum(version_id), allow_reverse)

    def plan(
        self, orders: Sequence[TapeOrder], blanks: Sequence[TapeBlank]
    ) -> BatchPlan:
        return plan_batch(orders, blanks)
