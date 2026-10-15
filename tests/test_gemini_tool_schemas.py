from __future__ import annotations

import unittest

from google.genai import types

from plugins.google_sheets import PLUGIN as GOOGLE_SHEETS_PLUGIN
from plugins.run_calculator import PLUGIN as RUN_CALCULATOR_PLUGIN


class GeminiToolSchemaTests(unittest.TestCase):
    def test_array_tool_parameters_are_valid_function_declarations(self):
        for plugin in (GOOGLE_SHEETS_PLUGIN, RUN_CALCULATOR_PLUGIN):
            with self.subTest(plugin=plugin["name"]):
                types.FunctionDeclaration.model_validate({
                    "name": plugin["name"],
                    "description": plugin["description"],
                    "parameters": plugin["parameters"],
                })


if __name__ == "__main__":
    unittest.main()
