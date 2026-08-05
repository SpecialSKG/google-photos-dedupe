# AGENTS.md

Single-package Python CLI that detects and consolidates duplicate photos/videos across multiple Google Takeout exports.

## Commands

```bash
python -m photos_dedupe --config config.yaml --action dry-run   # safe preview: no copies/moves
python -m photos_dedupe --config config.yaml --action copy      # copies UNIQUE/ + DUPLICATES/ (exports intact)
python -m photos_dedupe --config config.yaml --action move --confirm-move   # destructive: empties exports
python -m photos_dedupe --help
```

- Entry point is the package (`photos_dedupe/__main__.py` → `cli.py`); a `photos-dedupe` console script is also installed by `pip install -e .`.
- Verify a change with `pytest` (unit) and/or `python -m photos_dedupe --config config.yaml --action dry-run` (end-to-end). No CI, no lint/typecheck config — don't invent them.
- `--action move` empties the source exports and is **destructive**: it aborts with exit code `2` unless you also pass `--confirm-move`.
- Real data lives in `exports/` and `output_consolidado_struct/` (gitignored). `config.yaml` is gitignored; never commit it — only `config.example.yaml`.
- **Each run creates its own subfolder `<out_dir>/run_YYYYMMDD_HHMMSS/`** (`cli.make_run_dir`) containing `UNIQUE/`, `DUPLICATES/`, `LOGS/`, `REPORTS/` — consecutive runs never overlap. `cli.latest_run_dir(out_dir)` returns the most recent `run_*` (used by `gui.py` "Abrir logs/reportes/salida"). The old, pre-`run_` top-level `LOGS/`/`REPORTS/` folders in `output_consolidado_struct/` are leftovers from before this feature.

## Config semantics (`photos_dedupe/config.py`)

- Load order: class defaults → YAML file → CLI args (`--inputs`, `--out-dir`, `--mode`, `--action`, `--phash-threshold`, `--workers`, `--confirm-move`). CLI wins. Note: `--keep-structure` is `store_true` with `default=None`, so an unset flag does **not** clobber a `true` value in `config.yaml` (this previously silently reset `keep_structure` to `False`).
- `mode`: `exact`, `perceptual`, or `exact+perceptual`.
- `inputs` is an ordered list; docs/comments claim `inputs[0]` is the "principal" account used for winner selection, **but that is not implemented**. Winner selection (`dedupe.py:select_winner`) is by resolution → file size → alphabetical path. `inputs` order only affects account labeling in reports (`date_utils.infer_account`). Don't "fix" or rely on input-order priority.
- `workers` now **parallelizes** pHash and SHA-256 computation (`ThreadPoolExecutor` in `dedupe.py`); grouping output stays deterministic (files are re-iterated in source order before hashing, and results are consumed in order).
- `keep_structure` now actually nests files under their path relative to the scanned `Takeout/.../Google Photos` root (or Spanish `Google Fotos`). Under `group_by_year: true` the year subdirectories take precedence and `keep_structure` is ignored — intentional.
- `group_by_year`: each duplicate group's year is derived from the group's **winner** (`date_utils.get_capture_year_for_group`) so a group is never split across year folders.

## Pipeline (per run in `cli.py main()`)

1. `Scanner` auto-detects `Takeout/Google Photos` (or `Google Fotos`) per input; subpath can be forced via `photos_subpath`. JSON sidecars are skipped as media when `ignore_json: true`.
   - **Quirk**: some real exports use a non-breaking space (U+00A0, `\xa0`) in the folder name — `Takeout/Google\xa0Fotos` — not a regular space. `scanner.auto_detect_photos_folder` matches both variants (`Google Fotos` and `Google\xa0Fotos`); don't "simplify" the patterns back to plain ASCII spaces or those accounts silently get skipped.
2. `Deduplicator` — exact (SHA-256) then, in `exact+perceptual`, pHash on images **not already claimed by exact groups**. Videos are only ever deduped exactly (no pHash). pHash comparison uses a byte-bucket LSH pre-filter (`dedupe.find_perceptual_duplicates`) so it is no longer brute-force O(n²) for `phash_threshold <= 7`; the remaining slow step on large corpora is the per-image pHash *computation* (PIL decode), which `workers` now parallelizes — drop to `mode: exact` to skip it entirely.
3. Fixed report files under `<run_dir>/REPORTS/`: `dedupe_report.csv`, `dedupe_report.json`, `dedupe_report.xlsx`, `run_summary.txt`. Logs always go to `<run_dir>/LOGS/run.log` — `run_dir` is the timestamped subfolder from the current run. Filenames are hard-coded; don't invent others.
4. Always write `<out_dir>` **outside** `exports/`, or the scanner will re-scan its own output.

## Style

- Docs, config comments, log lines, and user-facing messages are in Spanish (Rioplatense); code identifiers are a mix of English and Spanish. Match the surrounding language when editing.
- Reuse `safe_copy/safe_move` collision handling (appends first 8 chars of SHA-256 to resolve name clashes) and the shared hashing cache (`HashCalculator`) — the `Deduplicator` never recomputes already-cached hashes.
- Warnings for truncated/corrupt PIL images are intentionally filtered in `setup_logging` — don't treat failed pHash/SHA-256 on one file as a run failure.