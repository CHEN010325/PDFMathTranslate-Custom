#!/usr/bin/env bash
# Apply local patches to the PDFMathTranslate-next submodule.
# Run this after: git submodule update --init --recursive
set -e
cd "$(dirname "$0")/.."

for patch in patches/*.patch; do
    echo "Applying $patch ..."
    git -C pdf2zh/kernel/PDFMathTranslate-next.git apply --check "$patch" 2>/dev/null \
      && git -C pdf2zh/kernel/PDFMathTranslate-next.git apply "$patch" \
      && echo "  applied." \
      || echo "  skipped (already applied or conflicting)."
done
echo "Done."
