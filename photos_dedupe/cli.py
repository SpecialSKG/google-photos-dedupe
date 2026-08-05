"""
cli.py

Command-line interface for the photos deduplication tool.
"""

import argparse
import logging
import sys
import time
import warnings
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from tqdm import tqdm
from photos_dedupe.config import Config
from photos_dedupe.scanner import Scanner
from photos_dedupe.dedupe import Deduplicator
from photos_dedupe.reporters import Reporter
from photos_dedupe.planner import (
    BUCKET_UNIQUE,
    BUCKET_DUPLICATES_EXACT,
    BUCKET_REVIEW_PERCEPTUAL,
    BUCKET_REVIEW_DATE,
    build_plan,
    write_manifests,
    format_bytes,
)

logger = logging.getLogger(__name__)

class TqdmLoggingHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg, file=sys.stdout)  # clave
            self.flush()
        except Exception:
            self.handleError(record)

def format_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    total = int(round(seconds))
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

def make_run_dir(output_dir: Path) -> Path:
    """Crea un subdirectorio run_<timestamp> dentro de output_dir (uno por ejecución)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = output_dir / f"run_{stamp}"
    suffix = 2
    while run_dir.exists():
        # dos ejecuciones en el mismo segundo: desambiguar con _2, _3...
        run_dir = output_dir / f"run_{stamp}_{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir

def latest_run_dir(output_dir: Path) -> Path:
    """Devuelve el subdirectorio run_* más reciente (o el base si no hay ninguno)."""
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return output_dir
    runs = sorted(
        (p for p in output_dir.iterdir() if p.is_dir() and p.name.startswith("run_")),
        key=lambda p: p.name,
    )
    return runs[-1] if runs else output_dir

@contextmanager
def timed_section(logger, title: str, sep: str = "-"):
    line = sep * 80
    logger.info(line)
    logger.info(title)
    logger.info(line)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        logger.info(line)
        logger.info(f"FIN: {title}  |  Tiempo: {format_duration(elapsed)}")
        logger.info(line)

def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    """Setup logging configuration."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run.log"

    log_level = logging.DEBUG if verbose else logging.INFO

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))

    # Console handler (compatible con tqdm)
    console_handler = TqdmLoggingHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Capturar warnings (PIL, etc.) y mandarlos al logging
    logging.captureWarnings(True)
    warnings.simplefilter("default")

    # Si estos warnings te ensucian la terminal, ignorarlos es lo mejor
    warnings.filterwarnings("ignore", message="Truncated File Read")
    warnings.filterwarnings("ignore", message="Image appears to be a malformed MPO file*")

    # Fase 11: silenciar los loggers DEBUG de PIL (evita logs de ~14 MB)
    for pil_logger in ("PIL", "PIL.Image", "PIL.ImageFile", "PIL.TiffImagePlugin", "PIL.ImageSequence"):
        logging.getLogger(pil_logger).setLevel(logging.WARNING)

    logger.info(f"Logging initialized. Log file: {log_file}")

def log_config_pretty(config: Config) -> None:
    logger.info("-" * 80)
    logger.info("CONFIGURACIÓN")
    logger.info("-" * 80)

    logger.info(f"Modo detección: {config.mode}")
    logger.info(f"Acción: {config.action}")
    logger.info(f"Salida (out_dir): {config.out_dir}")

    logger.info(f"Workers: {config.workers}")
    logger.info(f"pHash threshold: {config.phash_threshold}")
    logger.info(f"Keep structure: {config.keep_structure}")
    logger.info(f"Ignore JSON sidecars: {config.ignore_json}")

    # inputs (bonito)
    logger.info(f"Inputs: {len(config.inputs)}")
    for p in config.inputs:
        logger.info(f"  - {p}")

    # subpath opcional
    if getattr(config, "photos_subpath", None):
        if config.photos_subpath:
            logger.info(f"Photos subpath forzado: {config.photos_subpath}")
    
    # Year-based organization
    logger.info(f"Organizar por año: {config.group_by_year}")
    if config.group_by_year:
        logger.info(f"  - Prioridad de fechas: {config.date_source_priority}")
        logger.info(f"  - Timezone mode: {config.timezone_mode}")
        logger.info(f"  - Carpeta año desconocido: {config.unknown_year_dir}")
    
    # Report generation
    logger.info(f"Reportes: CSV={config.reports_csv}, JSON={config.reports_json}, XLSX={config.reports_xlsx}")

    logger.info("-" * 80)

def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Google Photos Deduplication Tool - Multi-account duplicate detection and consolidation',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
            Examples:
                # Using config file (recommended)
                python -m photos_dedupe --config config.yaml
                
                # Using CLI arguments
                python -m photos_dedupe --inputs exports/account1 exports/account2 --out-dir output --mode exact+perceptual

                # Dry run to preview results
                python -m photos_dedupe --config config.yaml --action dry-run
        """
    )
    
    parser.add_argument(
        '--config',
        type=str,
        help='Path to YAML configuration file'
    )
    
    parser.add_argument(
        '--inputs',
        nargs='+',
        help='Input directories containing Google Takeout exports'
    )
    
    parser.add_argument(
        '--out-dir',
        type=str,
        help='Output directory for consolidated files'
    )
    
    parser.add_argument(
        '--mode',
        choices=['exact', 'perceptual', 'exact+perceptual'],
        help='Duplicate detection mode'
    )
    
    parser.add_argument(
        '--action',
        choices=['copy', 'move', 'dry-run'],
        help='Action to perform on files'
    )
    
    parser.add_argument(
        '--phash-threshold',
        type=int,
        help='Perceptual hash distance threshold (default: 6)'
    )
    
    parser.add_argument(
        '--workers',
        type=int,
        help='Number of worker threads (default: 4)'
    )
    
    parser.add_argument(
        '--keep-structure',
        action='store_true',
        default=None,
        help='Preserve directory structure in output'
    )
    
    parser.add_argument(
        '--confirm-move',
        action='store_true',
        help='Confirmar la accion move (destructiva): vacía los exports'
    )
    
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    
    return parser.parse_args()


def execute_plan(plan, config: Config, run_dir: Path, action: str) -> dict:
    """Ejecuta el plan (Fase 5) tal cual fue planificado.

    - dry-run: no toca nada; solo informa y deja los manifiestos.
    - copy: copia a planned_destination (resuelto contra run_dir).
    - move: mueve (destructivo; ya validado por el guardia --confirm-move).
    """
    if action == "dry-run":
        s = plan.summary
        logger.info("DRY RUN MODE - No files will be moved or copied")
        logger.info(f"UNIQUE: {s['counts'].get(BUCKET_UNIQUE, 0)}")
        logger.info(f"DUPLICATES_EXACT: {s['counts'].get(BUCKET_DUPLICATES_EXACT, 0)}")
        logger.info(f"REVIEW_PERCEPTUAL: {s['counts'].get(BUCKET_REVIEW_PERCEPTUAL, 0)}")
        logger.info(f"REVIEW_DATE: {s['counts'].get(BUCKET_REVIEW_DATE, 0)}")
        logger.info(f"Espacio recuperable (exacto, garantizado): {format_bytes(s['guaranteed_exact_savings_bytes'])}")
        logger.info(f"Espacio potencial (perceptual, requiere revisión): {format_bytes(s['potential_perceptual_savings_bytes'])}")
        logger.info("Plan completo en MANIFESTS/processing_plan.jsonl")
        return {"dry_run": True}

    for bucket in (BUCKET_UNIQUE, BUCKET_DUPLICATES_EXACT, BUCKET_REVIEW_PERCEPTUAL, BUCKET_REVIEW_DATE):
        (run_dir / bucket).mkdir(parents=True, exist_ok=True)

    results = {"copied": 0, "moved": 0, "failed": 0, "skipped": 0}
    for op in tqdm(plan.operations, desc=f"Executing {action}", file=sys.stdout, dynamic_ncols=True):
        if op.status != "planned":
            results["skipped"] += 1
            continue
        src = op.source_path
        dest = run_dir / op.planned_destination
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                # defensivo: jamás sobrescribir (no debería ocurrir en run_dir fresco)
                logger.warning(f"Destino ya existía (se omite): {dest}")
                op.status = "skipped"
                op.error = "destination already exists"
                results["skipped"] += 1
                continue
            if action == "copy":
                import shutil as _sh
                _sh.copy2(src, dest)
                op.status = "copied"
                results["copied"] += 1
            elif action == "move":
                import shutil as _sh
                _sh.move(src, dest)
                op.status = "moved"
                results["moved"] += 1
            # Fase 6: metadata planificada (audit/write) sobre la copia
            if op.status in ("copied", "moved") and op.metadata_action != "none":
                try:
                    from photos_dedupe.metadata_writer import apply_metadata
                    op.metadata_result = apply_metadata(op, config, run_dir)
                except Exception as e:
                    logger.error(f"Error en metadata de {src}: {e}")
                    op.metadata_result = {"status": "WRITE_FAILED", "detail": str(e)[:200]}
        except Exception as e:
            logger.error(f"Error {action} {src} → {dest}: {e}")
            op.status = "failed"
            op.error = str(e)
            results["failed"] += 1
    logger.info(f"{action} completado: {results}")
    return results

def main():
    """Main entry point for the CLI."""
    args = parse_arguments()
    
    # Load configuration
    config = Config()
    
    if args.config:
        try:
            config.load_from_file(args.config)
        except Exception as e:
            print(f"Error loading config file: {e}", file=sys.stderr)
            sys.exit(1)
    
    # Merge CLI arguments
    config.merge_args(args)
    
    # Validate configuration
    try:
        config.validate()
    except Exception as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # Guardia de seguridad para la accion destructiva
    if config.action == 'move' and not args.confirm_move:
        print(
            "ADVERTENCIA: '--action move' MUEVE archivos desde los exports al out_dir y no se puede deshacer.\n"
            "Ejecutá primero un dry-run y agregá '--confirm-move' para confirmar explícitamente.",
            file=sys.stderr,
        )
        sys.exit(2)

    start_total = time.perf_counter()
    exit_code = 0
    results = {}
    
    # Setup output directories (ANTES del logging): cada ejecución crea su own subdirectorio run_<timestamp>
    base_dir = Path(config.out_dir)
    run_dir = make_run_dir(base_dir)
    logs_dir = run_dir / "LOGS"

    # Setup logging (ANTES de usar timed_section)
    setup_logging(logs_dir, args.verbose)

    try:
        with timed_section(logger, "STEP 0/4 - Inicialización"):
            logger.info("=" * 80)
            logger.info("Google Photos Deduplication Tool")
            logger.info("=" * 80)
            log_config_pretty(config)
    
        with timed_section(logger, "STEP 1/4 - Escaneo de archivos"):
            # Step 1: Scan for files
            #logger.info("Step 1: Scanning for media files...")
            scanner = Scanner(ignore_json=config.ignore_json)
            all_files = scanner.scan_inputs(config.inputs, config.photos_subpath)
    
            if not all_files:
                logger.error("No media files found!")
                sys.exit(1)
    
            logger.info(f"Found {len(all_files)} total media files")

        with timed_section(logger, "STEP 2/4 - Detección de duplicados"):
            hash_cache_file = None
            if config.use_hash_cache:
                hash_cache_file = config.hash_cache_file or str(
                    Path(config.out_dir) / ".photos_dedupe.hash_cache.json"
                )
            deduplicator = Deduplicator(
                mode=config.mode,
                phash_threshold=config.phash_threshold,
                workers=config.workers,
                hash_cache_file=hash_cache_file
            )
            duplicate_groups = deduplicator.create_duplicate_groups(all_files)
            if hash_cache_file:
                deduplicator.hash_calc.save_cache(hash_cache_file)

            logger.info(f"Found {len(duplicate_groups)} duplicate groups")

            # Fase 5: builder del plan inmutable (fechas, buckets, destinos, invariantes)
            plan = build_plan(all_files, duplicate_groups, config, scanner)

            if plan.invariant_violations:
                logger.error("INVARIANTES DEL PLAN VIOLADAS — abortando por seguridad:")
                for v in plan.invariant_violations[:50]:
                    logger.error(f"  - {v}")
                raise RuntimeError("invariant violations in plan; refusing to continue")

            s = plan.summary
            logger.info("=== PLAN (Fase 5) ===")
            for bucket in (BUCKET_UNIQUE, BUCKET_DUPLICATES_EXACT, BUCKET_REVIEW_PERCEPTUAL, BUCKET_REVIEW_DATE):
                c = s["counts"].get(bucket, 0)
                if c:
                    logger.info(f"  {bucket}: {c} archivos")
            logger.info(f"  Ahorro exacto garantizado: {format_bytes(s['guaranteed_exact_savings_bytes'])}")
            logger.info(f"  Ahorro perceptual potencial: {format_bytes(s['potential_perceptual_savings_bytes'])}")
            if s["requires_review_count"]:
                logger.info(f"  Requieren revisión manual: {s['requires_review_count']}")

            write_manifests(plan, run_dir / "MANIFESTS", config, run_dir)
            logger.info(f"Manifiestos escritos en {run_dir / 'MANIFESTS'}")

            winners = deduplicator.get_all_winners()
            duplicates = deduplicator.get_all_duplicates()
            unique_files = deduplicator.get_unique_files(all_files)

            # All unique files + winners should go to UNIQUE folder
            all_unique = unique_files + winners

            logger.info(f"Unique files: {len(all_unique)}")
            logger.info(f"Duplicate files: {len(duplicates)}")
    
        with timed_section(logger, "STEP 3/4 - Generación de reportes"):
            # Step 3: Generate reports
            reporter = Reporter(str(run_dir))
            reporter.generate_all_reports(
                groups=duplicate_groups,
                total_files=len(all_files),
                unique_files=len(all_unique),
                detected_roots=scanner.get_detected_roots(),
                mode=config.mode,
                action=config.action,
                config=config,
                plan=plan,
            )
    
        with timed_section(logger, "STEP 4/4 - Procesamiento de archivos"):
            # Step 4: ejecutar el plan (dry-run no toca nada)
            results = execute_plan(
                plan=plan,
                config=config,
                run_dir=run_dir,
                action=config.action,
            )
            failed = results.get("failed", 0)
            if failed:
                logger.error(f"{failed} operaciones fallaron — revisá el log.")

        with timed_section(logger, "Summary"):
            # Summary
            status_line = "=" * 80
            s = plan.summary
            failed = results.get("failed", 0)
            warnings = s["requires_review_count"]
            if failed:
                final_status = "FAILED"
                exit_code = 1
            elif warnings:
                final_status = "COMPLETED WITH WARNINGS"
                exit_code = 0
            else:
                final_status = "COMPLETED SUCCESSFULLY"
                exit_code = 0
            logger.info(status_line)
            logger.info(final_status)
            logger.info(status_line)
            logger.info(f"Output directory: {run_dir}")
            logger.info(f"Archivos planificados: {s['total']}")
            logger.info(f"  UNIQUE: {s['counts'].get(BUCKET_UNIQUE, 0)}")
            logger.info(f"  DUPLICATES_EXACT: {s['counts'].get(BUCKET_DUPLICATES_EXACT, 0)}")
            logger.info(f"  REVIEW_PERCEPTUAL: {s['counts'].get(BUCKET_REVIEW_PERCEPTUAL, 0)}")
            logger.info(f"  REVIEW_DATE: {s['counts'].get(BUCKET_REVIEW_DATE, 0)}")
            logger.info(f"Ahorro exacto garantizado: {format_bytes(s['guaranteed_exact_savings_bytes'])}")
            if failed:
                logger.error(f"{failed} operaciones fallaron")
            if warnings:
                logger.warning(f"{warnings} archivos requieren revisión manual")
            logger.info(f"Reports saved to: {run_dir / 'REPORTS'}")
            logger.info(f"Manifiestos en: {run_dir / 'MANIFESTS'}")
            logger.info(f"Logs saved to: {logs_dir}")
            logger.info(status_line)
            total_elapsed = time.perf_counter() - start_total
            logger.info(f"Tiempo total: {format_duration(total_elapsed)}")
            logger.info(status_line)

    except KeyboardInterrupt:
        logger.warning("Cancelado por el usuario (CTRL+C).")
        sys.exit(130)
    except Exception:
        logger.exception("Error inesperado ejecutando photos_dedupe (ver traceback).")
        logger.error(f"TIP: revisá {logs_dir / 'run.log'} para el detalle completo.")
        sys.exit(1)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
