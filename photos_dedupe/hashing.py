"""
hashing.py

Hash calculation for exact and perceptual duplicate detection.

Caché persistente (Fase 18): los hashes calculados se guardan en un archivo
JSON y se reutilizan en corridas posteriores. Cada entrada se valida con
(size, mtime_ns) del archivo fuente: si cambió, se recalcula.
"""

import json
import logging
import os
from typing import Optional, Dict
from pathlib import Path
from photos_dedupe.utils import calculate_sha256, is_supported_image

logger = logging.getLogger(__name__)

# Optional imagehash import for perceptual hashing
try:
    import imagehash
    from PIL import Image
    IMAGEHASH_AVAILABLE = True
except ImportError:
    IMAGEHASH_AVAILABLE = False
    logger.warning("imagehash not available. Perceptual hashing will be disabled.")

_CACHE_VERSION = 1


class HashCalculator:
    """Calculator for exact (SHA-256) and perceptual (pHash) hashes."""
    
    def __init__(self, cache_file: Optional[str] = None):
        self.sha256_cache: Dict[str, str] = {}
        self.phash_cache: Dict[str, str] = {}
        self.cache_file = cache_file
        self._disk_cache: Dict[str, dict] = {}  # abs_path -> {"size", "mtime_ns", "sha256", "phash"}
        self._cache_stats = {"sha256_hits": 0, "phash_hits": 0}
        if cache_file and os.path.isfile(cache_file):
            self.load_cache(cache_file)

    # ---------------- caché persistente ----------------

    def _stat(self, file_path: str) -> Optional[tuple]:
        try:
            st = os.stat(file_path)
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def _disk_lookup(self, file_path: str, kind: str) -> Optional[str]:
        """Hash desde caché persistente solo si size/mtime coinciden."""
        entry = self._disk_cache.get(os.path.abspath(file_path))
        if not entry:
            return None
        st = self._stat(file_path)
        if st is None or entry.get("size") != st[0] or entry.get("mtime_ns") != st[1]:
            return None
        return entry.get(kind)

    def _disk_store(self, file_path: str, kind: str, value: str) -> None:
        entry = self._disk_cache.setdefault(os.path.abspath(file_path), {})
        st = self._stat(file_path)
        if st is None:
            return
        entry["size"] = st[0]
        entry["mtime_ns"] = st[1]
        entry[kind] = value

    def load_cache(self, cache_file: str) -> int:
        """Carga la caché persistente. Devuelve cantidad de entradas; 0 si falla."""
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") != _CACHE_VERSION:
                logger.info(f"Caché de hashes con versión antigua, se ignora: {cache_file}")
                return 0
            entries = data.get("entries", {})
            self._disk_cache = {
                k: v for k, v in entries.items()
                if isinstance(v, dict) and isinstance(v.get("size"), int) and isinstance(v.get("mtime_ns"), int)
            }
            logger.info(f"Caché de hashes cargada: {len(self._disk_cache)} entradas desde {cache_file}")
            return len(self._disk_cache)
        except Exception as e:
            logger.warning(f"No se pudo cargar la caché de hashes {cache_file}: {e}")
            return 0

    def save_cache(self, cache_file: Optional[str] = None) -> int:
        """Guarda la caché persistente (descarta entradas cuyos archivos ya no existen)."""
        path = cache_file or self.cache_file
        if not path:
            return 0
        pruned = {}
        for abs_path, entry in self._disk_cache.items():
            st = self._stat(abs_path)
            if st is None:
                continue  # el archivo ya no existe: no vale la pena conservarlo
            if entry.get("size") != st[0] or entry.get("mtime_ns") != st[1]:
                continue  # cambió: se recalculará en la próxima corrida
            pruned[abs_path] = entry
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": _CACHE_VERSION, "entries": pruned}, f, ensure_ascii=False)
            os.replace(tmp, path)
            logger.info(
                f"Caché de hashes guardada: {len(pruned)} entradas en {path}"
                f" (sha hits {self._cache_stats['sha256_hits']}, phash hits {self._cache_stats['phash_hits']})"
            )
            self._disk_cache = pruned
            return len(pruned)
        except Exception as e:
            logger.warning(f"No se pudo guardar la caché de hashes {path}: {e}")
            return 0

    def cache_stats(self) -> dict:
        return dict(self._cache_stats)

    # ---------------- hashes ----------------

    def get_sha256(self, file_path: str, use_cache: bool = True) -> str:
        """
        Calculate SHA-256 hash of a file.
        
        Args:
            file_path: Path to the file
            use_cache: Whether to use cached hash if available
        """
        if use_cache:
            if file_path in self.sha256_cache:
                return self.sha256_cache[file_path]
            cached = self._disk_lookup(file_path, "sha256")
            if cached:
                self._cache_stats["sha256_hits"] += 1
                self.sha256_cache[file_path] = cached
                return cached
        
        try:
            hash_value = calculate_sha256(file_path)
            self.sha256_cache[file_path] = hash_value
            self._disk_store(file_path, "sha256", hash_value)
            return hash_value
        except Exception as e:
            logger.error(f"Error calculating SHA-256 for {file_path}: {e}")
            raise
    
    def get_phash(self, file_path: str, use_cache: bool = True) -> Optional[str]:
        """
        Calculate perceptual hash (pHash) of an image.
        
        Args:
            file_path: Path to the image file
            use_cache: Whether to use cached hash if available
            
        Returns:
            pHash as string, or None if not an image or calculation fails
        """
        if not IMAGEHASH_AVAILABLE:
            return None
        
        if not is_supported_image(file_path):
            return None
        
        if use_cache:
            if file_path in self.phash_cache:
                return self.phash_cache[file_path]
            cached = self._disk_lookup(file_path, "phash")
            if cached:
                self._cache_stats["phash_hits"] += 1
                self.phash_cache[file_path] = cached
                return cached
        
        try:
            with Image.open(file_path) as img:
                # Convert to RGB if necessary (handles HEIC, PNG with alpha, etc.)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                
                # Calculate perceptual hash
                phash = imagehash.phash(img)
                hash_str = str(phash)
                self.phash_cache[file_path] = hash_str
                self._disk_store(file_path, "phash", hash_str)
                return hash_str
        except Exception as e:
            logger.warning(f"Could not calculate pHash for {file_path}: {e}")
            return None
    
    def hamming_distance(self, hash1: str, hash2: str) -> int:
        """
        Calculate Hamming distance between two hash strings.
        
        Args:
            hash1: First hash string
            hash2: Second hash string
            
        Returns:
            Hamming distance (number of differing bits)
        """
        if not IMAGEHASH_AVAILABLE:
            return 0
        
        try:
            # imagehash can compare hash strings directly
            h1 = imagehash.hex_to_hash(hash1)
            h2 = imagehash.hex_to_hash(hash2)
            return h1 - h2  # This returns Hamming distance
        except Exception as e:
            logger.warning(f"Error calculating Hamming distance: {e}")
            return 999  # Return high value to indicate incomparable hashes
    
    def are_perceptually_similar(self, file1: str, file2: str, threshold: int = 6) -> tuple[bool, int]:
        """
        Check if two images are perceptually similar.
        
        Args:
            file1: Path to first image
            file2: Path to second image
            threshold: Maximum Hamming distance to consider similar
            
        Returns:
            Tuple of (is_similar, hamming_distance)
        """
        if not IMAGEHASH_AVAILABLE:
            return False, -1
        
        phash1 = self.get_phash(file1)
        phash2 = self.get_phash(file2)
        
        if phash1 is None or phash2 is None:
            return False, -1
        
        distance = self.hamming_distance(phash1, phash2)
        is_similar = distance <= threshold
        
        return is_similar, distance
    
    def clear_cache(self) -> None:
        """Clear all cached hashes (in-memory and persistent)."""
        self.sha256_cache.clear()
        self.phash_cache.clear()
        self._disk_cache.clear()
        logger.info("Hash cache cleared")
