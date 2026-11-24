# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Erik Nijenhuis
r"""Create a vertical LilyPond score video with a moving system highlight.

The script combines:
  * LilyPond SVG pages rendered with point-and-click restricted to note-event;
  * the note-event log written by ly2video-events.ily during the same run;
  * a MIDI file from the same score, for its tempo map;
  * an MP3/WAV (or other FFmpeg-readable audio file) for the soundtrack.

It does NOT modify the canonical LilyPond source.  Add the logger from a wrapper
file that sets the page size and then includes the real score:

    % mobile.ly
    \paper { paper-width = 90\mm  paper-height = 160\mm }
    \include "ly2video-events.ily"
    \include "my-score.ly"

    lilypond -I "$(ly2video --include-path)" \
        -fsvg -dpoint-and-click=note-event --output=mobile mobile.ly

    ly2video \
        --svg 'mobile*.svg' \
        --midi mobile.midi \
        --note-events mobile-events.tsv \
        --audio mobile.wav \
        -o mobile.mp4

``ly2video --include-path`` prints the directory that holds
ly2video-events.ily, for LilyPond's -I.

Dependencies:
    pip install .            (pulls in mido and cairosvg)
    sudo apt install ffmpeg

CairoSVG is optional if either rsvg-convert or inkscape is installed.

Showing only the systems that are filmed:
    --stack-systems throws away everything the chosen score does not print --
    other scores, prose, page numbers -- and repacks the systems that remain
    onto fresh screens of the original page size.

Files with more than one printed score:
    A file may print worked examples of single sections next to the full piece,
    while its \midi block is one performance.  The event log numbers the printed
    scores, and ly2video follows the one that spans the whole MIDI, or the one
    named by --score.  A section printed with \repeat volta cannot be followed
    against an \unfoldRepeats MIDI -- it prints once and sounds twice -- and the
    script says so rather than drifting.

How the highlight is synchronized:
    With --note-events every printed notehead is paired with the exact score
    moment LilyPond gave it, and that moment is converted to seconds through the
    MIDI tempo map.  Nothing is inferred from geometry or from counting, so the
    two cases that break naive approaches are handled by construction:

      * a tie prints a second notehead that MIDI never re-attacks;
      * LilyPond shifts one of two colliding noteheads sideways, so "same beat"
        and "same x coordinate" are not the same thing.

    Without --note-events the script falls back to clustering noteheads by x and
    pairing those columns with unique MIDI note-on timestamps in order.  That
    only works for scores with neither of the above, and the script refuses to
    render -- rather than guess -- when the counts disagree.  --events remains
    available to supply a hand-made visual-ordinal -> seconds table.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NoReturn, Sequence

from . import __version__

try:
    import mido
except ImportError:  # handled later with a friendlier message
    mido = None

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

TRANSLATE_RE = re.compile(
    r"translate\(\s*([-+0-9.eE]+)(?:[\s,]+([-+0-9.eE]+))?\s*\)"
)
VIEWBOX_RE = re.compile(r"\s+|,")
TEXTEDIT_RE = re.compile(r"^textedit://")
TEXTEDIT_REF_RE = re.compile(r"^textedit://(.*):(\d+):(\d+):(\d+)$")
PAGE_NUMBER_RE = re.compile(r"(?:^|[-_])(\d+)\.svg$", re.IGNORECASE)
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


@dataclass(frozen=True)
class StaffBlock:
    """One printed staff: a run of long horizontal lines at regular spacing."""

    index: int
    y0: float
    y1: float
    x0: float
    x1: float
    line_count: int

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(frozen=True)
class System:
    """A logical system: the staff blocks joined by one system start bar.

    A Staff plus the TabStaff underneath it are two printed blocks but a single
    logical system, because the highlight has to sweep across both together.
    """

    index: int
    y0: float
    y1: float
    x0: float
    x1: float
    members: tuple[StaffBlock, ...] = ()

    @property
    def center_y(self) -> float:
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True)
class SourceRef:
    """A LilyPond input coordinate, as used by point-and-click links.

    The same coordinate identifies a note in the SVG (``textedit://`` href) and
    in the event log written by ``ly2video-events.ily``.  Only the file's base
    name is compared, because LilyPond writes absolute paths into the SVG but
    the include path into the event log.
    """

    file: str
    line: int
    char: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.char}"


@dataclass(frozen=True)
class Anchor:
    """A single point-and-click link in the SVG, i.e. one printed notehead."""

    ref: SourceRef
    x: float
    y: float


@dataclass(frozen=True)
class NoteEvent:
    """One note-event as LilyPond interpreted it, with its score time."""

    score: int
    ref: SourceRef
    moment: float
    pitch: int


@dataclass(frozen=True)
class MusicalOnset:
    """Every note-event that one \\score places at the same score time."""

    score: int
    moment: float
    refs: tuple[SourceRef, ...]


@dataclass(frozen=True)
class VisualOnset:
    ordinal: int
    page_index: int
    page_path: Path
    system_index: int
    x: float
    y0: float
    y1: float
    source_refs: tuple[SourceRef, ...]
    moment: float | None = None
    score: int | None = None


@dataclass(frozen=True)
class TimedOnset:
    seconds: float
    note_count: int


@dataclass(frozen=True)
class EventMapEntry:
    """Exact timing entry keyed by a visual onset ordinal.

    JSON format accepted by --events:

      [
        {"visual": 0, "seconds": 0.0},
        {"visual": 1, "seconds": 0.428571},
        ...
      ]

    CSV/TSV format:

      visual,seconds
      0,0.0
      1,0.428571

    This intentionally maps the already-extracted visual onset sequence rather
    than source line numbers.  A LilyPond event logger can therefore emit exact
    moments and a later mapper can resolve those to ordinals without changing
    this video renderer.
    """

    visual: int
    seconds: float


@dataclass(frozen=True)
class SvgPage:
    index: int
    path: Path
    text: str
    view_x: float
    view_y: float
    view_w: float
    view_h: float
    blocks: tuple[StaffBlock, ...]
    systems: tuple[System, ...]
    anchors: tuple[Anchor, ...]


def die(message: str, code: int = 2) -> NoReturn:
    print(f"ly2video: error: {message}", file=sys.stderr)
    raise SystemExit(code)


def info(message: str) -> None:
    print(f"[ly2video] {message}", file=sys.stderr)


def natural_page_key(path: Path) -> tuple[int, str]:
    m = PAGE_NUMBER_RE.search(path.name)
    if m:
        return (int(m.group(1)), path.name)
    numbers = re.findall(r"\d+", path.stem)
    return (int(numbers[-1]) if numbers else 0, path.name)


def parse_viewbox(root: ET.Element, path: Path) -> tuple[float, float, float, float]:
    value = root.get("viewBox")
    if not value:
        die(f"{path}: SVG has no viewBox")
    parts = [p for p in VIEWBOX_RE.split(value.strip()) if p]
    if len(parts) != 4:
        die(f"{path}: invalid viewBox {value!r}")
    return tuple(map(float, parts))  # type: ignore[return-value]


def translate_of(element: ET.Element) -> tuple[float, float] | None:
    transform = element.get("transform", "")
    m = TRANSLATE_RE.search(transform)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2) or 0.0)


def parse_staff_blocks(root: ET.Element, view_w: float) -> list[StaffBlock]:
    """Find groups of long horizontal staff/tab lines.

    LilyPond emits each staff line as a <line> inside a translated <g>.
    We cluster y coordinates whose spacing is <= 2.2 SVG units.  This
    identifies 5-line Staff and 4-line TabStaff blocks.
    """

    lines: list[tuple[float, float, float]] = []  # y, x0, x1
    min_len = view_w * 0.45

    for g in root.iter(f"{{{SVG_NS}}}g"):
        tr = translate_of(g)
        if tr is None:
            continue
        tx, ty = tr
        for line in g.findall(f"{{{SVG_NS}}}line"):
            try:
                x1 = float(line.get("x1", "0"))
                x2 = float(line.get("x2", "0"))
                y1 = float(line.get("y1", "0"))
                y2 = float(line.get("y2", "0"))
            except ValueError:
                continue
            if abs(y1 - y2) > 1e-6 or abs(x2 - x1) < min_len:
                continue
            lines.append((ty + y1, tx + min(x1, x2), tx + max(x1, x2)))

    if not lines:
        return []

    # Collapse duplicates at almost the same y coordinate.
    lines.sort(key=lambda item: item[0])
    unique: list[tuple[float, float, float]] = []
    for y, x0, x1 in lines:
        if unique and abs(y - unique[-1][0]) < 0.05:
            py, px0, px1 = unique[-1]
            unique[-1] = ((py + y) / 2.0, min(px0, x0), max(px1, x1))
        else:
            unique.append((y, x0, x1))

    groups: list[list[tuple[float, float, float]]] = []
    current: list[tuple[float, float, float]] = []
    for item in unique:
        if current and item[0] - current[-1][0] > 2.2:
            if len(current) >= 4:
                groups.append(current)
            current = []
        current.append(item)
    if len(current) >= 4:
        groups.append(current)

    result: list[StaffBlock] = []
    for group in groups:
        result.append(
            StaffBlock(
                index=len(result),
                y0=min(v[0] for v in group),
                y1=max(v[0] for v in group),
                x0=min(v[1] for v in group),
                x1=max(v[2] for v in group),
                line_count=len(group),
            )
        )
    return result


def first_translated_descendant(anchor: ET.Element) -> tuple[float, float] | None:
    """Return the first translated graphical child of a point-and-click anchor."""
    for elem in anchor.iter():
        if elem is anchor:
            continue
        tr = translate_of(elem)
        if tr is not None:
            return tr
    return None


PATH_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
SCALE_RE = re.compile(r"scale\(\s*([-+0-9.eE]+)(?:[\s,]+([-+0-9.eE]+))?\s*\)")


def path_x_extent(d: str) -> tuple[float, float] | None:
    """Horizontal extent of an SVG path, from its points and control points.

    LilyPond writes glyphs such as noteheads as paths with M/L/C/Z commands,
    absolute or relative.  The control polygon encloses the curve, and for the
    symmetric notehead glyphs its middle is the middle of the glyph.
    """
    tokens = PATH_TOKEN_RE.findall(d)
    xs: list[float] = []
    cx = cy = sx = sy = 0.0
    cmd = ""
    i = 0
    sizes = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4}
    while i < len(tokens):
        t = tokens[i]
        if t.isalpha():
            cmd = t
            i += 1
            if cmd in "Zz":
                cx, cy = sx, sy
            continue
        up = cmd.upper()
        n = sizes.get(up)
        if n is None or i + n > len(tokens):
            break
        try:
            v = [float(x) for x in tokens[i:i + n]]
        except ValueError:
            break
        i += n
        rel = cmd.islower()
        if up == "H":
            pts = [(v[0] + (cx if rel else 0.0), cy)]
        elif up == "V":
            pts = [(cx, v[0] + (cy if rel else 0.0))]
        else:
            pts = [
                (v[k] + (cx if rel else 0.0), v[k + 1] + (cy if rel else 0.0))
                for k in range(0, n, 2)
            ]
        xs.extend(p[0] for p in pts)
        cx, cy = pts[-1]
        if up == "M":
            sx, sy = cx, cy
            cmd = "l" if rel else "L"   # implicit lineto after moveto
    if not xs:
        return None
    return min(xs), max(xs)


def glyph_center_dx(anchor: ET.Element) -> float:
    """Offset from a notehead's origin to its horizontal middle.

    LilyPond places a notehead glyph with its origin at the left edge, so the
    translate() of the link is the left side of the notehead, not its middle.
    """
    for elem in anchor.iter():
        if not elem.tag.endswith("path"):
            continue
        ext = path_x_extent(elem.get("d", ""))
        if ext is None:
            continue
        sx = 1.0
        m = SCALE_RE.search(elem.get("transform", ""))
        if m:
            sx = float(m.group(1))
        return sx * (ext[0] + ext[1]) / 2.0
    return 0.0


def parse_source_ref(href: str) -> SourceRef | None:
    m = TEXTEDIT_REF_RE.match(href)
    if not m:
        return None
    return SourceRef(
        file=os.path.basename(m.group(1)),
        line=int(m.group(2)),
        char=int(m.group(3)),
    )


def parse_anchors(root: ET.Element) -> list[Anchor]:
    anchors: list[Anchor] = []
    href_keys = (f"{{{XLINK_NS}}}href", "href")

    for a in root.iter(f"{{{SVG_NS}}}a"):
        href = None
        for key in href_keys:
            href = a.get(key)
            if href:
                break
        if not href or not TEXTEDIT_RE.match(href):
            continue
        ref = parse_source_ref(href)
        if ref is None:
            continue
        pos = first_translated_descendant(a)
        if pos is None:
            continue
        # The highlight is centred on x, so use the middle of the notehead.
        anchors.append(Anchor(ref=ref, x=pos[0] + glyph_center_dx(a), y=pos[1]))

    return anchors


def block_distance(block: StaffBlock, y: float) -> float:
    """Vertical distance from y to a staff block, 0 inside it.

    Measuring against the span rather than the centre tolerates ledger lines and
    noteheads printed above or below the actual staff lines.
    """
    if block.y0 <= y <= block.y1:
        return 0.0
    return min(abs(y - block.y0), abs(y - block.y1))


def parse_system_start_bars(root: ET.Element) -> list[tuple[float, float, float]]:
    """Return ``(x, y0, y1)`` of every vertical bar that could join staves.

    LilyPond draws a SystemStartBar down the left edge of every system that has
    more than one staff.  It comes out as a narrow, tall <rect> in a translated
    <g>; stems and barlines do too, so the caller has to look at x.
    """
    bars: list[tuple[float, float, float]] = []
    for g in root.iter(f"{{{SVG_NS}}}g"):
        tr = translate_of(g)
        if tr is None:
            continue
        tx, ty = tr
        for rect in g.findall(f"{{{SVG_NS}}}rect"):
            try:
                x = float(rect.get("x", "0"))
                y = float(rect.get("y", "0"))
                width = float(rect.get("width", "0"))
                height = float(rect.get("height", "0"))
            except ValueError:
                continue
            if height > 3.0 * width:
                bars.append((tx + x, ty + y, ty + y + height))
    return bars


MAX_START_BAR_DX = 0.5
"""How far (SVG units) a system start bar may be from the left end of a staff."""


def group_blocks_into_systems(
    blocks: Sequence[StaffBlock], bars: Sequence[tuple[float, float, float]]
) -> list[System]:
    """Group the printed staff blocks of one system.

    Two adjacent staves belong together when a system start bar at their left
    edge runs from one to the other.  Comparing the music on the staves instead
    cannot tell a Staff and its TabStaff from two consecutive systems of an
    unfolded repeat, and cannot join staves that print different music.
    """
    if len(blocks) < 2:
        return [
            System(index=0, y0=b.y0, y1=b.y1, x0=b.x0, x1=b.x1, members=(b,))
            for b in blocks
        ]

    def joined(a: StaffBlock, b: StaffBlock) -> bool:
        # The bar must reach past the last line of the upper staff and the first
        # line of the lower one.
        left = min(a.x0, b.x0)
        return any(
            abs(x - left) <= MAX_START_BAR_DX and y0 < a.y1 and y1 > b.y0
            for x, y0, y1 in bars
        )

    systems: list[System] = []
    members: list[StaffBlock] = [blocks[0]]
    for block in blocks[1:]:
        if joined(members[-1], block):
            members.append(block)
            continue
        systems.append(
            System(
                index=len(systems),
                y0=min(b.y0 for b in members),
                y1=max(b.y1 for b in members),
                x0=min(b.x0 for b in members),
                x1=max(b.x1 for b in members),
                members=tuple(members),
            )
        )
        members = [block]

    systems.append(
        System(
            index=len(systems),
            y0=min(b.y0 for b in members),
            y1=max(b.y1 for b in members),
            x0=min(b.x0 for b in members),
            x1=max(b.x1 for b in members),
            members=tuple(members),
        )
    )
    return systems


def parse_svg_page(path: Path, index: int) -> SvgPage:
    text = path.read_text(encoding="utf-8")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        die(f"{path}: invalid SVG: {exc}")

    view_x, view_y, view_w, view_h = parse_viewbox(root, path)
    blocks = parse_staff_blocks(root, view_w)
    anchors = parse_anchors(root)

    if not blocks:
        die(f"{path}: could not detect any staff systems")
    if not anchors:
        die(
            f"{path}: no textedit:// note links found; render LilyPond with "
            "-dpoint-and-click=note-event"
        )

    systems = group_blocks_into_systems(blocks, parse_system_start_bars(root))

    return SvgPage(
        index=index,
        path=path,
        text=text,
        view_x=view_x,
        view_y=view_y,
        view_w=view_w,
        view_h=view_h,
        blocks=tuple(blocks),
        systems=tuple(systems),
        anchors=tuple(anchors),
    )


def assign_anchor_to_system(anchor: Anchor, systems: Sequence[System]) -> System:
    def distance(system: System) -> float:
        if system.y0 <= anchor.y <= system.y1:
            return 0.0
        return min(abs(anchor.y - system.y0), abs(anchor.y - system.y1))

    return min(systems, key=distance)


def anchors_by_system(page: SvgPage) -> dict[int, list[Anchor]]:
    """Group a page's noteheads by logical system, dropping stray links."""
    result: dict[int, list[Anchor]] = {s.index: [] for s in page.systems}
    for anchor in page.anchors:
        system = assign_anchor_to_system(anchor, page.systems)
        # Ignore a point-and-click object if it is implausibly far from the
        # detected system.  This filters title/header artefacts if present.
        if anchor.y < system.y0 - 6.0 or anchor.y > system.y1 + 6.0:
            continue
        result[system.index].append(anchor)
    for values in result.values():
        values.sort(key=lambda a: a.x)
    return result


def cluster_x(
    anchors: Sequence[Anchor], tolerance: float
) -> list[tuple[float, tuple[SourceRef, ...]]]:
    """Cluster note anchors into simultaneous visual columns by x coordinate.

    This is the approximation used when no LilyPond event log is available.  It
    cannot see that LilyPond shifts one of two colliding noteheads sideways, so
    prefer --note-events whenever the score is available.
    """
    if not anchors:
        return []

    items = sorted(anchors, key=lambda a: a.x)
    clusters: list[list[Anchor]] = [[items[0]]]

    for anchor in items[1:]:
        current = clusters[-1]
        center = sum(a.x for a in current) / len(current)
        if abs(anchor.x - center) <= tolerance:
            current.append(anchor)
        else:
            clusters.append([anchor])

    result: list[tuple[float, tuple[SourceRef, ...]]] = []
    for cluster in clusters:
        # Duplicate note-event links occur because the same note is represented
        # both on Staff and TabStaff.  Preserve one source reference per note.
        refs = tuple(sorted({a.ref for a in cluster}, key=lambda r: (r.line, r.char)))
        xs = sorted(a.x for a in cluster)
        x = xs[len(xs) // 2]
        result.append((x, refs))
    return result


def extract_visual_onsets(
    pages: Sequence[SvgPage], x_tolerance: float, vertical_padding: float
) -> list[VisualOnset]:
    result: list[VisualOnset] = []

    for page in pages:
        if len(page.systems) != len(page.blocks):
            info(
                f"{page.path.name}: merged {len(page.blocks)} printed staff block(s) "
                f"into {len(page.systems)} logical system(s)"
            )

        by_system = anchors_by_system(page)

        page_count_before = len(result)
        for system in page.systems:
            clusters = cluster_x(by_system[system.index], x_tolerance)
            for x, refs in clusters:
                # Ignore left-margin objects; note columns should fall inside or
                # very near the horizontal extent of the staff.
                if x < system.x0 - 1.0 or x > system.x1 + 1.0:
                    continue
                result.append(
                    VisualOnset(
                        ordinal=len(result),
                        page_index=page.index,
                        page_path=page.path,
                        system_index=system.index,
                        x=x,
                        y0=system.y0 - vertical_padding,
                        y1=system.y1 + vertical_padding,
                        source_refs=refs,
                    )
                )

        info(
            f"{page.path.name}: {len(result) - page_count_before} visual onset column(s) "
            f"across {len(page.systems)} logical system(s)"
        )

    return result


def read_note_events(path: Path) -> list[NoteEvent]:
    """Read the TSV written by ``ly2video-events.ily``.

    Columns: score, file, line, char, column, moment, pitch.  A score that
    prints the same music in several Voice contexts logs each note-event once
    per context, so identical rows are collapsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"cannot read note events {path}: {exc}")

    reader = csv.DictReader(text.splitlines(), dialect=csv.excel_tab)
    required = {"score", "file", "line", "char", "moment"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        die(
            f"{path}: expected a tab-separated header with "
            f"{sorted(required)}; regenerate it with ly2video-events.ily"
        )

    seen: set[tuple] = set()
    events: list[NoteEvent] = []
    for row in reader:
        try:
            key = (
                int(row["score"]),
                row["file"],
                int(row["line"]),
                int(row["char"]),
                row["moment"],
                row.get("pitch", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            events.append(
                NoteEvent(
                    score=int(row["score"]),
                    ref=SourceRef(
                        file=os.path.basename(row["file"]),
                        line=int(row["line"]),
                        char=int(row["char"]),
                    ),
                    moment=float(row["moment"]),
                    pitch=int(row.get("pitch") or -1),
                )
            )
        except (TypeError, ValueError) as exc:
            die(f"{path}: malformed row {row!r}: {exc}")

    if not events:
        die(f"{path}: no note events found")
    return events


def group_note_events(events: Sequence[NoteEvent]) -> list[MusicalOnset]:
    """Collapse note-events into the file's ordered list of onsets.

    Ordered by (score, moment): every \\score restarts at moment 0, and LilyPond
    prints the scores in the order it interpreted them, so this is also the
    order the noteheads appear on the pages.
    """
    grouped: dict[tuple[int, float], list[SourceRef]] = {}
    for event in events:
        grouped.setdefault((event.score, event.moment), []).append(event.ref)
    return [
        MusicalOnset(score=score, moment=moment, refs=tuple(grouped[(score, moment)]))
        for score, moment in sorted(grouped)
    ]


def select_score(
    onsets: Sequence[MusicalOnset], midi_last_moment: float, wanted: int | None
) -> int:
    """Decide which \\score of the file the video should follow.

    A file can hold several \\layout scores -- worked examples of single
    sections, a melody-only staff for the lyrics -- while the \\midi score is
    one performance.  Only a score that spans the same music as that MIDI can be
    filmed, and the last moment of each is a cheap and reliable way to tell.
    """
    spans: dict[int, float] = {}
    for onset in onsets:
        spans[onset.score] = max(spans.get(onset.score, 0.0), onset.moment)

    def describe() -> str:
        return "\n".join(
            f"  --score {index}: {sum(1 for o in onsets if o.score == index)} onset(s), "
            f"last at moment {spans[index]:g}"
            for index in sorted(spans)
        )

    if wanted is not None:
        if wanted not in spans:
            die(
                f"the event log has no score {wanted}.  It contains:\n{describe()}"
            )
        return wanted

    if len(spans) == 1:
        return next(iter(spans))

    matching = [i for i, span in spans.items() if abs(span - midi_last_moment) < 1e-6]
    if len(matching) == 1:
        info(
            f"the file has {len(spans)} printed score(s); following score "
            f"{matching[0]}, the only one that spans the whole MIDI "
            f"(last note at moment {midi_last_moment:g})"
        )
        return matching[0]

    die(
        f"the file has {len(spans)} printed scores and "
        + (
            f"{len(matching)} of them span the MIDI"
            if matching
            else "none of them spans the whole MIDI"
        )
        + f" (last MIDI note at moment {midi_last_moment:g}).  "
        f"Pick one with --score:\n{describe()}"
    )


def anchors_by_staff(page: SvgPage) -> dict[tuple[int, int], list[Anchor]]:
    """Group a page's noteheads by (logical system, printed staff within it).

    Keeping the printed staves apart matters because they do not all show the
    same notes: a TabStaff leaves out the tab number of a tied continuation,
    while the Staff above it still prints the notehead.
    """
    result: dict[tuple[int, int], list[Anchor]] = {}
    for system in page.systems:
        for member_index in range(len(system.members)):
            result[(system.index, member_index)] = []

    for anchor in page.anchors:
        system = assign_anchor_to_system(anchor, page.systems)
        # Ignore a point-and-click object if it is implausibly far from the
        # detected system.  This filters title/header artefacts if present.
        if anchor.y < system.y0 - 6.0 or anchor.y > system.y1 + 6.0:
            continue
        member = min(
            range(len(system.members)),
            key=lambda i: block_distance(system.members[i], anchor.y),
        )
        result[(system.index, member)].append(anchor)

    for anchors in result.values():
        anchors.sort(key=lambda a: a.x)
    return result


def assign_moments_to_anchors(
    pages: Sequence[SvgPage], onsets: Sequence[MusicalOnset]
) -> dict[int, dict[int, list[tuple[Anchor, MusicalOnset]]]]:
    """Give every printed notehead the score moment at which it sounds.

    LilyPond prints systems in score order and lays each one out left to right,
    so the noteheads of a single printed staff, read across all pages, form a
    subsequence of the score's onsets: same order, with the notes that staff
    does not print simply missing.  Walking both in step therefore pins every
    notehead to exactly one moment.

    Unlike clustering noteheads by x coordinate this is exact.  It is unbothered
    by the sideways shift LilyPond applies to colliding noteheads of different
    voices, and it knows about notes that are printed but never re-attacked in
    MIDI, such as the second half of a tie.

    Returns ``{page index: {system index: [(anchor, onset), ...]}}``.
    """
    staff_count = max(
        (len(system.members) for page in pages for system in page.systems), default=0
    )

    # One independent walk per printed staff row: the Staff line through the
    # whole piece, then the TabStaff line, and so on.
    assigned: dict[int, dict[int, list[tuple[Anchor, MusicalOnset]]]] = {
        page.index: {system.index: [] for system in page.systems} for page in pages
    }

    staves = {page.index: anchors_by_staff(page) for page in pages}

    # Chord names are point-and-click note-events too, but they are not played
    # by a Voice, so the log does not know them.  A real mismatch between the
    # pages and the log still shows up as onsets that are never printed.
    logged = {ref for onset in onsets for ref in onset.refs}

    for row in range(staff_count):
        cursor = -1
        pending: dict[SourceRef, int] = {}
        for page in pages:
            by_staff = staves[page.index]
            for system in page.systems:
                for anchor in by_staff.get((system.index, row), ()):
                    if anchor.ref not in logged:
                        continue
                    while not pending.get(anchor.ref):
                        cursor += 1
                        if cursor >= len(onsets):
                            die(
                                f"{page.path.name}: notehead {anchor.ref} in system "
                                f"{system.index + 1} matches no remaining note-event.  "
                                "Either the SVG pages and the event log come from "
                                "different LilyPond runs, or ly2video did not see "
                                "which printed staves belong to one system; that "
                                "needs LilyPond's SystemStartBar."
                            )
                        pending = {}
                        for ref in onsets[cursor].refs:
                            pending[ref] = pending.get(ref, 0) + 1
                    pending[anchor.ref] -= 1
                    assigned[page.index][system.index].append(
                        (anchor, onsets[cursor])
                    )

    return assigned


def onsets_from_note_events(
    pages: Sequence[SvgPage],
    onsets: Sequence[MusicalOnset],
    score: int,
    vertical_padding: float,
) -> list[VisualOnset]:
    """Turn LilyPond note-events plus SVG geometry into timed highlight columns.

    Every notehead on the pages is placed first, including the ones belonging to
    other scores in the same file -- that is what keeps the walk in step -- and
    only afterwards is the result narrowed to the score being filmed.
    """
    assigned = assign_moments_to_anchors(pages, onsets)

    result: list[VisualOnset] = []
    for page in pages:
        if len(page.systems) != len(page.blocks):
            info(
                f"{page.path.name}: merged {len(page.blocks)} printed staff block(s) "
                f"into {len(page.systems)} logical system(s)"
            )

        page_count_before = len(result)
        for system in page.systems:
            columns: dict[float, list[Anchor]] = {}
            for anchor, onset in assigned[page.index][system.index]:
                if onset.score != score:
                    continue
                columns.setdefault(onset.moment, []).append(anchor)

            for moment in sorted(columns):
                xs = sorted(a.x for a in columns[moment])
                result.append(
                    VisualOnset(
                        ordinal=len(result),
                        page_index=page.index,
                        page_path=page.path,
                        system_index=system.index,
                        # Median, so a notehead that LilyPond shifted sideways to
                        # dodge another voice cannot drag the highlight off beat.
                        x=xs[len(xs) // 2],
                        y0=system.y0 - vertical_padding,
                        y1=system.y1 + vertical_padding,
                        source_refs=tuple(
                            sorted(
                                {a.ref for a in columns[moment]},
                                key=lambda r: (r.file, r.line, r.char),
                            )
                        ),
                        moment=moment,
                        score=score,
                    )
                )

        info(
            f"{page.path.name}: {len(result) - page_count_before} onset column(s) "
            f"across {len(page.systems)} logical system(s)"
        )

    moments = [v.moment for v in result]
    if moments != sorted(moments):
        die(
            "printed onsets do not run in score order; are the SVG pages sorted "
            "correctly, or is a system split across pages?"
        )
    expected = sum(1 for onset in onsets if onset.score == score)
    if len(result) != expected:
        die(
            f"score {score} has {expected} onset(s) but {len(result)} appeared in "
            "the SVG pages; is the SVG glob missing a page?"
        )
    return result


@dataclass(frozen=True)
class SystemBand:
    """A horizontal slice of a printed page holding exactly one system."""

    page_index: int
    system_index: int
    y0: float
    y1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def collect_translate_ys(element: ET.Element, base_y: float, out: list[float]) -> None:
    """Absolute y of every translated node in a subtree."""
    tr = translate_of(element)
    y = base_y + (tr[1] if tr else 0.0)
    if tr is not None:
        out.append(y)
    for child in element:
        collect_translate_ys(child, y, out)


def page_furniture(page: SvgPage, root: ET.Element) -> set[int]:
    """Top-level elements that belong to the page rather than to any system.

    LilyPond prints the page number as a lone text in the top or bottom margin.
    It sits inside the strip reserved for the outermost system, so geometry
    alone cannot tell it apart -- but it is the outermost ink on the page, it is
    clear of every staff, and it spells out the page's own number.
    """
    printed = str(natural_page_key(page.path)[0])
    entries: list[tuple[float, float, ET.Element]] = []
    for child in root:
        ys: list[float] = []
        collect_translate_ys(child, 0.0, ys)
        if ys:
            entries.append((min(ys), max(ys), child))
    if not entries or not page.blocks:
        return set()

    topmost = min(entry[0] for entry in entries)
    bottommost = max(entry[1] for entry in entries)
    staff_top = min(block.y0 for block in page.blocks)
    staff_bottom = max(block.y1 for block in page.blocks)

    found: set[int] = set()
    for low, high, child in entries:
        if low != topmost and high != bottommost:
            continue
        if not (high < staff_top or low > staff_bottom):
            continue
        if "".join(child.itertext()).strip() != printed:
            continue
        found.add(id(child))
    return found


def system_bands(
    pages: Sequence[SvgPage],
    visual: Sequence[VisualOnset],
    furniture: dict[int, set[int]],
    roots: dict[int, ET.Element],
) -> list[SystemBand]:
    """Cut each filmed system out of its page, in the order it is played.

    A system's own staff lines say nothing about how much room its stems, slurs,
    marks and lyrics need.  The printed spacing does: splitting the gap to the
    system above and below in half gives back the strip LilyPond reserved for
    it.  At the top and bottom of a page there is no such gap, so the strip runs
    to the page edge and is then trimmed back to the ink actually inside it --
    which is what keeps a lone system from dragging half a blank page along.
    """
    order: list[tuple[int, int]] = []
    for onset in visual:
        key = (onset.page_index, onset.system_index)
        if key not in order:
            order.append(key)

    system_of: dict[tuple[int, int], System] = {}
    for page in pages:
        for system in page.systems:
            system_of[(page.index, system.index)] = system

    per_page: dict[int, list[tuple[int, int]]] = {}
    for key in order:
        per_page.setdefault(key[0], []).append(key)
    for keys in per_page.values():
        keys.sort(key=lambda k: system_of[k].y0)

    # A glyph is drawn around its anchor, not below it, so keep a little air
    # when trimming a strip back to the ink it holds.
    ink_margin = 2.0

    # Two filmed systems split the gap between them evenly.  Against a system
    # that is being dropped there is more to claim -- a rehearsal mark or a
    # tempo sits well above its staff -- and the trim below removes whatever
    # turns out to be empty anyway.
    neighbour_share = 0.75

    ink: dict[int, list[float]] = {}
    for page in pages:
        values: list[float] = []
        for child in roots[page.index]:
            if id(child) in furniture[page.index]:
                continue
            collect_translate_ys(child, 0.0, values)
        values.sort()
        ink[page.index] = values

    bands: dict[tuple[int, int], SystemBand] = {}
    for page_index, keys in per_page.items():
        page = pages[page_index]
        # Systems on the page that are not filmed, so a strip running to the
        # page edge cannot swallow the staff of a worked example next to it.
        others = [
            system for system in page.systems if (page_index, system.index) not in keys
        ]
        for position, key in enumerate(keys):
            system = system_of[key]
            if position > 0:
                above = (system.y0 - system_of[keys[position - 1]].y1) / 2.0
            else:
                above = min(
                    [system.y0 - page.view_y]
                    + [(system.y0 - o.y1) * neighbour_share for o in others
                       if o.y1 <= system.y0]
                )
            if position + 1 < len(keys):
                below = (system_of[keys[position + 1]].y0 - system.y1) / 2.0
            else:
                below = min(
                    [page.view_y + page.view_h - system.y1]
                    + [(o.y0 - system.y1) * neighbour_share for o in others
                       if o.y0 >= system.y1]
                )

            low = system.y0 - above
            high = system.y1 + below
            inside = [y for y in ink[page_index] if low <= y <= high]
            if inside:
                low = max(low, inside[0] - ink_margin)
                high = min(high, inside[-1] + ink_margin)
            bands[key] = SystemBand(
                page_index=page_index,
                system_index=system.index,
                y0=low,
                y1=high,
            )

    return [bands[key] for key in order]


def band_fragment(
    root: ET.Element,
    band: SystemBand,
    dy: float,
    clip_id: str,
    skip: set[int],
    slack: float = 8.0,
) -> str:
    """Serialize one band, moved to its place on the screen and clipped to it.

    LilyPond writes its pages as a flat list of absolutely translated groups, so
    a band can take just the groups it needs instead of a clipped copy of the
    whole page.  The clip is what makes it exact; the filter only keeps the
    fragment small, and so uses a slack wide enough for a glyph anchored just
    outside the band but drawn into it.
    """
    keep: list[str] = []
    for child in root:
        if id(child) in skip:
            continue
        ys: list[float] = []
        collect_translate_ys(child, 0.0, ys)
        if not ys:
            continue
        if max(ys) < band.y0 - slack or min(ys) > band.y1 + slack:
            continue
        keep.append(ET.tostring(child, encoding="unicode"))

    return (
        f'<clipPath id="{clip_id}" clipPathUnits="userSpaceOnUse">'
        f'<rect x="-10000" y="{band.y0 + dy:.4f}" width="20000" '
        f'height="{band.height:.4f}"/></clipPath>\n'
        f'<g clip-path="url(#{clip_id})"><g transform="translate(0, {dy:.4f})">\n'
        + "".join(keep)
        + "\n</g></g>\n"
    )


def stack_systems(
    pages: Sequence[SvgPage], visual: Sequence[VisualOnset]
) -> tuple[list[SvgPage], list[VisualOnset]]:
    """Rebuild the pages out of the filmed systems alone.

    Everything the chosen score does not print -- worked examples of single
    sections, prose, page numbers -- disappears, and the systems that remain are
    packed onto fresh screens of the original page size, as many as fit.
    """
    if not visual:
        return list(pages), list(visual)

    roots = {page.index: ET.fromstring(page.text) for page in pages}
    furniture = {page.index: page_furniture(page, roots[page.index]) for page in pages}

    bands = system_bands(pages, visual, furniture, roots)
    template = pages[0]
    height = template.view_h

    screens: list[list[SystemBand]] = [[]]
    used = 0.0
    for band in bands:
        if screens[-1] and used + band.height > height:
            screens.append([])
            used = 0.0
        screens[-1].append(band)
        used += band.height

    open_tag = re.search(r"<svg\b[^>]*>", template.text, flags=re.I | re.S)
    if not open_tag:
        die("invalid SVG: no root <svg> element")
    style = re.search(r"<style\b.*?</style>", template.text, flags=re.I | re.S)

    placement: dict[tuple[int, int], tuple[int, float]] = {}
    result_pages: list[SvgPage] = []

    for index, members in enumerate(screens):
        total = sum(band.height for band in members)
        cursor = template.view_y + max(0.0, (height - total) / 2.0)
        parts: list[str] = []
        for position, band in enumerate(members):
            dy = cursor - band.y0
            placement[(band.page_index, band.system_index)] = (index, dy)
            parts.append(
                band_fragment(
                    roots[band.page_index],
                    band,
                    dy,
                    f"ly2video-band-{position}",
                    furniture[band.page_index],
                )
            )
            cursor += band.height

        text = (
            open_tag.group(0)
            + "\n"
            + (style.group(0) + "\n" if style else "")
            + "".join(parts)
            + "</svg>\n"
        )
        result_pages.append(
            SvgPage(
                index=index,
                path=template.path.with_name(f"screen-{index + 1}"),
                text=text,
                view_x=template.view_x,
                view_y=template.view_y,
                view_w=template.view_w,
                view_h=template.view_h,
                blocks=(),
                systems=(),
                anchors=(),
            )
        )

    moved: list[VisualOnset] = []
    for onset in visual:
        screen, dy = placement[(onset.page_index, onset.system_index)]
        moved.append(
            replace(
                onset,
                page_index=screen,
                page_path=result_pages[screen].path,
                y0=onset.y0 + dy,
                y1=onset.y1 + dy,
            )
        )

    info(
        f"stacked {len(bands)} filmed system(s) onto {len(result_pages)} screen(s), "
        "dropping everything else on the pages"
    )
    return result_pages, moved


def midi_onsets(path: Path, epsilon_ms: float) -> tuple[list[TimedOnset], float]:
    if mido is None:
        die("Python package 'mido' is required: pip install mido")

    try:
        mid = mido.MidiFile(path)
    except Exception as exc:
        die(f"cannot read MIDI {path}: {exc}")

    tempo = 500_000  # MIDI default: 120 BPM
    seconds = 0.0
    raw: list[float] = []

    for msg in mido.merge_tracks(mid.tracks):
        seconds += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0:
            raw.append(seconds)

    if not raw:
        die(f"{path}: no MIDI note_on events found")

    eps = epsilon_ms / 1000.0
    grouped: list[list[float]] = [[raw[0]]]
    for t in raw[1:]:
        if abs(t - grouped[-1][0]) <= eps:
            grouped[-1].append(t)
        else:
            grouped.append([t])

    onsets = [TimedOnset(seconds=g[0], note_count=len(g)) for g in grouped]
    return onsets, seconds


class MidiTimeline:
    """Convert LilyPond score moments to seconds using the MIDI tempo map.

    A moment is measured in whole notes, so ``moment * 4`` is a count of quarter
    notes and ``ticks_per_beat`` turns that into MIDI ticks.  Going through the
    tempo map instead of counting note-ons means tied, muted or otherwise
    silent notation still lands on the right second, and any ``\\tempo`` change
    (including a ritardando spelled out as tempo marks) is honoured.
    """

    def __init__(self, path: Path) -> None:
        if mido is None:
            die("Python package 'mido' is required: pip install mido")
        try:
            mid = mido.MidiFile(path)
        except Exception as exc:
            die(f"cannot read MIDI {path}: {exc}")

        self.path = path
        self.ticks_per_beat = mid.ticks_per_beat
        # (tick, seconds, tempo in microseconds per quarter note)
        self.changes: list[tuple[int, float, int]] = [(0, 0.0, 500_000)]

        tick = 0
        seconds = 0.0
        tempo = 500_000
        last_note_on = 0
        for msg in mido.merge_tracks(mid.tracks):
            tick += msg.time
            seconds += mido.tick2second(msg.time, self.ticks_per_beat, tempo)
            if msg.type == "set_tempo":
                tempo = msg.tempo
                if self.changes[-1][0] == tick:
                    self.changes[-1] = (tick, seconds, tempo)
                else:
                    self.changes.append((tick, seconds, tempo))
            elif msg.type == "note_on" and msg.velocity > 0:
                last_note_on = tick
        self.end_seconds = seconds
        # In whole notes, so it can be compared with a \\layout score's moments.
        self.last_note_moment = last_note_on / (4.0 * self.ticks_per_beat)

    def seconds_at_moment(self, moment: float) -> float:
        if mido is None:  # pragma: no cover - guarded in __init__
            die("Python package 'mido' is required: pip install mido")
        tick = moment * 4.0 * self.ticks_per_beat
        base_tick, base_seconds, tempo = self.changes[0]
        for change in self.changes:
            if change[0] > tick:
                break
            base_tick, base_seconds, tempo = change
        return base_seconds + mido.tick2second(
            tick - base_tick, self.ticks_per_beat, tempo
        )


MAX_STRAY_GAP_SECONDS = 2.0
"""A MIDI attack further than this from every printed onset is not an unprinted
part but different music."""


def check_midi_agrees(
    timings: Sequence[float],
    midi_events: Sequence[TimedOnset],
    tolerance: float = 0.01,
) -> None:
    r"""Warn when MIDI attacks do not line up with the notated onsets.

    Every MIDI note-on should coincide with a printed onset.  The reverse does
    not hold: tied continuations are printed but never re-attacked.  A MIDI
    attack that matches nothing means the \layout and \midi scores describe
    different music -- unfolded repeats, a different voice selection, a stray
    transposition -- and the video would drift.
    """
    stray = 0
    worst = 0.0
    index = 0
    for event in midi_events:
        while index + 1 < len(timings) and timings[index + 1] <= event.seconds + tolerance:
            index += 1
        candidates = timings[max(0, index - 1) : index + 2]
        delta = min(abs(event.seconds - t) for t in candidates)
        worst = max(worst, delta)
        if delta > tolerance:
            stray += 1

    untouched = len(timings) - (len(midi_events) - stray)
    if untouched > 0:
        info(
            f"{untouched} printed onset(s) have no MIDI attack "
            "(tied continuations and the like)"
        )
    if not stray:
        return

    # A few strays are normal: the MIDI usually carries parts that the filmed
    # score does not print, but those still sound within a beat of a printed
    # note.  A large share of strays, or one that is seconds away from any
    # printed note, means the two describe different music -- typically a
    # \repeat printed once but unfolded in the MIDI -- and the highlight would
    # drift away from the sound.
    share = stray / len(midi_events)
    message = (
        f"{stray} of {len(midi_events)} MIDI attack(s) ({share:.0%}) do not land on "
        f"a printed onset (worst gap {worst * 1000:.0f} ms)"
    )
    if share > 0.5 or worst > MAX_STRAY_GAP_SECONDS:
        die(
            message
            + ".  The filmed score and the \\midi score are not the same music; "
            "a \\repeat that is printed once but sounds twice does this.  Film "
            "a score with \\unfoldRepeats, like the \\midi one."
        )
    info(message + "; they are parts the filmed score does not print")


def read_event_map(path: Path) -> list[EventMapEntry]:
    """Read exact visual-ordinal -> seconds mapping from JSON, CSV or TSV.

    JSON:
      [{"visual": 0, "seconds": 0.0}, ...]

    CSV/TSV:
      visual,seconds
      0,0.0
      1,0.5
    """

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = [
                EventMapEntry(visual=int(row["visual"]), seconds=float(row["seconds"]))
                for row in data
            ]
        except Exception as exc:
            die(f"cannot read event map {path}: {exc}")
    else:
        text = path.read_text(encoding="utf-8")
        dialect = csv.excel_tab if "\t" in text.splitlines()[0] else csv.excel
        reader = csv.DictReader(text.splitlines(), dialect=dialect)
        try:
            entries = [
                EventMapEntry(visual=int(row["visual"]), seconds=float(row["seconds"]))
                for row in reader
            ]
        except Exception as exc:
            die(f"cannot read event map {path}: {exc}")

    entries.sort(key=lambda e: e.visual)
    for expected, entry in enumerate(entries):
        if entry.visual != expected:
            die(
                f"event map {path}: expected visual ordinal {expected}, "
                f"found {entry.visual}"
            )
    return entries


def ffprobe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    proc = subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def inject_highlight(
    svg_text: str,
    x: float | None,
    y0: float | None,
    y1: float | None,
    width: float,
    color: str,
    opacity: float,
    viewbox: tuple[float, float, float, float],
    background_color: str,
) -> str:
    view_x, view_y, view_w, view_h = viewbox
    background = (
        f'\n<rect id="ly2video-background" '
        f'x="{view_x:.4f}" y="{view_y:.4f}" '
        f'width="{view_w:.4f}" height="{view_h:.4f}" '
        f'fill="{background_color}"/>\n'
    )

    highlight = ""
    if x is not None:
        assert y0 is not None and y1 is not None
        highlight = (
            f'<rect id="ly2video-highlight" '
            f'x="{x - width / 2.0:.4f}" y="{y0:.4f}" '
            f'width="{width:.4f}" height="{max(0.01, y1 - y0):.4f}" '
            f'fill="{color}" fill-opacity="{opacity:.4f}" rx="0.18" ry="0.18"/>\n'
        )

    # Insert immediately after the root <svg ...> start tag.  The white (or
    # configured) page background comes first, then the translucent highlight,
    # and all LilyPond grobs remain on top.
    m = re.search(r"<svg\b[^>]*>", svg_text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        die("invalid SVG: no root <svg> element")
    return svg_text[: m.end()] + background + highlight + svg_text[m.end() :]


def rasterize_svg(svg_text: str, out: Path, width: int, height: int) -> None:
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(
            bytestring=svg_text.encode("utf-8"),
            write_to=str(out),
            output_width=width,
            output_height=height,
        )
        return
    except ImportError:
        pass
    except Exception as exc:
        die(f"CairoSVG failed while rendering {out.name}: {exc}")

    # Fallback 1: librsvg
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp.write(svg_text.encode("utf-8"))
            tmp_path = Path(tmp.name)
        try:
            subprocess.run(
                [
                    rsvg,
                    "--width",
                    str(width),
                    "--height",
                    str(height),
                    "--output",
                    str(out),
                    str(tmp_path),
                ],
                check=True,
            )
            return
        finally:
            tmp_path.unlink(missing_ok=True)

    # Fallback 2: Inkscape
    inkscape = shutil.which("inkscape")
    if inkscape:
        with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as tmp:
            tmp.write(svg_text.encode("utf-8"))
            tmp_path = Path(tmp.name)
        try:
            subprocess.run(
                [
                    inkscape,
                    str(tmp_path),
                    f"--export-filename={out}",
                    f"--export-width={width}",
                    f"--export-height={height}",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            return
        finally:
            tmp_path.unlink(missing_ok=True)

    die(
        "no SVG rasterizer found; install 'cairosvg' (pip), "
        "'librsvg2-bin' (rsvg-convert), or Inkscape"
    )


def write_concat_file(
    path: Path, frames: Sequence[tuple[Path, float]], final_frame: Path
) -> None:
    def quote(p: Path) -> str:
        # FFmpeg concat demuxer quoting.  Temporary paths normally contain no
        # apostrophe, but handle it anyway.
        value = str(p.resolve()).replace("'", "'\\''")
        return f"'{value}'"

    with path.open("w", encoding="utf-8") as f:
        for frame, duration in frames:
            if duration <= 0:
                continue
            f.write(f"file {quote(frame)}\n")
            f.write(f"duration {duration:.9f}\n")
        # The concat demuxer ignores the duration of the final file unless the
        # last image is repeated.
        f.write(f"file {quote(final_frame)}\n")


def render_video(
    concat_file: Path,
    audio: Path,
    output: Path,
    fps: int,
    width: int,
    height: int,
    crf: int,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        die("ffmpeg not found in PATH")

    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        f"scale={width}:{height}:flags=lanczos,format=yuv420p",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(output),
    ]
    info("running FFmpeg")
    subprocess.run(cmd, check=True)


def write_debug_map(
    path: Path,
    visual: Sequence[VisualOnset],
    timings: Sequence[float],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "visual",
                "seconds",
                "score",
                "moment",
                "page",
                "system",
                "x",
                "y0",
                "y1",
                "source_refs",
            ]
        )
        for v, seconds in zip(visual, timings):
            writer.writerow(
                [
                    v.ordinal,
                    f"{seconds:.9f}",
                    "" if v.score is None else v.score,
                    "" if v.moment is None else f"{v.moment:.6f}",
                    v.page_index + 1,
                    v.system_index + 1,
                    f"{v.x:.4f}",
                    f"{v.y0:.4f}",
                    f"{v.y1:.4f}",
                    " | ".join(str(r) for r in v.source_refs),
                ]
            )


INCLUDE_DIR = Path(__file__).resolve().parent
"""Directory shipped with the package that holds ly2video-events.ily."""


class _PrintAndExit(argparse.Action):
    """Like ``action="version"``: print a fixed value and exit, so the option
    works without the otherwise required --svg/--midi/--audio/-o."""

    def __init__(self, option_strings, dest, value: str, help: str | None = None):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=help)
        self.value = value

    def __call__(self, parser, namespace, values, option_string=None):
        print(self.value)
        parser.exit()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ly2video",
        description="Create a 9:16 highlighted LilyPond score video from SVG + MIDI + audio."
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    p.add_argument(
        "--include-path",
        action=_PrintAndExit,
        value=str(INCLUDE_DIR),
        help=(
            "print the directory holding ly2video-events.ily, for "
            "`lilypond -I`, and exit"
        ),
    )
    p.add_argument(
        "--svg",
        required=True,
        help="SVG glob, e.g. 'video*.svg'",
    )
    p.add_argument("--midi", type=Path, required=True, help="MIDI timing source")
    p.add_argument("--audio", type=Path, required=True, help="MP3/WAV soundtrack")
    p.add_argument("-o", "--output", type=Path, required=True, help="output MP4")
    p.add_argument(
        "--note-events",
        type=Path,
        help=(
            "TSV written by ly2video-events.ily during the LilyPond run; the "
            "exact and preferred timing source"
        ),
    )
    p.add_argument(
        "--stack-systems",
        action="store_true",
        help=(
            "show only the systems that are filmed, repacked onto fresh screens; "
            "drops other scores, prose and page numbers"
        ),
    )
    p.add_argument(
        "--score",
        type=int,
        help=(
            "which printed score of the file to film, numbered as in the "
            "note event log (default: the only one, or the one that spans the "
            "whole MIDI)"
        ),
    )
    p.add_argument(
        "--events",
        type=Path,
        help="optional exact visual->seconds event map (JSON/CSV/TSV)",
    )
    p.add_argument("--width", type=int, default=1080, help="video width (default: 1080)")
    p.add_argument("--height", type=int, default=1920, help="video height (default: 1920)")
    p.add_argument("--fps", type=int, default=30, help="output FPS (default: 30)")
    p.add_argument("--crf", type=int, default=18, help="x264 CRF (default: 18)")
    p.add_argument(
        "--background-color",
        default="#FFFFFF",
        help="page background RGB hex color (default: #FFFFFF)",
    )
    p.add_argument(
        "--highlight-color",
        default="#FFD84D",
        help="highlight RGB hex color (default: #FFD84D)",
    )
    p.add_argument(
        "--highlight-opacity",
        type=float,
        default=0.32,
        help="highlight opacity 0..1 (default: 0.32)",
    )
    p.add_argument(
        "--highlight-width",
        type=float,
        default=1.55,
        help="highlight width in SVG viewBox units (default: 1.55)",
    )
    p.add_argument(
        "--vertical-padding",
        type=float,
        default=1.35,
        help="padding above/below a detected system in SVG units (default: 1.35)",
    )
    p.add_argument(
        "--x-tolerance",
        type=float,
        default=0.05,
        help="max x delta for simultaneous SVG noteheads (default: 0.05)",
    )
    p.add_argument(
        "--onset-epsilon-ms",
        type=float,
        default=1.0,
        help="MIDI note-ons within this window count as simultaneous (default: 1 ms)",
    )
    p.add_argument(
        "--keep-workdir",
        type=Path,
        help="keep generated PNG frames/debug files in this directory",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="extract/map events and write debug map, but do not render frames/video",
    )
    return p.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.width <= 0 or args.height <= 0:
        die("video dimensions must be positive")
    if args.fps <= 0:
        die("FPS must be positive")
    if not (0.0 <= args.highlight_opacity <= 1.0):
        die("--highlight-opacity must be between 0 and 1")
    if args.highlight_width <= 0:
        die("--highlight-width must be positive")
    if args.x_tolerance <= 0:
        die("--x-tolerance must be positive")
    if not HEX_COLOR_RE.match(args.background_color):
        die("--background-color must be #RRGGBB")
    if not HEX_COLOR_RE.match(args.highlight_color):
        die("--highlight-color must be #RRGGBB")
    if not args.midi.exists():
        die(f"MIDI file not found: {args.midi}")
    if args.note_events and not args.note_events.exists():
        die(f"note event log not found: {args.note_events}")
    if args.note_events and args.events:
        die("--note-events and --events are mutually exclusive")
    if args.score is not None and not args.note_events:
        die("--score needs --note-events; it names a score in that log")
    if not args.audio.exists():
        die(f"audio file not found: {args.audio}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_args(args)

    svg_paths = [Path(p) for p in glob.glob(args.svg)]
    svg_paths.sort(key=natural_page_key)
    if not svg_paths:
        die(f"no SVG files match {args.svg!r}")

    info(f"reading {len(svg_paths)} SVG page(s)")
    pages = [parse_svg_page(path, i) for i, path in enumerate(svg_paths)]

    midi_events, midi_end = midi_onsets(args.midi, args.onset_epsilon_ms)
    info(f"found {len(midi_events)} unique MIDI note-on moment(s)")

    if args.note_events:
        note_events = read_note_events(args.note_events)
        onsets = group_note_events(note_events)
        info(
            f"read {len(note_events)} LilyPond note-event(s) at "
            f"{len(onsets)} distinct moment(s) from {args.note_events}"
        )

        timeline = MidiTimeline(args.midi)
        score = select_score(onsets, timeline.last_note_moment, args.score)

        visual = onsets_from_note_events(
            pages, onsets, score=score, vertical_padding=args.vertical_padding
        )
        if not visual:
            die(f"score {score} has no note-onset columns on these pages")
        info(f"found {len(visual)} onset column(s)")

        timings = [
            timeline.seconds_at_moment(v.moment)  # type: ignore[arg-type]
            for v in visual
        ]
        midi_end = max(midi_end, timeline.end_seconds)
        check_midi_agrees(timings, midi_events)
    else:
        visual = extract_visual_onsets(
            pages,
            x_tolerance=args.x_tolerance,
            vertical_padding=args.vertical_padding,
        )
        if not visual:
            die("no visual note-onset columns found")
        info(f"found {len(visual)} visual onset column(s)")

        if args.events:
            event_map = read_event_map(args.events)
            if len(event_map) != len(visual):
                die(
                    f"event map has {len(event_map)} entries but SVG contains "
                    f"{len(visual)} visual onset columns"
                )
            timings = [entry.seconds for entry in event_map]
            info(f"using exact timing map {args.events}")
        else:
            if len(midi_events) != len(visual):
                die(
                    "visual/MIDI onset count mismatch: "
                    f"SVG={len(visual)}, MIDI={len(midi_events)}.\n"
                    "This usually means a tied continuation, a notehead that "
                    "LilyPond shifted sideways to avoid a collision, or another "
                    "notated moment without a MIDI re-attack.  The script refuses "
                    "to guess: re-run LilyPond with ly2video-events.ily included "
                    "and pass the resulting TSV with --note-events."
                )
            timings = [event.seconds for event in midi_events]

    # Timing must be strictly nondecreasing and usually strictly increasing.
    for i in range(1, len(timings)):
        if timings[i] <= timings[i - 1]:
            die(
                f"non-increasing timing at visual onset {i}: "
                f"{timings[i - 1]} -> {timings[i]}"
            )

    if args.stack_systems:
        pages, visual = stack_systems(pages, visual)

    audio_end = ffprobe_duration(args.audio)
    final_end = audio_end if audio_end and audio_end > timings[-1] else midi_end
    if final_end <= timings[-1]:
        # Keep the final highlight visible briefly if the MIDI technically ends
        # exactly at its attack because of malformed/very short test input.
        final_end = timings[-1] + 0.25

    if args.keep_workdir:
        workdir = args.keep_workdir.resolve()
        workdir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        tmp = tempfile.TemporaryDirectory(prefix="ly2video-")
        workdir = Path(tmp.name)
        cleanup = True

    debug_map = workdir / "ly2video-map.csv"
    write_debug_map(debug_map, visual, timings)
    info(f"wrote mapping: {debug_map}")

    if args.dry_run:
        if cleanup:
            # Dry-run diagnostics are useful, so copy next to output rather than
            # deleting them with the temporary directory.
            target = args.output.with_suffix(".map.csv")
            shutil.copy2(debug_map, target)
            info(f"dry-run map copied to {target}")
        return 0

    # Cache page source text by page index.
    page_by_index = {p.index: p for p in pages}
    frames: list[tuple[Path, float]] = []

    # Optional pre-roll: show the opening page, without highlight, before the
    # first attack.  That is the page the filmed score starts on, which is not
    # page 1 when a file prints other scores first.
    if timings[0] > 1e-6:
        first = page_by_index[visual[0].page_index]
        frame = workdir / "frame-000000.png"
        pre = inject_highlight(
            first.text,
            x=None,
            y0=None,
            y1=None,
            width=args.highlight_width,
            color=args.highlight_color,
            opacity=args.highlight_opacity,
            viewbox=(first.view_x, first.view_y, first.view_w, first.view_h),
            background_color=args.background_color,
        )
        rasterize_svg(pre, frame, args.width, args.height)
        frames.append((frame, timings[0]))

    offset = len(frames)
    for i, (event, start) in enumerate(zip(visual, timings)):
        end = timings[i + 1] if i + 1 < len(timings) else final_end
        duration = max(1e-6, end - start)
        page = page_by_index[event.page_index]
        svg = inject_highlight(
            page.text,
            x=event.x,
            y0=event.y0,
            y1=event.y1,
            width=args.highlight_width,
            color=args.highlight_color,
            opacity=args.highlight_opacity,
            viewbox=(page.view_x, page.view_y, page.view_w, page.view_h),
            background_color=args.background_color,
        )
        frame = workdir / f"frame-{i + offset:06d}.png"
        rasterize_svg(svg, frame, args.width, args.height)
        frames.append((frame, duration))

        if (i + 1) % 25 == 0 or i + 1 == len(visual):
            info(f"rendered {i + 1}/{len(visual)} highlighted frame(s)")

    if not frames:
        die("no frames generated")

    concat_file = workdir / "frames.ffconcat"
    write_concat_file(concat_file, frames, frames[-1][0])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    render_video(
        concat_file,
        args.audio,
        args.output,
        fps=args.fps,
        width=args.width,
        height=args.height,
        crf=args.crf,
    )

    info(f"done: {args.output}")
    if args.keep_workdir:
        info(f"work directory kept at {workdir}")
    return 0


def run() -> None:
    """Console-script entry point."""
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        die(f"external command failed with exit code {exc.returncode}")


if __name__ == "__main__":
    run()