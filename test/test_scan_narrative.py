import unittest
from types import SimpleNamespace
from custom_pdf2zh.scan_narrative import (
    split_footnotes,
    translate_paragraph,
    ScanTerminology,
)
from custom_pdf2zh.scan_math import rich_text


class NarrativeTests(unittest.TestCase):
    def test_operators_are_protected_and_invented_operators_rejected(self):
        calls = []

        def echo(text):
            calls.append(text)
            return text

        source = "Pressure + allowance − loss"
        self.assertEqual(
            translate_paragraph(SimpleNamespace(translate=echo), source, "zh"), source
        )
        self.assertNotIn("+", calls[0])
        self.assertNotIn("−", calls[0])
        for operator in ("+", "−"):
            with self.assertRaisesRegex(RuntimeError, "新增数值"):
                translate_paragraph(
                    SimpleNamespace(translate=lambda _: "译文" + operator),
                    "Pressure",
                    "zh",
                )

    def test_cancellation_during_request_prevents_clause_retries(self):
        cancelled = False
        calls = []

        def check():
            if cancelled:
                raise RuntimeError("cancelled")

        def translate(text):
            nonlocal cancelled
            calls.append(text)
            cancelled = True
            return "omitted symbols"

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            translate_paragraph(
                SimpleNamespace(translate=translate),
                "First at 200 °C. Otherwise at 220 °C.",
                "zh",
                cancel_check=check,
            )
        self.assertEqual(len(calls), 1)

    def test_missing_repeated_symbol_retries_complete_conditions(self):
        calls = []

        def translate(text):
            calls.append(text)
            if len(calls) == 1:
                return "温度为{v0}。"
            return "温度为{v0}。" if "First" in text else "其他条件为{v1}。"

        result = translate_paragraph(
            SimpleNamespace(translate=translate),
            "First at 200 °C. Otherwise at 220 °C.",
            "zh",
        )
        self.assertIn("200 °C", result)
        self.assertIn("220 °C", result)
        self.assertEqual(len(calls), 3)

    def test_terminology_requires_both_document_domain_and_target_language(self):
        model = SimpleNamespace(translate=lambda _: "化合物的融合兼容性")
        source = "compound fusion compatibility"
        self.assertEqual(
            ScanTerminology(model, "polyethylene-piping", "zh").translate(source),
            "混配料的熔接相容性",
        )
        self.assertEqual(
            ScanTerminology(model, None, "zh").translate(source), "化合物的融合兼容性"
        )
        self.assertEqual(
            ScanTerminology(model, "polyethylene-piping", "en").translate(source),
            "化合物的融合兼容性",
        )

    def test_complete_sentence_keeps_value_binding_and_subscripts(self):
        calls = []

        def translate(text):
            calls.append(text)
            return "如果满足条件，可以在{v0}或{v1}进行试验；临界压力为{v2}。"

        result = translate_paragraph(
            SimpleNamespace(translate=translate),
            r"The test may be carried out at 200 °C or 220 °C if conditions hold; critical pressure is $p_{c,S4}$.",
            "zh",
        )
        self.assertIn("200 °C", result)
        self.assertIn("220 °C", result)
        self.assertIn("p<sub>c,S4</sub>", rich_text(result))
        self.assertNotIn("200", calls[0])
        self.assertIn("if conditions hold", calls[0])

    def test_missing_duplicate_and_invented_values_are_rejected(self):
        for output in ["无数值", "{v0}{v0}", "{v0} 999"]:
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(RuntimeError, "保护校验|新增数值"),
            ):
                translate_paragraph(
                    SimpleNamespace(translate=lambda _: output), "At 210 °C", "zh"
                )

    def test_footnotes_are_split_only_with_contiguous_labels(self):
        result = split_footnotes("a First sentence.b Another requirement.c Third note.")
        self.assertEqual([p["label"] for p in result], list("abc"))
        self.assertEqual(result[1]["text"], "Another requirement.")
        self.assertFalse(split_footnotes("a First sentence.c Missing note."))


if __name__ == "__main__":
    unittest.main()
