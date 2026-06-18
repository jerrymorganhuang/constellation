import tempfile
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from build_constellation_soxx import (
    extract_signature_relationships_from_table,
    extract_signature_name_from_cell,
    is_plausible_person_name,
    parse_filing_with_sources,
    person_name_rejection_reason,
    write_signature_debug_log,
)


class StrictPersonNameValidatorTest(unittest.TestCase):
    def test_rejects_document_names(self):
        for candidate in (
            "Deferred Restricted Stock Unit Agreement",
            "Restricted Stock Unit Agreement",
            "Securities Exchange Act",
            "Executive Compensation Plan",
            "Exhibit Schedule",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_plausible_person_name(candidate))
                self.assertIsNotNone(person_name_rejection_reason(candidate))

    def test_rejects_title_only_strings(self):
        for candidate in (
            "Executive Vice President",
            "Chief Financial Officer",
            "President",
            "Director",
            "Officer Pursuant",
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_plausible_person_name(candidate))
                self.assertIsNotNone(person_name_rejection_reason(candidate))

    def test_allows_person_like_names(self):
        for candidate in ("Jane Doe", "John Q. Public", "Mary Anne Smith"):
            with self.subTest(candidate=candidate):
                self.assertTrue(is_plausible_person_name(candidate))
                self.assertIsNone(person_name_rejection_reason(candidate))

    def test_signature_name_cell_strips_trailing_table_label(self):
        cases = {
            "Susan L. Spradley Title": "Susan L Spradley",
            "Robert A. Bruggeworth Title": "Robert A Bruggeworth",
        }
        for candidate, expected in cases.items():
            with self.subTest(candidate=candidate):
                self.assertEqual(extract_signature_name_from_cell([candidate]), expected)

    def test_signature_name_cell_rejects_label_only_or_title_phrase_after_strip(self):
        for candidate in ("Title", "Chief Financial Officer Title"):
            with self.subTest(candidate=candidate):
                self.assertEqual(extract_signature_name_from_cell([candidate]), "")

    def test_signature_only_extraction_filters_non_people(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>/s/ Deferred Restricted Stock Unit Agreement</p><p>Chief Financial Officer</p>
        <p>/s/ Jane Doe</p><p>Chief Financial Officer and Director</p>
        <p>Restricted Stock Unit Agreement Director</p>
        <p>Securities Exchange Act Executive Vice President</p>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [("Jane Doe", "CFO_OF"), ("Jane Doe", "BOARD_OF")],
        )

    def test_signature_table_uses_anchor_and_same_row_title(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Title</th><th>Date</th></tr>
          <tr>
            <td>/s/ William Brennan<br>William Brennan</td>
            <td>President, Chief Executive Officer and Director</td>
            <td>July 1, 2025</td>
          </tr>
          <tr>
            <td>/s/ Daniel Fleming<br>Daniel Fleming</td>
            <td>Chief Financial Officer (principal financial and accounting officer)</td>
            <td>July 1, 2025</td>
          </tr>
          <tr>
            <td>/s/ Sylvia Acevedo<br>Sylvia Acevedo</td>
            <td>Director</td>
            <td>July 1, 2025</td>
          </tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [
                ("William Brennan", "CEO_OF"),
                ("William Brennan", "BOARD_OF"),
                ("Daniel Fleming", "CFO_OF"),
                ("Sylvia Acevedo", "BOARD_OF"),
            ],
        )

    def test_signature_table_does_not_leak_titles_between_rows(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Title</th><th>Date</th></tr>
          <tr>
            <td>/s/ Jen-Hsun Huang<br>Jen-Hsun Huang</td>
            <td>President, Chief Executive Officer and Director</td>
            <td>March 1, 2026</td>
          </tr>
          <tr>
            <td>/s/ Jane Finance<br>Jane Finance</td>
            <td>Chief Financial Officer</td>
            <td>March 1, 2026</td>
          </tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [
                ("Jen-Hsun Huang", "CEO_OF"),
                ("Jen-Hsun Huang", "BOARD_OF"),
                ("Jane Finance", "CFO_OF"),
            ],
        )
        self.assertNotIn(
            ("Jen-Hsun Huang", "CFO_OF"),
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
        )

    def test_signature_table_pairs_repeated_signature_title_groups_qrvo_fixture(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Title</th><th>Signature</th><th>Title</th></tr>
          <tr>
            <td>/s/ Robert A. Bruggeworth<br>Robert A. Bruggeworth</td>
            <td>President and Chief Executive Officer and Director</td>
            <td>/s/ Grant Brown<br>Grant Brown</td>
            <td>Chief Financial Officer</td>
          </tr>
        </table>
        </body></html>"""

        table = BeautifulSoup(html, "html.parser").find("table")
        relationships = extract_signature_relationships_from_table(table)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in relationships],
            [
                ("Robert A Bruggeworth", "CEO_OF"),
                ("Robert A Bruggeworth", "BOARD_OF"),
                ("Grant Brown", "CFO_OF"),
            ],
        )
        self.assertNotIn(("Robert A Bruggeworth", "CFO_OF"), [(rel.name, rel.relationship_type) for rel in relationships])

    def test_signature_table_infers_nearest_local_title_for_oled_fixture(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr>
            <td>/s/ Steven V. Abramson<br>Steven V. Abramson</td>
            <td>Director</td>
            <td>/s/ Brian Millard<br>Brian Millard</td>
            <td>Chief Financial Officer</td>
          </tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [("Steven V Abramson", "BOARD_OF"), ("Brian Millard", "CFO_OF")],
        )
        self.assertNotIn(("Steven V Abramson", "CFO_OF"), [(rel.name, rel.relationship_type) for rel in extraction.relationships])

    def test_signature_table_does_not_pair_left_power_of_attorney_with_cfo_title_cohr_fixture(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr>
            <td>Attorney-in-fact<br>Officer Pursuant</td>
            <td>Director</td>
            <td>/s/ Mary Jane Raymond<br>Mary Jane Raymond</td>
            <td>Chief Financial Officer</td>
          </tr>
          <tr>
            <td>/s/ R. Anderson<br>R. Anderson</td>
            <td>Director</td>
            <td></td>
            <td></td>
          </tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [("Mary Jane Raymond", "CFO_OF"), ("R Anderson", "BOARD_OF")],
        )
        self.assertNotIn(("Officer Pursuant", "BOARD_OF"), [(rel.name, rel.relationship_type) for rel in extraction.relationships])
        self.assertNotIn(("Officer Pursuant", "CFO_OF"), [(rel.name, rel.relationship_type) for rel in extraction.relationships])
        self.assertNotIn(("R Anderson", "CFO_OF"), [(rel.name, rel.relationship_type) for rel in extraction.relationships])

    def test_signature_free_text_does_not_cross_prior_title_boundary(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>/s/ R. Anderson R. Anderson Director Principal Financial Officer</p>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [("R Anderson", "BOARD_OF")],
        )

    def test_signature_name_title_header_pairs_name_column_to_title(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Name</th><th>Title</th></tr>
          <tr><td>/s/</td><td>Robert A. Bruggeworth</td><td>Chief Executive Officer and Director</td></tr>
          <tr><td>/s/</td><td>Grant Brown</td><td>Chief Financial Officer</td></tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(
            [(rel.name, rel.relationship_type) for rel in extraction.relationships],
            [("Robert A Bruggeworth", "CEO_OF"), ("Robert A Bruggeworth", "BOARD_OF"), ("Grant Brown", "CFO_OF")],
        )

    def test_signature_table_ignores_swapped_title_cells_with_other_signer_names(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><td>Brian Millard</td><td>Steven Abramson Chief Executive Officer</td></tr>
          <tr><td>Steven Abramson</td><td>Brian Millard Chief Financial Officer</td></tr>
        </table>
        </body></html>"""

        extraction = parse_filing_with_sources(html, signature_only=True)

        self.assertEqual(extraction.relationships, [])

    def test_signature_debug_log_records_pairings_without_changing_extraction(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Title</th><th>Signature</th><th>Title</th></tr>
          <tr>
            <td>/s/ Robert A. Bruggeworth<br>Robert A. Bruggeworth</td>
            <td>President and Chief Executive Officer and Director</td>
            <td>/s/ Grant Brown<br>Grant Brown</td>
            <td>Chief Financial Officer</td>
          </tr>
        </table>
        </body></html>"""

        before = parse_filing_with_sources(html, signature_only=True).relationships
        with tempfile.TemporaryDirectory() as tmpdir:
            write_signature_debug_log("QRVO", html, Path(tmpdir))
            debug_text = (Path(tmpdir) / "debug_QRVO.log").read_text(encoding="utf-8")
        after = parse_filing_with_sources(html, signature_only=True).relationships

        self.assertEqual(before, after)
        self.assertIn("1. Raw table rows detected after the Exchange Act anchor", debug_text)
        self.assertIn("2. Detected signer candidates", debug_text)
        self.assertIn("3. Detected title candidates", debug_text)
        self.assertIn("4. Name-title pairings before relationship creation", debug_text)
        self.assertIn("5. Relationships emitted from each pairing", debug_text)
        self.assertIn("Robert A Bruggeworth => Robert A Bruggeworth -> CEO_OF", debug_text)
        self.assertIn("Grant Brown => Grant Brown -> CFO_OF", debug_text)

    def test_signature_debug_log_traces_requested_free_text_cfo_evidence(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>/s/ Robert Bruggeworth Chief Financial Officer</p>
        </body></html>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            write_signature_debug_log("QRVO", html, Path(tmpdir))
            debug_text = (Path(tmpdir) / "debug_QRVO.log").read_text(encoding="utf-8")

        self.assertIn("Robert Bruggeworth -> CFO_OF: emitted by signature_free_text", debug_text)
        self.assertIn("matched title text: Chief Financial Officer", debug_text)
        self.assertIn("matched signer text (lookback): Robert Bruggeworth", debug_text)
        self.assertIn("exact text span", debug_text)

    def test_signature_debug_log_traces_requested_table_cfo_evidence(self):
        html = """<html><body><h1>SIGNATURES</h1>
        <p>Pursuant to the requirements of the Securities Exchange Act of 1934,
        this report has been signed below by the following persons.</p>
        <table>
          <tr><th>Signature</th><th>Title</th></tr>
          <tr><td>/s/ Steven Abramson<br>Steven Abramson</td><td>Chief Financial Officer</td></tr>
        </table>
        </body></html>"""

        with tempfile.TemporaryDirectory() as tmpdir:
            write_signature_debug_log("OLED", html, Path(tmpdir))
            debug_text = (Path(tmpdir) / "debug_OLED.log").read_text(encoding="utf-8")

        self.assertIn("Steven Abramson -> CFO_OF: emitted by signature_table", debug_text)
        self.assertIn("table row: Table 1 row 2", debug_text)
        self.assertIn("title cell: cell 1: Chief Financial Officer", debug_text)
        self.assertIn("signer cell: cell 0: Steven Abramson", debug_text)


if __name__ == "__main__":
    unittest.main()
