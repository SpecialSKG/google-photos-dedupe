"""
Utility functions for file operations and metadata extraction.
"""

import os
import shutil
import hashlib
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image


def get_file_size(file_path: str) -> int:
    """Get file size in bytes."""
    return os.path.getsize(file_path)


def get_image_dimensions(file_path: str) -> Tuple[Optional[int], Optional[int]]:
    """
    Get image dimensions (width, height).
    Returns (None, None) if file is not an image or cannot be opened.
    """
    try:
        with Image.open(file_path) as img:
            return img.width, img.height
    except Exception:
        return None, None


def calculate_sha256(file_path: str, chunk_size: int = 8192) -> str:
    """Calculate SHA-256 hash of a file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def safe_copy(src: str, dst: str, keep_structure: bool = False) -> str:
    """
    Safely copy a file to destination, handling name collisions.
    
    Args:
        src: Source file path
        dst: Destination directory
        keep_structure: Whether to preserve subdirectory structure
        
    Returns:
        Final destination path of the copied file
    """
    dst_path = Path(dst)
    src_path = Path(src)
    
    # Create destination directory if it doesn't exist
    dst_path.mkdir(parents=True, exist_ok=True)
    
    # Determine final filename
    final_path = dst_path / src_path.name
    
    # Handle name collisions by appending hash
    if final_path.exists():
        stem = src_path.stem
        suffix = src_path.suffix
        short_hash = calculate_sha256(src)[:8]
        final_path = dst_path / f"{stem}__{short_hash}{suffix}"
    
    # Copy the file
    shutil.copy2(src, final_path)
    return str(final_path)


def safe_move(src: str, dst: str, keep_structure: bool = False) -> str:
    """
    Safely move a file to destination, handling name collisions.
    
    Args:
        src: Source file path
        dst: Destination directory
        keep_structure: Whether to preserve subdirectory structure
        
    Returns:
        Final destination path of the moved file
    """
    dst_path = Path(dst)
    src_path = Path(src)
    
    # Create destination directory if it doesn't exist
    dst_path.mkdir(parents=True, exist_ok=True)
    
    # Determine final filename
    final_path = dst_path / src_path.name
    
    # Handle name collisions by appending hash
    if final_path.exists():
        stem = src_path.stem
        suffix = src_path.suffix
        short_hash = calculate_sha256(src)[:8]
        final_path = dst_path / f"{stem}__{short_hash}{suffix}"
    
    # Move the file
    shutil.move(src, final_path)
    return str(final_path)


def sanitize_path(path: str) -> str:
    """Remove invalid characters from path and normalize it."""
    return os.path.normpath(path)


def is_supported_image(file_path: str) -> bool:
    """Check if file is a supported image format."""
    supported_extensions = {'.jpg', '.jpeg', '.png', '.webp', '.gif', 
                          '.heic', '.heif', '.tif', '.tiff'}
    return Path(file_path).suffix.lower() in supported_extensions


def is_supported_video(file_path: str) -> bool:
    """Check if file is a supported video format."""
    supported_extensions = {'.mp4', '.mov', '.m4v', '.avi', '.mkv'}
    return Path(file_path).suffix.lower() in supported_extensions


def is_supported_media(file_path: str) -> bool:
    """Check if file is a supported media format (image or video)."""
    return is_supported_image(file_path) or is_supported_video(file_path)
