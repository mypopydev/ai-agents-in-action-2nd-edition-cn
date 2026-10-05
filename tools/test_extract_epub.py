import unittest

from tools.extract_epub import convert


class ExtractorTableFormattingTest(unittest.TestCase):
    def test_inline_formatting_markers_appear_once_in_table_cells(self):
        html = (
            "<table><tr><td><p>before <strong>bold</strong>, "
            "<em>italic</em>, and <code>inline()</code> after</p></td></tr></table>"
        )

        self.assertEqual(
            convert(html),
            "| before **bold**, *italic*, and `inline()` after |\n|---|\n",
        )

    def test_inline_formatting_outside_table_is_unchanged(self):
        self.assertEqual(convert("<p>before <strong>bold</strong></p>"), "before **bold**\n")


if __name__ == "__main__":
    unittest.main()
