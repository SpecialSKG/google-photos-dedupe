"""
Command-line interface for the photos deduplication tool.
"""

import argparse
import logging
import sys
import time
import warnings
from contextlib import contextmanager
from pathlib import Path
from tqdm import tqdm
from photos_dedupe.config import Config
from photos_dedupe.scanner import Scanner
from photos_dedupe.dedupe import Deduplicator
from photos_dedupe.reporters import Reporter
from photos_dedupe.utils import safe_copy, safe_move
from photos_dedupe.date_utils import get_capture_year_for_group

logger = logging.getLogger(__name__)

class TqdmLoggingHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)  # respeta la barra de progreso
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
    warnings.filterwarnings("once", message="Truncated File Read")
    warnings.filterwarnings("once", message="Image appears to be a malformed MPO file*")

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
        help='Preserve directory structure in output'
    )
    
    parser.add_argument(
        '--verbose',
        '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    
    return parser.parse_args()


def process_files(files: list, action: str, unique_dir: Path, duplicates_dir: Path, 
                 winners: list, duplicates: list, keep_structure: bool = False,
                 config: Config = None, duplicate_groups: list = None) -> None:
    """Process files according to the specified action.
    
    Args:
        files: All files
        action: Action to perform (copy, move, dry-run)
        unique_dir: Base UNIQUE directory
        duplicates_dir: Base DUPLICATES directory
        winners: List of winner files
        duplicates: List of duplicate files
        keep_structure: Whether to preserve directory structure
        config: Configuration object (for year-based organization)
        duplicate_groups: List of DuplicateGroup objects (for group-based year assignment)
    """
    
    if action == 'dry-run':
        logger.info("DRY RUN MODE - No files will be moved or copied")
        logger.info(f"Would copy {len(winners)} winners to UNIQUE/")
        logger.info(f"Would copy {len(duplicates)} duplicates to DUPLICATES/")
        return
    
    # Check if year-based organization is enabled
    group_by_year = config and config.group_by_year
    
    # If group_by_year is enabled, we need to process by groups
    if group_by_year and duplicate_groups:
        logger.info("Organizing output by year (group-based)...")
        
        # Build a mapping of file paths to their group's year
        file_to_year = {}
        
        for group in tqdm(duplicate_groups, desc="Determining years for groups"):
            # Get year from the winner (authoritative source for the group)
            winner_path = Path(group.winner)
            group_year = get_capture_year_for_group(
                winner_path,
                config.date_source_priority,
                config.unknown_year_dir,
                config.timezone_mode
            )
            
            # Assign this year to all files in the group
            file_to_year[str(group.winner)] = group_year
            for dup in group.duplicates:
                file_to_year[str(dup)] = group_year
        
        # Process winners (unique files)
        logger.info(f"Processing {len(winners)} winner files...")
        for file_path in tqdm(winners, desc="Copying winners to UNIQUE/<YEAR>"):
            try:
                # Get year for this file (if it's part of a group)
                year = file_to_year.get(str(file_path), config.unknown_year_dir)
                
                # Create year-based destination
                year_unique_dir = unique_dir / year
                
                if action == 'copy':
                    safe_copy(file_path, str(year_unique_dir), keep_structure)
                elif action == 'move':
                    safe_move(file_path, str(year_unique_dir), keep_structure)
            except Exception as e:
                logger.error(f"Error processing winner {file_path}: {e}")
        
        # Process duplicates
        logger.info(f"Processing {len(duplicates)} duplicate files...")
        for file_path in tqdm(duplicates, desc="Copying duplicates to DUPLICATES/<YEAR>"):
            try:
                # Get year for this file (should be in the mapping)
                year = file_to_year.get(str(file_path), config.unknown_year_dir)
                
                # Create year-based destination
                year_duplicates_dir = duplicates_dir / year
                
                if action == 'copy':
                    safe_copy(file_path, str(year_duplicates_dir), keep_structure)
                elif action == 'move':
                    safe_move(file_path, str(year_duplicates_dir), keep_structure)
            except Exception as e:
                logger.error(f"Error processing duplicate {file_path}: {e}")
    
    else:
        # Original behavior: no year-based organization
        # Process winners (unique files)
        logger.info(f"Processing {len(winners)} winner files...")
        for file_path in tqdm(winners, desc="Copying winners to UNIQUE"):
            try:
                if action == 'copy':
                    safe_copy(file_path, str(unique_dir), keep_structure)
                elif action == 'move':
                    safe_move(file_path, str(unique_dir), keep_structure)
            except Exception as e:
                logger.error(f"Error processing winner {file_path}: {e}")
        
        # Process duplicates
        logger.info(f"Processing {len(duplicates)} duplicate files...")
        for file_path in tqdm(duplicates, desc="Copying duplicates to DUPLICATES"):
            try:
                if action == 'copy':
                    safe_copy(file_path, str(duplicates_dir), keep_structure)
                elif action == 'move':
                    safe_move(file_path, str(duplicates_dir), keep_structure)
            except Exception as e:
                logger.error(f"Error processing duplicate {file_path}: {e}")


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

    start_total = time.perf_counter()
    
    # Setup output directories (ANTES del logging)
    output_dir = Path(config.out_dir)
    unique_dir = output_dir / "UNIQUE"
    duplicates_dir = output_dir / "DUPLICATES"
    logs_dir = output_dir / "LOGS"

    # Setup logging (ANTES de usar timed_section)
    setup_logging(logs_dir, args.verbose)

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
        # Step 2: Detect duplicates
        #logger.info("Step 2: Detecting duplicates...")
        deduplicator = Deduplicator(mode=config.mode, phash_threshold=config.phash_threshold)
        duplicate_groups = deduplicator.create_duplicate_groups(all_files)
    
        winners = deduplicator.get_all_winners()
        duplicates = deduplicator.get_all_duplicates()
        unique_files = deduplicator.get_unique_files(all_files)
    
        # All unique files + winners should go to UNIQUE folder
        all_unique = unique_files + winners
    
        logger.info(f"Found {len(duplicate_groups)} duplicate groups")
        logger.info(f"Unique files: {len(all_unique)}")
        logger.info(f"Duplicate files: {len(duplicates)}")
    
    with timed_section(logger, "STEP 3/4 - Generación de reportes"):
        # Step 3: Generate reports
        #logger.info("Step 3: Generating reports...")
        reporter = Reporter(str(output_dir))
        reporter.generate_all_reports(
            groups=duplicate_groups,
            total_files=len(all_files),
            unique_files=len(all_unique),
            detected_roots=scanner.get_detected_roots(),
            mode=config.mode,
            action=config.action,
            config=config
        )
    
    with timed_section(logger, "STEP 4/4 - Procesamiento de archivos"):
        # Step 4: Process files
        #logger.info("Step 4: Processing files...")
        process_files(
            files=all_files,
            action=config.action,
            unique_dir=unique_dir,
            duplicates_dir=duplicates_dir,
            winners=all_unique,
            duplicates=duplicates,
            keep_structure=config.keep_structure,
            config=config,
            duplicate_groups=duplicate_groups
        )
    
    with timed_section(logger, "Summary"):
        # Summary
        logger.info("=" * 80)
        logger.info("COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info(f"Output directory: {output_dir}")
        logger.info(f"Unique files: {len(all_unique)}")
        logger.info(f"Duplicate files: {len(duplicates)}")
        logger.info(f"Reports saved to: {output_dir / 'REPORTS'}")
        logger.info(f"Logs saved to: {logs_dir}")
        logger.info("=" * 80)
        total_elapsed = time.perf_counter() - start_total
        logger.info(f"Tiempo total: {format_duration(total_elapsed)}")

if __name__ == "__main__":
    main()
