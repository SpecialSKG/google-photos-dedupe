"""
config.py

Configuration loading and validation.
"""

import logging
import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional

from photos_dedupe.date_models import (
    SOURCE_PHOTO_TAKEN_TIME,
    SOURCE_CREATION_TIME,
    SOURCE_EXIF_DATETIME_ORIGINAL,
    SOURCE_EXIF_DATETIME_DIGITIZED,
    SOURCE_EXIF_DATETIME,
    SOURCE_QUICKTIME_MEDIA_CREATE,
    SOURCE_QUICKTIME_TRACK_CREATE,
    SOURCE_QUICKTIME_CREATE,
    SOURCE_FILENAME,
    SOURCE_FOLDER_YEAR_HINT,
    SOURCE_MTIME,
)


class Config:
    """Configuration container for the deduplication tool."""
    
    def __init__(self):
        # Required settings
        self.inputs: List[str] = []
        self.out_dir: str = "output_consolidado"
        
        # Detection settings
        self.mode: str = "exact"  # exact, perceptual, exact+perceptual
        self.action: str = "dry-run"  # copy, move, dry-run
        
        # Advanced settings
        self.phash_threshold: int = 6
        self.workers: int = 4
        self.keep_structure: bool = False
        self.ignore_json: bool = True
        self.photos_subpath: Optional[str] = None
        
        # Year-based organization settings
        self.group_by_year: bool = False
        self.unknown_year_dir: str = "_UNKNOWN"
        self.date_source_priority: List[str] = ["takeout_json", "exif", "mtime"]
        self.timezone_mode: str = "local"  # 'local' or 'UTC'
        
        # Report generation flags
        self.reports_csv: bool = True
        self.reports_json: bool = True
        self.reports_xlsx: bool = True
        
        # Date resolution settings (Fase 1)
        self.min_valid_year: int = 1970
        self.future_date_tolerance_days: int = 1
        self.date_conflict_tolerance_seconds: int = 300
        self.allow_filename_date: bool = True
        self.allow_mtime_as_capture_date: bool = False
        self.low_confidence_date_policy: str = "review"  # review | accept
        
        # Duplicate policy (Fase 3)
        self.perceptual_policy: str = "review_all"  # review_all | legacy_winner
        
        # Bucket names (Fase 3)
        self.exact_duplicates_dir: str = "DUPLICATES_EXACT"
        self.perceptual_review_dir: str = "REVIEW_PERCEPTUAL"
        self.date_review_dir: str = "REVIEW_DATE"
        
        # Metadata (Fase 6)
        self.metadata_mode: str = "audit"  # disabled | audit | write
        self.metadata_writer: str = "exiftool"
        self.exiftool_path: Optional[str] = None
        self.metadata_write_min_confidence: int = 70
        self.metadata_failure_policy: str = "review"
        self.verify_written_metadata: bool = True
        
        # Caché persistente de hashes (Fase 18)
        self.use_hash_cache: bool = True
        self.hash_cache_file: Optional[str] = None  # None → <out_dir>/.photos_dedupe.hash_cache.json
        
    def load_from_file(self, config_path: str) -> None:
        """Load configuration from YAML file."""
        config_file = Path(config_path)
        
        if not config_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")
        
        with open(config_file, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
        
        self.load_from_dict(config_data)
    
    def load_from_dict(self, config_data: Dict[str, Any]) -> None:
        """Load configuration from dictionary."""
        if not config_data:
            return
            
        # Required fields
        if 'inputs' in config_data:
            self.inputs = config_data['inputs']
        if 'out_dir' in config_data:
            self.out_dir = config_data['out_dir']
        
        # Detection settings
        if 'mode' in config_data:
            self.mode = config_data['mode']
        if 'action' in config_data:
            self.action = config_data['action']
        
        # Advanced settings
        if 'phash_threshold' in config_data:
            self.phash_threshold = config_data['phash_threshold']
        if 'workers' in config_data:
            self.workers = config_data['workers']
        if 'keep_structure' in config_data:
            self.keep_structure = config_data['keep_structure']
        if 'ignore_json' in config_data:
            self.ignore_json = config_data['ignore_json']
        if 'photos_subpath' in config_data:
            self.photos_subpath = config_data['photos_subpath']
        
        # Year-based organization settings
        if 'group_by_year' in config_data:
            self.group_by_year = config_data['group_by_year']
        if 'unknown_year_dir' in config_data:
            self.unknown_year_dir = config_data['unknown_year_dir']
        if 'date_source_priority' in config_data:
            self.date_source_priority = config_data['date_source_priority']
        if 'timezone_mode' in config_data:
            self.timezone_mode = config_data['timezone_mode']
        
        # Report generation flags
        if 'reports' in config_data:
            reports = config_data['reports']
            if 'csv' in reports:
                self.reports_csv = reports['csv']
            if 'json' in reports:
                self.reports_json = reports['json']
            if 'xlsx' in reports:
                self.reports_xlsx = reports['xlsx']
        
        # Date resolution settings
        if 'min_valid_year' in config_data:
            self.min_valid_year = config_data['min_valid_year']
        if 'future_date_tolerance_days' in config_data:
            self.future_date_tolerance_days = config_data['future_date_tolerance_days']
        if 'date_conflict_tolerance_seconds' in config_data:
            self.date_conflict_tolerance_seconds = config_data['date_conflict_tolerance_seconds']
        if 'allow_filename_date' in config_data:
            self.allow_filename_date = config_data['allow_filename_date']
        if 'allow_mtime_as_capture_date' in config_data:
            self.allow_mtime_as_capture_date = config_data['allow_mtime_as_capture_date']
        if 'low_confidence_date_policy' in config_data:
            self.low_confidence_date_policy = config_data['low_confidence_date_policy']
        
        # Duplicate policy
        if 'perceptual_policy' in config_data:
            self.perceptual_policy = config_data['perceptual_policy']
        
        # Bucket names
        if 'exact_duplicates_dir' in config_data:
            self.exact_duplicates_dir = config_data['exact_duplicates_dir']
        if 'perceptual_review_dir' in config_data:
            self.perceptual_review_dir = config_data['perceptual_review_dir']
        if 'date_review_dir' in config_data:
            self.date_review_dir = config_data['date_review_dir']
        
        # Metadata
        if 'metadata_mode' in config_data:
            self.metadata_mode = config_data['metadata_mode']
        if 'metadata_writer' in config_data:
            self.metadata_writer = config_data['metadata_writer']
        if 'exiftool_path' in config_data:
            self.exiftool_path = config_data['exiftool_path']
        if 'metadata_write_min_confidence' in config_data:
            self.metadata_write_min_confidence = config_data['metadata_write_min_confidence']
        if 'metadata_failure_policy' in config_data:
            self.metadata_failure_policy = config_data['metadata_failure_policy']
        if 'verify_written_metadata' in config_data:
            self.verify_written_metadata = config_data['verify_written_metadata']
        
        # Caché persistente de hashes
        if 'use_hash_cache' in config_data:
            self.use_hash_cache = bool(config_data['use_hash_cache'])
        if 'hash_cache_file' in config_data:
            self.hash_cache_file = config_data['hash_cache_file']
        
        # Migración de date_source_priority antiguo (advertencia)
        self._migrate_date_sources(config_data)
    
    def _migrate_date_sources(self, config_data: Dict[str, Any]) -> None:
        """Convierte fuentes de fecha antiguas (takeout_json, exif, mtime) al esquema nuevo."""
        if 'date_source_priority' not in config_data:
            return
        warning = logging.getLogger(__name__).warning
        migrated = []
        changed = False
        for src in self.date_source_priority:
            s = str(src).lower().strip()
            if s == 'takeout_json':
                if SOURCE_PHOTO_TAKEN_TIME not in migrated:
                    migrated.append(SOURCE_PHOTO_TAKEN_TIME)
                changed = True
            elif s == 'exif':
                if SOURCE_EXIF_DATETIME_ORIGINAL not in migrated:
                    migrated.append(SOURCE_EXIF_DATETIME_ORIGINAL)
                changed = True
            else:
                if s not in migrated:
                    migrated.append(s)
        if changed:
            warning(
                "Migración de date_source_priority: 'takeout_json'→'takeout_photo_taken_time', "
                "'exif'→'exif_datetime_original'."
            )
            self.date_source_priority = migrated

    def merge_args(self, args: Any) -> None:
        """Merge command-line arguments, overriding config file values."""
        if hasattr(args, 'inputs') and args.inputs:
            self.inputs = args.inputs
        if hasattr(args, 'out_dir') and args.out_dir:
            self.out_dir = args.out_dir
        if hasattr(args, 'mode') and args.mode:
            self.mode = args.mode
        if hasattr(args, 'action') and args.action:
            self.action = args.action
        if hasattr(args, 'phash_threshold') and args.phash_threshold is not None:
            self.phash_threshold = args.phash_threshold
        if hasattr(args, 'workers') and args.workers is not None:
            self.workers = args.workers
        if hasattr(args, 'keep_structure') and args.keep_structure is not None:
            self.keep_structure = args.keep_structure
    
    def validate(self) -> None:
        """Validate configuration settings."""
        if not self.inputs:
            raise ValueError("At least one input directory must be specified")
        
        if not self.out_dir:
            raise ValueError("Output directory must be specified")
        
        if self.mode not in ['exact', 'perceptual', 'exact+perceptual']:
            raise ValueError(f"Invalid mode: {self.mode}. Must be 'exact', 'perceptual', or 'exact+perceptual'")
        
        if self.action not in ['copy', 'move', 'dry-run']:
            raise ValueError(f"Invalid action: {self.action}. Must be 'copy', 'move', or 'dry-run'")
        
        if self.phash_threshold < 0:
            raise ValueError("phash_threshold must be non-negative")
        
        if self.workers < 1:
            raise ValueError("workers must be at least 1")
        
        # Validate year-based organization settings
        valid_date_sources = [
            SOURCE_PHOTO_TAKEN_TIME, SOURCE_CREATION_TIME,
            SOURCE_EXIF_DATETIME_ORIGINAL, SOURCE_EXIF_DATETIME_DIGITIZED, SOURCE_EXIF_DATETIME,
            SOURCE_QUICKTIME_MEDIA_CREATE, SOURCE_QUICKTIME_TRACK_CREATE, SOURCE_QUICKTIME_CREATE,
            SOURCE_FILENAME, SOURCE_FOLDER_YEAR_HINT, SOURCE_MTIME,
            # legadas aceptadas y migradas
            'takeout_json', 'exif',
        ]
        for source in self.date_source_priority:
            if source not in valid_date_sources:
                raise ValueError(f"Invalid date source: {source}. Must be one of {valid_date_sources}")
        
        if self.timezone_mode not in ['local', 'UTC', 'utc']:
            raise ValueError(f"Invalid timezone_mode: {self.timezone_mode}. Must be 'local' or 'UTC'")
        
        if self.low_confidence_date_policy not in ('review', 'accept'):
            raise ValueError(
                f"Invalid low_confidence_date_policy: {self.low_confidence_date_policy}. "
                "Use 'review' or 'accept'"
            )
        
        if self.perceptual_policy not in ('review_all', 'legacy_winner'):
            raise ValueError(
                f"Invalid perceptual_policy: {self.perceptual_policy}. "
                "Use 'review_all' or 'legacy_winner'"
            )
        
        if self.metadata_mode not in ('disabled', 'audit', 'write'):
            raise ValueError(
                f"Invalid metadata_mode: {self.metadata_mode}. "
                "Use 'disabled', 'audit' or 'write'"
            )
        
        if self.min_valid_year < 1900 or self.min_valid_year > 2030:
            raise ValueError(f"min_valid_year out of range: {self.min_valid_year}")
        
        if self.future_date_tolerance_days < 0:
            raise ValueError("future_date_tolerance_days must be non-negative")
        
        if self.date_conflict_tolerance_seconds < 0:
            raise ValueError("date_conflict_tolerance_seconds must be non-negative")
        
        # Validate input directories exist
        for input_dir in self.inputs:
            if not Path(input_dir).exists():
                raise FileNotFoundError(f"Input directory not found: {input_dir}")
    
    def __repr__(self) -> str:
        """String representation of configuration."""
        return (
            f"Config(\n"
            f"  inputs={self.inputs},\n"
            f"  out_dir='{self.out_dir}',\n"
            f"  mode='{self.mode}',\n"
            f"  action='{self.action}',\n"
            f"  phash_threshold={self.phash_threshold},\n"
            f"  workers={self.workers},\n"
            f"  keep_structure={self.keep_structure},\n"
            f"  ignore_json={self.ignore_json},\n"
            f"  group_by_year={self.group_by_year},\n"
            f"  unknown_year_dir='{self.unknown_year_dir}',\n"
            f"  date_source_priority={self.date_source_priority},\n"
            f"  timezone_mode='{self.timezone_mode}',\n"
            f"  reports=(csv={self.reports_csv}, json={self.reports_json}, xlsx={self.reports_xlsx}),\n"
            f"  date=(min_year={self.min_valid_year}, allow_filename={self.allow_filename_date}, "
            f"allow_mtime={self.allow_mtime_as_capture_date}),\n"
            f"  perceptual_policy='{self.perceptual_policy}',\n"
            f"  metadata_mode='{self.metadata_mode}'\n"
            f")"
        )
