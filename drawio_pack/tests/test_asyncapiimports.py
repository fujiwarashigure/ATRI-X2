import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "skills", "drawio-skill", "scripts", "asyncapiimports.py")


def load_importer():
    spec = importlib.util.spec_from_file_location("asyncapiimports", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAsyncApiImports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.importer = load_importer()

    def test_asyncapi_2_operations_channels_and_payload_schemas(self):
        spec = {
            "asyncapi": "2.6.0",
            "channels": {
                "orders/created": {
                    "publish": {
                        "operationId": "emitOrder",
                        "message": {"$ref": "#/components/messages/OrderCreated"},
                    },
                    "subscribe": {
                        "summary": "Consume order",
                        "message": {"payload": {"$ref": "#/components/schemas/Audit"}},
                    },
                },
                "orders/shipped": {
                    "publish": {"message": {"payload": {"type": "object"}}},
                },
            },
            "components": {
                "messages": {
                    "OrderCreated": {"payload": {"$ref": "#/components/schemas/Order"}}
                },
                "schemas": {
                    "Order": {
                        "properties": {"audit": {"$ref": "#/components/schemas/Audit"}}
                    },
                    "Audit": {"properties": {"id": {"type": "string"}}},
                },
            },
        }

        graph = self.importer.build(spec, group=True, direction="TB")
        by_id = {node["id"]: node for node in graph["nodes"]}
        pairs = {(edge["source"], edge["target"]) for edge in graph["edges"]}

        self.assertEqual(graph["direction"], "TB")
        self.assertEqual(by_id["channel:orders/created"]["group"], "orders")
        self.assertIn("fillColor=#d5e8d4", by_id["operation:orders/created:publish"]["style"])
        self.assertIn("fillColor=#dae8fc", by_id["operation:orders/created:subscribe"]["style"])
        # No summary or operationId: fall back to the channel, not "channel:action".
        self.assertEqual(
            "PUBLISH\norders/shipped", by_id["operation:orders/shipped:publish"]["label"]
        )
        self.assertEqual("Audit\n(1 field)", by_id["schema:Audit"]["label"])
        self.assertIn(("operation:orders/created:publish", "channel:orders/created"), pairs)
        self.assertIn(("operation:orders/created:publish", "schema:Order"), pairs)
        self.assertIn(("operation:orders/created:subscribe", "schema:Audit"), pairs)
        self.assertIn(("schema:Order", "schema:Audit"), pairs)
        self.assertEqual(
            by_id["operation:orders/created:publish"]["provenance"]["pointer"],
            "#/channels/orders~1created/publish",
        )

    def test_asyncapi_3_send_operation_and_channel_tag(self):
        spec = {
            "asyncapi": "3.0.0",
            "channels": {
                "orderEvents": {
                    "address": "orders/{orderId}",
                    "tags": [{"name": "commerce"}],
                    "messages": {
                        "changed": {"$ref": "#/components/messages/OrderChanged"},
                        "ignored": {"payload": {"$ref": "#/components/schemas/Audit"}},
                    },
                }
            },
            "operations": {
                "sendOrder": {
                    "action": "send",
                    "channel": {"$ref": "#/channels/orderEvents"},
                    "messages": [{"$ref": "#/channels/orderEvents/messages/changed"}],
                }
            },
            "components": {
                "messages": {
                    "OrderChanged": {"payload": {"$ref": "#/components/schemas/Order"}}
                },
                "schemas": {
                    "Order": {"type": "object"},
                    "Audit": {"type": "object"},
                },
            },
        }

        graph = self.importer.build(spec, group=True)
        by_id = {node["id"]: node for node in graph["nodes"]}
        pairs = {(edge["source"], edge["target"]) for edge in graph["edges"]}

        self.assertEqual(by_id["channel:orderEvents"]["label"], "orders/{orderId}")
        self.assertEqual(by_id["operation:sendOrder"]["group"], "commerce")
        self.assertIn(("operation:sendOrder", "channel:orderEvents"), pairs)
        self.assertIn(("operation:sendOrder", "schema:Order"), pairs)
        self.assertNotIn(("operation:sendOrder", "schema:Audit"), pairs)

    def test_cli_emits_graph_json(self):
        spec = {
            "asyncapi": "3.0.0",
            "channels": {"events": {"address": "events"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "asyncapi.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            result = subprocess.run(
                [sys.executable, SCRIPT, path, "--direction", "TB"],
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["direction"], "TB")
        self.assertIn("1 channels", result.stderr)


if __name__ == "__main__":
    unittest.main()
