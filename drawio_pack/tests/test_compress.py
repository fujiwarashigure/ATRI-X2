#!/usr/bin/env python3
"""Tests for compress.py (big .drawio -> executive-summary compressor).

Run from the repo root:
    python3 -m unittest tests.test_compress -v
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

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


# Two K3 triangles {a,b,c} and {d,e,f} joined by exactly one bridge edge c-d.
TRIANGLES_NODES = ["a", "b", "c", "d", "e", "f"]
TRIANGLES_EDGES = [("a", "b"), ("a", "c"), ("b", "c"),
                   ("d", "e"), ("d", "f"), ("e", "f"), ("c", "d")]


class TestLabelPropagation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("compress")

    def test_two_triangles_split_into_two_communities(self):
        labels = self.m.label_propagation(TRIANGLES_NODES, TRIANGLES_EDGES)
        communities = {}
        for n in TRIANGLES_NODES:
            communities.setdefault(labels[n], []).append(n)
        self.assertEqual(len(communities), 2)
        members = sorted(sorted(m) for m in communities.values())
        self.assertEqual(members, [["a", "b", "c"], ["d", "e", "f"]])

    def test_deterministic_across_runs(self):
        r1 = self.m.label_propagation(TRIANGLES_NODES, TRIANGLES_EDGES)
        r2 = self.m.label_propagation(list(reversed(TRIANGLES_NODES)), TRIANGLES_EDGES)
        self.assertEqual(r1, r2)

    def test_isolated_node_is_its_own_community(self):
        labels = self.m.label_propagation(["a", "b", "z"], [("a", "b")])
        self.assertEqual(labels["z"], "z")


class TestAggregateEdges(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("compress")

    def test_cross_edges_dedupe_with_count(self):
        # a,b in community X; c,d in community Y; two edges cross X->Y.
        community_of = {"a": "X", "b": "X", "c": "Y", "d": "Y"}
        edges = [("a", "c"), ("b", "d")]
        agg = self.m.aggregate_edges(edges, community_of)
        self.assertEqual(agg, {("X", "Y"): 2})

    def test_internal_edges_are_dropped(self):
        community_of = {"a": "X", "b": "X"}
        agg = self.m.aggregate_edges([("a", "b")], community_of)
        self.assertEqual(agg, {})


class TestClusterName(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("compress")

    def test_common_leading_token(self):
        labels = {"s1": "Auth Service", "s2": "Auth DB", "s3": "Auth Cache"}
        name = self.m.cluster_name(["s1", "s2", "s3"], labels, {"s1": 1, "s2": 1, "s3": 1})
        self.assertEqual(name, "Auth (3)")

    def test_fallback_to_highest_degree_label(self):
        labels = {"s1": "Payments", "s2": "Ledger"}
        degree = {"s1": 1, "s2": 5}
        name = self.m.cluster_name(["s1", "s2"], labels, degree)
        self.assertEqual(name, "Ledger (2)")


class TestParse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("compress")

    # A container ("Tier") holding a leaf vertex, plus an edge with a label
    # child (edgeLabel vertex) that must NOT be treated as a node.
    DOC = ('<mxfile><diagram name="P1"><mxGraphModel><root>'
          '<mxCell id="0"/><mxCell id="1" parent="0"/>'
          '<mxCell id="grp" value="Tier" vertex="1" parent="1">'
          '<mxGeometry x="0" y="0" width="240" height="120" as="geometry"/></mxCell>'
          '<mxCell id="a" value="A" vertex="1" parent="grp">'
          '<mxGeometry x="10" y="10" width="80" height="40" as="geometry"/></mxCell>'
          '<mxCell id="b" value="B" vertex="1" parent="1">'
          '<mxGeometry x="300" y="0" width="80" height="40" as="geometry"/></mxCell>'
          '<mxCell id="e1" edge="1" parent="1" source="a" target="b">'
          '<mxGeometry relative="1" as="geometry"/></mxCell>'
          '<mxCell id="lbl" value="calls" style="edgeLabel;html=1;" vertex="1" '
          'connectable="0" parent="e1">'
          '<mxGeometry x="0" y="0" relative="1" as="geometry"/></mxCell>'
          '</root></mxGraphModel></diagram></mxfile>')

    def test_leaf_vertices_only_skips_containers_and_edge_labels(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.drawio")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.DOC)
            nodes, edges = self.m.parse(path)
            self.assertEqual(set(nodes), {"a", "b"})       # grp is a container, lbl is an edge label
            self.assertEqual(nodes["a"][0], "A")
            self.assertEqual(edges, {("a", "b")})


class TestAssembly(unittest.TestCase):
    """Full CLI run: needs Graphviz `dot` (autolayout.py places the exec nodes)."""

    @staticmethod
    def _drawio(nodes, edges):
        cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
        for i, (cid, label) in enumerate(nodes):
            cells.append(f'<mxCell id="{cid}" value="{label}" vertex="1" parent="1">'
                         f'<mxGeometry x="{i * 150}" y="0" width="120" height="60" '
                         'as="geometry"/></mxCell>')
        for i, (s, t) in enumerate(edges):
            cells.append(f'<mxCell id="e{i}" edge="1" parent="1" source="{s}" target="{t}">'
                         '<mxGeometry relative="1" as="geometry"/></mxCell>')
        return ('<mxfile><diagram id="orig" name="Original"><mxGraphModel><root>'
                + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>")

    def setUp(self):
        if not shutil.which("dot"):
            self.skipTest("Graphviz dot not installed")

    def test_two_page_output_with_drilldown(self):
        nodes = [(n, n.upper()) for n in TRIANGLES_NODES]
        doc = self._drawio(nodes, TRIANGLES_EDGES)
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "big.drawio")
            with open(src, "w", encoding="utf-8") as f:
                f.write(doc)
            out = os.path.join(d, "exec.drawio")
            r = run("compress.py", src, "-o", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("2 clusters", r.stderr)
            with open(out, encoding="utf-8") as f:
                xml = f.read()
            self.assertEqual(xml.count("<diagram"), 2)
            self.assertIn('link="data:page/id,full-diagram"', xml)
            self.assertIn('id="full-diagram"', xml)
            # page 2 keeps the original leaf labels verbatim
            for _, label in nodes:
                self.assertIn(f'value="{label}"', xml)
            v = run("validate.py", out)
            self.assertEqual(v.returncode, 0, v.stdout)


if __name__ == "__main__":
    unittest.main()
