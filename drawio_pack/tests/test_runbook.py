#!/usr/bin/env python3
"""Tests for runbook.py -- pure XML-parsing + HTML-building, no CLI needed.

Run from the repo root:
    python3 -m unittest tests.test_runbook -v
"""
import importlib.util
import os
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


# Start (ellipse) -> Decision (rhombus) --Yes--> Escalate (process)
#                                        --No---> Close ticket (process)
FLOWCHART = """<mxfile><diagram id="p1" name="Page-1"><mxGraphModel><root>
<mxCell id="0"/>
<mxCell id="1" parent="0"/>
<mxCell id="start" value="Start" vertex="1" parent="1" style="ellipse;whiteSpace=wrap;html=1;">
<mxGeometry x="40" y="40" width="120" height="60" as="geometry"/></mxCell>
<mxCell id="dec" value="Is it broken?" vertex="1" parent="1" style="rhombus;whiteSpace=wrap;html=1;">
<mxGeometry x="40" y="140" width="120" height="80" as="geometry"/></mxCell>
<mxCell id="a" value="Escalate" vertex="1" parent="1" style="rounded=1;whiteSpace=wrap;html=1;">
<mxGeometry x="0" y="260" width="120" height="60" as="geometry"/></mxCell>
<mxCell id="b" value="Close ticket" vertex="1" parent="1" style="rounded=1;whiteSpace=wrap;html=1;">
<mxGeometry x="160" y="260" width="120" height="60" as="geometry"/></mxCell>
<mxCell id="e1" edge="1" parent="1" source="start" target="dec" style="endArrow=classic;html=1;">
<mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e2" value="Yes" edge="1" parent="1" source="dec" target="a" style="endArrow=classic;html=1;">
<mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e3" value="No" edge="1" parent="1" source="dec" target="b" style="endArrow=classic;html=1;">
<mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""


class TestRunbook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("runbook")

    def _parse(self, xml_text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "flow.drawio")
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml_text)
            return self.m.parse(path)

    def test_node_types_inferred_from_style(self):
        nodes, _, _ = self._parse(FLOWCHART)
        self.assertEqual(nodes["start"], {"label": "Start", "type": "start"})
        self.assertEqual(nodes["dec"]["type"], "decision")
        self.assertEqual(nodes["a"]["type"], "process")
        self.assertEqual(nodes["b"]["type"], "process")

    def test_edges_carry_choice_labels(self):
        _, edges, _ = self._parse(FLOWCHART)
        self.assertEqual(len(edges), 3)
        by_target = {e["target"]: e["label"] for e in edges}
        self.assertEqual(by_target["a"], "Yes")
        self.assertEqual(by_target["b"], "No")
        # unlabeled edge (Start -> Decision) has an empty choice label
        e1 = next(e for e in edges if e["source"] == "start" and e["target"] == "dec")
        self.assertEqual(e1["label"], "")

    def test_start_node_is_ellipse_with_indegree_zero(self):
        _, _, start_id = self._parse(FLOWCHART)
        self.assertEqual(start_id, "start")

    def test_build_html_self_contained_with_all_labels_and_choices(self):
        nodes, edges, start_id = self._parse(FLOWCHART)
        out = self.m.build_html("Triage", nodes, edges, start_id)
        for label in ("Start", "Is it broken?", "Escalate", "Close ticket"):
            self.assertIn(label, out)
        self.assertIn('"Yes"', out)
        self.assertIn('"No"', out)
        self.assertIn('"start": "start"', out)
        self.assertNotIn("http://", out)
        self.assertNotIn("https://", out)

    def test_label_xml_entities_decoded(self):
        xml_text = FLOWCHART.replace('value="Start"', 'value="A &amp; B &lt;end&gt;"')
        nodes, _, _ = self._parse(xml_text)
        self.assertEqual(nodes["start"]["label"], "A & B <end>")

    def test_terminal_nodes_have_no_outgoing_edges(self):
        _, edges, _ = self._parse(FLOWCHART)
        sources = {e["source"] for e in edges}
        self.assertNotIn("a", sources)
        self.assertNotIn("b", sources)


if __name__ == "__main__":
    unittest.main()
