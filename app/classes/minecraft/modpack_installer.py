"""
modpack_installer.py - resolve and install modpacks into a Crafty server dir.

Supports Modrinth ``.mrpack`` archives (``modrinth.index.json``) and CurseForge
modpack zips (``manifest.json``). Self-contained like content_manager.py: only
the stdlib plus the small HTTP helpers shared with ContentManager.

Two phases so callers can show what a pack is before touching the disk:

    resolve()/resolve_archive()  -> PackInfo   (index only, nothing written)
    install(pack, mode)          -> summary    (downloads + overrides)

Progress is pushed through ``progress_cb`` and mirrored to
``<server>/.content_cache/modpack_install.json`` so the UI can poll it.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath, Path

from app.classes.minecraft.content_manager import (
    CURSEFORGE,
    MODRINTH,
    _download,
    _http_json,
)

# Hosts a pack is allowed to pull files from (Modrinth's mrpack whitelist plus
# the CurseForge CDN). Anything else is skipped and reported.
ALLOWED_HOSTS = {
    "cdn.modrinth.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitlab.com",
    "edge.forgecdn.net",
    "mediafilez.forgecdn.net",
    "media.forgecdn.net",
}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000
MAX_INDEX_BYTES = 16 * 1024 * 1024
STATUS_REL_PATH = os.path.join(".content_cache", "modpack_install.json")
MRPACK_LOADER_KEYS = {
    "fabric-loader": "fabric",
    "forge": "forge",
    "neoforge": "neoforge",
    "quilt-loader": "quilt",
}


# Packs resolved by the wizard before a server exists, keyed by token so the
# create-server call can reuse the downloaded archive.
_RESOLVED_PACKS = {}
_RESOLVED_TTL = 3600


def remember_pack(pack):
    token = uuid.uuid4().hex
    now = time.time()
    for key, (ts, _) in list(_RESOLVED_PACKS.items()):
        if now - ts > _RESOLVED_TTL:
            _RESOLVED_PACKS.pop(key, None)
    _RESOLVED_PACKS[token] = (now, pack.to_dict())
    return token


def recall_pack(token):
    entry = _RESOLVED_PACKS.pop(token, None) if token else None
    if not entry or time.time() - entry[0] > _RESOLVED_TTL:
        return None
    return PackInfo.from_dict(entry[1])


class ModpackError(Exception):
    """Raised for unusable packs; ``.code`` is a stable machine-readable id."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class PackFile:
    path: str  # relative, already validated (posix separators)
    urls: list
    sha1: str = None
    sha512: str = None
    size: int = 0
    name: str = ""


@dataclass
class PackInfo:
    source: str  # modrinth | curseforge | upload
    name: str
    version: str
    mc: str
    loader: str  # fabric | forge | neoforge | quilt
    loader_build: str = None
    project_id: str = None
    version_id: str = None
    url: str = None
    archive_path: str = None  # local zip the overrides come from
    overrides: list = field(default_factory=list)
    files: list = field(default_factory=list)  # list[PackFile]
    blocked: list = field(default_factory=list)  # CF allowModDistribution=false
    skipped: list = field(default_factory=list)  # client-only / bad host / path
    format: str = "mrpack"  # mrpack | curseforge

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["files"] = [PackFile(**f) for f in data.get("files", [])]
        return cls(**data)

    def summary(self):
        """Compact shape for API responses (no per-file list)."""
        return {
            "source": self.source,
            "format": self.format,
            "name": self.name,
            "version": self.version,
            "mc": self.mc,
            "loader": self.loader,
            "loader_build": self.loader_build,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "url": self.url,
            "file_count": len(self.files),
            "total_bytes": sum(f.size or 0 for f in self.files),
            "blocked": self.blocked,
            "skipped": self.skipped,
        }


def parse_loader_id(loader_id):
    """'forge-47.3.0' -> ('forge', '47.3.0'); 'fabric-0.16.9' -> ('fabric', '0.16.9')"""
    if not loader_id or "-" not in loader_id:
        return (loader_id or "").lower(), None
    name, build = loader_id.split("-", 1)
    return name.lower(), build


def safe_relative_path(rel):
    """Return a normalised posix relative path or raise ModpackError."""
    if not isinstance(rel, str) or not rel.strip():
        raise ModpackError("bad_path", repr(rel))
    p = PurePosixPath(rel.replace("\\", "/"))
    if (
        p.is_absolute()
        or ":" in p.parts[0]
        or any(part in ("..", "") for part in p.parts)
    ):
        raise ModpackError("path_traversal", rel)
    return p.as_posix()


def safe_join(base, rel):
    """Join *rel* (validated with safe_relative_path) under *base* and make
    sure the resolved path is still inside *base*."""
    rel = safe_relative_path(rel)
    base_abs = Path(base).resolve()
    target = base_abs.joinpath(*rel.split("/"))
    resolved = target.resolve()
    if os.path.commonpath([str(base_abs), str(resolved)]) != str(base_abs):
        raise ModpackError("path_traversal", rel)
    return str(target)


def _url_allowed(url):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return u.scheme == "https" and u.hostname in ALLOWED_HOSTS


def _is_symlink(info):
    return (info.external_attr >> 16) & 0o170000 == 0o120000


class ModpackInstaller:
    def __init__(self, server_path, cf_key="", progress_cb=None, work_dir=None):
        self.server_path = server_path
        self.cf_key = cf_key or ""
        self.progress_cb = progress_cb
        # where downloaded archives land; inside the server when we have one,
        # a temp dir otherwise (wizard resolves before the server exists).
        if work_dir:
            self.work_dir = work_dir
        elif server_path:
            self.work_dir = os.path.join(server_path, ".content_cache", "modpacks")
        else:
            self.work_dir = tempfile.mkdtemp(prefix="crafty-modpack-")
        self._status = {}

    # ------------------------------------------------------------ resolving
    def resolve(self, source, ident, version_id=None):
        """Fetch the pack archive for a Modrinth/CurseForge version and parse it."""
        if source == "modrinth":
            return self._resolve_modrinth(ident, version_id)
        if source == "curseforge":
            return self._resolve_curseforge(ident, version_id)
        raise ModpackError("unknown_source", source)

    def _resolve_modrinth(self, ident, version_id):
        if version_id:
            _, v = _http_json(f"{MODRINTH}/version/{version_id}")
        else:
            _, vs = _http_json(f"{MODRINTH}/project/{ident}/version")
            vs = [x for x in (vs or []) if x.get("version_type") == "release"] or (
                vs or []
            )
            vs.sort(key=lambda x: x.get("date_published", ""), reverse=True)
            v = vs[0] if vs else None
        if not v:
            raise ModpackError("version_not_found", str(version_id or ident))
        files = v.get("files", [])
        pf = next(
            (f for f in files if f.get("filename", "").endswith(".mrpack")),
            next((f for f in files if f.get("primary")), files[0] if files else None),
        )
        if not pf:
            raise ModpackError("no_file", "version has no downloadable file")
        archive = self._fetch_archive(
            pf["url"], pf["filename"], pf.get("hashes", {}).get("sha1")
        )
        pack = self.resolve_archive(archive)
        pack.source = "modrinth"
        pack.project_id = v.get("project_id")
        pack.version_id = v.get("id")
        pack.version = v.get("version_number") or pack.version
        _, proj = _http_json(f"{MODRINTH}/project/{pack.project_id}")
        if proj:
            pack.name = proj.get("title") or pack.name
            pack.url = f"https://modrinth.com/modpack/{proj.get('slug')}"
        return pack

    def _resolve_curseforge(self, ident, version_id):
        if not self.cf_key:
            raise ModpackError("curseforge_key_required")
        hdr = {"x-api-key": self.cf_key}
        if version_id:
            _, r = _http_json(
                f"{CURSEFORGE}/mods/{ident}/files/{version_id}", headers=hdr
            )
            fobj = (r or {}).get("data")
        else:
            _, r = _http_json(
                f"{CURSEFORGE}/mods/{ident}/files?pageSize=50", headers=hdr
            )
            files = (r or {}).get("data") or []
            files.sort(key=lambda f: f.get("fileDate", ""), reverse=True)
            fobj = next((f for f in files if f.get("releaseType") == 1), None) or (
                files[0] if files else None
            )
        if not fobj:
            raise ModpackError("version_not_found", str(version_id or ident))
        url = fobj.get("downloadUrl")
        if not url:
            _, r = _http_json(
                f"{CURSEFORGE}/mods/{ident}/files/{fobj['id']}/download-url",
                headers=hdr,
            )
            url = (r or {}).get("data")
        if not url:
            raise ModpackError("download_blocked", "pack archive not distributable")
        sha1 = next(
            (h["value"] for h in fobj.get("hashes", []) if h.get("algo") == 1), None
        )
        archive = self._fetch_archive(url, fobj["fileName"], sha1)
        pack = self.resolve_archive(archive)
        pack.source = "curseforge"
        pack.project_id = str(ident)
        pack.version_id = str(fobj.get("id"))
        pack.version = fobj.get("displayName") or pack.version
        _, m = _http_json(f"{CURSEFORGE}/mods/{ident}", headers=hdr)
        if m and m.get("data"):
            pack.name = m["data"].get("name") or pack.name
            pack.url = (m["data"].get("links") or {}).get("websiteUrl")
        return pack

    def _fetch_archive(self, url, filename, sha1=None):
        if not _url_allowed(url):
            raise ModpackError("bad_host", url)
        os.makedirs(self.work_dir, exist_ok=True)
        dest = os.path.join(self.work_dir, os.path.basename(filename))
        self._set_status(status="resolving", current=os.path.basename(filename))
        ok, _ = _download(url, dest, sha1)
        if not ok:
            raise ModpackError("hash_mismatch", filename)
        return dest

    def resolve_archive(self, archive_path):
        """Parse a local .mrpack / CurseForge zip without installing anything."""
        if os.path.getsize(archive_path) > MAX_ARCHIVE_BYTES:
            raise ModpackError("archive_too_large")
        try:
            zf = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile as exc:
            raise ModpackError("bad_archive", str(exc)) from exc
        with zf:
            names = zf.namelist()
            if len(names) > MAX_ARCHIVE_ENTRIES:
                raise ModpackError("archive_too_many_entries")
            if "modrinth.index.json" in names:
                pack = self._parse_mrpack(zf)
            elif "manifest.json" in names:
                pack = self._parse_curseforge(zf)
            else:
                raise ModpackError("unknown_format")
        pack.archive_path = archive_path
        return pack

    def _read_index(self, zf, name):
        info = zf.getinfo(name)
        if info.file_size > MAX_INDEX_BYTES:
            raise ModpackError("index_too_large", name)
        try:
            return json.loads(zf.read(name).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModpackError("bad_index", str(exc)) from exc

    def _parse_mrpack(self, zf):
        index = self._read_index(zf, "modrinth.index.json")
        deps = index.get("dependencies") or {}
        loader, build = None, None
        for key, name in MRPACK_LOADER_KEYS.items():
            if deps.get(key):
                loader, build = name, deps[key]
                break
        if not deps.get("minecraft") or not loader:
            raise ModpackError("missing_dependencies", json.dumps(deps))
        pack = PackInfo(
            source="upload",
            format="mrpack",
            name=index.get("name") or "modpack",
            version=index.get("versionId") or "",
            mc=deps["minecraft"],
            loader=loader,
            loader_build=build,
            overrides=["overrides", "server-overrides"],
        )
        for f in index.get("files", []) or []:
            env = (f.get("env") or {}).get("server", "required")
            if env == "unsupported":
                pack.skipped.append({"name": f.get("path"), "reason": "client_only"})
                continue
            try:
                rel = safe_relative_path(f.get("path"))
            except ModpackError:
                pack.skipped.append({"name": f.get("path"), "reason": "bad_path"})
                continue
            urls = [u for u in f.get("downloads", []) or [] if _url_allowed(u)]
            if not urls:
                pack.skipped.append({"name": rel, "reason": "bad_host"})
                continue
            hashes = f.get("hashes") or {}
            pack.files.append(
                PackFile(
                    path=rel,
                    urls=urls,
                    sha1=hashes.get("sha1"),
                    sha512=hashes.get("sha512"),
                    size=int(f.get("fileSize") or 0),
                    name=os.path.basename(rel),
                )
            )
        return pack

    def _parse_curseforge(self, zf):
        manifest = self._read_index(zf, "manifest.json")
        mc = (manifest.get("minecraft") or {}).get("version")
        loaders = (manifest.get("minecraft") or {}).get("modLoaders") or []
        primary = next(
            (l for l in loaders if l.get("primary")), loaders[0] if loaders else None
        )
        loader, build = parse_loader_id((primary or {}).get("id"))
        if not mc or loader not in ("fabric", "forge", "neoforge", "quilt"):
            raise ModpackError("missing_dependencies", f"mc={mc} loader={loader}")
        overrides = manifest.get("overrides") or "overrides"
        try:
            overrides = safe_relative_path(overrides)
        except ModpackError:
            overrides = "overrides"
        pack = PackInfo(
            source="upload",
            format="curseforge",
            name=manifest.get("name") or "modpack",
            version=manifest.get("version") or "",
            mc=mc,
            loader=loader,
            loader_build=build,
            overrides=[overrides],
        )
        entries = [f for f in manifest.get("files", []) or [] if f.get("fileID")]
        if not entries:
            return pack
        if not self.cf_key:
            raise ModpackError("curseforge_key_required")
        file_ids = [int(f["fileID"]) for f in entries]
        by_id = {}
        for i in range(0, len(file_ids), 100):
            _, r = _http_json(
                f"{CURSEFORGE}/mods/files",
                "POST",
                headers={"x-api-key": self.cf_key},
                body={"fileIds": file_ids[i : i + 100]},
            )
            for fobj in (r or {}).get("data", []) or []:
                by_id[fobj.get("id")] = fobj
        blocked_mod_ids = []
        for entry in entries:
            fobj = by_id.get(int(entry["fileID"]))
            if not fobj:
                pack.skipped.append(
                    {"name": f"file {entry['fileID']}", "reason": "not_found"}
                )
                continue
            fname = fobj.get("fileName") or f"{entry['fileID']}.jar"
            url = fobj.get("downloadUrl")
            if not url:
                # allowModDistribution=false: cannot be fetched by third parties.
                blocked_mod_ids.append(fobj.get("modId"))
                pack.blocked.append(
                    {
                        "name": fname,
                        "project_id": fobj.get("modId"),
                        "file_id": fobj.get("id"),
                        "url": None,
                    }
                )
                continue
            if not _url_allowed(url):
                pack.skipped.append({"name": fname, "reason": "bad_host"})
                continue
            sha1 = next(
                (h["value"] for h in fobj.get("hashes", []) if h.get("algo") == 1),
                None,
            )
            try:
                rel = safe_relative_path("mods/" + fname)
            except ModpackError:
                pack.skipped.append({"name": fname, "reason": "bad_path"})
                continue
            pack.files.append(
                PackFile(
                    path=rel,
                    urls=[url],
                    sha1=sha1,
                    size=int(fobj.get("fileLength") or 0),
                    name=fname,
                )
            )
        if blocked_mod_ids:
            self._fill_blocked_urls(pack, blocked_mod_ids)
        return pack

    def _fill_blocked_urls(self, pack, mod_ids):
        """Look up project pages for blocked files so the user can grab them."""
        ids = sorted({m for m in mod_ids if m})
        links = {}
        for i in range(0, len(ids), 100):
            _, r = _http_json(
                f"{CURSEFORGE}/mods",
                "POST",
                headers={"x-api-key": self.cf_key},
                body={"modIds": ids[i : i + 100]},
            )
            for m in (r or {}).get("data", []) or []:
                links[m.get("id")] = (m.get("links") or {}).get("websiteUrl")
        for b in pack.blocked:
            b["url"] = links.get(b.get("project_id"))

    # ----------------------------------------------------------- installing
    def install(self, pack, mode="merge"):
        """Download all server-side files and apply overrides.

        mode="replace" moves the current mods/ folder to mods/_backup/<ts>/
        first; "merge" leaves existing files alone (same-hash files skipped).
        """
        if not self.server_path:
            raise ModpackError("no_server_path")
        total = len(pack.files) + len(pack.overrides)
        self._set_status(
            status="running",
            mode=mode,
            name=pack.name,
            version=pack.version,
            total=total,
            done=0,
            current="",
            installed=[],
            skipped=[s["name"] for s in pack.skipped],
            blocked=pack.blocked,
            errors=[],
            started=time.time(),
            finished=None,
        )
        summary = {"installed": [], "skipped": [], "errors": [], "backup": None}
        if mode == "replace":
            summary["backup"] = self._backup_mods()
        for pf in pack.files:
            self._set_status(current=pf.name)
            try:
                dest = safe_join(self.server_path, pf.path)
                if (
                    os.path.exists(dest)
                    and pf.sha1
                    and self._sha1(dest) == pf.sha1.lower()
                ):
                    summary["skipped"].append(pf.path)
                else:
                    self._download_any(pf, dest)
                    summary["installed"].append(pf.path)
            except (ModpackError, OSError) as exc:
                summary["errors"].append({"name": pf.path, "reason": str(exc)})
            self._bump(summary)
        for prefix in pack.overrides:
            self._set_status(current=f"{prefix}/")
            try:
                summary["installed"].extend(
                    self._extract_overrides(pack.archive_path, prefix)
                )
            except (ModpackError, OSError, zipfile.BadZipFile) as exc:
                summary["errors"].append({"name": prefix, "reason": str(exc)})
            self._bump(summary)
        self._set_status(
            status="done",
            current="",
            finished=time.time(),
            installed=summary["installed"],
            errors=summary["errors"],
            backup=summary["backup"],
        )
        return summary

    def _bump(self, summary):
        self._set_status(
            done=self._status.get("done", 0) + 1,
            installed=summary["installed"],
            errors=summary["errors"],
        )

    def _download_any(self, pf, dest):
        last = None
        for url in pf.urls:
            try:
                ok, _ = _download(url, dest, pf.sha1)
            except OSError as exc:
                last = exc
                continue
            if ok:
                return
            last = ModpackError("hash_mismatch", pf.name)
        raise last if last else ModpackError("download_failed", pf.name)

    def _extract_overrides(self, archive_path, prefix):
        if not archive_path or not os.path.exists(archive_path):
            return []
        written = []
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if not name.startswith(prefix + "/") or name.endswith("/"):
                    continue
                if _is_symlink(info):
                    continue
                rel = name[len(prefix) + 1 :]
                try:
                    dest = safe_join(self.server_path, rel)
                except ModpackError:
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                written.append(rel)
        return written

    def _backup_mods(self):
        mods = os.path.join(self.server_path, "mods")
        if not os.path.isdir(mods):
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(mods, "_backup", stamp)
        os.makedirs(backup, exist_ok=True)
        for entry in os.listdir(mods):
            if entry == "_backup":
                continue
            shutil.move(os.path.join(mods, entry), os.path.join(backup, entry))
        return os.path.relpath(backup, self.server_path)

    @staticmethod
    def _sha1(path):
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # --------------------------------------------------------------- status
    def _set_status(self, **fields):
        self._status.update(fields)
        if self.progress_cb:
            try:
                self.progress_cb(dict(self._status))
            except Exception:  # pylint: disable=broad-except
                pass
        if self.server_path:
            self.write_status(self.server_path, self._status)

    @staticmethod
    def write_status(server_path, status):
        path = os.path.join(server_path, STATUS_REL_PATH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f)
        os.replace(tmp, path)

    @staticmethod
    def read_status(server_path):
        path = os.path.join(server_path, STATUS_REL_PATH)
        if not os.path.exists(path):
            return {"status": "idle"}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"status": "idle"}

    def cleanup(self, pack):
        """Remove the downloaded archive when it lives in our work dir."""
        ap = pack.archive_path
        if ap and os.path.exists(ap) and os.path.dirname(ap) == self.work_dir:
            try:
                os.remove(ap)
            except OSError:
                pass
