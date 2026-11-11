# HopSync Demo

Template repository for publishing a Minecraft modpack to **HopSync** players.

You declare which mods the pack contains in `hopsync.toml`. A GitHub Action resolves every
mod, validates the pack and publishes a `manifest.json` that the HopSync client reads to keep
each player's `mods/` and `config/` folders in sync. You never edit the manifest or host
Modrinth jars yourself.

## How it works

```
 you edit hopsync.toml ──push──► GitHub Action (build.yml)
                                   │  1. resolves each mod on the Modrinth API
                                   │  2. reads metadata of jars in local-mods/
                                   │  3. validates (versions, duplicates, schema)
                                   │  4. writes manifest.json
                                   ▼
                     GitHub release "hopsync"
                     ├── manifest.json            ◄── HopSync client reads this
                     ├── <hash>-<local mod>.jar   ◄── jars not on Modrinth
                     └── <hash>-<config file>
                                                       Modrinth CDN ◄── Modrinth jars
```

- Mods available on Modrinth are downloaded by players straight from the Modrinth CDN.
- Jars that are not on Modrinth live in `local-mods/` and are uploaded as release assets.
- Every file carries its `sha512`, so the client knows exactly when a mod changed.
- If anything fails (unknown mod, wrong version, duplicate), the Action fails and the
  previously published manifest stays untouched. Players never get a broken pack.

## Repository layout

| Path | Edited by | Purpose |
|------|-----------|---------|
| `hopsync.toml` | you | Pack metadata and the list of Modrinth mods |
| `local-mods/` | you | Jars that are not on Modrinth (dropped in as files) |
| `configs/` | you | Files installed into the player's `config/` folder |
| `scripts/hopsync.py` | — | The tool: `build`, `add`, `update`, `import` |
| `schema/manifest.schema.json` | — | Schema of the generated manifest |
| `.github/workflows/build.yml` | — | Validates on PRs, publishes on pushes to `main` |
| `.github/workflows/check-updates.yml` | — | Weekly PR with mod updates |

## Using this template

1. Create a repository from this template. It must be **public** so players can download.
2. Edit `[pack]` in `hopsync.toml` (name, version, Minecraft version, loader).
3. Replace the `[[mods]]` entries with your own, or migrate an existing mods folder:
   ```bash
   python3 scripts/hopsync.py import path/to/mods --name "My Pack" --minecraft 1.21.1 --loader neoforge --force
   ```
   Jars are matched against Modrinth by hash; the ones not found are copied to `local-mods/`.
4. Enable *Settings → Actions → General → Allow GitHub Actions to create and approve pull
   requests* (needed by the weekly update check). Optionally set the repository variable
   `HOPSYNC_UPDATES_BASE` to the branch those PRs should target (default branch otherwise).
5. Push to `main`. Your manifest URL is:
   ```
   https://github.com/<owner>/<repo>/releases/download/hopsync/manifest.json
   ```
   Give that URL to your players in HopSync.

## `hopsync.toml`

```toml
[pack]
name = "My Pack"
version = "1.0.0"
description = "Optional"
minecraft = "1.21.1"
loader = "neoforge"             # neoforge | forge | fabric | quilt
server_ip = "play.example.net"  # optional

[configs]
# Files under configs/ are installed only if the player does not have them yet.
# List the ones the server must enforce (paths relative to configs/).
overwrite = ["mymod-common.toml"]

[[mods]]
modrinth = "sodium"             # the slug from modrinth.com/mod/<slug>
version = "mc1.21.1-0.6.13-neoforge"

[[mods]]
modrinth = "some-fabric-mod"
version = "2.0.1"
loaders = ["fabric"]            # only when it differs from [pack] loader (e.g. via Sinytra Connector)
game_versions = ["1.21"]        # only when the mod is not tagged with [pack] minecraft
```

`version` is the version number shown on Modrinth. When Modrinth has two uploads with the
same number, the build asks you to pin the Modrinth version id instead (e.g. `"XlJUda6G"`).
`version = "latest"` floats to the newest release on every build; prefer pinned versions.

## Day-to-day tasks

| Task | What to do |
|------|------------|
| Add a Modrinth mod | `python3 scripts/hopsync.py add <slug>`, or add a `[[mods]]` block in the GitHub web editor |
| Update a mod | Change its `version`, or merge the weekly "bump mod versions" PR |
| Remove a mod | Delete its `[[mods]]` block |
| Add a mod that is not on Modrinth | Drop the jar into `local-mods/` |
| Add or change a config | Put the file under `configs/` |
| Check everything locally | `python3 scripts/hopsync.py build` (writes `manifest.json`, uploads nothing) |

Bump `[pack] version` when you release a change to players.

Redistribution: jars in `local-mods/` are re-hosted by you. Check each mod's license allows it.

## Manifest format

See `schema/manifest.schema.json`. Abridged:

```json
{
  "format_version": 2,
  "pack": { "name": "My Pack", "version": "1.0.0", "minecraft": "1.21.1", "loader": "neoforge" },
  "content_hash": "<sha256 of mods + configs>",
  "mods": [
    { "id": "almanac", "name": "Almanac", "version": "1.5.0", "author": "frikinjay",
      "description": "...", "file": "Almanac-1.21.1-2-neoforge-1.5.0.jar",
      "url": "https://cdn.modrinth.com/...", "sha512": "...", "size": 25234, "source": "modrinth" }
  ],
  "configs": [
    { "path": "fabric_loader_dependencies.json", "url": "https://github.com/...",
      "sha512": "...", "size": 137, "overwrite": false }
  ]
}
```

Requires Python 3.11+ to run the tool locally; no third-party packages.
