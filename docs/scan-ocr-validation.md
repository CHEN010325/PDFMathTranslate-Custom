# Workbench scan OCR validation (2026-10-20)

## Scope

The precise workbench now detects image-only pages before invoking translation.
Hosted PaddleOCR-VL-1.6 supplies complete document structure and PP-OCRv6 supplies
word coordinates. Both use the same selected scan pages. The configured text
translation provider is retained. Native pages use the original precise kernel;
mixed documents translate each group separately and merge in original order.
Tesseract remains an optional compatibility path. No local Paddle model is installed.

Numerical cells and display formulas retain source pixels. Narrative footnotes
translate as whole paragraphs, with checked technical-span placeholders. Failed
preservation, empty translations, mismatched paragraph counts, unsupported inline
notation and fitting failures stop export. Scan-only terminology correction is
limited to detected polyethylene-piping documents and Chinese output.

Settings include OCR model and access token. Partial API updates preserve the
other field and use the validated settings for execution. Cloud job receipts are
bound to file bytes, page selection, model and token identity; signed result
downloads receive no API credential. Customer artifacts under `output/` are ignored.

## Audit repairs

- Replaced a 99,999-entry page-selection set with an unrestricted sentinel.
- Reference-counted the temporary scan worker entry so overlapping streams cannot
  restore the native entry while another scan stream is active.
- Added cancellation checks around paragraph and clause translation requests and
  between compatibility-flow fragments. An in-flight model request must return
  before those checks can run.
- Protected standalone plus and Unicode minus operators; newly introduced
  operators fail the paragraph contract.
- Reject incomplete translation lists before modifying the page.
- Fixed partial OCR settings updates and whitespace normalization of tokens.

The separate PP-OCRv6/Tesseract flow compositor is retained because those paths
lack the VL paragraph structure; it is not a duplicate of hybrid paragraph flow.
No existing fast-mode or precise-kernel sources were modified.

## Verification

Runtime: Python 3.12.7, PyMuPDF 1.28.2, pdf2zh-next 2.9.0, BabelDOC 0.6.4.
Translation: the existing HY-MT2 7B model through Ollama, temperature 0, QPS 1.

- 39 OCR, routing, geometry, rendering, narrative and settings tests passed.
  Mixed-page routing includes both side-by-side and alternating dual layouts.
- Workbench self-test: 36 passed, 0 failed; its optional live E2E flag was not used.
- Native three-page document: all six mono/dual pages match the unmodified
  `d0bd06d` workbench in extracted text and rendered pixels.
- On the same two-page mixed-document input, the native page matches the
  unmodified workbench in both mono and dual output, in text and pixels.
- The 24-page BS ISO 4427-1:2019 sample completed through the actual workbench in
  approximately 98 seconds. This run resumed the existing cloud OCR tasks and
  used translation caches; it is not a fresh-upload or cold-cache benchmark.
- All 24 rendered mono pages match the previously reviewed translation. All
  original panels in the dual output match the source. OCR preparation preserves
  all source page pixels and dimensions.
- All 30 purely numerical table cells and four standalone formula regions match
  source pixels. For pages 16 and 18, all 132 checked numerical/symbol regions
  preserve original ink with no added dark pixels; 131 also match their entire
  bounding box. One comparison-symbol box has a background-only difference.
- Footnotes on pages 14, 17 and 19 preserve numeric and formula multisets, use
  full-size paragraph flow, and contain no internal placeholder leakage. The
  residual-English pixel audit reported no candidates under its exclusions.
- JavaScript syntax and Git whitespace checks passed.

Expanded kernel tests: 46 tests, 41 passed, two failures and three errors.
The same five cases fail on an isolated checkout of unmodified `d0bd06d`:
`TestV2Bridge.test_cli_args_basic`,
`TestV2BridgeEndToEnd.test_output_defaults_to_input_parent`, and the three
`TestLegacyKernelTranslation` cases. These involve Windows path expectations
and the legacy kernel import environment. This is not an all-green repository
test result.

## Limits

Pages with an existing but poor text/OCR layer are skipped. Mixed image/text
regions within a single page are not automatically re-recognized. Complex inline
formula notation may fail closed. This sample and the native comparison do not
establish recognition quality for arbitrary scans or every translation provider,
and the residual audit excludes protected values, source headers and images.
Full legacy-runtime, installer and cross-platform acceptance were not performed.
