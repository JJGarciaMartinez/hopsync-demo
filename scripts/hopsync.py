#!/usr/bin/env python3
"""HopSync modpack tool.

Turns the declarative `hopsync.toml` into the `manifest.json` that HopSync clients read.

Commands:
  import <dir>   One-time migration: match the jars in <dir> against Modrinth by hash,
                 write hopsync.toml and copy the unmatched jars into local-mods/.
  add <slug>     Append a Modrinth mod pinned to its latest compatible version.
  build          Resolve every mod, validate the pack and write manifest.json.
                 With --upload, publish manifest.json and the hosted files to the
                 GitHub release (requires the `gh` CLI and GH_TOKEN).
  update         Bump pinned Modrinth mods to their newest compatible version.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_FILE = ROOT / "hopsync.toml"
MANIFEST_FILE = ROOT / "manifest.json"
LOCAL_MODS_DIR = ROOT / "local-mods"
CONFIGS_DIR = ROOT / "configs"

MANIFEST_FORMAT = 2
DEFAULT_RELEASE_TAG = "hopsync"
MODRINTH_API = "https://api.modrinth.com/v2"


class PackError(Exception):
    pass


# --------------------------------------------------------------------------- utils

def sha512_of(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_asset_name(sha512: str, relative: str) -> str:
    """Content-addressed release asset name; GitHub mangles spaces and brackets otherwise."""
    flat = re.sub(r"[^A-Za-z0-9._-]+", "_", relative.replace("/", "__"))
    return f"{sha512[:10]}-{flat}"


def detect_repository() -> str:
    if repo := os.environ.get("GITHUB_REPOSITORY"):
        return repo
    try:
        url = subprocess.run(
            ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        raise PackError("cannot detect the GitHub repository; set [hosting] repository in hopsync.toml")
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise PackError(f"origin is not a GitHub remote: {url}")
    return match.group(1)


# ------------------------------------------------------------------------ modrinth

class Modrinth:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent

    def _request(self, path: str, params: dict | None = None, body: dict | None = None):
        url = MODRINTH_API + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: json.dumps(v) if isinstance(v, list) else v for k, v in params.items()}
            )
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"User-Agent": self.user_agent})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise PackError(f"Modrinth API error {e.code} on {path}") from e

    def versions_by_hash(self, hashes: list[str]) -> dict:
        if not hashes:
            return {}
        return self._request("/version_files", body={"hashes": hashes, "algorithm": "sha512"}) or {}

    def project_versions(self, slug: str, loaders: list[str], game_versions: list[str]) -> list | None:
        return self._request(
            f"/project/{urllib.parse.quote(slug)}/version",
            {"loaders": loaders, "game_versions": game_versions},
        )

    def projects(self, ids: list[str]) -> dict:
        """Returns projects keyed by both id and slug."""
        result = {}
        for i in range(0, len(ids), 100):
            for p in self._request("/projects", {"ids": ids[i:i + 100]}) or []:
                result[p["id"]] = result[p["slug"]] = p
        return result

    def team_owners(self, team_ids: list[str]) -> dict:
        owners = {}
        for i in range(0, len(team_ids), 100):
            for members in self._request("/teams", {"ids": team_ids[i:i + 100]}) or []:
                if not members:
                    continue
                owner = next((m for m in members if m.get("is_owner") or m.get("role") == "Owner"), members[0])
                owners[owner["team_id"]] = owner["user"]["username"]
        return owners


def matches(version: dict, wanted: str) -> bool:
    """A pin is either a version number or, when that number is ambiguous, a Modrinth version id."""
    return wanted in (version["version_number"], version["id"])


def pin_for(version: dict, versions: list[dict]) -> str:
    same_number = [v for v in versions if v["version_number"] == version["version_number"]]
    return version["version_number"] if len(same_number) == 1 else version["id"]


def primary_file(version: dict) -> dict:
    return next((f for f in version["files"] if f.get("primary")), version["files"][0])


# ---------------------------------------------------------------------- pack model

@dataclass
class Pack:
    data: dict
    text: str

    @classmethod
    def load(cls) -> "Pack":
        if not PACK_FILE.exists():
            raise PackError(f"{PACK_FILE.name} not found")
        text = PACK_FILE.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise PackError(f"{PACK_FILE.name} is not valid TOML: {e}") from e
        pack = data.get("pack", {})
        for key in ("name", "version", "minecraft", "loader"):
            if not pack.get(key):
                raise PackError(f"[pack] {key} is required")
        return cls(data, text)

    @property
    def meta(self) -> dict:
        return self.data["pack"]

    @property
    def mods(self) -> list[dict]:
        return self.data.get("mods", [])

    @property
    def overwrite_configs(self) -> set[str]:
        return set(self.data.get("configs", {}).get("overwrite", []))

    @property
    def hosting(self) -> dict:
        return self.data.get("hosting", {})

    def loaders_for(self, mod: dict) -> list[str]:
        return mod.get("loaders") or [self.meta["loader"]]

    def game_versions_for(self, mod: dict) -> list[str]:
        return mod.get("game_versions") or [self.meta["minecraft"]]

    def save(self, text: str) -> None:
        tomllib.loads(text)  # never write a file we cannot read back
        PACK_FILE.write_text(text, encoding="utf-8")
        self.text = text


def set_pinned_version(text: str, slug: str, new_version: str) -> str:
    """Rewrites `version = "..."` inside the [[mods]] block of `slug`, keeping comments intact."""
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if re.match(r"\s*\[", line)] + [len(lines)]
    slug_re = re.compile(rf'\s*modrinth\s*=\s*"{re.escape(slug)}"')
    version_re = re.compile(r'(\s*version\s*=\s*")[^"]*(")')
    for start, end in zip(headers, headers[1:]):
        block = lines[start:end]
        if lines[start].strip() == "[[mods]]" and any(slug_re.match(l) for l in block):
            for i in range(start, end):
                if version_re.match(lines[i]):
                    lines[i] = version_re.sub(rf"\g<1>{new_version}\g<2>", lines[i], count=1)
                    return "".join(lines)
    raise PackError(f"no pinned version line found for {slug}")


def mod_block(slug: str, version: str, loaders: list[str] | None = None,
              game_versions: list[str] | None = None) -> str:
    block = f'\n[[mods]]\nmodrinth = "{slug}"\nversion = "{version}"\n'
    if loaders:
        block += f"loaders = {json.dumps(loaders)}\n"
    if game_versions:
        block += f"game_versions = {json.dumps(game_versions)}\n"
    return block


# --------------------------------------------------------------------- local jars

def read_jar_metadata(jar: Path) -> dict:
    """Best-effort metadata from the loader descriptor inside the jar."""
    meta = {"id": jar.stem, "name": jar.stem, "version": "", "author": "", "description": ""}
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            manifest_version = ""
            if "META-INF/MANIFEST.MF" in names:
                mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
                if m := re.search(r"^Implementation-Version:\s*(.+)$", mf, re.M):
                    manifest_version = m.group(1).strip()

            for descriptor in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if descriptor in names:
                    data = tomllib.loads(z.read(descriptor).decode("utf-8", "replace"))
                    mod = (data.get("mods") or [{}])[0]
                    version = str(mod.get("version", ""))
                    if "${" in version:
                        version = manifest_version
                    meta.update(
                        id=mod.get("modId", meta["id"]),
                        name=mod.get("displayName", meta["name"]),
                        version=version,
                        author=str(mod.get("authors", data.get("authors", ""))),
                        description=" ".join(str(mod.get("description", "")).split()),
                    )
                    return meta

            if "fabric.mod.json" in names:
                data = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False)
                authors = [a if isinstance(a, str) else a.get("name", "") for a in data.get("authors", [])]
                meta.update(
                    id=data.get("id", meta["id"]),
                    name=data.get("name", meta["name"]),
                    version=data.get("version", ""),
                    author=", ".join(filter(None, authors)),
                    description=data.get("description", ""),
                )
                return meta

            meta["version"] = manifest_version
    except (zipfile.BadZipFile, tomllib.TOMLDecodeError, json.JSONDecodeError, UnicodeError) as e:
        print(f"  warning: could not read metadata of {jar.name}: {e}", file=sys.stderr)
    return meta


# -------------------------------------------------------------------------- build

@dataclass
class HostedFile:
    path: Path
    asset: str


def resolve_modrinth_mods(pack: Pack, api: Modrinth, errors: list[str]) -> tuple[list[dict], list[dict]]:
    """Returns (manifest entries, resolved Modrinth versions)."""
    resolved = []
    for mod in pack.mods:
        slug = mod.get("modrinth")
        if not slug:
            errors.append(f"[[mods]] entry without `modrinth`: {mod}")
            continue
        wanted = str(mod.get("version", "")).strip()
        if not wanted:
            errors.append(f"{slug}: `version` is required (use \"latest\" to float)")
            continue

        loaders, game_versions = pack.loaders_for(mod), pack.game_versions_for(mod)
        versions = api.project_versions(slug, loaders, game_versions)
        if versions is None:
            errors.append(f"{slug}: project not found on Modrinth")
            continue
        if wanted == "latest":
            candidates = [v for v in versions if v["version_type"] == "release"] or versions
        else:
            candidates = [v for v in versions if matches(v, wanted)]
        if not candidates:
            errors.append(f"{slug}: no version '{wanted}' for loaders={loaders} game_versions={game_versions}")
            continue
        if wanted != "latest" and len(candidates) > 1:
            ids = ", ".join(v["id"] for v in candidates)
            errors.append(f"{slug}: version '{wanted}' is ambiguous on Modrinth; pin one of these ids instead: {ids}")
            continue
        resolved.append((slug, candidates[0]))

    projects = api.projects(sorted({v["project_id"] for _, v in resolved}))
    owners = api.team_owners(sorted({p["team"] for p in projects.values()}))

    entries, seen = [], {}
    for slug, version in resolved:
        project = projects.get(version["project_id"], {})
        if version["project_id"] in seen:
            errors.append(f"{slug}: duplicated (same project as {seen[version['project_id']]})")
            continue
        seen[version["project_id"]] = slug
        f = primary_file(version)
        entries.append({
            "id": project.get("slug", slug),
            "name": project.get("title", slug),
            "version": version["version_number"],
            "author": owners.get(project.get("team"), ""),
            "description": project.get("description", ""),
            "file": f["filename"],
            "url": f["url"],
            "sha512": f["hashes"]["sha512"],
            "size": f["size"],
            "source": "modrinth",
        })
    return entries, [v for _, v in resolved]


def collect_local_mods(api: Modrinth, base_url: str, hosted: list[HostedFile]) -> tuple[list[dict], set[str]]:
    """Returns (manifest entries, Modrinth project ids the local jars correspond to)."""
    jars = sorted(LOCAL_MODS_DIR.glob("*.jar")) if LOCAL_MODS_DIR.exists() else []
    hashes = {sha512_of(j): j for j in jars}
    on_modrinth = api.versions_by_hash(list(hashes))
    entries = []
    for sha, jar in hashes.items():
        if sha in on_modrinth:
            print(f"  hint: {jar.name} is on Modrinth (project {on_modrinth[sha]['project_id']}); "
                  "consider declaring it in hopsync.toml instead", file=sys.stderr)
        meta = read_jar_metadata(jar)
        asset = safe_asset_name(sha, jar.name)
        hosted.append(HostedFile(jar, asset))
        entries.append({
            **meta,
            "file": jar.name,
            "url": f"{base_url}/{asset}",
            "sha512": sha,
            "size": jar.stat().st_size,
            "source": "local",
        })
    return entries, {v["project_id"] for v in on_modrinth.values()}


def collect_configs(pack: Pack, base_url: str, hosted: list[HostedFile], errors: list[str]) -> list[dict]:
    files = sorted(p for p in CONFIGS_DIR.rglob("*") if p.is_file()) if CONFIGS_DIR.exists() else []
    relative = {p.relative_to(CONFIGS_DIR).as_posix(): p for p in files}
    for missing in pack.overwrite_configs - relative.keys():
        errors.append(f"[configs] overwrite lists '{missing}', which is not in configs/")
    entries = []
    for rel, path in relative.items():
        sha = sha512_of(path)
        asset = safe_asset_name(sha, rel)
        hosted.append(HostedFile(path, asset))
        entries.append({
            "path": rel,
            "url": f"{base_url}/{asset}",
            "sha512": sha,
            "size": path.stat().st_size,
            "overwrite": rel in pack.overwrite_configs,
        })
    return entries


def check_dependencies(api: Modrinth, versions: list[dict], present: set[str]) -> None:
    missing = {}
    for v in versions:
        for dep in v.get("dependencies", []):
            if dep.get("dependency_type") == "required" and dep.get("project_id") \
                    and dep["project_id"] not in present:
                missing.setdefault(dep["project_id"], set()).add(v["project_id"])
    if not missing:
        return
    projects = api.projects(sorted(missing.keys() | {p for s in missing.values() for p in s}))
    for dep, needed_by in missing.items():
        dep_name = projects.get(dep, {}).get("slug", dep)
        by = ", ".join(sorted(projects.get(p, {}).get("slug", p) for p in needed_by))
        print(f"  warning: '{dep_name}' is required by {by} but is not in the pack "
              "(ignore if a local jar provides it)", file=sys.stderr)


def upload(hosted: list[HostedFile], tag: str) -> None:
    if not shutil.which("gh"):
        raise PackError("--upload needs the GitHub CLI (gh)")
    view = subprocess.run(["gh", "release", "view", tag, "--json", "assets"], capture_output=True, text=True)
    if view.returncode != 0:
        subprocess.run(["gh", "release", "create", tag, "--title", "HopSync files", "--latest=false",
                        "--notes", "Hosted files for HopSync. Managed by scripts/hopsync.py; do not edit."],
                       check=True)
        existing = set()
    else:
        existing = {a["name"] for a in json.loads(view.stdout)["assets"]}

    with tempfile.TemporaryDirectory() as tmp:
        pending = []
        for f in hosted:
            if f.asset not in existing:
                target = Path(tmp) / f.asset
                shutil.copyfile(f.path, target)
                pending.append(str(target))
        manifest_copy = Path(tmp) / MANIFEST_FILE.name
        shutil.copyfile(MANIFEST_FILE, manifest_copy)
        print(f"Uploading {len(pending)} new file(s) and manifest.json to release '{tag}'")
        # Hosted assets are content-addressed, so only manifest.json is ever replaced.
        for i in range(0, len(pending), 20):
            subprocess.run(["gh", "release", "upload", tag, *pending[i:i + 20]], check=True)
        subprocess.run(["gh", "release", "upload", tag, str(manifest_copy), "--clobber"], check=True)


def cmd_build(args) -> int:
    pack = Pack.load()
    repo = pack.hosting.get("repository") or detect_repository()
    tag = pack.hosting.get("release_tag", DEFAULT_RELEASE_TAG)
    base_url = f"https://github.com/{repo}/releases/download/{tag}"
    api = Modrinth(f"hopsync-builder (github.com/{repo})")

    errors: list[str] = []
    hosted: list[HostedFile] = []
    print(f"Building '{pack.meta['name']}' {pack.meta['version']} "
          f"({pack.meta['loader']} {pack.meta['minecraft']})")

    modrinth_entries, versions = resolve_modrinth_mods(pack, api, errors)
    local_entries, local_project_ids = collect_local_mods(api, base_url, hosted)
    configs = collect_configs(pack, base_url, hosted, errors)

    mods = modrinth_entries + local_entries
    by_file = {}
    for m in mods:
        if m["file"] in by_file:
            errors.append(f"two mods resolve to the same file name: {m['file']}")
        by_file[m["file"]] = m

    check_dependencies(api, versions, {v["project_id"] for v in versions} | local_project_ids)

    if errors:
        print("\nBuild failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    mods.sort(key=lambda m: m["file"].lower())
    content = {"mods": mods, "configs": configs}
    manifest = {
        "$schema": f"https://raw.githubusercontent.com/{repo}/main/schema/manifest.schema.json",
        "format_version": MANIFEST_FORMAT,
        "pack": {k: pack.meta[k] for k in ("name", "version", "description", "server_ip", "minecraft", "loader")
                 if pack.meta.get(k) is not None},
        "content_hash": hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest(),
        **content,
    }
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {MANIFEST_FILE.name}: {len(modrinth_entries)} Modrinth mods, "
          f"{len(local_entries)} local mods, {len(configs)} configs")
    print(f"Manifest URL: {base_url}/{MANIFEST_FILE.name}")

    if args.upload:
        upload(hosted, tag)
    return 0


# ------------------------------------------------------------------ add / update

def cmd_add(args) -> int:
    pack = Pack.load()
    if any(m.get("modrinth") == args.slug for m in pack.mods):
        raise PackError(f"{args.slug} is already in {PACK_FILE.name}")
    api = Modrinth("hopsync-cli")
    versions = api.project_versions(args.slug, [pack.meta["loader"]], [pack.meta["minecraft"]])
    if versions is None:
        raise PackError(f"{args.slug}: project not found on Modrinth")
    releases = [v for v in versions if v["version_type"] == "release"] or versions
    if not releases:
        raise PackError(f"{args.slug}: no version for {pack.meta['loader']} {pack.meta['minecraft']}")
    pin = pin_for(releases[0], versions)
    pack.save(pack.text.rstrip("\n") + "\n" + mod_block(args.slug, pin))
    print(f"Added {args.slug} {pin}")
    return 0


def cmd_update(args) -> int:
    pack = Pack.load()
    api = Modrinth("hopsync-updater")
    text, changes = pack.text, []
    for mod in pack.mods:
        slug, current = mod.get("modrinth"), str(mod.get("version", ""))
        if not slug or current in ("", "latest"):
            continue
        versions = api.project_versions(slug, pack.loaders_for(mod), pack.game_versions_for(mod)) or []
        pinned = next((v for v in versions if matches(v, current)), None)
        allow_prerelease = pinned is not None and pinned["version_type"] != "release"
        candidates = [v for v in versions if allow_prerelease or v["version_type"] == "release"]
        if not candidates:
            continue
        newest = candidates[0]
        if newest is not pinned and (pinned is None or newest["date_published"] > pinned["date_published"]):
            pin = pin_for(newest, versions)
            text = set_pinned_version(text, slug, pin)
            changes.append((slug, current, pin))

    if not changes:
        print("Everything is up to date")
        return 0
    pack.save(text)
    lines = [f"| `{s}` | {old} | {new} |" for s, old, new in changes]
    summary = "| Mod | Current | New |\n|---|---|---|\n" + "\n".join(lines) + "\n"
    print(summary)
    if args.summary:
        Path(args.summary).write_text(summary, encoding="utf-8")
    return 0


# -------------------------------------------------------------------------- import

def cmd_import(args) -> int:
    source = Path(args.directory)
    jars = sorted(source.glob("*.jar"))
    if not jars:
        raise PackError(f"no .jar files in {source}")
    if PACK_FILE.exists() and not args.force:
        raise PackError(f"{PACK_FILE.name} already exists (use --force to overwrite)")

    api = Modrinth("hopsync-importer")
    hashes = {sha512_of(j): j for j in jars}
    found = api.versions_by_hash(list(hashes))
    projects = api.projects(sorted({v["project_id"] for v in found.values()}))

    blocks = []
    for sha, version in sorted(found.items(), key=lambda kv: projects[kv[1]["project_id"]]["slug"]):
        slug = projects[version["project_id"]]["slug"]
        loaders = None if args.loader in version["loaders"] else version["loaders"]
        game_versions = None if args.minecraft in version["game_versions"] else version["game_versions"]
        siblings = api.project_versions(slug, loaders or [args.loader], game_versions or [args.minecraft]) or []
        blocks.append(mod_block(slug, pin_for(version, siblings), loaders, game_versions))

    LOCAL_MODS_DIR.mkdir(exist_ok=True)
    unmatched = [jar for sha, jar in hashes.items() if sha not in found]
    for jar in unmatched:
        shutil.copy2(jar, LOCAL_MODS_DIR / jar.name)

    header = (
        "# HopSync modpack definition. This is the only file you need to edit.\n"
        "# Docs: README.md. Validate locally with: python3 scripts/hopsync.py build\n\n"
        "[pack]\n"
        f'name = "{args.name}"\n'
        'version = "1.0.0"\n'
        'description = ""\n'
        f'minecraft = "{args.minecraft}"\n'
        f'loader = "{args.loader}"\n'
        '# server_ip = "play.example.net"\n\n'
        "[configs]\n"
        "# Files under configs/ are installed only if the player does not have them yet.\n"
        "# List here the ones the server must enforce (relative to configs/).\n"
        "overwrite = []\n\n"
        "# Mods hosted on Modrinth. Jars that are not on Modrinth go into local-mods/.\n"
    )
    PACK_FILE.write_text(header + "".join(blocks), encoding="utf-8")
    print(f"Wrote {PACK_FILE.name} with {len(found)} Modrinth mods")
    print(f"Copied {len(unmatched)} unmatched jar(s) to {LOCAL_MODS_DIR.name}/:")
    for jar in unmatched:
        print(f"  {jar.name}")
    return 0


# ---------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="HopSync modpack tool")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build", help="resolve mods and write manifest.json")
    p.add_argument("--upload", action="store_true", help="publish to the GitHub release")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("add", help="add a Modrinth mod at its latest compatible version")
    p.add_argument("slug")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="bump pinned mods to their newest compatible version")
    p.add_argument("--summary", help="write a Markdown table of the changes to this file")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("import", help="create hopsync.toml from a folder of jars")
    p.add_argument("directory")
    p.add_argument("--name", default="My Modpack")
    p.add_argument("--minecraft", required=True)
    p.add_argument("--loader", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import)

    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # keep stdout and stderr interleaved in CI logs
    try:
        return args.func(args)
    except PackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
