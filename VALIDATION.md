# Publication validation

- Original recorded rollouts: 3/3 PersonaMem-v3 and 3/3 SpreadsheetBench-2
  completed; scores, traces and per-run verification records are bundled.
- Spreadsheet gold-output positive controls: 3/3 strict accuracy 1.0.
- Adapter unit tests: 4 PersonaMem-v3 and 12 SpreadsheetBench-2 tests passed.
- Copied-checkout spreadsheet integration smoke: passed with the real AIAgent,
  SDK fixture transport, terminal execution, two concurrent tasks, resume,
  and official positive/negative scoring controls. No paid API requests.
- Portable rollout entry point: both command paths validated using --dry-run.
- Full original DAClaw tests were attempted before publication. Collection is
  blocked in the source environment by missing optional `acp` and `mcp` packages
  (7 collection errors). This is not recorded as a passing full test suite.
- Publication excludes credentials, installed environments, cached binary
  dependencies, redundant archive downloads and nested Git repositories.

Historical run `verification.json` files describe the original runs. Fresh
outputs use paths resolved from the cloned checkout.

Fresh virtual environment: `pip install -r requirements-rollout.txt` succeeded.
All 16 adapter unit tests and the real-AIAgent SDK/terminal integration smoke
also passed in that environment. PersonaMem snapshot/prompt/score recomputation
passed for all 3 bundled instances using only the documented install.
