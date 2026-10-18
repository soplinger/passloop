#!/usr/bin/env python3
"""passloop: an offline single-track railway timetable checker.

Reads a plain-text working timetable, works out run and dwell times with
exact arithmetic, and checks every block section, passing loop and main
track for overlaps and headway breaches. It can add the smallest whole-minute
departure holds that clear the timetable, draw an occupancy diagram, or write
an SVG time-distance graph. Standard library only; no network access.
"""

from __future__ import annotations

import argparse
import json
import math
import string
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

DAY = 24 * 60  # minutes in a day
DEFAULT_SPEED = Fraction(80)  # km/h
DEFAULT_CLEARANCE = Fraction(2)  # minutes between two occupations
DEFAULT_HEADWAY = Fraction(5)  # minutes between same-direction trains entering a block
DEFAULT_DWELL = Fraction(2)  # minutes at an intermediate stop
MAX_HOLD_ROUNDS = 500
EXIT_OK, EXIT_CONFLICT, EXIT_INPUT = 0, 1, 2
SYMBOLS = "".join(c for c in string.ascii_uppercase if c not in "IO") + "23456789"
PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b", "#e377c2", "#17becf"]


@dataclass
class Station:
    name: str
    loop: bool
    line: int


@dataclass
class Section:
    a: str
    b: str
    km: Fraction
    limit: Optional[Fraction]
    line: int


@dataclass
class Train:
    id: str
    direction: str  # "up" = first station to last, "down" = reverse
    depart: Fraction
    speed: Fraction
    dwell: Optional[Fraction]
    loops: FrozenSet[str]
    line: int


@dataclass
class Timetable:
    name: str = "Unnamed line"
    clearance: Fraction = DEFAULT_CLEARANCE
    headway: Fraction = DEFAULT_HEADWAY
    dwell: Fraction = DEFAULT_DWELL
    stations: List[Station] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    trains: List[Train] = field(default_factory=list)
    errors: List[Tuple[int, str]] = field(default_factory=list)

    def section_between(self, a: str, b: str) -> Optional[Section]:
        for sec in self.sections:
            if {sec.a, sec.b} == {a, b}:
                return sec
        return None


@dataclass(frozen=True)
class Visit:
    station: str
    arr: Fraction
    dep: Fraction
    loop: bool


@dataclass(frozen=True)
class Occupancy:
    resource: Tuple[str, str]  # ("block", "A - B") | ("main", station) | ("loop", station)
    start: Fraction
    end: Fraction
    train: str
    direction: str


@dataclass(frozen=True)
class Conflict:
    kind: str  # "overlap" or "headway"
    resource: Tuple[str, str]
    earlier: str
    later: str
    at: Fraction  # when the later train enters the resource
    need: Fraction  # minutes the later train must move to be clear


@dataclass
class Resolution:
    holds: Dict[str, int]
    remaining: List[Conflict]
    rounds: int


# ---------------------------------------------------------------- parsing

def parse_time(text: str) -> Fraction:
    """Return minutes since midnight for HH:MM or HH:MM:SS."""
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"bad time '{text}', expected HH:MM or HH:MM:SS")
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ValueError(f"time '{text}' is out of range")
    return Fraction(hours * 3600 + minutes * 60 + seconds, 60)


def parse_number(text: str, what: str, positive: bool) -> Fraction:
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"{what} must be a number, got '{text}'") from None
    if value < 0 or (positive and value == 0):
        need = "positive" if positive else "zero or more"
        raise ValueError(f"{what} must be {need}, got '{text}'")
    return value


def parse_timetable(text: str) -> Timetable:
    tt = Timetable()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        body = raw.split("#", 1)[0].strip()
        if not body:
            continue
        keyword, *args = body.split()
        try:
            _parse_directive(tt, lineno, keyword.lower(), args, body)
        except ValueError as exc:
            tt.errors.append((lineno, str(exc)))
    _validate(tt)
    tt.errors.sort()
    return tt


def _parse_directive(tt: Timetable, lineno: int, keyword: str, args: List[str], body: str) -> None:
    if keyword == "line":
        if not args:
            raise ValueError("line needs a name")
        tt.name = body[len(keyword):].strip()
    elif keyword in ("clearance", "headway", "dwell"):
        if len(args) != 1:
            raise ValueError(f"{keyword} takes one value in minutes")
        setattr(tt, keyword, parse_number(args[0], keyword, positive=False))
    elif keyword == "station":
        if len(args) not in (1, 2) or (len(args) == 2 and args[1].lower() != "loop"):
            raise ValueError("station syntax: station NAME [loop]")
        if any(s.name == args[0] for s in tt.stations):
            raise ValueError(f"station '{args[0]}' is defined twice")
        tt.stations.append(Station(args[0], len(args) == 2, lineno))
    elif keyword == "section":
        if len(args) not in (3, 4):
            raise ValueError("section syntax: section FROM TO KM [LIMIT_KMH]")
        km = parse_number(args[2], "section length", positive=True)
        limit = parse_number(args[3], "speed limit", positive=True) if len(args) == 4 else None
        tt.sections.append(Section(args[0], args[1], km, limit, lineno))
    elif keyword == "train":
        _parse_train(tt, lineno, args)
    else:
        raise ValueError(f"unknown directive '{keyword}'")


def _parse_train(tt: Timetable, lineno: int, args: List[str]) -> None:
    if len(args) < 3:
        raise ValueError("train syntax: train ID up|down HH:MM [speed=N] [dwell=N] [loop=A,B]")
    train_id, direction, when, *options = args
    if direction.lower() not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got '{direction}'")
    if any(t.id == train_id for t in tt.trains):
        raise ValueError(f"train '{train_id}' is defined twice")
    depart = parse_time(when)
    speed: Optional[Fraction] = None
    dwell: Optional[Fraction] = None
    loops: set = set()
    for option in options:
        key, sep, value = option.partition("=")
        if not sep or not value:
            raise ValueError(f"option '{option}' must look like key=value")
        key = key.lower()
        if key == "speed":
            speed = parse_number(value, "speed", positive=True)
        elif key == "dwell":
            dwell = parse_number(value, "dwell", positive=False)
        elif key == "loop":
            loops.update(v for v in value.split(",") if v)
        else:
            raise ValueError(f"unknown train option '{key}'")
    tt.trains.append(Train(
        train_id, direction.lower(), depart,
        speed if speed is not None else DEFAULT_SPEED,
        dwell, frozenset(loops), lineno,
    ))


def _validate(tt: Timetable) -> None:
    errors = tt.errors
    if len(tt.stations) < 2:
        errors.append((0, "a line needs at least two stations"))
        return
    names = [s.name for s in tt.stations]
    pairs = list(zip(tt.stations, tt.stations[1:]))
    consecutive = {frozenset((left.name, right.name)) for left, right in pairs}
    for sec in tt.sections:
        for end in (sec.a, sec.b):
            if end not in names:
                errors.append((sec.line, f"section uses unknown station '{end}'"))
        if sec.a in names and sec.b in names and frozenset((sec.a, sec.b)) not in consecutive:
            errors.append((sec.line, f"section {sec.a} {sec.b} does not join consecutive stations"))
    for left, right in pairs:
        matches = [s for s in tt.sections if {s.a, s.b} == {left.name, right.name}]
        if not matches:
            errors.append((right.line, f"no section defined between '{left.name}' and '{right.name}'"))
        elif len(matches) > 1:
            errors.append((matches[1].line, f"section {left.name} {right.name} is defined twice"))
    ends = {names[0], names[-1]}
    loop_names = {s.name for s in tt.stations if s.loop}
    for train in tt.trains:
        for stop in sorted(train.loops):
            if stop not in names:
                errors.append((train.line, f"train {train.id} loops at unknown station '{stop}'"))
            elif stop not in loop_names:
                errors.append((train.line, f"station '{stop}' has no passing loop"))
            elif stop in ends:
                errors.append((train.line, f"loop at terminus '{stop}' has no effect"))


# ---------------------------------------------------------------- schedule model

def route(tt: Timetable, train: Train) -> List[str]:
    names = [s.name for s in tt.stations]
    return names if train.direction == "up" else names[::-1]


def run_minutes(tt: Timetable, train: Train, a: str, b: str) -> Fraction:
    sec = tt.section_between(a, b)
    speed = train.speed if sec.limit is None else min(train.speed, sec.limit)
    return sec.km / speed * 60


def build_visits(tt: Timetable, train: Train, hold: Fraction = Fraction(0)) -> List[Visit]:
    path = route(tt, train)
    last = len(path) - 1
    visits: List[Visit] = []
    prev_dep = Fraction(0)
    for i, name in enumerate(path):
        if i == 0:
            arr = train.depart + hold
        else:
            arr = prev_dep + run_minutes(tt, train, path[i - 1], name)
        if i in (0, last):
            dwell = Fraction(0)
        else:
            dwell = train.dwell if train.dwell is not None else tt.dwell
        dep = arr + dwell
        visits.append(Visit(name, arr, dep, name in train.loops))
        prev_dep = dep
    return visits


def occupancies(tt: Timetable, train: Train, hold: Fraction = Fraction(0)) -> List[Occupancy]:
    visits = build_visits(tt, train, hold)
    out: List[Occupancy] = []
    for i, visit in enumerate(visits):
        kind = "loop" if visit.loop else "main"
        out.append(Occupancy((kind, visit.station), visit.arr, visit.dep, train.id, train.direction))
        if i + 1 < len(visits):
            nxt = visits[i + 1]
            block = " - ".join(sorted((visit.station, nxt.station)))
            out.append(Occupancy(("block", block), visit.dep, nxt.arr, train.id, train.direction))
    return out


def line_occupancies(tt: Timetable, holds: Dict[str, int]) -> List[Occupancy]:
    out: List[Occupancy] = []
    for train in tt.trains:
        out.extend(occupancies(tt, train, Fraction(holds.get(train.id, 0))))
    return out


def find_conflicts(tt: Timetable, occs: Sequence[Occupancy]) -> List[Conflict]:
    clearance, headway = tt.clearance, tt.headway
    grouped: Dict[Tuple[str, str], List[Occupancy]] = defaultdict(list)
    for occ in occs:
        grouped[occ.resource].append(occ)
    found: List[Conflict] = []
    for resource, items in grouped.items():
        items.sort(key=lambda o: (o.start, o.train))
        for i, first in enumerate(items):
            for second in items[i + 1:]:
                if first.train == second.train:
                    continue
                overlap = first.end + clearance - second.start
                gap = Fraction(0)
                if resource[0] == "block" and first.direction == second.direction:
                    gap = headway - (second.start - first.start)
                need = max(overlap, gap)
                if need <= 0:
                    continue
                kind = "overlap" if overlap >= gap else "headway"
                found.append(Conflict(kind, resource, first.train, second.train, second.start, need))
    found.sort(key=lambda c: (c.at, c.later, c.earlier))
    return found


def check_line(tt: Timetable) -> List[Conflict]:
    return find_conflicts(tt, line_occupancies(tt, {}))


def resolve(tt: Timetable, max_rounds: int = MAX_HOLD_ROUNDS) -> Resolution:
    """Hold the later train at its origin by whole minutes until nothing clashes."""
    holds: Dict[str, int] = {}
    conflicts = find_conflicts(tt, line_occupancies(tt, holds))
    rounds = 0
    while conflicts and rounds < max_rounds:
        first = conflicts[0]
        holds[first.later] = holds.get(first.later, 0) + math.ceil(first.need)
        rounds += 1
        if holds[first.later] > DAY:
            break
        conflicts = find_conflicts(tt, line_occupancies(tt, holds))
    return Resolution(holds, conflicts, rounds)


# ---------------------------------------------------------------- formatting

def fmt_time(minutes: Fraction) -> str:
    total = round(Fraction(minutes) * 60)
    days, rem = divmod(total, DAY * 60)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    text = f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{text} (+{days})" if days else text


def describe(resource: Tuple[str, str]) -> str:
    kind, name = resource
    if kind == "block":
        return f"block {name}"
    return f"{kind} track at {name}"


def describe_conflict(c: Conflict) -> str:
    what = "same-direction headway" if c.kind == "headway" else "overlap"
    return (f"{what} on {describe(c.resource)}: {c.earlier} and {c.later} "
            f"conflict at {fmt_time(c.at)}; hold {c.later} by {math.ceil(c.need)} min")


def _format_errors(path: str, errors: List[Tuple[int, str]]) -> List[str]:
    return [f"{path}:{lineno}: {msg}" if lineno else f"{path}: {msg}" for lineno, msg in errors]


def _conflict_dict(c: Conflict) -> dict:
    return {
        "kind": c.kind,
        "resource": describe(c.resource),
        "earlier": c.earlier,
        "later": c.later,
        "at": fmt_time(c.at),
        "hold_minutes": math.ceil(c.need),
    }


def times_text(tt: Timetable, holds: Dict[str, int], only: Optional[str] = None) -> str:
    blocks = []
    for train in tt.trains:
        if only and train.id != only:
            continue
        held = holds.get(train.id, 0)
        note = f"  held {held} min at origin" if held else ""
        blocks.append(f"Train {train.id}  {train.direction}  depart {fmt_time(train.depart)}{note}")
        blocks.append(f"  {'station':<14}{'arrive':<16}{'depart':<16}")
        for i, v in enumerate(build_visits(tt, train, Fraction(held))):
            arr = "" if i == 0 else fmt_time(v.arr)
            blocks.append(f"  {v.station:<14}{arr:<16}{fmt_time(v.dep):<16}")
        blocks.append("")
    if not blocks:
        return f"no trains{' named ' + only if only else ''}"
    return "\n".join(blocks).rstrip()


def diagram_text(tt: Timetable, holds: Dict[str, int], width: int = 60) -> str:
    if not tt.trains:
        return "no trains"
    cols = max(width, 10)
    visits = {t.id: build_visits(tt, t, Fraction(holds.get(t.id, 0))) for t in tt.trains}
    t0 = min(vs[0].arr for vs in visits.values())
    t1 = max(vs[-1].arr for vs in visits.values())
    span = max(t1 - t0, Fraction(1))
    index = {s.name: i for i, s in enumerate(tt.stations)}
    symbols = {t.id: SYMBOLS[n % len(SYMBOLS)] for n, t in enumerate(tt.trains)}
    grid = [[" "] * cols for _ in range(2 * len(tt.stations) - 1)]

    def col(t: Fraction) -> int:
        return min(cols - 1, int((t - t0) / span * (cols - 1)))

    def put(row: int, c: int, sym: str) -> None:
        cur = grid[row][c]
        grid[row][c] = sym if cur in (" ", sym) else "*"

    for train in tt.trains:
        sym = symbols[train.id]
        vs = visits[train.id]
        for k, v in enumerate(vs):
            for c in range(col(v.arr), col(v.dep) + 1):
                put(2 * index[v.station], c, sym)
            if k + 1 < len(vs):
                nxt = vs[k + 1]
                row = 2 * min(index[v.station], index[nxt.station]) + 1
                for c in range(col(v.dep), col(nxt.arr) + 1):
                    put(row, c, sym)

    label_w = max(len(s.name) for s in tt.stations) + 1
    lines = [f"{tt.name}: {fmt_time(t0)} to {fmt_time(t1)}  (* = two trains in one cell)"]
    for r, row in enumerate(grid):
        label = tt.stations[r // 2].name if r % 2 == 0 else ""
        lines.append(f"{label:<{label_w}}| " + "".join(row).rstrip())
    legend = "  ".join(f"{symbols[t.id]}={t.id}" for t in tt.trains)
    lines.append(f"legend: {legend}")
    return "\n".join(lines)


def svg_text(tt: Timetable, holds: Dict[str, int]) -> str:
    visits = {t.id: build_visits(tt, t, Fraction(holds.get(t.id, 0))) for t in tt.trains}
    if not visits:
        t0, t1 = Fraction(0), Fraction(60)
    else:
        t0 = min(vs[0].arr for vs in visits.values())
        t1 = max(vs[-1].arr for vs in visits.values())
    span = max(t1 - t0, Fraction(1))
    pos: Dict[str, Fraction] = {}
    acc = Fraction(0)
    for i, station in enumerate(tt.stations):
        pos[station.name] = acc
        if i + 1 < len(tt.stations):
            acc += tt.section_between(station.name, tt.stations[i + 1].name).km
    total_km = acc or Fraction(1)
    width, height = 960, 560
    left, right, top, bottom = 130, 30, 40, 50
    plot_w, plot_h = width - left - right, height - top - bottom

    def x(t: Fraction) -> float:
        return left + float((t - t0) / span) * plot_w

    def y(name: str) -> float:
        return top + float(pos[name] / total_km) * plot_h

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="12">',
        f"<title>{escape(tt.name)} time-distance</title>",
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for station in tt.stations:
        sy = y(station.name)
        out.append(f'<line x1="{left}" y1="{sy:.1f}" x2="{left + plot_w}" y2="{sy:.1f}" stroke="#cccccc"/>')
        out.append(f'<text x="{left - 8}" y="{sy + 4:.1f}" text-anchor="end">{escape(station.name)}</text>')
    hour = math.ceil(t0 / 60)
    while hour * 60 <= t1:
        hx = x(Fraction(hour * 60))
        out.append(f'<line x1="{hx:.1f}" y1="{top}" x2="{hx:.1f}" y2="{top + plot_h}" stroke="#eeeeee"/>')
        out.append(f'<text x="{hx:.1f}" y="{top + plot_h + 18}" text-anchor="middle">{hour % 24:02d}:00</text>')
        hour += 1
    for n, train in enumerate(tt.trains):
        vs = visits[train.id]
        points = []
        for v in vs:
            points.append(f"{x(v.arr):.1f},{y(v.station):.1f}")
            points.append(f"{x(v.dep):.1f},{y(v.station):.1f}")
        colour = PALETTE[n % len(PALETTE)]
        out.append(f'<polyline fill="none" stroke="{colour}" stroke-width="2" points="{" ".join(points)}"/>')
        out.append(f'<text x="{x(vs[0].arr) + 4:.1f}" y="{y(vs[0].station) - 4:.1f}" fill="{colour}">'
                   f'{escape(train.id)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- CLI

def _load(path: str) -> Timetable:
    with open(path, encoding="utf-8") as fh:
        return parse_timetable(fh.read())


def cmd_check(tt: Timetable, args: argparse.Namespace) -> int:
    conflicts = check_line(tt)
    if args.json:
        print(json.dumps({"trains": len(tt.trains), "conflicts": [_conflict_dict(c) for c in conflicts]}, indent=2))
    elif conflicts:
        for c in conflicts:
            print(f"CONFLICT {describe_conflict(c)}")
        print(f"{len(conflicts)} conflict(s) across {len(tt.trains)} train(s)")
    else:
        print(f"OK: {len(tt.trains)} train(s) on '{tt.name}' clear, no conflicts")
    return EXIT_CONFLICT if conflicts else EXIT_OK


def cmd_resolve(tt: Timetable, args: argparse.Namespace) -> int:
    res = resolve(tt)
    if args.json:
        print(json.dumps({
            "holds": res.holds,
            "rounds": res.rounds,
            "remaining": [_conflict_dict(c) for c in res.remaining],
        }, indent=2))
    else:
        if res.holds:
            for train_id, minutes in sorted(res.holds.items()):
                print(f"hold {train_id} by {minutes} min at origin")
        else:
            print("no holds needed")
        if res.remaining:
            for c in res.remaining:
                print(f"UNRESOLVED {describe_conflict(c)}")
            print(f"{len(res.remaining)} conflict(s) remain after {res.rounds} round(s)")
        else:
            print(f"timetable clear after {res.rounds} round(s)")
    return EXIT_CONFLICT if res.remaining else EXIT_OK


def cmd_times(tt: Timetable, args: argparse.Namespace, holds: Dict[str, int]) -> int:
    print(times_text(tt, holds, args.train))
    return EXIT_OK


def cmd_diagram(tt: Timetable, args: argparse.Namespace, holds: Dict[str, int]) -> int:
    print(diagram_text(tt, holds, args.width))
    return EXIT_OK


def cmd_svg(tt: Timetable, args: argparse.Namespace, holds: Dict[str, int]) -> int:
    try:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(svg_text(tt, holds))
    except OSError as exc:
        print(f"passloop: cannot write {args.out}: {exc.strerror}", file=sys.stderr)
        return EXIT_INPUT
    print(f"wrote {args.out}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="passloop", description="Offline single-track timetable checker.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="report overlaps and headway breaches (exit 1 if any)")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="print machine-readable output")

    p = sub.add_parser("resolve", help="add minimum whole-minute departure holds until clear")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="print machine-readable output")

    p = sub.add_parser("times", help="print station arrival and departure tables")
    p.add_argument("file")
    p.add_argument("--train", help="only show this train id")
    p.add_argument("--resolved", action="store_true", help="apply the holds from 'resolve'")

    p = sub.add_parser("diagram", help="print an ASCII occupancy diagram")
    p.add_argument("file")
    p.add_argument("--width", type=int, default=60, help="columns for the time axis (default 60)")
    p.add_argument("--resolved", action="store_true", help="apply the holds from 'resolve'")

    p = sub.add_parser("svg", help="write an SVG time-distance graph")
    p.add_argument("file")
    p.add_argument("out", help="output .svg path")
    p.add_argument("--resolved", action="store_true", help="apply the holds from 'resolve'")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        tt = _load(args.file)
    except OSError as exc:
        print(f"passloop: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return EXIT_INPUT
    if tt.errors:
        for line in _format_errors(args.file, tt.errors):
            print(line)
        return EXIT_INPUT
    holds = resolve(tt).holds if getattr(args, "resolved", False) else {}
    if args.command == "check":
        return cmd_check(tt, args)
    if args.command == "resolve":
        return cmd_resolve(tt, args)
    if args.command == "times":
        return cmd_times(tt, args, holds)
    if args.command == "diagram":
        return cmd_diagram(tt, args, holds)
    return cmd_svg(tt, args, holds)


if __name__ == "__main__":
    sys.exit(main())
