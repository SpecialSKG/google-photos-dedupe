"""
Entry point for running the package as a module.
Usage: python -m photos_dedupe --config config.yaml
"""

from photos_dedupe.cli import main

if __name__ == "__main__":
    main()
