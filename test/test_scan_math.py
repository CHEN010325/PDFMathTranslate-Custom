import unittest

from custom_pdf2zh.scan_math import inline_math, rich_text


class ScanMathTests(unittest.TestCase):
    def test_inline_notation_preserves_decimal_operators_and_index_case(self):
        self.assertEqual(inline_math(r"at $ (23 \pm 2) $ °C"), "at (23 ± 2) °C")
        self.assertEqual(
            inline_math(r"$e_n \leq e \leq e_n + t_y$"), "e_(n) ≤ e ≤ e_(n) + t_(y)"
        )
        self.assertEqual(
            inline_math(r"$\sigma_{LPL}$ and $f_{A}=1$"), "σ_(LPL) and f_(A)=1"
        )
        self.assertEqual(inline_math(r"$80\,^{\circ}\mathrm{C}$"), "80 °C")
        self.assertIn("0.15", inline_math(r"$0.15 \leq MFR < 0.20$"))

    def test_rich_math_is_escaped_and_renders_real_subscripts(self):
        text = rich_text(
            'e_(min), f_(A), σ_(LPL), 0.1 mm <img src="https://invalid.example">'
        )
        self.assertIn("e<sub>min</sub>", text)
        self.assertIn("f<sub>A</sub>", text)
        self.assertIn("σ<sub>LPL</sub>", text)
        self.assertNotIn("<img", text)
        self.assertIn("0.1 mm", text)

    def test_unknown_inline_formula_stops_instead_of_corrupting_it(self):
        with self.assertRaisesRegex(RuntimeError, "行内公式"):
            inline_math(r"$\unsupported{z}$")


if __name__ == "__main__":
    unittest.main()
