# Marketplace Seller Research & Workflow Automation

A Windows/Linux desktop application for
turning authorized public marketplace information into structured business
data. It covers Gmarket, Coupang, Foodspring, and AliExpress workflows and
exports normalized seller records as CSV and JSON.

Built as an operator-facing product rather than a single extraction script,
it connects category planning, background jobs, recovery and data handoff.

The product is built for a research or operations team that needs a repeatable
collection process. It provides category planning, visible progress, pause and
cancel controls, checkpointed recovery, and output files that can be reviewed
by people or passed to another system.

Developed by **Lee Noah**. The repository includes the desktop application,
recovery tests, and Windows packaging documentation.

## The business problem

Seller research becomes expensive when a team manually opens listings, copies
business details, removes duplicates, and starts over after a browser or
network failure. A long-running job also needs to show what is complete and
what remains uncertain.

This application makes that process operationally visible:

- choose a marketplace and category family;
- inspect the planned work before starting;
- collect seller records with controlled pacing;
- pause, cancel, or resume after interruption;
- preserve partial progress and separate incomplete work from finished data;
- export standard CSV/JSON files for review, analysis, or authorized outreach.

The result is a workflow that can be handed to an operations person instead of
a one-off script that only its author can restart.

## Core workflows

### Marketplace coverage

- **Gmarket:** pre-scan listing categories, cache product identifiers, then
  collect seller information and export category-level and combined results.
- **Coupang:** collect store and seller information through the category
  workflow, with category-family planning, optional multiple instances, and
  per-category continuation.
- **Foodspring:** produce seller and product spreadsheets for the documented
  delivery-event workflow.
- **AliExpress:** explore a large category tree or accept a direct URL, then
  collect Korean-market seller information with local-safe or configured
  proxy-assisted pacing.

### Reliability controls

- A pre-scan turns an open-ended crawl into a visible plan.
- SQLite checkpoints record category progress and collected identifiers.
- Resume logic skips confirmed work and keeps uncertain work marked for review.
- Pause and cancel operate at safe work boundaries so partial output is not
  silently discarded.
- Failed or blocked work is isolated to the affected category or instance
  where the workflow supports it.
- CSV output includes formula-injection protection, and existing result files
  are not overwritten by a later run.

### Output

Depending on the workflow, records include product and store identifiers,
company name, representative, business registration number, e-commerce report
number where available, phone, email, address, store name, rating, follower
count, positive-feedback rate, and collection time. Missing fields remain
missing; the application does not invent business information.

## Why this is useful to a business

The value is repeatability and recoverability. A research team can define a
category scope, review the plan, resume after a controlled interruption, and
receive a consistent dataset with an operational trail. That supports seller
research, market mapping, authorized partner discovery, and internal data
quality review without making a spreadsheet the source of truth.

Live collection depends on platform responses, category changes, account
permissions, and network conditions. Review the release-readiness notes for
the target workflow before running it.

## Architecture

```text
app/main.py                 Qt application entry point
app/core/                   planning, control, crawling, and storage
app/models/                 typed result records
app/workers/                background workers and UI signals
app/ui/                     settings, progress, logs, and results
tests/                      unit and workflow resilience checks
```

The core collection engine is kept separate from the Qt interface. The UI owns
the cancellation/pause controller and passes it to the worker; worker events
are sent back to the UI through Qt signals. This keeps the interface responsive
while the collection engine is waiting on a page or writing a checkpoint.

## Technology

- Python 3.10+ (3.12 recommended)
- PyQt6 desktop UI
- SQLite checkpoint and state storage
- Patchright, Camoufox, and Playwright-compatible browser tooling
- Requests, Beautiful Soup, curl-cffi, Scrapling, and openpyxl
- JSON/CSV and Excel-compatible output
- PyInstaller packaging for the optional Windows executable

## Run from source

### Requirements

- Python 3.10+ (3.12 recommended)
- Windows 10/11 or a compatible Linux desktop
- Internet access to the target marketplace and the required browser runtime

Create an isolated environment and install the pinned dependencies:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/setup_coupang_runtime.py
```

Start the desktop application from the repository root:

```bash
python -m app.main
```

The equivalent entry point is `python app/main.py`.

The browser runtime setup is intentionally explicit. If the packaged
executable is used instead, the supported setup and verification commands are:

```text
SellerCollector.exe --setup-runtime
SellerCollector.exe --verify-runtime
```

The setup may download a large browser runtime. Use the same Windows user for
setup and normal execution so the per-user browser cache is found.

## Typical user flow

1. Select a marketplace, category, output folder, and collection limits.
2. Run the pre-scan where available and review the planned scope.
3. Start collection and monitor progress, logs, and partial results.
4. Pause or cancel at a safe boundary when needed.
5. Reopen the same output location and choose resume after an interruption.
6. Review category-level and combined CSV/JSON output before downstream use.

The original Korean operating manuals remain available in the repository:

- [`docs/SETTINGS_PLATFORM_GUIDE_KO.html`](docs/SETTINGS_PLATFORM_GUIDE_KO.html)
- [`docs/SETTINGS_PLATFORM_GUIDE_KO.pdf`](docs/SETTINGS_PLATFORM_GUIDE_KO.pdf)
- [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md)
- [`docs/CI.md`](docs/CI.md)

## Tests

Run the standard-library test suite from the project root:

```bash
python -m unittest discover -s tests -v
```

The suite covers storage, cancellation, error handling, CSV injection
protection, and resume behavior. UI tests that require PyQt6 are skipped when
the package is unavailable; in a desktop environment they can be run with:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest tests.test_main_window -v
```

The repository also contains focused resilience and release tests for the
platform workflows. Use the commands documented in `TEST_READY.md` and
`RELEASE_READINESS.md` when preparing a packaged build.

## Responsible-use boundary

This project is intended for public, authorized business information and
internal research. Before running it, the operator is responsible for:

- having permission to access and process the target information;
- following each platform’s terms, robots or access rules, and applicable
  privacy and outreach laws;
- using safe request pacing and stopping when a platform signals blocking or
  instability;
- keeping account credentials and proxy settings outside Git;
- reviewing and correcting incomplete or stale records before making a
  business decision.

The repository’s output folders and local settings are not portfolio data.
Never commit real cookies, tokens, account passwords, customer records, or
unreviewed exports.

## Packaging

The optional Windows build uses `pyinstaller.spec` and
`scripts/build_windows.bat`. A packaged executable should be accompanied by
the generated checksum and the release-readiness notes. Code signing and
SmartScreen trust are separate release concerns; the repository documentation
distinguishes internal test signing from a public certificate.

## License and attribution

No root `LICENSE` file was found in this repository. It is available for
technical review, but reuse, redistribution, or commercial use should be
confirmed with the author before copying the code. Runtime dependencies and
browser projects retain their own licenses; consult their upstream terms
before packaging or redistributing them.

