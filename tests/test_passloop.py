import json
from fractions import Fraction
import xml.etree.ElementTree as ET

import pytest

import passloop as pl


def two_stations(sections: str, trains: str, extra: str = "") -> str:
    return f"{extra}station A\nstation B\n{sections}\n{trains}\n"


def test_parse_time_accepts_hh_mm_and_hh_mm_ss():
    assert pl.parse_time("06:30") == Fraction(390)
    assert pl.parse_time("06:30:30") == Fraction(781, 2)


def test_parse_time_rejects_out_of_range():
    with pytest.raises(ValueError):
        pl.parse_time("24:00")
    with pytest.raises(ValueError):
        pl.parse_time("6-30")


def test_parse_number_rejects_zero_when_positive():
    with pytest.raises(ValueError):
        pl.parse_number("0", "section length", positive=True)
    assert pl.parse_number("0", "dwell", positive=False) == 0
    with pytest.raises(ValueError):
        pl.parse_number("abc", "speed", positive=True)


def test_parse_errors_carry_line_numbers():
    text = "line X\nstation A\nstation B\nsection A B 5\ntrain 1 sideways 06:00\n"
    tt = pl.parse_timetable(text)
    assert (5, "direction must be 'up' or 'down', got 'sideways'") in tt.errors


def test_unknown_directive_is_reported_with_its_line():
    tt = pl.parse_timetable("frobnicate 3\n")
    assert any(lineno == 1 and "unknown directive" in msg for lineno, msg in tt.errors)


def test_missing_section_between_consecutive_stations_is_an_error():
    tt = pl.parse_timetable("station A\nstation B\n")
    assert (2, "no section defined between 'A' and 'B'") in tt.errors


def test_run_time_is_exact_and_capped_by_section_limit():
    tt = pl.parse_timetable(two_stations("section A B 5", "train 1A up 06:00 speed=90"))
    visits = pl.build_visits(tt, tt.trains[0])
    assert visits[1].arr == Fraction(360) + Fraction(10, 3)

    tt = pl.parse_timetable(two_stations("section A B 10 60", "train 1A up 06:00 speed=90"))
    visits = pl.build_visits(tt, tt.trains[0])
    assert visits[1].arr == 370


def test_dwell_applies_only_at_intermediate_stops():
    text = (
        "dwell 2\nstation A\nstation B\nstation C\n"
        "section A B 6 60\nsection B C 6 60\n"
        "train 1A up 06:00\ntrain 2A up 06:30 dwell=5\n"
    )
    tt = pl.parse_timetable(text)
    assert not tt.errors
    v1 = pl.build_visits(tt, tt.trains[0])
    assert v1[1].dep - v1[1].arr == 2
    assert v1[0].dep == v1[0].arr and v1[2].dep == v1[2].arr
    v2 = pl.build_visits(tt, tt.trains[1])
    assert v2[1].dep - v2[1].arr == 5


def test_head_on_overlap_is_a_conflict_and_hold_is_the_overlap():
    text = two_stations("section A B 12 60", "train 1A up 06:00\ntrain 2B down 06:05")
    tt = pl.parse_timetable(text)
    conflicts = pl.check_line(tt)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "overlap"
    assert conflicts[0].resource == ("block", "A - B")
    assert (conflicts[0].earlier, conflicts[0].later) == ("1A", "2B")
    assert pl.resolve(tt).holds == {"2B": 9}


def test_passing_loop_meet_resolves_with_a_three_minute_hold():
    text = (
        "station A\nstation M loop\nstation B\n"
        "section A M 6 60\nsection M B 6 60\n"
        "train 1A up 06:00\ntrain 2B down 06:03 loop=M\n"
    )
    tt = pl.parse_timetable(text)
    assert not tt.errors
    conflicts = pl.check_line(tt)
    assert [(c.kind, c.resource) for c in conflicts] == [("overlap", ("block", "B - M"))]
    res = pl.resolve(tt)
    assert res.holds == {"1A": 3}
    assert res.remaining == []


def test_same_direction_entries_closer_than_headway_breach_it():
    text = (
        "clearance 2\nheadway 5\nstation A\nstation B\n"
        "section A B 1 60\ntrain 1A up 06:00\ntrain 1B up 06:03\n"
    )
    tt = pl.parse_timetable(text)
    conflicts = pl.check_line(tt)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "headway"
    assert conflicts[0].need == 2
    assert pl.resolve(tt).holds == {"1B": 2}


def test_touching_exactly_at_clearance_is_legal():
    text = two_stations("section A B 1 60", "train 1A up 06:00\ntrain 2B down 06:03")
    tt = pl.parse_timetable(text)
    assert pl.check_line(tt) == []
    res = pl.resolve(tt)
    assert res.holds == {} and res.remaining == [] and res.rounds == 0


def test_fmt_time_formats_and_rolls_over_midnight():
    assert pl.fmt_time(Fraction(390)) == "06:30:00"
    assert pl.fmt_time(Fraction(1450)) == "00:10:00 (+1)"


def test_diagram_has_station_rows_and_legend():
    text = (
        "station Alpha\nstation Mid loop\nstation Omega\n"
        "section Alpha Mid 6 60\nsection Mid Omega 6 60\n"
        "train 1A up 06:00\ntrain 2B down 06:03 loop=Mid\n"
    )
    tt = pl.parse_timetable(text)
    out = pl.diagram_text(tt, {}, width=40)
    assert "legend: A=1A  B=2B" in out
    assert any(line.startswith("Alpha") for line in out.splitlines())
    assert any(line.startswith("Omega") for line in out.splitlines())


def test_svg_output_is_well_formed_xml():
    text = two_stations("section A B 12 60", "train 1A up 06:00\ntrain 2B down 06:05")
    tt = pl.parse_timetable(text)
    root = ET.fromstring(pl.svg_text(tt, {}))
    assert root.tag.endswith("svg")
    assert len(root.findall(".//{http://www.w3.org/2000/svg}polyline")) == 2


def test_cli_check_exit_codes(tmp_path, capsys):
    clash = tmp_path / "clash.timetable"
    clash.write_text(two_stations("section A B 12 60", "train 1A up 06:00\ntrain 2B down 06:05"))
    clear = tmp_path / "clear.timetable"
    clear.write_text(two_stations("section A B 1 60", "train 1A up 06:00\ntrain 2B down 06:03"))
    broken = tmp_path / "broken.timetable"
    broken.write_text("train 1A sideways 06:00\n")

    assert pl.main(["check", str(clear)]) == 0
    assert pl.main(["check", str(clash)]) == 1
    assert pl.main(["check", str(broken)]) == 2
    assert pl.main(["check", str(tmp_path / "missing.timetable")]) == 2
    out = capsys.readouterr().out
    assert "broken.timetable:1:" in out


def test_cli_resolve_json_reports_holds(tmp_path, capsys):
    path = tmp_path / "loop.timetable"
    path.write_text(
        "station A\nstation M loop\nstation B\n"
        "section A M 6 60\nsection M B 6 60\n"
        "train 1A up 06:00\ntrain 2B down 06:03 loop=M\n"
    )
    assert pl.main(["resolve", "--json", str(path)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["holds"] == {"1A": 3}
    assert data["remaining"] == []


def test_cli_svg_writes_file(tmp_path, capsys):
    src = tmp_path / "t.timetable"
    src.write_text(two_stations("section A B 12 60", "train 1A up 06:00"))
    out = tmp_path / "t.svg"
    assert pl.main(["svg", str(src), str(out)]) == 0
    ET.parse(out)
