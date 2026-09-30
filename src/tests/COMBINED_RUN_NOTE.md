# Running `src/tests` and `streamlit_app/tests` in one pytest process

**Official commands: run the two suites separately.**

    cd src && python -m pytest tests
    cd src && python -m pytest ../streamlit_app/tests

Run together (either order), 18 tests in `src/tests/test_provenance.py` fail with
`provenance_source_missing`. Investigated: this is an **environment interaction**, not
shared data or state contamination.

- `streamlit_app/sage_paths.py` runs `os.environ.setdefault(...)` at IMPORT time for
  `IR_PAPERS_ROOT`, `IR_STORE_ROOT`, `IR_RUNS_ROOT`, `IR_RESULTS_ROOT`, `IR_CORRECTIONS_ROOT`,
  pinning them to the real repo directories. In a combined run pytest imports the Streamlit
  test modules during collection, so those variables exist before any `src` test starts.
- `validators._papers_root()` prefers `IR_PAPERS_ROOT` over the module constant
  `validators.PAPERS_ROOT`, which `test_provenance.py` monkeypatches to a tmp dir. With the
  variable set the monkeypatch is ignored and the fake paper is never found.
- Confirmed: `IR_PAPERS_ROOT=<any dir> python -m pytest tests/test_provenance.py` alone gives the
  same 18 failures; without it, all 18 pass.

Not a data hazard so far: no new files appeared under `src/runs` or `src/results` after combined
runs. It is a latent one, though -- a `src` test that neither sets nor clears these variables
would read the real directories in a combined run. If the suites are ever merged, clear the five
`IR_*` variables in a `src/tests/conftest.py` autouse fixture (each test that needs one sets it
with `monkeypatch`).
