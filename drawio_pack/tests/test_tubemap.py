"""Tests for scripts/tubemap.py (Tube-Map Mode).

Pure-function tests — tubemap builds `.drawio` XML directly from a metro JSON and
needs neither the draw.io CLI nor Graphviz.
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "drawio-skill", "scripts")


def load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(script, *args, **kw):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, script), *args],
        capture_output=True, text=True, **kw)


tm = load("tubemap")

DEMO = {
    "stations": {
        "a": {"label": "A", "gx": 0, "gy": 0},
        "b": {"label": "B", "gx": 2, "gy": 0, "interchange": True},
        "c": {"label": "C & <x>", "gx": 2, "gy": 2},
        "d": {"label": "D", "gx": 4, "gy": 1},
    },
    "lines": [
        {"name": "L1", "color": "#0098d4", "stations": ["a", "b", "c"]},
        {"name": "L2", "stations": ["b", "d"]},  # no colour → palette
    ],
}


class TestOctilinear(unittest.TestCase):
    def test_horizontal_needs_no_bend(self):
        self.assertEqual(tm.octilinear_waypoints(0, 0, 5, 0), [])

    def test_vertical_needs_no_bend(self):
        self.assertEqual(tm.octilinear_waypoints(0, 0, 0, 5), [])

    def test_exact_diagonal_needs_no_bend(self):
        self.assertEqual(tm.octilinear_waypoints(0, 0, 4, 4), [])
        self.assertEqual(tm.octilinear_waypoints(0, 0, -3, 3), [])

    def test_wide_delta_goes_diagonal_then_horizontal(self):
        # dx=4, dy=1: diagonal run length 1, then straight into the target row.
        self.assertEqual(tm.octilinear_waypoints(0, 0, 4, 1), [(1, 1)])

    def test_tall_delta_goes_diagonal_then_vertical(self):
        # dx=1, dy=4: diagonal run length 1, then straight into the target column.
        self.assertEqual(tm.octilinear_waypoints(0, 0, 1, 4), [(1, 1)])

    def test_bend_lands_on_the_target_axis(self):
        # The single bend must leave a final straight (axis-aligned) run to the target.
        for x2, y2 in [(7, 2), (2, 7), (-6, 3), (3, -6)]:
            wp = tm.octilinear_waypoints(0, 0, x2, y2)
            self.assertEqual(len(wp), 1)
            bx, by = wp[0]
            self.assertTrue(bx == x2 or by == y2,
                            f"bend {wp[0]} not aligned with target ({x2},{y2})")


class TestBuild(unittest.TestCase):
    def test_counts_and_wellformed(self):
        xml, n_st, n_ln = tm.build(DEMO, grid=100)
        self.assertEqual((n_st, n_ln), (4, 2))
        self.assertTrue(xml.startswith("<?xml"))
        self.assertIn('<mxCell id="1" parent="0"/>', xml)  # layer footgun honoured

    def test_interchange_marker_distinct(self):
        xml, _, _ = tm.build(DEMO, grid=100)
        # Interchange stations use the black ring; regular stops the grey ring.
        self.assertIn("strokeColor=#111111", xml)
        self.assertIn("strokeColor=#555555", xml)

    def test_line_colour_and_palette_fallback(self):
        xml, _, _ = tm.build(DEMO, grid=100)
        self.assertIn("strokeColor=#0098d4", xml)             # explicit colour
        self.assertIn(f"strokeColor={tm.TUBE_PALETTE[1]}", xml)  # 2nd line → palette[1]

    def test_label_escaped(self):
        xml, _, _ = tm.build(DEMO, grid=100)
        self.assertIn("C &amp; &lt;x&gt;", xml)
        self.assertNotIn("C & <x>", xml)

    def test_unknown_station_id_errors(self):
        bad = {"stations": {"a": {"gx": 0, "gy": 0}},
               "lines": [{"name": "L", "stations": ["a", "ghost"]}]}
        with self.assertRaises(SystemExit):
            tm.build(bad)


class TestCli(unittest.TestCase):
    def test_stdout_roundtrip(self):
        r = run("tubemap.py", "-", input=json.dumps(DEMO))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("<mxfile", r.stdout)
        self.assertIn("Tube Map", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
