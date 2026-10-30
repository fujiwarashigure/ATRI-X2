#!/usr/bin/env python3
"""Tests for raster2drawio.py (image graph JSON -> editable .drawio).

Run from the repo root:
    python3 -m unittest tests.test_derasterize -v
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "drawio-skill", "scripts")


def load(name):
    """Import a bundled script module by file path."""
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(script, *args, **kw):
    """Run a script as a subprocess; return CompletedProcess."""
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                          capture_output=True, text=True, **kw)


class TestRaster2Drawio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("raster2drawio")

    def test_positioned_nodes_and_shapes(self):
        nodes = [
            {"id": "n1", "label": "API Gateway", "x": 120, "y": 60, "w": 160, "h": 60,
             "shape": "rect", "fill": "#dae8fc", "stroke": "#6c8ebf"},
            {"id": "n2", "label": "Auth DB", "x": 360, "y": 60, "shape": "cylinder"},
            {"id": "n3", "label": "Decide", "x": 600, "y": 60, "shape": "rhombus"},
        ]
        xml = self.m.to_drawio(nodes, [])
        root = ET.fromstring(xml)
        cells = {c.get("id"): c for c in root.iter("mxCell") if c.get("vertex") == "1"}
        self.assertEqual(set(cells), {"n1", "n2", "n3"})

        n1, geo1 = cells["n1"], cells["n1"].find("mxGeometry")
        self.assertEqual((geo1.get("x"), geo1.get("y"), geo1.get("width"), geo1.get("height")),
                         ("120", "60", "160", "60"))
        self.assertIn("fillColor=#dae8fc", n1.get("style"))
        self.assertIn("strokeColor=#6c8ebf", n1.get("style"))

        self.assertIn("shape=cylinder3", cells["n2"].get("style"))
        self.assertIn("rhombus", cells["n3"].get("style"))

    def test_default_shape_size_and_fill(self):
        nodes = [{"id": "n1", "x": 0, "y": 0}]
        xml = self.m.to_drawio(nodes, [])
        root = ET.fromstring(xml)
        cell = next(c for c in root.iter("mxCell") if c.get("id") == "n1")
        geo = cell.find("mxGeometry")
        self.assertEqual((geo.get("width"), geo.get("height")), ("120", "60"))
        self.assertIn("fillColor=#dae8fc", cell.get("style"))
        self.assertIn("strokeColor=#6c8ebf", cell.get("style"))
        # default shape ("rect") is a plain box: no rounded=1, no shape=
        self.assertNotIn("rounded=1", cell.get("style"))

    def test_edge_dashed_and_no_arrow(self):
        nodes = [{"id": "n1", "x": 0, "y": 0}, {"id": "n2", "x": 200, "y": 0}]
        edges = [
            {"source": "n1", "target": "n2", "label": "HTTPS", "dashed": True},
            {"source": "n2", "target": "n1", "arrow": False},
        ]
        xml = self.m.to_drawio(nodes, edges)
        root = ET.fromstring(xml)
        edge_cells = [c for c in root.iter("mxCell") if c.get("edge") == "1"]
        self.assertEqual(len(edge_cells), 2)
        dashed = next(c for c in edge_cells if c.get("source") == "n1")
        no_arrow = next(c for c in edge_cells if c.get("source") == "n2")
        self.assertIn("dashed=1", dashed.get("style"))
        self.assertIn("endArrow=none", no_arrow.get("style"))
        # every edge cell has a relative geometry child
        for c in edge_cells:
            geo = c.find("mxGeometry")
            self.assertIsNotNone(geo)
            self.assertEqual(geo.get("relative"), "1")
        # layer cell footgun: id=1 must carry parent="0"
        layer = next(c for c in root.iter("mxCell") if c.get("id") == "1")
        self.assertEqual(layer.get("parent"), "0")

    def test_label_escaping(self):
        nodes = [{"id": "n1", "x": 0, "y": 0, "label": 'A & B <C> "D"\nsecond line'}]
        xml = self.m.to_drawio(nodes, [])
        self.assertIn("A &amp; B &lt;C&gt; &quot;D&quot;&#xa;second line", xml)
        self.assertNotIn('"D"\n', xml)

    def test_missing_position_autoplace(self):
        if shutil.which("dot") is None:
            self.skipTest("Graphviz dot not on PATH")
        graph = {
            "nodes": [{"id": "n1", "label": "A"}, {"id": "n2", "label": "B", "x": 100, "y": 0}],
            "edges": [{"source": "n1", "target": "n2", "label": "calls"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "graph.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(graph, f)
            r = run("raster2drawio.py", src)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("auto-placed", r.stderr)
        root = ET.fromstring(r.stdout)
        ids = {c.get("id") for c in root.iter("mxCell") if c.get("vertex") == "1"}
        self.assertEqual(ids, {"n1", "n2"})


if __name__ == "__main__":
    unittest.main()
