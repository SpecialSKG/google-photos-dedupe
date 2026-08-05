"""
dedupe.py

Duplicate detection and winner selection logic.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
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
        # Fase 4: scoreboard de selección del winner
        self.winner_selection_score: int = 0
        self.winner_selection_reason: str = ""
        self.preferred_input_index: int = -1
        self.date_evidence_score: int = 0
        self.canonical_date_source: str = ""
        self.all_members: List[str] = []  # todos los miembros (winner + duplicates)


class Deduplicator:
    """Main deduplication engine."""
    
    def __init__(self, mode: str = "exact", phash_threshold: int = 6, workers: int = 4,
                 hash_cache_file: Optional[str] = None):
        self.mode = mode
        self.phash_threshold = phash_threshold
        self.workers = workers if workers and workers >= 1 else 4
        self.hash_calc = HashCalculator(cache_file=hash_cache_file)
        self.duplicate_groups: List[DuplicateGroup] = []

    def _run_parallel(self, func, items: list) -> list:
        if self.workers <= 1 or len(items) < 2:
            return [func(x) for x in items]
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            return list(executor.map(func, items))

    def _sha256_job(self, file_path: str):
        try:
            return ("ok", file_path, self.hash_calc.get_sha256(file_path))
        except Exception as e:
            logger.error(f"Error processing {file_path}: {e}")
            return ("err", file_path, e)

    def _phash_job(self, file_path: str):
        return file_path, self.hash_calc.get_phash(file_path)

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
        
        results = self._run_parallel(self._sha256_job, files)
        for status, file_path, value in results:
            if status == "err":
                continue
            hash_to_files[value].append(file_path)
        
        # Filter groups with duplicates (more than one file)
        duplicate_groups = [group for group in hash_to_files.values() if len(group) > 1]
        
        logger.info(f"Found {len(duplicate_groups)} exact duplicate groups")
        return duplicate_groups
    
    def find_perceptual_duplicates(self, files: List[str]) -> List[Tuple[List[str], int]]:
        """
        Find perceptual duplicates using pHash with byte-bucket LSH optimization.
        
        Args:
            files: List of file paths (should be images)
            
        Returns:
            List of tuples (duplicate_group, hamming_distance)
        """
        logger.info(f"Finding perceptual duplicates in {len(files)} files...")
        
        # Filter to only images
        image_files = [f for f in files if is_supported_image(f)]
        logger.info(f"Processing {len(image_files)} image files for perceptual hashing")
        
        # Calculate pHash for all images in parallel
        results = self._run_parallel(self._phash_job, image_files)
        file_hashes: Dict[str, str] = {path: phash for path, phash in results if phash}
        
        logger.info(f"Successfully calculated pHash for {len(file_hashes)} images")
        
        # Build byte sets for candidate filtering (LSH optimization)
        # Distance <= 7 guarantees at least one identical 8-bit byte at same position
        def _get_byte_signature(hex_hash: str) -> Optional[frozenset]:
            if len(hex_hash) < 16:
                return None
            try:
                return frozenset((i, int(hex_hash[i*2:i*2+2], 16)) for i in range(8))
            except ValueError:
                return None

        byte_signatures: Dict[str, Optional[frozenset]] = {
            f: _get_byte_signature(h) for f, h in file_hashes.items()
        }
        use_lsh = self.phash_threshold <= 7

        processed = set()
        duplicate_groups = []
        
        files_list = list(file_hashes.keys())
        for i, file1 in enumerate(files_list):
            if file1 in processed:
                continue
            
            group = [file1]
            hash1 = file_hashes[file1]
            sig1 = byte_signatures[file1]
            min_distance = None
            
            for file2 in files_list[i+1:]:
                if file2 in processed:
                    continue
                
                # LSH Candidate check: skip if threshold <= 7 and no shared byte at same position
                if use_lsh and sig1 and byte_signatures[file2]:
                    if not (sig1 & byte_signatures[file2]):
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

    def select_winner_enhanced(
        self,
        files: List[str],
        inputs: Optional[List[str]] = None,
        date_evidence: Optional[Dict[str, int]] = None,
        canonical_date_source: str = "",
        canonical_year: Optional[str] = None,
    ) -> Tuple[str, str, int, int, str]:
        """
        Selección de winner mejorada (Fase 4).
        
        Orden determinista:
          1. Cuenta de entrada preferida (inputs[0] = mejor).
          2. Mayor evidencia de sidecars (date_evidence_score).
          3. Carpeta anual coherente con la fecha canónica (canonical_year).
          4. Resolución (si aplica, distinto SHA entre perceptuales).
          5. Tamaño.
          6. Ruta relativa más corta/limpia.
          7. Nombre estable.
          8. Alfabético como desempate.
        
        Returns: (winner_path, reason, score, preferred_input_index, date_evidence_source)
        """
        if len(files) == 1:
            return files[0], "only file in group", 100, -1, ""

        inputs = list(inputs) if inputs else []

        def _input_index(p: str) -> int:
            try:
                fp = Path(p).resolve()
            except Exception:
                return -1
            for i, inp in enumerate(inputs):
                try:
                    root = Path(inp).resolve()
                    fp.relative_to(root)
                    return i
                except Exception:
                    continue
            return -1

        date_evidence = date_evidence or {}

        scored = []
        for f in files:
            inp_idx = _input_index(f)
            ev = date_evidence.get(f, 0)
            width, height = get_image_dimensions(f)
            size = get_file_size(f)
            resolution = (width * height) if (width and height) else 0
            # carpeta anual: un segmento de la ruta == año canónico (coherencia real)
            folder_year_match = 0
            if canonical_year and canonical_year.isdigit():
                year_segments = {p for p in Path(f).parts if len(p) == 4 and p.isdigit()}
                folder_year_match = 1 if canonical_year in year_segments else 0
            # nombre estable (no incluye (), copia, etc.)
            name = Path(f).name.lower()
            unstable = any(s in name for s in ("(1)", "(2)", "- copia", "edit", "editado"))
            stable_name = 0 if unstable else 1
            rel_len = len(Path(f).parts)

            score = (
                (1000 if inp_idx == 0 else (500 if inp_idx > 0 else 0))
                + ev * 300
                + folder_year_match * 200
                + resolution // 10000
                + size // (1024 * 1024)
                + stable_name * 20
                - rel_len
            )
            scored.append({
                'path': f,
                'score': score,
                'inp_idx': inp_idx,
                'ev': ev,
                'resolution': resolution,
                'size': size,
                'folder_year_match': folder_year_match,
                'rel_len': rel_len,
                'stable_name': stable_name,
            })

        scored.sort(
            key=lambda x: (
                -x['score'],
                x['inp_idx'] if x['inp_idx'] >= 0 else 9999,
                -x['ev'],
                -x['resolution'],
                -x['size'],
                x['path'],
            )
        )
        w = scored[0]
        second = scored[1] if len(scored) > 1 else None

        parts_reason = []
        if w['inp_idx'] == 0:
            parts_reason.append("preferred input (inputs[0])")
        elif w['inp_idx'] > 0:
            parts_reason.append(f"input #{w['inp_idx']}")
        if w['ev'] > 0 and (second is None or w['ev'] > second['ev']):
            parts_reason.append("richest sidecar evidence")
        if w['folder_year_match']:
            parts_reason.append("folder year coherent")
        if second is not None and w['resolution'] > second['resolution']:
            parts_reason.append("higher resolution")
        if second is not None and w['size'] > second['size']:
            parts_reason.append("larger size")
        if not parts_reason:
            parts_reason.append("alphabetically first path")
        reason = "; ".join(parts_reason)
        return w['path'], reason, w['score'], w['inp_idx'], canonical_date_source
    
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
                group.phash_distance = int(distance)
                
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
