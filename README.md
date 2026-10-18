# passloop

**Plan and check single-track railway timetables offline: block and passing-loop conflicts, headway breaches, minimum departure holds and SVG time-distance graphs.**

passloop reads a plain-text working timetable for a single-track line, works out every train's run and dwell times with exact arithmetic, and checks each block section, passing loop and main track for conflicting occupations. It suggests whole-minute departure holds, draws an ASCII occupancy diagram, and writes an SVG time-distance graph. It uses only the Python 3 standard library and makes no network calls.

## Features

- **Plain-text timetable format** with comments, named stations, sections and trains, so the file can live in version control.
- **Line-numbered errors** such as `valley.timetable:7: section uses unknown station 'Gama'`, with exit code 2 for bad input.
- **Exact arithmetic** using `Fraction`, so run times are never rounded before the conflict checks.
- **Run times** per section from distance and speed, capped by both the train's speed and any section speed limit.
- **Dwell times** per stop, with a line default and per-train overrides. Terminus stops take no dwell.
- **Track model** of single-track blocks, a main track at each station and a passing loop where the station has one.
- **Explicit loop routing** per train (`loop=Beta`), so the checker knows which trains wait in a loop and which pass on the main line.
- **Head-on conflict detection** on shared blocks and tracks, with a configurable clearance margin.
- **Headway checks** for same-direction trains entering the same block too close together.
- **Exact clearance boundary**: a gap equal to the clearance is legal, a gap one second short is not.
- **Automatic resolution** (`resolve`): repeatedly holds the later train at its origin by the smallest whole number of minutes that clears the first conflict, and reports anything it cannot clear.
- **Station timetable** with arrival and departure times to the second, and a `+1` day marker for trains running past midnight.
- **ASCII occupancy diagram** that shows which train holds each block or track over time, with `*` marking two trains in one cell.
- **SVG time-distance graph** where the vertical axis is true distance in km, with hour gridlines and labelled train paths.
- **JSON output** for `check` and `resolve`, for scripts and CI dashboards.
- **CI exit codes**: `0` clear, `1` conflicts remain, `2` input error.

## Quick start

The example line is in `examples/valley.timetable`: five stations, two of them with passing loops, and six trains in both directions.

```bash
python3 passloop.py check examples/valley.timetable
python3 passloop.py resolve examples/valley.timetable
python3 passloop.py times examples/valley.timetable --resolved
python3 passloop.py diagram examples/valley.timetable --resolved
python3 passloop.py svg examples/valley.timetable valley.svg --resolved
```

`check` lists every conflict it finds, with the train that should be held and by how many minutes. `resolve` prints the holds it applied and any conflicts it could not clear. The `--resolved` flag applies those holds before printing the timetable, diagram or SVG.

A minimal timetable looks like this:

```text
line Valley Branch
clearance 2          # minutes a block or track must be clear between occupations
headway 5            # minimum minutes between same-direction trains entering a block
dwell 2              # default stop time at intermediate stations

station Alpha
station Beta loop
station Gamma

section Alpha Beta 12.0 80   # km, optional speed limit km/h
section Beta Gamma 8.5

train 1A up 06:00 speed=90
train 1B down 06:04 loop=Beta
```

Train directions are `up` (first station to last) and `down` (last station to first). Train options are `speed=`, `dwell=` and `loop=`, with comma-separated station names for `loop=`.

## How to run

Requires Python 3.9 or newer. There is nothing to install for the program itself.

```bash
python3 passloop.py --help
python3 passloop.py check FILE [--json]
python3 passloop.py resolve FILE [--json]
python3 passloop.py times FILE [--train ID] [--resolved]
python3 passloop.py diagram FILE [--width 60] [--resolved]
python3 passloop.py svg FILE OUT.svg [--resolved]
```

Exit codes: `0` means the timetable is clear, `1` means conflicts remain, and `2` means the file could not be read or has input errors.

## How the checks work

- A **block** is one section between two adjacent stations. A train occupies it from leaving the previous station until reaching the next one.
- A **main track** at a station is occupied from arrival to departure by trains that stop on it, and a **loop** is occupied the same way by trains routed through it.
- Two trains conflict when they occupy the same resource and the earlier one's exit, plus the clearance, is later than the later one's entry.
- Trains travelling in the same direction also need `headway` minutes between their entries to a block.
- A hold moves a train's whole timetable, so it is conservative. Re-running `check` after a hold confirms the result.

## Limitations

- No acceleration or braking curves: each section is run at a constant speed.
- Holds are applied at the train's origin. A real controller might hold at a loop instead, which can need less total delay.
- This is a timetable and occupancy check, not a signalling or safety system. Use it to plan and review, not to authorise train movements.

## How to test

```bash
python3 -m pytest -q
```

The tests in `tests/test_passloop.py` cover parsing and line-numbered errors, exact run and dwell times, head-on and passing-loop conflicts, headway breaches, the exact clearance boundary, resolution holds, the diagram, SVG well-formedness and the CLI exit codes.

## Project layout

```text
passloop.py                     the whole tool (single module)
examples/valley.timetable       sample line used in the quick start
tests/conftest.py               puts the project root on the import path
tests/test_passloop.py          unit and CLI tests
```
