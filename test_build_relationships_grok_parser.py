import importlib.util
import sys
import unittest
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
BUILD_RELATIONSHIPS_PATH = ROOT_DIR / "scripts" / "build_relationships_grok.py"
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("build_relationships_grok_parser", BUILD_RELATIONSHIPS_PATH)
build_relationships_grok = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = build_relationships_grok
assert spec.loader is not None
spec.loader.exec_module(build_relationships_grok)


class RelationshipParserTest(unittest.TestCase):
    def test_parse_relationships_accepts_markdown_json_fence(self):
        raw_response = '''```json
{
  "companies": [
    {
      "ticker": "NVDA",
      "relationships": [
        {
          "person_name": "Jensen Huang",
          "role": "President and Chief Executive Officer",
          "role_category": "EXECUTIVE"
        }
      ]
    }
  ]
}
```'''

        rows, returned_tickers = build_relationships_grok.parse_relationships(raw_response)

        self.assertEqual(returned_tickers, {"NVDA"})
        self.assertEqual(
            rows,
            [
                {
                    "ticker": "NVDA",
                    "person_name": "Jensen Huang",
                    "person_key": "JENSEN_HUANG",
                    "role": "President and Chief Executive Officer",
                    "role_category": "EXECUTIVE",
                }
            ],
        )

    def test_parse_relationships_accepts_bare_json_with_surrounding_whitespace(self):
        raw_response = '  {"companies": [{"ticker": "AAPL", "relationships": []}]}\n'

        rows, returned_tickers = build_relationships_grok.parse_relationships(raw_response)

        self.assertEqual(rows, [])
        self.assertEqual(returned_tickers, {"AAPL"})


if __name__ == "__main__":
    unittest.main()
