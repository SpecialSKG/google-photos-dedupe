"""
Configuration loading and validation.
"""

import yaml
from pathlib import Path
from typing import Dict, List, Any, Optional


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
        valid_date_sources = ['takeout_json', 'exif', 'mtime']
        for source in self.date_source_priority:
            if source not in valid_date_sources:
                raise ValueError(f"Invalid date source: {source}. Must be one of {valid_date_sources}")
        
        if self.timezone_mode not in ['local', 'UTC']:
            raise ValueError(f"Invalid timezone_mode: {self.timezone_mode}. Must be 'local' or 'UTC'")
        
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
            f"  reports=(csv={self.reports_csv}, json={self.reports_json}, xlsx={self.reports_xlsx})\n"
            f")"
        )
