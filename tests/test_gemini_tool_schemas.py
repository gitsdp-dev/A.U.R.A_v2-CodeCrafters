from __future__ import annotations

import unittest

from google.genai import types

from plugins.google_sheets import PLUGIN as GOOGLE_SHEETS_PLUGIN


class GeminiToolSchemaTests(unittest.TestCase):
    def test_plugin_parameters_are_valid_function_declarations(self):
        types.FunctionDeclaration.model_validate({
            "name": GOOGLE_SHEETS_PLUGIN["name"],
            "description": GOOGLE_SHEETS_PLUGIN["description"],
            "parameters": GOOGLE_SHEETS_PLUGIN["parameters"],
        })

    def test_nested_array_parameters_are_valid_function_declarations(self):
        types.FunctionDeclaration.model_validate({
            "name": "array_schema_test",
            "description": "Validate nested array parameters.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "values": {
                        "type": "ARRAY",
                        "items": {"type": "ARRAY", "items": {"type": "STRING"}},
                    },
                },
                "required": ["values"],
            },
        })


if __name__ == "__main__":
    unittest.main()
