# Windows deployment and update validation (2026-10-21)

## Changes

- Setup resolves its own repository path before Git operations. It can run from
  another working directory and reuses a compatible existing kernel venv.
- `requirements-workbench.txt` is applied on every deployment, including when
  the bootstrap engine is already installed. The rendering stack remains
  BabelDOC 0.6.4 and PyMuPDF 1.28.2. `bitstring` is constrained below 5 because
  version 5 removed the `BitStream` interface used by this BabelDOC version.
- Python subprocess output explicitly uses UTF-8. A real fresh installation
  reproduced the previous GBK error when the patch script printed a check mark.
- The offline runtime check imports the actual launcher kernel and OCR modules,
  detects an image-only page, writes Chinese scan text and rich formula notation,
  and checks that text PDFs bypass OCR without changing the source.
- The launcher continues to use the pinned source submodule, as before this
  change. Its current source version is 2.7.1; the installed bootstrap dependency
  package is pdf2zh-next 2.9.0. Both identities are now distinguished in the
  runtime check. No native-kernel switch or translation-setting change was made.
- GPU pip requirements are separate arguments rather than one invalid combined
  package argument. Existing GPU installation is retained when already present.
- `update_windows.bat` fetches and fast-forwards `origin/main`, then reruns setup.
  It requires local `main`, rejects tracked changes or local-only commits, and
  respects Git's untracked-file collision protection. It never resets or cleans
  user files, stashes edits, or creates a branch.
- Setup and update refuse to run while the normal workbench owns its library
  lock. Generated launchers use relative repository paths and keep the existing
  desktop shortcut name. Settings, tokens and history remain in the same library.
- `-NoStart`, `-SkipOllama` and `-SkipAssets` support unattended checks and users
  with another translation service. Skipping assets does not make first-use
  native translation offline; assets are still downloaded when needed.

## Actual verification

Validation ran on Windows with Windows PowerShell 5.1 and Python 3.12.7. Two
separate Git clones and venvs were created. The final fresh setup started from an
empty venv, installed its packages, replayed patches and passed its runtime check.
Pip's wheel/download cache was allowed; neither venv inherits system site-packages.

A separate Windows user-profile directory was used for real scan translation,
so existing OCR receipts and translation caches were not reused for that first
run. The workbench was started with the new venv, its settings API saved a valid
OCR token, and its upload/translate APIs processed physical pages 16 and 18 of
BS ISO 4427-1:2019. Both cloud OCR jobs were newly submitted. The task completed
in approximately 181 seconds and exported two-page mono and dual PDFs.
The translation service was the existing Ollama HY-MT2 endpoint; its model was
not downloaded again.

For the update test, an independent local Git remote supplied a new `main`
commit. OpenCV was deliberately uninstalled from the test venv. The real updater
fast-forwarded main, repaired the missing dependency and passed setup. All ten
existing library files outside the disposable side cache retained their hashes,
including saved settings, the token and translation artifacts. The generated
batch launcher then started the service on a separate test port; the original
token was still present and the scan task completed again in approximately 44
seconds. This second run resumed the same cloud jobs and used translation caches.

The updated scan retained the original ink in all 132 checked numerical/symbol
regions on pages 16 and 18, without added dark pixels. Both dual source panels
matched the original pixels. In the independent venv, a three-page native PDF
was translated with the original `d0bd06d` engine and current engine under the
same conditions. All six mono/dual output pages matched in text and pixels.
The native run also downloaded its assets into a separate empty profile cache.

- OCR and Windows-update tests: 44 passed. Update cases cover a path containing
  spaces, preservation of user files, dirty tracked files, local commits,
  untracked collisions and a running-workbench lock.
- Workbench unit/API self-test from the independent clone: 36 passed, 0 failed.
- The API tests also verified that the real running service blocks its updater.
- PowerShell syntax, Python formatting and Git whitespace checks passed.

## Boundaries

These are isolated-directory/runtime tests on this Windows computer, not a
second physical machine or a pristine Windows VM. First-time winget/Ollama
installation, new NVIDIA package installation, and every supported Python or
Windows version were not rerun. The tests used CPU layout and an already
available translation service. A valid cloud token, available OCR quota,
network access and a working translation service remain required.
No tokens, customer PDFs, venvs or generated logs are committed.
