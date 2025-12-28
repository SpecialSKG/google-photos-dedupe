"""
Duplicate detection and winner selection logic.
"""

import logging
from typing import List, Dict, Tuple, Optional
from collections import defaultdict
from pathlib import Path
from photos_dedupe.hashing import HashCalculator
from photos_dedupe.utils import get_file_size, get_image_dimensions, is_supported_image

logger = logging.getLogger(__name__)


class DuplicateGroup:
    """Represents a group of duplicate files."""
    
    def __init__(self, group_id: int, detection_type: str):
        self.group_id = group_id
        self.detection_type = detection_type  # 'exact' or 'perceptual'
        self.winner: Optional[str] = None
        self.duplicates: List[str] = []
        self.winner_metadata: Dict = {}
        self.duplicate_metadata: List[Dict] = []
        self.reason: str = ""
        self.phash_distance: Optional[int] = None


class Deduplicator:
    """Main deduplication engine."""
    
    def __init__(self, mode: str = "exact", phash_threshold: int = 6):
        self.mode = mode
        self.phash_threshold = phash_threshold
        self.hash_calc = HashCalculator()
        self.duplicate_groups: List[DuplicateGroup] = []
    
    def find_exact_duplicates(self, files: List[str]) -> List[List[str]]:
        """
        Find exact duplicates using SHA-256 hashing.
        
        Args:
            files: List of file paths
            
        Returns:
            List of duplicate groups (each group is a list of file paths)
        """
        logger.info(f"Finding exact duplicates in {len(files)} files...")
        
        hash_to_files: Dict[str, List[str]] = defaultdict(list)
        
        for file_path in files:
            try:
                file_hash = self.hash_calc.get_sha256(file_path)
                hash_to_files[file_hash].append(file_path)
            except Exception as e:
                logger.error(f"Error processing {file_path}: {e}")
                continue
        
        # Filter groups with duplicates (more than one file)
        duplicate_groups = [group for group in hash_to_files.values() if len(group) > 1]
        
        logger.info(f"Found {len(duplicate_groups)} exact duplicate groups")
        return duplicate_groups
    
    def find_perceptual_duplicates(self, files: List[str]) -> List[Tuple[List[str], int]]:
        """
        Find perceptual duplicates using pHash.
        
        Args:
            files: List of file paths (should be images)
            
        Returns:
            List of tuples (duplicate_group, hamming_distance)
        """
        logger.info(f"Finding perceptual duplicates in {len(files)} files...")
        
        # Filter to only images
        image_files = [f for f in files if is_supported_image(f)]
        logger.info(f"Processing {len(image_files)} image files for perceptual hashing")
        
        # Calculate pHash for all images
        file_hashes: Dict[str, str] = {}
        for file_path in image_files:
            phash = self.hash_calc.get_phash(file_path)
            if phash:
                file_hashes[file_path] = phash
        
        logger.info(f"Successfully calculated pHash for {len(file_hashes)} images")
        
        # Find similar images using brute force comparison
        # Note: For large datasets, consider using more efficient algorithms
        processed = set()
        duplicate_groups = []
        
        files_list = list(file_hashes.keys())
        for i, file1 in enumerate(files_list):
            if file1 in processed:
                continue
            
            group = [file1]
            hash1 = file_hashes[file1]
            min_distance = None
            
            for file2 in files_list[i+1:]:
                if file2 in processed:
                    continue
                
                hash2 = file_hashes[file2]
                distance = self.hash_calc.hamming_distance(hash1, hash2)
                
                if distance <= self.phash_threshold:
                    group.append(file2)
                    if min_distance is None or distance < min_distance:
                        min_distance = distance
            
            if len(group) > 1:
                processed.update(group)
                duplicate_groups.append((group, min_distance or 0))
        
        logger.info(f"Found {len(duplicate_groups)} perceptual duplicate groups")
        return duplicate_groups
    
    def select_winner(self, files: List[str]) -> Tuple[str, str]:
        """
        Select the best file from a group of duplicates.
        
        Rules:
        1. Prefer higher resolution (width × height) for images
        2. If tie, prefer larger file size
        3. If still tied, use alphabetically first path
        
        Args:
            files: List of duplicate file paths
            
        Returns:
            Tuple of (winner_path, reason)
        """
        if len(files) == 1:
            return files[0], "only file in group"
        
        # Sort files for deterministic results
        sorted_files = sorted(files)
        
        # Collect metadata
        file_metadata = []
        for file_path in sorted_files:
            width, height = get_image_dimensions(file_path)
            size = get_file_size(file_path)
            resolution = (width * height) if (width and height) else 0
            
            file_metadata.append({
                'path': file_path,
                'width': width,
                'height': height,
                'resolution': resolution,
                'size': size
            })
        
        # Sort by resolution (desc), then size (desc), then path (asc)
        file_metadata.sort(key=lambda x: (-x['resolution'], -x['size'], x['path']))
        
        winner = file_metadata[0]
        
        # Determine reason
        if winner['resolution'] > file_metadata[1]['resolution']:
            reason = f"highest resolution ({winner['width']}x{winner['height']})"
        elif winner['size'] > file_metadata[1]['size']:
            reason = f"largest file size ({winner['size']} bytes)"
        else:
            reason = "alphabetically first path"
        
        return winner['path'], reason
    
    def create_duplicate_groups(self, files: List[str]) -> List[DuplicateGroup]:
        """
        Create duplicate groups based on configured mode.
        
        Args:
            files: List of file paths to analyze
            
        Returns:
            List of DuplicateGroup objects
        """
        self.duplicate_groups = []
        group_id = 0
        
        if self.mode in ['exact', 'exact+perceptual']:
            # Find exact duplicates
            exact_groups = self.find_exact_duplicates(files)
            
            for group_files in exact_groups:
                group = DuplicateGroup(group_id, 'exact')
                group_id += 1
                
                # Select winner
                winner, reason = self.select_winner(group_files)
                group.winner = winner
                group.duplicates = [f for f in group_files if f != winner]
                group.reason = reason
                
                # Add metadata
                group.winner_metadata = self._get_file_metadata(winner)
                group.duplicate_metadata = [self._get_file_metadata(f) for f in group.duplicates]
                
                self.duplicate_groups.append(group)
        
        if self.mode in ['perceptual', 'exact+perceptual']:
            # Find perceptual duplicates (only for images not already in exact groups)
            processed_files = set()
            for group in self.duplicate_groups:
                processed_files.add(group.winner)
                processed_files.update(group.duplicates)
            
            remaining_files = [f for f in files if f not in processed_files]
            perceptual_groups = self.find_perceptual_duplicates(remaining_files)
            
            for group_files, distance in perceptual_groups:
                group = DuplicateGroup(group_id, 'perceptual')
                group_id += 1
                
                # Select winner
                winner, reason = self.select_winner(group_files)
                group.winner = winner
                group.duplicates = [f for f in group_files if f != winner]
                group.reason = reason
                group.phash_distance = distance
                
                # Add metadata
                group.winner_metadata = self._get_file_metadata(winner)
                group.duplicate_metadata = [self._get_file_metadata(f) for f in group.duplicates]
                
                self.duplicate_groups.append(group)
        
        logger.info(f"Created {len(self.duplicate_groups)} duplicate groups")
        return self.duplicate_groups
    
    def _get_file_metadata(self, file_path: str) -> Dict:
        """Get file metadata including hash, size, and dimensions."""
        width, height = get_image_dimensions(file_path)
        size = get_file_size(file_path)
        sha256 = self.hash_calc.get_sha256(file_path)
        
        return {
            'path': file_path,
            'sha256': sha256,
            'width': width,
            'height': height,
            'size': size
        }
    
    def get_all_winners(self) -> List[str]:
        """Get list of all winner files."""
        return [group.winner for group in self.duplicate_groups]
    
    def get_all_duplicates(self) -> List[str]:
        """Get list of all duplicate files."""
        duplicates = []
        for group in self.duplicate_groups:
            duplicates.extend(group.duplicates)
        return duplicates
    
    def get_unique_files(self, all_files: List[str]) -> List[str]:
        """Get list of unique files (not duplicates or winners)."""
        all_dupes = set(self.get_all_winners() + self.get_all_duplicates())
        return [f for f in all_files if f not in all_dupes]
