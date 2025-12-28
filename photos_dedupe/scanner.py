"""
File system scanning and auto-detection of Google Takeout folders.
"""

import os
import logging
from pathlib import Path
from typing import List, Optional
from photos_dedupe.utils import is_supported_media

logger = logging.getLogger(__name__)


class Scanner:
    """Scanner for detecting and indexing media files from Google Takeout exports."""
    
    def __init__(self, ignore_json: bool = True):
        self.ignore_json = ignore_json
        self.detected_roots: List[str] = []
    
    def auto_detect_photos_folder(self, input_dir: str) -> Optional[str]:
        """
        Auto-detect the Google Photos folder within a Takeout export.
        
        Looks for:
        - Takeout/Google Fotos/
        - Takeout/Google Photos/
        
        Returns:
            Path to the photos folder, or None if not found.
        """
        input_path = Path(input_dir)
        
        # Common patterns to search for
        patterns = [
            "Takeout/Google Fotos",
            "Takeout/Google Photos",
            "Google Fotos",
            "Google Photos",
        ]
        
        for pattern in patterns:
            potential_path = input_path / pattern
            if potential_path.exists() and potential_path.is_dir():
                logger.info(f"Auto-detected photos folder: {potential_path}")
                return str(potential_path)
        
        # If no pattern matched, check if the input directory itself contains media files
        # This handles cases where user directly points to the photos folder
        if self._contains_media_files(input_dir):
            logger.info(f"Using input directory directly as photos folder: {input_dir}")
            return input_dir
        
        logger.warning(f"Could not auto-detect photos folder in: {input_dir}")
        return None
    
    def _contains_media_files(self, directory: str, max_depth: int = 2) -> bool:
        """Check if directory contains media files (quick check, not exhaustive)."""
        dir_path = Path(directory)
        
        for root, dirs, files in os.walk(dir_path):
            # Limit depth for performance
            depth = len(Path(root).relative_to(dir_path).parts)
            if depth > max_depth:
                continue
            
            for file in files:
                file_path = os.path.join(root, file)
                if is_supported_media(file_path):
                    return True
        
        return False
    
    def scan_directory(self, root_dir: str) -> List[str]:
        """
        Recursively scan directory for media files.
        
        Args:
            root_dir: Root directory to scan
            
        Returns:
            List of absolute paths to media files
        """
        media_files = []
        root_path = Path(root_dir)
        
        logger.info(f"Scanning directory: {root_dir}")
        
        for root, dirs, files in os.walk(root_path):
            for file in files:
                file_path = os.path.join(root, file)
                
                # Skip JSON files if configured
                if self.ignore_json and file.lower().endswith('.json'):
                    continue
                
                # Check if it's a supported media file
                if is_supported_media(file_path):
                    media_files.append(file_path)
        
        logger.info(f"Found {len(media_files)} media files in {root_dir}")
        return media_files
    
    def scan_inputs(self, input_dirs: List[str], photos_subpath: Optional[str] = None) -> List[str]:
        """
        Scan multiple input directories for media files.
        
        Args:
            input_dirs: List of input directories
            photos_subpath: Optional forced subpath instead of auto-detection
            
        Returns:
            List of all media files found across all inputs
        """
        all_files = []
        
        for input_dir in input_dirs:
            logger.info(f"Processing input: {input_dir}")
            
            # Determine the actual photos folder
            if photos_subpath:
                # Use forced subpath
                photos_folder = str(Path(input_dir) / photos_subpath)
                if not Path(photos_folder).exists():
                    logger.warning(f"Forced subpath does not exist: {photos_folder}")
                    continue
            else:
                # Auto-detect
                photos_folder = self.auto_detect_photos_folder(input_dir)
                if not photos_folder:
                    logger.warning(f"Skipping input (no photos folder found): {input_dir}")
                    continue
            
            # Track detected roots for reporting
            self.detected_roots.append(photos_folder)
            
            # Scan the folder
            files = self.scan_directory(photos_folder)
            all_files.extend(files)
        
        logger.info(f"Total media files found: {len(all_files)}")
        return all_files
    
    def get_detected_roots(self) -> List[str]:
        """Get list of detected photo folder roots."""
        return self.detected_roots
