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


if __name__ == "__main__":
    unittest.main()
