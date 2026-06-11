import unittest

from build_constellation_soxx import (
    is_plausible_person_name,
    parse_filing_with_sources,
    person_name_rejection_reason,
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
        ):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_plausible_person_name(candidate))
                self.assertIsNotNone(person_name_rejection_reason(candidate))

    def test_allows_person_like_names(self):
        for candidate in ("Jane Doe", "John Q. Public", "Mary Anne Smith"):
            with self.subTest(candidate=candidate):
                self.assertTrue(is_plausible_person_name(candidate))
                self.assertIsNone(person_name_rejection_reason(candidate))

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


if __name__ == "__main__":
    unittest.main()
