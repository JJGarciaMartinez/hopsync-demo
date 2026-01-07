#!/usr/bin/env python3
"""
Manifest generator for TCraft Client.
Copy this file to: tcraft-mods-list/scripts/generate-manifest.py

Scans the mods/ and configs/ folders and updates manifest.json
while preserving modpack metadata.
"""

import os
import json
from pathlib import Path

# Configuration
BASE_PATH = Path("modList")
MODS_PATH = "mods"
CONFIGS_PATH = "configs"
MANIFEST_FILE = BASE_PATH / "manifest.json"

def scan_directory(directory: Path, extension: str = None) -> list:
    """Scans a directory and returns a list of files."""
    if not directory.exists():
        print(f"Warning: Directory does not exist: {directory}")
        return []

    files = []
    for f in sorted(directory.iterdir()):
        if f.is_file():
            if extension is None or f.name.endswith(extension):
                files.append(f.name)
    return files

def load_existing_manifest() -> dict:
    """Loads existing manifest or returns default template."""
    if MANIFEST_FILE.exists():
        with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    # Default template
    return {
        "modpack": {
            "name": "TCraft Server",
            "version": "1.0.0",
            "description": "Modpack for TCraft server",
            "server_ip": "play.tcraft.net",
            "minecraft_version": "1.21.6"
        },
        "repository": {
            "base_url": "https://raw.githubusercontent.com/JJGarciaMartinez/tcraft-mods-list/main/modList",
            "mods_path": "mods/",
            "configs_path": "configs/"
        }
    }

def main():
    print("=== Manifest Generator ===")

    # Scan files
    mods_dir = BASE_PATH / MODS_PATH
    configs_dir = BASE_PATH / CONFIGS_PATH

    mods = scan_directory(mods_dir, ".jar")
    configs = scan_directory(configs_dir)

    print(f"Mods found: {len(mods)}")
    print(f"Configs found: {len(configs)}")

    # Load existing manifest (preserves metadata)
    manifest = load_existing_manifest()

    # Update file lists
    manifest["mods"] = mods
    manifest["configs"] = configs

    # Save manifest
    with open(MANIFEST_FILE, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Manifest saved: {MANIFEST_FILE}")
    print(f"  - {len(mods)} mods")
    print(f"  - {len(configs)} configs")

if __name__ == "__main__":
    main()