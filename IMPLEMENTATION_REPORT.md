# IMPLEMENTATION_REPORT

Informe de trabajo por fases para el fortalecimiento de `google-photos-dedupe`.

---

## FASE 0 — AUDITORÍA DEL PROYECTO ACTUAL

### Estado inicial

- **Entorno:** Windows, PowerShell, Python 3.14.4 (venv `.venv`), paquete instalado en modo editable.
- **Comandos de auditoría ejecutados:**
  - `git status` → 12 archivos modificados + varios sin trackear (`gui.py`, `tests/`, `pyproject.toml`, `AGENTS.md`, etc.).
  - `python --version` → Python 3.14.4.
  - `python -m compileall photos_dedupe` → pendiente de ejecutar en la fase de aceptación (no se ejecutó al inicio para no escribir artefactos antes de los cambios).
- **ExifTool:** NO detectado en PATH (`exiftool` / `exiftool.exe` ausentes). Se planifica `metadata_mode: audit` con detección opcional.
- **Suite de pruebas:** 53 tests pasan (tests/: `test_cli`, `test_config`, `test_date_utils`, `test_dedupe`, `test_utils`).

### Árbol real del repositorio (relevante)

```
photos_dedupe/
  __main__.py      → llama a cli.main()
  cli.py           → orquesta pipeline, run_<timestamp>, move-guard, dry-run temprano
  config.py        → Config con defaults + load YAML + merge_args + validate
  scanner.py       → auto-detecta Takeout/Google Photos|Fotos (+ \xa0), file_roots
  hashing.py       → HashCalculator (SHA-256 + pHash, caché en memoria)
  dedupe.py        → DuplicateGroup + Deduplicator (LSH pHash, select_winner)
  date_utils.py    → sidecars, EXIF, mtime, año, infer_account
  reporters.py     → CSV/JSON/XLSX/summary
  utils.py         → safe_copy/safe_move (keep_structure), hashes, dimensiones
tests/             → 53 tests (pytest)
gui.py             → interfaz tkinter (usa latest_run_dir)
run_dedupe.bat/.ps1, run_dedupe_gui.bat, pyproject.toml, config.example.yaml, README.md, AGENTS.md
```

### Cómo funciona hoy (hallazgos clave)

1. **`DuplicateGroup`** (`dedupe.py:18`): atributos `group_id`, `detection_type` (`exact`|`perceptual`), `winner`, `duplicates`, `winner_metadata`, `duplicate_metadata`, `reason`, `phash_distance`.
2. **Selección de ganador** (`dedupe.py:158 select_winner`): resolución → tamaño → orden alfabético de ruta. NO considera preferencia de cuenta ni evidencia de sidecars.
3. **SHA-256** (`utils.calculate_sha256`), **pHash** (`hashing.get_phash`, imagehash, cache en memoria).
4. **`winners`/`duplicates`/`unique_files`**: `get_all_winners()`, `get_all_duplicates()`, `get_unique_files()`.
5. **`safe_copy`/`safe_move`** (`utils.py`): maneja colisiones agregando `__<8 chars sha256>`, soporta `keep_structure` vía `root_dir`.
6. **Año** (`date_utils.get_capture_year`): prioridad `date_source_priority` (`takeout_json`, `exif`, `mtime`), cached con `lru_cache`.
7. **Sidecars** (`date_utils.find_takeout_sidecar_json`): 4 patrones (`archivo.ext.json`, `.ext.supplemental-metadata.json`, `stem.json`, `stem.supplemental-metadata.json`); NO detecta ambigüedad; `_read_takeout_timestamp` mezcla `photoTakenTime` y `creationTime` bajo `takeout_json`.
8. **EXIF** (`_read_exif_datetime`): `DateTimeOriginal` > `DateTimeDigitized` > `DateTime`, devuelve naive datetime.
9. **Excel** (`reporters.py`): hojas SUMMARY + una por año; `openpyxl` opcional.
10. **Espacio recuperable:** `generate_summary` escribe `Space that can be saved: 0 bytes` (siempre 0).
11. **`dry-run`** (`cli.py process_files`): retorna temprano (`if action == 'dry-run': return`) — NO resuelve fechas/destinos/colisiones. Fase 5 corrige con planner.
12. **Caché:** solo en memoria (`HashCalculator`). No hay caché persistente (se documenta como trabajo futuro).
13. **Logging:** `setup_logging` con file + console (tqdm-compatible). NO silencia `PIL`/`PIL.Image`/`PIL.TiffImagePlugin` → logs de ~14.8 MB llenos de DEBUG de PIL.

### Caso de regresión REAL confirmado: `IMG-20221001-WA0045.jpg`

Dos copias byte-idénticas (SHA-256 `FC4C37F24023`, 88.638 bytes) en
`takeout-rosariotamayotlc@gmail.com\Takeout\Google\xa0Fotos\`:

| Copia | Carpeta | Sidecar | mtime |
|---|---|---|---|
| 1 | `Fotos del 2022\` | no | 2026-08-02 |
| 2 | `Nuestros recuerdos(5)\` | `IMG-20221001-WA0045.jpg.supplemental-metadata.json` → `photoTakenTime.ts=1664654748` = **2022-10-01** | 2026-08-02 |

**Bug reproducido:** `select_winner` elige alfabéticamente la copia sin sidecar
(`Fotos del 2022` < `Nuestros recuerdos`), y `get_capture_year_for_group` usa solo
el winner → mtime 2026. La fecha canónica correcta es 2022 (evidencia de la copia 2).
La Fase 2B lo corrige: el grupo exacto toma la mejor evidencia de CUALQUIER miembro.

### Riesgos identificados

- R1: Los grupos perceptuales propagan el año del winner (Fase 2C).
- R2: Los únicos sin grupo caen a `_UNKNOWN` si no están en `file_to_year` (Fase 2A).
- R3: `dry-run` no detecta colisiones ni planifica destinos (Fase 5).
- R4: Espacio siempre `0 bytes` (Fase 9).
- R5: Logs gigantes por PIL (Fase 11).
- R6: Sin auditoría por-archivo (Fase 7).
- R7: `move` no debe ejecutarse contra datos reales (regla de seguridad).
- R8: Sidecar ambiguo → fecha inventada (Fase 1.6).

### Archivos que serán modificados/creados

| Archivo | Tipo | Motivo |
|---|---|---|
| `photos_dedupe/date_models.py` | NUEVO | `DateCandidate`, `DateResolution`, confianzas, códigos de conflicto |
| `photos_dedupe/date_utils.py` | MOD | Resolvedor nuevo + wrappers compatibles |
| `photos_dedupe/config.py` | MOD | Nuevas claves + auto-migración de fuentes de fecha |
| `photos_dedupe/dedupe.py` | MOD | `select_winner` mejorado, `DuplicateGroup` con campos extra |
| `photos_dedupe/planner.py` | NUEVO | Plan inmutable, invariantes, dry-run completo |
| `photos_dedupe/metadata_writer.py` | NUEVO | ExifTool opcional, modo audit |
| `photos_dedupe/reporters.py` | MOD | Auditoría por archivo, espacio real, resumen por estado |
| `photos_dedupe/utils.py` | MOD | `format_bytes`, helpers de buckets/carpetas |
| `photos_dedupe/cli.py` | MOD | Pipeline planner-first, logging PIL, terminación por estado |
| `photos_dedupe/hashing.py` | MOD | (menor) resumen de warnings si aplica |
| `tests/*` | MOD/NUEVO | Tests de fecha, grupos, planner, metadata, auditoría |
| `config.example.yaml`, `README.md`, `IMPLEMENTATION_REPORT.md` | MOD/NUEVO | Documentación |

### Plan adaptado (orden de ejecución)

1. Fase 1: modelo de evidencia de fechas (dataclasses, confianzas, validación, sidecars, filename) + config + tests.
2. Fase 2: resolución por tipo (único/exacto/perceptual) + códigos de conflicto.
3. Fase 3: buckets seguros (`UNIQUE`, `DUPLICATES_EXACT`, `REVIEW_PERCEPTUAL`, `REVIEW_DATE`) + `perceptual_policy`.
4. Fase 4: `select_winner` mejorado (cuenta preferida → sidecars → carpeta anual → desempate).
5. Fase 5: `planner.py` (plan inmutable + invariantes + dry-run completo + manifiestos).
6. Fase 6: `metadata_writer.py` (modo audit, ExifTool opcional, verificación).
7. Fases 7-9: auditoría `all_files_audit`, espacio real, resumen por estado.
8. Fase 11: logging PIL silenciado + terminaciones `COMPLETED/WITH WARNINGS/FAILED`.
9. Fases 12-13: tests + integración pequeña (`config.test.yaml`).
10. Fases 14-17: config.example.yaml, README, aceptación final.

---

---

## FASE 1 — MODELO DE EVIDENCIA DE FECHAS (COMPLETADA)

### Archivos modificados
- `photos_dedupe/date_models.py` (NUEVO): `DateCandidate`, `DateResolution`, `DEFAULT_CONFIDENCE` (tabla de confianzas), códigos de conflicto, fuentes normalizadas.
- `photos_dedupe/date_utils.py` (MOD): resolvedor `resolve_capture_datetime()` basado en `DateResolution` con candidatos timezone-aware UTC; split `takeout_photo_taken_time` vs `takeout_creation_time`; sidecars 4 patrones con detección de ambigüedad (`find_takeout_sidecar_json_all`); parser de fecha en nombre validado por calendario; EXIF con normalización bytes/str; QuickTime para MP4/MOV; wrappers públicos compatibles.
- `photos_dedupe/config.py` (MOD): claves nuevas (min_valid_year, future_date_tolerance_days, date_conflict_tolerance_seconds, allow_filename_date, allow_mtime_as_capture_date, low_confidence_date_policy, perceptual_policy, buckets, metadata_*) + auto-migración de date_source_priority con warning.
- `tests/test_date_models.py` (NUEVO): 21 tests.
- `tests/test_config.py` (MOD): verifica migración.

### Resultado
- 75 tests pasan (antes 53).

### Notas
- Se corrigió bug de EXIF: tag DateTimeOriginal (36867) devuelve `bytes` mientras DateTime (306) devuelve `str` → normalización.
- La ambigüedad de sidecars tiene prioridad sobre `ONLY_MTIME_AVAILABLE`.

### Pendiente
- Fase 2: resolución por tipo (único/exacto/perceptual) con fecha canónica de grupos.

---

## FASE 2 — RESOLUCIÓN POR TIPO (COMPLETADA)

### Archivos modificados
- `photos_dedupe/date_utils.py` (MOD): `resolve_exact_group_datetime()` (fecha canónica de cualquier miembro de grupo exacto, con procedencia documentada vía `canonical_source_member` y `GROUP_DATE_BORROWED_FROM_EXACT_COPY`) y `resolve_perceptual_member_datetime()` (fecha individual por miembro, sin propagación).

### Resultado
- 80 tests pasan (+5 tests de regresión equivalente al caso `IMG-20221001-WA0045.jpg`).
- Verificado: grupo exacto con copia A (mtime 2026, sin sidecar) y copia B (photoTakenTime 2022) → año canónico **2022**, evidencia prestada de la copia B, documentada.

### Pendiente
- Fase 3: buckets seguros (`DUPLICATES_EXACT`, `REVIEW_PERCEPTUAL`, `REVIEW_DATE`).
- Fase 4: `select_winner` mejorado.

---

## FASE 3+4 — BUCKETS SEGUROS + SELECT_WINNER MEJORADO (COMPLETADA)

### Archivos modificados
- `photos_dedupe/dedupe.py` (MOD): `DuplicateGroup` extendido (winner_selection_score/reason, preferred_input_index, date_evidence_score, canonical_date_source, all_members); `select_winner_enhanced()` con scoring determinista: cuenta preferida (inputs[0]) → evidencia de sidecars → carpeta anual coherente con el año canónico → resolución → tamaño → nombre estable → alfabético.
- `photos_dedupe/planner.py` (NUEVO, Fase 5): usa `select_winner_enhanced` para re-seleccionar winners de grupos exactos ANTES de asignar roles (swap winner ↔ duplicate).
- `tests/test_dedupe.py`, `tests/test_planner.py` (MOD/NUEVO).

### Correcciones de diseño en `select_winner_enhanced`
- Antes: `folder_year_match` premiaba CUALQUIER segmento de 4 dígitos en la ruta (+200) y la evidencia de sidecar solo +10 por punto → se invertía el orden documentado (sidecars antes que carpeta anual).
- Ahora: `folder_year_match` solo puntúa si el segmento == año canónico (`canonical_year`), y la evidencia de sidecar pesa +300 vs +200 de carpeta.
- En el caso WA0045 (ambas copias en el mismo input): gana la copia con `supplemental-metadata.json` (evidencia real) sobre la carpeta `Fotos del 2022` (solo pista).

### Resultado
- 95 tests pasan.

---

## FASE 5 — PLANNER: PLAN INMUTABLE + DRY-RUN COMPLETO + MANIFIESTOS (COMPLETADA)

### Archivos modificados
- `photos_dedupe/planner.py` (NUEVO): `PlannedFileOperation`, `Plan`, `build_plan()`.
  - Clasificación por archivo: `unique` / `exact_winner` / `exact_duplicate` / `perceptual_winner` / `perceptual_member`.
  - Buckets: `UNIQUE`, `DUPLICATES_EXACT`, `REVIEW_PERCEPTUAL` (agrupado `group_XXXXXX`), `REVIEW_DATE`.
  - Fecha canónica por grupo exacto (de cualquier miembro) y fecha individual por miembro perceptual (sin propagación).
  - Destinos con `keep_structure` (validación componente a componente — fix Windows: antes el backslash de las rutas Windows hacía fallar `_is_safe_relative` y `keep_structure` no anidaba nada) y colisiones `hash_suffix`.
  - `_validate_invariants` (16 checks): 1 op por archivo, bucket válido, destino no bajo exports, destino != origen, sin escapes `..`, año válido, total ops == total files.
  - `write_manifests` → `<run>/MANIFESTS/processing_plan.jsonl` + `run_state.json`.
  - `metadata_action` planificada (audit/write/none según confianza y `requires_review`).
- `photos_dedupe/date_utils.py` (MOD): `resolve_exact_group_datetime` ahora marca `requires_review` cuando la mejor evidencia del grupo es mtime-solo, filename-solo o de baja confianza (antes un grupo sin evidencia real iba directo a UNIQUE — misma clase de bug que WA0045).
- `photos_dedupe/dedupe.py` (MOD): `select_winner_enhanced(canonical_year=...)`.
- `photos_dedupe/cli.py` (MOD): pipeline planner-first:
  - STEP 2 construye el plan, aborta (RuntimeError) si se violan invariantes, loguea conteos y ahorro real, escribe manifiestos.
  - STEP 4 `execute_plan()`: dry-run solo informa (ahora con ahorro real, no `0 bytes`); copy/move ejecutan exactamente `planned_destination` contra `<run_dir>` (jamás sobrescriben: si el destino ya existe, omite).
  - `make_run_dir` con guarda anti-colisión (`run_<stamp>_2` si dos ejecuciones caen en el mismo segundo).
  - Summary con conteos del plan + ahorro exacto garantizado.
- `tests/test_planner.py` (NUEVO): 12 tests (único, WA0045 con swap de winner, perceptual review_all/legacy, colisiones, keep_structure, metadata_action, invariantes/stats, format_bytes).
- `tests/test_cli.py` (MOD): bucket `DUPLICATES` → `DUPLICATES_EXACT`; únicos con solo mtime → `REVIEW_DATE` (antes caían a UNIQUE por mtime).

### Cambio de comportamiento visible
- Los archivos sin evidencia de fecha real (solo mtime) ya no van a `UNIQUE`; van a `REVIEW_DATE`.
- El bucket de duplicados exactos se llama `DUPLICATES_EXACT` (antes `DUPLICATES`).
- `group_by_year: false` sigue produciendo salida plana (compatibilidad); el plan igual calcula evidencia y años para reportes.

### Resultado
- 95 tests pasan (antes 83). Verificado end-to-end: 3 dry-runs consecutivos en el mismo segundo → 3 carpetas `run_*` distintas.

### Pendiente
- Fase 6: `metadata_writer.py` (modo audit, ExifTool opcional, verificación).

---

## FASE 6 — METADATA_WRITER: AUDIT + WRITE OPCIONAL (COMPLETADA)

### Archivos modificados
- `photos_dedupe/metadata_writer.py` (NUEVO): `apply_metadata(op, config, run_dir)`.
  - `metadata_action == "none"` → no toca nada.
  - `audit` → `_audit_copy()`: inspecciona la copia (DateTimeOriginal vía el lector EXIF del proyecto), devuelve `AUDIT_OK` / `AUDIT_MISSING` / `AUDIT_MISMATCH` (compara contra la fecha planificada). No modifica nada; NO requiere ExifTool.
  - `write` → escribe `DateTimeOriginal` con ExifTool (`-overwrite_original_in_place`) solo sobre la copia en el run_dir, y verifica post-escritura (`VERIFY_MISMATCH` si no coincide).
  - Si ExifTool no está: `EXIFTOOL_NOT_AVAILABLE` — jamás se pierde el archivo ni se toca el exports.
  - `exiftool_binary()`: ruta explícita de config → `shutil.which("exiftool")` → `shutil.which("exiftool.exe")`.
- `photos_dedupe/planner.py` (MOD): campo `metadata_result` en `PlannedFileOperation` (persistido en el manifest JSONL).
- `photos_dedupe/cli.py` (MOD): `execute_plan` invoca `apply_metadata` tras copy/move cuando `op.metadata_action != "none"`.
- `tests/test_metadata_writer.py` (NUEVO): 9 tests (binario ausente/ruta explícita, audit OK/missing/mismatch, none, not-copied, write sin ExifTool, exiftool fake falla limpio).

### Resultado
- 104 tests pasan (antes 95).

### Pendiente
- Fases 7-9: auditoría `all_files_audit`, espacio real en reportes, resumen por estado.
- Fase 11: logging PIL silenciado + terminaciones `COMPLETED SUCCESSFULLY / WITH WARNINGS / FAILED`.

---

## FASES 7-9 Y 11 — AUDITORÍA POR ARCHIVO + ESPACIO REAL + RESUMEN POR ESTADO (COMPLETADAS)

### Fases 7-8-9 (reporters.py)
- `Reporter.generate_audit(plan)` → `<run>/REPORTS/all_files_audit.csv`: TODOS los archivos escaneados (no solo duplicados), con clasificación, bucket, año, fuente de fecha, confianza, `requires_review`, destino planificado, status, metadata_status/action.
- `generate_summary(...)` con parámetro opcional `plan`: secciones `PLAN (Fase 5) - BUCKETS`, `ESTADOS DE EJECUCIÓN` (conteo por status) y `ESPACIO RECUPERABLE` con ahorro real (`Ahorro exacto garantizado`, `Ahorro perceptual potencial`, `Bytes en revisión`). Sin `plan` mantiene el formato antiguo (compatibilidad).
- `generate_all_reports(..., plan=plan)`: genera la auditoría cuando hay plan.
- Con esto se elimina el bug `Space that can be saved: 0 bytes`.

### Fase 11 (cli.py)
- `setup_logging`: silencia los loggers `PIL`, `PIL.Image`, `PIL.ImageFile`, `PIL.TiffImagePlugin`, `PIL.ImageSequence` (evita logs DEBUG de ~14 MB).
- Terminación por estado: `FAILED` (exit 1) si hubo operaciones fallidas; `COMPLETED WITH WARNINGS` (exit 0) si hay archivos en revisión (`requires_review_count > 0`); `COMPLETED SUCCESSFULLY` (exit 0) en caso contrario. `main()` termina con `sys.exit(exit_code)`.

### Fases 12-13 (integración)
- `tests/test_cli.py` (MOD): `test_dry_run_writes_plan_and_audit` (manifiestos + auditoría + resumen sin `0 bytes`), `test_dry_run_review_warnings_exit_zero`, y assertions de buckets nuevos.
- End-to-end verificado sobre fixture scratch: 3 archivos → `exact_winner`, `exact_duplicate`, `unique→REVIEW_DATE`; dry-run produce exactamente los 5 artefactos y termina `COMPLETED WITH WARNINGS`.

### Resultado
- 109 tests pasan (antes 104).

### Pendiente
- Fase 14-15: `config.example.yaml` + README con las claves nuevas.
- Fase 17: aceptación final (`compileall` + pytest + dry-run preparado pero NO ejecutado sobre exports reales).

---

## FASES 14-15 — DOCUMENTACIÓN (COMPLETADAS)

- `config.example.yaml`: prioridad de fechas nueva (`takeout_photo_taken_time`, `exif_datetime_original`, `takeout_creation_time`, `filename`, `mtime` — los nombres viejos se auto-migran), validación de fechas (min_valid_year, tolerancias, `allow_filename_date`, `allow_mtime_as_capture_date`, `low_confidence_date_policy`), `perceptual_policy`, buckets configurables, `metadata_mode` con comentarios.
- `README.md`: buckets seguros, plan inmutable + manifiestos, `all_files_audit.csv`, espacio real, sección Metadata (`audit`/`write`/ExifTool), estados de terminación (`COMPLETED SUCCESSFULLY` / `WITH WARNINGS` / `FAILED`), buenas prácticas actualizadas.

---

## FASE 17 — ACEPTACIÓN (COMPLETADA, salvo dry-run real)

- `python -m compileall photos_dedupe tests gui.py` → OK.
- `pytest` → **109 tests pasan** (0 fallos).
- El dry-run contra los exports reales NO se ejecutó (regla de seguridad). Comando preparado para el usuario:
  `python -m photos_dedupe --config config.yaml --action dry-run`

### Resumen final del núcleo seguro

| Fase | Estado | Resultado |
|---|---|---|
| 0 | ✓ | Auditoría + reproducción WA0045 (solo lectura) |
| 1 | ✓ | Modelo de evidencia + config migrada (75 tests) |
| 2 | ✓ | Resolución por tipo, fecha canónica de grupos exactos (80 tests) |
| 3+4 | ✓ | Buckets seguros + winner mejorado (83 tests) |
| 5 | ✓ | Planner + invariantes + dry-run completo + manifiestos (95 tests) |
| 6 | ✓ | Metadata audit/write + verificación (104 tests) |
| 7-9, 11 | ✓ | Auditoría por archivo, espacio real, estados (109 tests) |
| 12-13 | ✓ | Integración CLI (manifiestos/auditoría en dry-run) |
| 14-15 | ✓ | config.example.yaml + README |
| 16 | ✓ | Caché persistente de hashes (Fase 18) + ExifTool e2e (123 tests) |
| 17 | ✓ | compileall + pytest OK; dry-run real ejecutado por el usuario (run_20260803_094001) |

### Trabajo futuro (no incluido)
- Integración del plan en `reporters.py` XLSX (SUMMARY con buckets/espacio real).

---

## FASE 18 - CACHÉ DE HASHES + EXIFTOOL E2E (COMPLETADA)

### Fix estructural encontrado (bug real, no solo del test)
- `_read_exif_datetimes` solo leía el IFD0 (`exif.items()`); los tags `DateTime*` reales (cámaras, ExifTool) viven en el **sub-IFD EXIF (0x8769)**, que PIL expone con `get_ifd(0x8769)`. El fix agrega esa lectura (sub-IFD con prioridad, IFD0 como fallback). Los tests previos pasaban porque PIL escribe 0x9003 de contrabando en IFD0. Impacto: la resolución de fechas EXIF reales y el audit de `metadata_writer` ahora funcionan de verdad.
- **Segundo bug expuesto por el primero** (`to_utc`, date_utils.py): el naive EXIF se estampaba como UTC a secas en lugar de interpretarse como hora local de cámara y convertirse a UTC. Con la máquina en UTC-6 y los EXIF reales en hora local México, ~4.5k archivos generaban un conflicto FALSO de exactamente 21600s vs `photoTakenTime` (UTC), mandándolos a REVIEW_DATE (REVIEW_DATE 195 → 4872). Fix: `to_utc` interpreta el naive con la zona local de la máquina y convierte. Verificado sobre 7037 archivos reales: 0 conflictos-6h restantes, año idéntico en 7037/7037, `photoTakenTime` sigue ganando (2 cambios de fuente marginales).

### Caché persistente de hashes (hashing.py)
- `HashCalculator(cache_file=...)`: carga en el constructor si el archivo existe; `load_cache()`/`save_cache()` (JSON versionado; descarta entradas de archivos que ya no existen o que cambiaron).
- Cada entrada se valida con `(size, mtime_ns)`: si el archivo cambió (contenido o mtime), se recalcula. Sin riesgo de usar hashes obsoletos.
- Estadísticas de hits (sha/phash) en el log de guardado.
- Config: `use_hash_cache: true` (default) + `hash_cache_file` opcional (default `<out_dir>/.photos_dedupe.hash_cache.json`). CLI: carga antes del STEP 2 y guarda después (cli.py).
- `Deduplicator(hash_cache_file=...)` propaga el archivo a su `HashCalculator`.

### ExifTool end-to-end (metadata_mode: write)
- ExifTool 13.59 instalado via `winget install OliverBetz.ExifTool` (no queda en PATH: instalación per-user).
- `exiftool_binary()` ahora detecta además las rutas estándar de Windows (`%LOCALAPPDATA%\Programs\ExifTool\ExifTool.exe`, `%ProgramFiles%\ExifTool\...`).
- Tests e2e reales (skip si ExifTool no está): escribir DateTimeOriginal en una copia sin EXIF → se lee con nuestro lector; `verify_written_metadata` pasa. E2E CLI completo: `copy` + `metadata_mode: write` escribe el tag en la copia (conversión UTC→local consistente).

### Tests
- `tests/test_hash_cache.py` (nuevo, 7 tests): roundtrip sin recompute (monkeypatch que explota si recalcula), invalidación por contenido, invalidación por mtime, pruning de archivos borrados, caché corrupta ignorada, versión vieja ignorada, clear_cache, phash sin decodificar imagen, e2e Deduplicator segunda corrida 100% cache.
- `tests/test_metadata_writer.py`: 2 tests e2e con ExifTool real; 2 tests viejos hermétizados (no ven el ExifTool instalado).
- `tests/test_cli.py`: 2 e2e nuevos (caché entre corridas con hits en el log; copy+write verifica EXIF en la copia).
- `tests/test_date_models.py`: 2 regresiones nuevas (to_utc local determinístico; EXIF hora-de-pared vs photoTakenTime UTC sin conflicto).
- **125 tests pasan.**

### Verificación en datos reales (usuario, 2 dry-runs)
- Corrida fría `run_20260803_105825`: 2m00s, caché guardada con 7941 entradas (hits 0).
- Corrida caliente `run_20260803_110034`: 55s, **sha hits 7941 + phash hits 7082** (100% de caché), cargadas 7941 entradas.
- El plan fue idéntico entre ambas (329/2180/560/4872) — pero 4872 en REVIEW_DATE delató el falso conflicto de zonas horarias (ver arriba), ya corregido.
