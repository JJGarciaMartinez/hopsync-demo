# HopSync Demo

Repository template for Minecraft modpacks compatible with **HopSync** (formerly TcraftClient).

## What is HopSync

HopSync is a Minecraft launcher that automatically syncs mods and configurations from a GitHub repository. This repository serves as the **data source** that HopSync consumes to keep players' mods up to date.

## How it works

```
┌─────────────────┐      push       ┌──────────────────┐
│  Administrator  │ ──────────────► │  GitHub Repo     │
│  (uploads mods) │                 │  (this template) │
└─────────────────┘                 └────────┬─────────┘
                                             │
                                    GitHub Action auto-generates
                                    manifest.json
                                             │
                                             ▼
┌─────────────────┐   reads manifest ┌──────────────────┐
│    HopSync      │ ◄──────────────  │  manifest.json   │
│   (launcher)    │                  │  + .jar files    │
└────────┬────────┘                  └──────────────────┘
         │
         │ downloads missing mods
         ▼
┌─────────────────┐
│  Player's .mods │
│     folder      │
└─────────────────┘
```

## Repository structure

```
├── .github/
│   └── workflows/
│       └── generate-manifest.yml   # Auto-generates manifest.json on push
├── modList/
│   ├── manifest.json               # Mod and config index (auto-generated)
│   ├── manifest.schema.json        # Validation schema
│   ├── mods/                       # Mod .jar files
│   └── configs/                    # Configuration files (optional)
├── scripts/
│   └── generate-manifest.py        # Script that generates the manifest
└── README.md
```

## Usage

### 1. Create your repository from this template

Use this repository as a template or fork it.

### 2. Configure manifest.json

Edit `modList/manifest.json` and update the `modpack` and `repository` sections:

```json
{
  "modpack": {
    "name": "Your Modpack",
    "version": "1.0.0",
    "description": "Description of your modpack",
    "server_ip": "play.yourserver.net",
    "minecraft_version": "1.21.1"
  },
  "repository": {
    "base_url": "https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/modList",
    "mods_path": "mods/",
    "configs_path": "configs/"
  }
}
```

### 3. Upload mods

Place `.jar` files in `modList/mods/`. When you push:

1. GitHub Action detects changes in `modList/mods/` or `modList/configs/`
2. Runs `scripts/generate-manifest.py`
3. Updates the `mods` and `configs` arrays in the manifest
4. Auto-commits the updated manifest

### 4. Connect with HopSync

In HopSync, add the manifest URL:
```
https://raw.githubusercontent.com/YOUR_USER/YOUR_REPO/main/modList/manifest.json
```

## Manifest schema

The `manifest.schema.json` file defines the valid manifest structure. Key fields:

| Field | Description |
|-------|-------------|
| `modpack.name` | Modpack name (required) |
| `modpack.version` | Semantic version (required) |
| `modpack.minecraft_version` | Required Minecraft version |
| `repository.base_url` | Raw repository base URL |
| `mods` | List of .jar files (auto-generated) |
| `configs` | List of config files (auto-generated) |

## Notes

- The `mods` and `configs` arrays are auto-generated - don't edit them manually
- The script preserves `modpack` and `repository` metadata when regenerating
- Make sure the repository is public so HopSync can access it
