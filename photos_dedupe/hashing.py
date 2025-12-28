"""
Hash calculation for exact and perceptual duplicate detection.
"""

import logging
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


class HashCalculator:
    """Calculator for exact (SHA-256) and perceptual (pHash) hashes."""
    
    def __init__(self):
        self.sha256_cache: Dict[str, str] = {}
        self.phash_cache: Dict[str, str] = {}
    
    def get_sha256(self, file_path: str, use_cache: bool = True) -> str:
        """
        Calculate SHA-256 hash of a file.
        
        Args:
            file_path: Path to the file
            use_cache: Whether to use cached hash if available
            
        Returns:
            SHA-256 hash as hexadecimal string
        """
        if use_cache and file_path in self.sha256_cache:
            return self.sha256_cache[file_path]
        
        try:
            hash_value = calculate_sha256(file_path)
            self.sha256_cache[file_path] = hash_value
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
        
        if use_cache and file_path in self.phash_cache:
            return self.phash_cache[file_path]
        
        try:
            with Image.open(file_path) as img:
                # Convert to RGB if necessary (handles HEIC, PNG with alpha, etc.)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')
                
                # Calculate perceptual hash
                phash = imagehash.phash(img)
                hash_str = str(phash)
                self.phash_cache[file_path] = hash_str
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
        """Clear all cached hashes."""
        self.sha256_cache.clear()
        self.phash_cache.clear()
        logger.info("Hash cache cleared")
