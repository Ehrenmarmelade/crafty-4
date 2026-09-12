#!/usr/bin/env python3
"""
content_manager.py - Motor de gestión de contenido (mods/packs) para Crafty.

Autocontenido (solo stdlib). Identifica mods por hash en Modrinth (sha1) y
CurseForge (murmur2 fingerprint), calcula updates para la version de MC + loader
del servidor, y aplica actualizaciones con verificación de hash + backup.

Fuente: Modrinth primero, CurseForge como fallback (su buscador de texto está
capado para keys estándar, así que el descubrimiento va por Modrinth).
"""

import os, json, time, hashlib
import urllib.request, urllib.error, urllib.parse

UA = "crafty-content-manager/0.1"
MODRINTH = "https://api.modrinth.com/v2"
CURSEFORGE = "https://api.curseforge.com/v1"
CF_LOADER = {"neoforge": 6, "forge": 1, "fabric": 4, "quilt": 5, "liteloader": 3}
CF_LOADER_NAME = {v: k for k, v in CF_LOADER.items()}
CF_GAME_ID = 432  # Minecraft
CF_CLASS_MODPACK = 4471
# loaders that Crafty can install for a server (see main_controller loader mgr)
SERVER_LOADERS = ("fabric", "forge", "neoforge", "quilt")
CF_RELEASETYPE = {1: "release", 2: "beta", 3: "alpha"}
CHANNEL_RANK = {"release": 0, "beta": 1, "alpha": 2}


def channels_allowed(channel):
    rank = CHANNEL_RANK.get(channel, 0)
    return {c for c, r in CHANNEL_RANK.items() if r <= rank}


def _murmur2(data, seed=1):
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = (seed ^ length) & 0xFFFFFFFF
    i = 0
    while length >= 4:
        k = data[i] | (data[i + 1] << 8) | (data[i + 2] << 16) | (data[i + 3] << 24)
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
        length -= 4
    if length == 3:
        h ^= data[i + 2] << 16
    if length >= 2:
        h ^= data[i + 1] << 8
    if length >= 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h & 0xFFFFFFFF


def _cf_fingerprint(raw):
    return _murmur2(bytes(b for b in raw if b not in (9, 10, 13, 32)), 1)


def _http_json(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdr = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdr.update(headers)
    if data is not None:
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def _download(url, dest, expected_sha1=None):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    h = hashlib.sha1()
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    got = h.hexdigest()
    if expected_sha1 and got.lower() != expected_sha1.lower():
        os.remove(tmp)
        return False, got
    os.replace(tmp, dest)
    return True, got


FOLDER_BY_TYPE = {
    "mod": "mods",
    "resourcepack": "resourcepacks",
    "shader": "shaderpacks",
    "datapack": os.path.join("world", "datapacks"),
}
# (content_type, extensión) que se escanean en cada carpeta
SCAN_TYPES = [
    ("mod", ".jar"),
    ("resourcepack", ".zip"),
    ("shader", ".zip"),
    ("datapack", ".zip"),
]


class ContentManager:
    def __init__(self, server_path, mc=None, loader=None, cf_key=""):
        self.server_path = server_path
        self.mods_dir = os.path.join(server_path, "mods")
        self.cache_dir = os.path.join(server_path, ".content_cache")
        self.cache_file = os.path.join(self.cache_dir, "inventory.json")
        self.cf_key = cf_key
        # Autodetecta MC/loader del servidor si no se pasan explícitos.
        dmc, dloader = self._autodetect()
        # True when the context is real (passed in or found on disk) instead of
        # the historical defaults below; modpack search only filters when it is.
        self.mc_detected = bool(mc or dmc)
        self.loader_detected = bool(loader or dloader)
        self.mc = mc or dmc or "1.21.1"
        self.loader = (loader or dloader or "neoforge").lower()
        self.lt = CF_LOADER.get(self.loader, 6)

    def _autodetect(self):
        """Deduce (mc, loader) mirando libraries/ del servidor."""
        net = os.path.join(self.server_path, "libraries", "net")
        neo = os.path.join(net, "neoforged", "neoforge")
        if os.path.isdir(neo):
            vers = sorted(os.listdir(neo))
            if vers:
                p = vers[-1].split(".")  # p.ej. 21.1.235 -> 1.21.1  (21.0.x -> 1.21)
                if len(p) >= 2:
                    mc = f"1.{p[0]}" if p[1] == "0" else f"1.{p[0]}.{p[1]}"
                    return mc, "neoforge"
        forge = os.path.join(net, "minecraftforge", "forge")
        if os.path.isdir(forge):
            vers = os.listdir(forge)
            if vers:
                return vers[0].split("-")[0], "forge"
        if os.path.isdir(os.path.join(net, "fabricmc")):
            return None, "fabric"
        return None, None

    # ---------- scan ----------
    def _folder(self, content_type):
        return os.path.join(self.server_path, FOLDER_BY_TYPE.get(content_type, "mods"))

    def scan(self):
        inv, sha_to, fp_to = {}, {}, {}
        for ctype, ext in SCAN_TYPES:
            folder = self._folder(ctype)
            if not os.path.isdir(folder):
                continue
            for name in os.listdir(folder):
                low = name.lower()
                if not (low.endswith(ext) or low.endswith(ext + ".disabled")):
                    continue
                with open(os.path.join(folder, name), "rb") as fh:
                    raw = fh.read()
                sha1 = hashlib.sha1(raw).hexdigest()
                fp = _cf_fingerprint(raw)
                enabled = not low.endswith(".disabled")
                display = name if enabled else name[:-9]
                side = (
                    "client-only"
                    if ctype in ("resourcepack", "shader")
                    else "server-only" if ctype == "datapack" else None
                )
                inv[name] = {
                    "filename": name,
                    "display": display,
                    "enabled": enabled,
                    "content_type": ctype,
                    "sha1": sha1,
                    "murmur2": fp,
                    "size": len(raw),
                    "added": os.path.getmtime(os.path.join(folder, name)),
                    "title": display,
                    "icon": None,
                    "author": None,
                    "modrinth": None,
                    "curseforge": None,
                    "side": side,
                    "latest": None,
                    "update": False,
                    "channel": None,
                    "source": "unknown",
                    "mr_date": None,
                    "cf_date": None,
                }
                sha_to[sha1] = name
                fp_to[fp] = name

        sha1s = list(sha_to)
        _, ident = _http_json(
            f"{MODRINTH}/version_files",
            "POST",
            body={"hashes": sha1s, "algorithm": "sha1"},
        )
        ident = ident if isinstance(ident, dict) else {}
        # updates: mods filtran por loader; packs/shaders/datapacks solo por versión MC
        mod_sha = [s for s in sha1s if inv[sha_to[s]]["content_type"] == "mod"]
        pack_sha = [s for s in sha1s if inv[sha_to[s]]["content_type"] != "mod"]
        upd = {}
        if mod_sha:
            _, u = _http_json(
                f"{MODRINTH}/version_files/update",
                "POST",
                body={
                    "hashes": mod_sha,
                    "algorithm": "sha1",
                    "loaders": [self.loader],
                    "game_versions": [self.mc],
                },
            )
            if isinstance(u, dict):
                upd.update(u)
        if pack_sha:
            _, u = _http_json(
                f"{MODRINTH}/version_files/update",
                "POST",
                body={
                    "hashes": pack_sha,
                    "algorithm": "sha1",
                    "game_versions": [self.mc],
                },
            )
            if isinstance(u, dict):
                upd.update(u)

        pids = {v["project_id"] for v in ident.values() if v.get("project_id")}
        projects = self._mr_projects(pids)
        for sha1, ver in ident.items():
            rec = inv[sha_to[sha1]]
            pid = ver.get("project_id")
            proj = projects.get(pid, {})
            rec["modrinth"] = {
                "projectId": pid,
                "versionId": ver.get("id"),
                "slug": proj.get("slug"),
                "title": proj.get("title"),
            }
            rec["mr_date"] = ver.get("date_published")
            if proj.get("title"):
                rec["title"] = proj["title"]
            rec["icon"] = proj.get("icon_url")
            ss, cs = proj.get("server_side"), proj.get("client_side")
            if ss and cs:
                rec["side"] = (
                    "client-only"
                    if ss == "unsupported"
                    else "server-only" if cs == "unsupported" else "both"
                )
            rec["source"] = "modrinth"
        for sha1, ver in upd.items():
            rec = inv.get(sha_to.get(sha1))
            if not rec:
                continue
            files = ver.get("files", [])
            pf = next(
                (f for f in files if f.get("primary")), files[0] if files else None
            )
            rec["channel"] = ver.get("version_type")
            if pf:
                rec["latest"] = pf.get("filename")
                rec["update"] = pf.get("hashes", {}).get("sha1") != sha1

        # CF fingerprint solo para MODS no identificados
        unknown = [
            n
            for n, r in inv.items()
            if r["source"] == "unknown" and r["content_type"] == "mod"
        ]
        if unknown and self.cf_key:
            matches = self._cf_fingerprints([inv[n]["murmur2"] for n in unknown])
            for fp, m in matches.items():
                rec = inv[fp_to[fp]]
                cur = m["file"]
                rec["curseforge"] = {
                    "modId": m["modId"],
                    "fileId": cur.get("id"),
                    "fileName": cur.get("fileName"),
                }
                rec["cf_date"] = cur.get("fileDate")
                rec["source"] = "curseforge"
                info = self._cf_mod(m["modId"])
                if info:
                    rec["title"] = info.get("name") or rec["title"]
                    rec["icon"] = (info.get("logo") or {}).get("url")
                    authors = info.get("authors") or []
                    rec["author"] = authors[0]["name"] if authors else None
                best = self._cf_best(m["modId"], channels_allowed("alpha"))
                time.sleep(0.05)
                if best:
                    rec["latest"] = best.get("fileName")
                    rec["channel"] = CF_RELEASETYPE.get(best.get("releaseType"))
                    rec["update"] = best.get("id") != cur.get("id")
        result = list(inv.values())
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "scanned_at": time.strftime("%Y-%m-%d %H:%M"),
                        "mc": self.mc,
                        "loader": self.loader,
                        "inventory": result,
                    },
                    f,
                    ensure_ascii=False,
                )
        except OSError:
            pass
        return result

    def load_cache(self):
        if not os.path.exists(self.cache_file):
            return None
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def save_cache(self, inventory):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "scanned_at": time.strftime("%Y-%m-%d %H:%M"),
                        "mc": self.mc,
                        "loader": self.loader,
                        "inventory": inventory,
                    },
                    f,
                    ensure_ascii=False,
                )
            return {"ok": True}
        except OSError:
            return {"ok": False}

    def _record_for(self, folder, name, ctype):
        with open(os.path.join(folder, name), "rb") as fh:
            raw = fh.read()
        enabled = not name.lower().endswith(".disabled")
        display = name if enabled else name[:-9]
        side = (
            "client-only"
            if ctype in ("resourcepack", "shader")
            else "server-only" if ctype == "datapack" else None
        )
        return {
            "filename": name,
            "display": display,
            "enabled": enabled,
            "content_type": ctype,
            "sha1": hashlib.sha1(raw).hexdigest(),
            "murmur2": _cf_fingerprint(raw),
            "size": len(raw),
            "added": os.path.getmtime(os.path.join(folder, name)),
            "title": display,
            "icon": None,
            "author": None,
            "modrinth": None,
            "curseforge": None,
            "side": side,
            "latest": None,
            "update": False,
            "channel": None,
            "source": "unknown",
            "mr_date": None,
            "cf_date": None,
        }

    def scan_one(self, filename, content_type="mod"):
        """Escanea un único archivo (rápido) y devuelve su registro — para
        refrescar la UI de forma reactiva tras instalar/actualizar."""
        folder = self._folder(content_type)
        if not filename or not os.path.exists(os.path.join(folder, filename)):
            return None
        rec = self._record_for(folder, filename, content_type)
        _, ident = _http_json(
            f"{MODRINTH}/version_files",
            "POST",
            body={"hashes": [rec["sha1"]], "algorithm": "sha1"},
        )
        ver = (ident or {}).get(rec["sha1"]) if isinstance(ident, dict) else None
        if ver and ver.get("project_id"):
            pid = ver["project_id"]
            proj = self._mr_projects([pid]).get(pid, {})
            rec["modrinth"] = {
                "projectId": pid,
                "versionId": ver.get("id"),
                "slug": proj.get("slug"),
                "title": proj.get("title"),
            }
            rec["mr_date"] = ver.get("date_published")
            if proj.get("title"):
                rec["title"] = proj["title"]
            rec["icon"] = proj.get("icon_url")
            ss, cs = proj.get("server_side"), proj.get("client_side")
            if ss and cs:
                rec["side"] = (
                    "client-only"
                    if ss == "unsupported"
                    else "server-only" if cs == "unsupported" else "both"
                )
            rec["source"] = "modrinth"
            best = self._mr_best_generic(pid, content_type, channels_allowed("alpha"))
            if best:
                files = best.get("files", [])
                pf = next(
                    (f for f in files if f.get("primary")), files[0] if files else None
                )
                rec["channel"] = best.get("version_type")
                if pf:
                    rec["latest"] = pf.get("filename")
                    rec["update"] = (
                        best.get("date_published", "") > (rec["mr_date"] or "")
                        and pf.get("hashes", {}).get("sha1") != rec["sha1"]
                    )
        elif content_type == "mod" and self.cf_key:
            m = self._cf_fingerprints([rec["murmur2"]]).get(rec["murmur2"])
            if m:
                cur = m["file"]
                rec["curseforge"] = {
                    "modId": m["modId"],
                    "fileId": cur.get("id"),
                    "fileName": cur.get("fileName"),
                }
                rec["cf_date"] = cur.get("fileDate")
                rec["source"] = "curseforge"
                info = self._cf_mod(m["modId"])
                if info:
                    rec["title"] = info.get("name") or rec["title"]
                    rec["icon"] = (info.get("logo") or {}).get("url")
                    a = info.get("authors") or []
                    rec["author"] = a[0]["name"] if a else None
                best = self._cf_best(m["modId"], channels_allowed("alpha"))
                if best:
                    rec["latest"] = best.get("fileName")
                    rec["channel"] = CF_RELEASETYPE.get(best.get("releaseType"))
                    rec["update"] = best.get("id") != cur.get("id")
        return rec

    # ---------- plan / apply ----------
    def plan(self, inventory, channel="release"):
        allowed = channels_allowed(channel)
        installed = {
            r["modrinth"]["projectId"]
            for r in inventory
            if r.get("modrinth") and r["modrinth"].get("projectId")
        }
        out = []
        for r in inventory:
            if r["source"] == "modrinth" and r.get("modrinth"):
                best = self._mr_best(r["modrinth"]["projectId"], allowed)
                if not best:
                    continue
                files = best.get("files", [])
                pf = next(
                    (f for f in files if f.get("primary")), files[0] if files else None
                )
                if not pf:
                    continue
                new_sha1 = pf.get("hashes", {}).get("sha1")
                cand_date = best.get("date_published")
                newer = (not r.get("mr_date")) or (
                    cand_date and cand_date > r["mr_date"]
                )
                if new_sha1 and new_sha1 != r["sha1"] and newer:
                    deps = [
                        d["project_id"]
                        for d in best.get("dependencies", [])
                        if d.get("dependency_type") == "required"
                        and d.get("project_id")
                        and d["project_id"] not in installed
                    ]
                    out.append(
                        {
                            "name": r["filename"],
                            "source": "modrinth",
                            "url": pf.get("url"),
                            "sha1": new_sha1,
                            "newname": pf.get("filename"),
                            "channel": best.get("version_type"),
                            "deps": deps,
                            "blocked": False,
                        }
                    )
            elif r["source"] == "curseforge" and r.get("curseforge"):
                mod_id = r["curseforge"]["modId"]
                best = self._cf_best(mod_id, allowed)
                cand_date = best.get("fileDate") if best else None
                newer = (not r.get("cf_date")) or (
                    cand_date and cand_date > r["cf_date"]
                )
                if best and best.get("id") != r["curseforge"]["fileId"] and newer:
                    url = best.get("downloadUrl") or self._cf_download_url(
                        mod_id, best["id"]
                    )
                    out.append(
                        {
                            "name": r["filename"],
                            "source": "curseforge",
                            "url": url,
                            "sha1": self._cf_sha1(best),
                            "newname": best.get("fileName"),
                            "channel": CF_RELEASETYPE.get(best.get("releaseType")),
                            "deps": [],
                            "blocked": url is None,
                        }
                    )
        return out

    def apply(self, plan):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(self.mods_dir, "_backup", stamp)
        dl = os.path.join(self.server_path, ".content_cache", stamp)
        results = []
        for p in plan:
            if p.get("blocked") or not p.get("url"):
                results.append(
                    {"name": p["name"], "ok": False, "reason": "no_download_url"}
                )
                continue
            dest = os.path.join(dl, p["newname"])
            good, got = _download(p["url"], dest, p.get("sha1"))
            if not good:
                results.append(
                    {"name": p["name"], "ok": False, "reason": "hash_mismatch"}
                )
                continue
            os.makedirs(backup, exist_ok=True)
            old = os.path.join(self.mods_dir, p["name"])
            if os.path.exists(old):
                os.replace(old, os.path.join(backup, p["name"]))
            os.replace(dest, os.path.join(self.mods_dir, p["newname"]))
            results.append({"name": p["name"], "ok": True, "newname": p["newname"]})
        return {"backup": backup, "results": results}

    # ---------- acciones por fila (conscientes de la carpeta según content_type) ----------
    def toggle(self, filename, content_type="mod"):
        folder = self._folder(content_type)
        src = os.path.join(folder, filename)
        if not os.path.exists(src):
            return {"ok": False, "reason": "not_found"}
        if filename.lower().endswith(".disabled"):
            dst = os.path.join(folder, filename[:-9])
            os.replace(src, dst)
            return {"ok": True, "enabled": True, "filename": os.path.basename(dst)}
        dst = src + ".disabled"
        os.replace(src, dst)
        return {"ok": True, "enabled": False, "filename": os.path.basename(dst)}

    def remove(self, filename, content_type="mod"):
        folder = self._folder(content_type)
        src = os.path.join(folder, filename)
        if not os.path.exists(src):
            return {"ok": False, "reason": "not_found"}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(folder, "_backup", stamp)
        os.makedirs(backup, exist_ok=True)
        os.replace(src, os.path.join(backup, filename))
        return {"ok": True, "backup": backup}

    def update_one(self, filename, channel="release", content_type="mod"):
        folder = self._folder(content_type)
        path = os.path.join(folder, filename)
        if not os.path.exists(path):
            return {"ok": False, "reason": "not_found"}
        with open(path, "rb") as fh:
            raw = fh.read()
        sha1 = hashlib.sha1(raw).hexdigest()
        fp = _cf_fingerprint(raw)
        allowed = channels_allowed(channel)
        _, ident = _http_json(
            f"{MODRINTH}/version_files",
            "POST",
            body={"hashes": [sha1], "algorithm": "sha1"},
        )
        ver = (ident or {}).get(sha1)
        if ver and ver.get("project_id"):
            best = self._mr_best_generic(ver["project_id"], content_type, allowed)
            if not best:
                return {"ok": False, "reason": "no_version"}
            files = best.get("files", [])
            pf = next(
                (f for f in files if f.get("primary")), files[0] if files else None
            )
            new_sha1 = (pf or {}).get("hashes", {}).get("sha1")
            older = best.get("date_published", "") <= (ver.get("date_published") or "")
            if not new_sha1 or new_sha1 == sha1 or older:
                return {"ok": False, "reason": "already_latest"}
            return self._swap(
                filename, pf["url"], new_sha1, pf["filename"], content_type
            )
        if content_type == "mod":
            m = self._cf_fingerprints([fp]).get(fp)
            if m:
                best = self._cf_best(m["modId"], allowed)
                older = best and best.get("fileDate", "") <= (
                    m["file"].get("fileDate") or ""
                )
                if not best or best.get("id") == m["file"].get("id") or older:
                    return {"ok": False, "reason": "already_latest"}
                url = best.get("downloadUrl") or self._cf_download_url(
                    m["modId"], best["id"]
                )
                if not url:
                    return {"ok": False, "reason": "download_blocked"}
                return self._swap(
                    filename,
                    url,
                    self._cf_sha1(best),
                    best.get("fileName"),
                    content_type,
                )
        return {"ok": False, "reason": "not_identified"}

    def _swap(self, old_name, url, sha1, new_name, content_type="mod"):
        folder = self._folder(content_type)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dl = os.path.join(self.server_path, ".content_cache", stamp)
        good, _ = _download(url, os.path.join(dl, new_name), sha1)
        if not good:
            return {"ok": False, "reason": "hash_mismatch"}
        backup = os.path.join(folder, "_backup", stamp)
        os.makedirs(backup, exist_ok=True)
        old = os.path.join(folder, old_name)
        if os.path.exists(old):
            os.replace(old, os.path.join(backup, old_name))
        os.replace(os.path.join(dl, new_name), os.path.join(folder, new_name))
        return {"ok": True, "old": old_name, "new": new_name}

    # ---------- search ----------
    def search(self, query, project_type="mod", limit=10):
        facets = [[f"project_type:{project_type}"], [f"versions:{self.mc}"]]
        if project_type == "mod":
            facets.append([f"categories:{self.loader}"])
        params = {
            "query": query or "",
            "limit": str(limit),
            "index": "relevance",
            "facets": json.dumps(facets),
        }
        _, r = _http_json(f"{MODRINTH}/search?" + urllib.parse.urlencode(params))
        return r.get("hits", []) if isinstance(r, dict) else []

    # ---------- install (añadir nuevo desde Modrinth) ----------
    def install(self, identifier, project_type="mod", channel="release"):
        if not identifier:
            return {"ok": False, "reason": "missing_identifier"}
        allowed = channels_allowed(channel)
        best = self._mr_best_generic(identifier, project_type, allowed)
        if not best:
            return {"ok": False, "reason": "no_matching_version"}
        files = best.get("files", [])
        pf = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not pf:
            return {"ok": False, "reason": "no_file"}
        folder = os.path.join(
            self.server_path, FOLDER_BY_TYPE.get(project_type, "mods")
        )
        good, _ = _download(
            pf["url"],
            os.path.join(folder, pf["filename"]),
            pf.get("hashes", {}).get("sha1"),
        )
        if not good:
            return {"ok": False, "reason": "hash_mismatch"}
        installed = [pf["filename"]]
        # dependencias requeridas (solo para mods, van a mods/)
        for d in best.get("dependencies", []):
            if d.get("dependency_type") == "required" and d.get("project_id"):
                db = self._mr_best(d["project_id"], allowed)
                if not db:
                    continue
                dfiles = db.get("files", [])
                dpf = next(
                    (f for f in dfiles if f.get("primary")),
                    dfiles[0] if dfiles else None,
                )
                if dpf and not os.path.exists(
                    os.path.join(self.mods_dir, dpf["filename"])
                ):
                    ok, _ = _download(
                        dpf["url"],
                        os.path.join(self.mods_dir, dpf["filename"]),
                        dpf.get("hashes", {}).get("sha1"),
                    )
                    if ok:
                        installed.append(dpf["filename"])
        return {"ok": True, "installed": installed}

    def _mr_best_generic(self, ident, project_type, allowed):
        gv = urllib.parse.quote(json.dumps([self.mc]))
        if project_type == "mod":
            lo = urllib.parse.quote(json.dumps([self.loader]))
            url = f"{MODRINTH}/project/{ident}/version?loaders={lo}&game_versions={gv}"
        else:
            url = f"{MODRINTH}/project/{ident}/version?game_versions={gv}"
        _, r = _http_json(url)
        vs = [v for v in (r or []) if v.get("version_type") in allowed]
        vs.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        return vs[0] if vs else None

    # ---------- ficha detallada + versiones ----------
    def detail(self, source, ident):
        if source == "modrinth":
            _, p = _http_json(f"{MODRINTH}/project/{ident}")
            if not p:
                return None
            return {
                "source": "modrinth",
                "title": p.get("title"),
                "summary": p.get("description"),
                "icon": p.get("icon_url"),
                "downloads": p.get("downloads"),
                "updated": p.get("updated"),
                "categories": p.get("categories", []),
                "author": None,
                "url": f"https://modrinth.com/mod/{p.get('slug')}",
                "links": {
                    "source": p.get("source_url"),
                    "issues": p.get("issues_url"),
                    "wiki": p.get("wiki_url"),
                },
            }
        _, r = _http_json(
            f"{CURSEFORGE}/mods/{ident}", headers={"x-api-key": self.cf_key}
        )
        d = (r or {}).get("data")
        if not d:
            return None
        authors = d.get("authors") or []
        return {
            "source": "curseforge",
            "title": d.get("name"),
            "summary": d.get("summary"),
            "icon": (d.get("logo") or {}).get("url"),
            "downloads": d.get("downloadCount"),
            "updated": d.get("dateModified"),
            "categories": [c.get("name") for c in d.get("categories", [])],
            "author": authors[0]["name"] if authors else None,
            "url": (d.get("links") or {}).get("websiteUrl"),
            "links": {},
        }

    def versions(self, source, ident, all_versions=False, project_type="mod"):
        out = []
        gv = urllib.parse.quote(json.dumps([self.mc]))
        if source == "modrinth":
            if all_versions:
                url = f"{MODRINTH}/project/{ident}/version"
            elif project_type == "mod":
                lo = urllib.parse.quote(json.dumps([self.loader]))
                url = f"{MODRINTH}/project/{ident}/version?loaders={lo}&game_versions={gv}"
            else:  # resourcepack/shader/datapack: NO se filtra por loader de mods
                url = f"{MODRINTH}/project/{ident}/version?game_versions={gv}"
            _, vs = _http_json(url)
            for v in vs or []:
                files = v.get("files", [])
                pf = next(
                    (f for f in files if f.get("primary")), files[0] if files else None
                )
                out.append(
                    {
                        "id": v.get("id"),
                        "name": v.get("version_number"),
                        "type": v.get("version_type"),
                        "date": v.get("date_published"),
                        "game_versions": v.get("game_versions", []),
                        "loaders": v.get("loaders", []),
                        "filename": pf.get("filename") if pf else None,
                    }
                )
        else:
            if all_versions:
                url = f"{CURSEFORGE}/mods/{ident}/files?pageSize=50"
            elif project_type == "mod":
                url = (
                    f"{CURSEFORGE}/mods/{ident}/files"
                    f"?gameVersion={self.mc}&modLoaderType={self.lt}&pageSize=50"
                )
            else:
                url = (
                    f"{CURSEFORGE}/mods/{ident}/files?gameVersion={self.mc}&pageSize=50"
                )
            _, r = _http_json(url, headers={"x-api-key": self.cf_key})
            for f in (r or {}).get("data", []):
                gvs = f.get("gameVersions", [])
                out.append(
                    {
                        "id": f.get("id"),
                        "name": f.get("displayName") or f.get("fileName"),
                        "type": CF_RELEASETYPE.get(f.get("releaseType")),
                        "date": f.get("fileDate"),
                        "game_versions": [g for g in gvs if g[:1].isdigit()],
                        "loaders": [
                            g.lower() for g in gvs if g.lower() in SERVER_LOADERS
                        ],
                        "filename": f.get("fileName"),
                    }
                )
        return out

    def install_version(
        self, source, ident, version_id, current_filename=None, project_type="mod"
    ):
        if source == "modrinth":
            _, v = _http_json(f"{MODRINTH}/version/{version_id}")
            if not v:
                return {"ok": False, "reason": "version_not_found"}
            files = v.get("files", [])
            pf = next(
                (f for f in files if f.get("primary")), files[0] if files else None
            )
            if not pf:
                return {"ok": False, "reason": "no_file"}
            url, sha1, fname = (
                pf.get("url"),
                pf.get("hashes", {}).get("sha1"),
                pf.get("filename"),
            )
        else:
            _, r = _http_json(
                f"{CURSEFORGE}/mods/{ident}/files/{version_id}",
                headers={"x-api-key": self.cf_key},
            )
            fobj = (r or {}).get("data")
            if not fobj:
                return {"ok": False, "reason": "file_not_found"}
            fname, sha1 = fobj.get("fileName"), self._cf_sha1(fobj)
            url = fobj.get("downloadUrl") or self._cf_download_url(ident, version_id)
            if not url:
                return {"ok": False, "reason": "download_blocked"}
        if current_filename:
            return self._swap(current_filename, url, sha1, fname, project_type)
        folder = os.path.join(
            self.server_path, FOLDER_BY_TYPE.get(project_type, "mods")
        )
        good, _ = _download(url, os.path.join(folder, fname), sha1)
        return (
            {"ok": True, "new": fname}
            if good
            else {"ok": False, "reason": "hash_mismatch"}
        )

    # ---------- modpacks ----------
    def modpack_search(self, query, limit=10, mc=None, loader=None, source=None):
        """Search modpacks on Modrinth (+ CurseForge when a key is configured).

        mc/loader are soft filters; when omitted the server context is used only
        if it was actually detected (a modpack defines its own MC + loader, so
        the wizard searches unfiltered).
        """
        mc = mc if mc is not None else (self.mc if self.mc_detected else None)
        loader = (
            loader
            if loader is not None
            else (self.loader if self.loader_detected else None)
        )
        hits = []
        if source in (None, "modrinth"):
            hits.extend(self._mr_modpack_search(query, limit, mc, loader))
        if self.cf_key and source in (None, "curseforge"):
            hits.extend(self._cf_modpack_search(query, limit, mc, loader))
        return {"hits": hits, "cf_enabled": bool(self.cf_key)}

    def _mr_modpack_search(self, query, limit, mc, loader):
        facets = [["project_type:modpack"]]
        if mc:
            facets.append([f"versions:{mc}"])
        if loader:
            facets.append([f"categories:{loader}"])
        params = {
            "query": query or "",
            "limit": str(limit),
            "index": "relevance" if query else "downloads",
            "facets": json.dumps(facets),
        }
        _, r = _http_json(f"{MODRINTH}/search?" + urllib.parse.urlencode(params))
        out = []
        for h in r.get("hits", []) if isinstance(r, dict) else []:
            cats = h.get("categories", []) + h.get("display_categories", [])
            out.append(
                {
                    "source": "modrinth",
                    "id": h.get("project_id"),
                    "slug": h.get("slug"),
                    "title": h.get("title"),
                    "summary": h.get("description"),
                    "icon": h.get("icon_url"),
                    "author": h.get("author"),
                    "downloads": h.get("downloads", 0),
                    "mc_versions": h.get("versions", []),
                    "loaders": sorted({c for c in cats if c in SERVER_LOADERS}),
                    "url": f"https://modrinth.com/modpack/{h.get('slug')}",
                }
            )
        return out

    def _cf_modpack_search(self, query, limit, mc, loader):
        params = {
            "gameId": CF_GAME_ID,
            "classId": CF_CLASS_MODPACK,
            "pageSize": str(limit),
            "sortField": 2,  # popularity
            "sortOrder": "desc",
        }
        if query:
            params["searchFilter"] = query
        if mc:
            params["gameVersion"] = mc
        if loader and loader in CF_LOADER:
            params["modLoaderType"] = CF_LOADER[loader]
        _, r = _http_json(
            f"{CURSEFORGE}/mods/search?" + urllib.parse.urlencode(params),
            headers={"x-api-key": self.cf_key},
        )
        out = []
        for m in (r or {}).get("data", []) or []:
            idx = m.get("latestFilesIndexes", []) or []
            out.append(
                {
                    "source": "curseforge",
                    "id": m.get("id"),
                    "slug": m.get("slug"),
                    "title": m.get("name"),
                    "summary": m.get("summary"),
                    "icon": (m.get("logo") or {}).get("url"),
                    "author": ((m.get("authors") or [{}])[0]).get("name"),
                    "downloads": m.get("downloadCount", 0),
                    "mc_versions": sorted(
                        {i.get("gameVersion") for i in idx if i.get("gameVersion")}
                    ),
                    "loaders": sorted(
                        {
                            CF_LOADER_NAME[i["modLoader"]]
                            for i in idx
                            if i.get("modLoader") in CF_LOADER_NAME
                        }
                    ),
                    "url": (m.get("links") or {}).get("websiteUrl"),
                }
            )
        return out

    def cf_slug_to_id(self, slug):
        """Resolve a CurseForge modpack slug (from its URL) to a numeric id."""
        if not self.cf_key:
            return None
        params = {"gameId": CF_GAME_ID, "classId": CF_CLASS_MODPACK, "slug": slug}
        _, r = _http_json(
            f"{CURSEFORGE}/mods/search?" + urllib.parse.urlencode(params),
            headers={"x-api-key": self.cf_key},
        )
        data = (r or {}).get("data") or []
        return data[0].get("id") if data else None

    # ---------- helpers ----------
    def _mr_projects(self, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 100):
            q = urllib.parse.quote(json.dumps(ids[i : i + 100]))
            _, r = _http_json(f"{MODRINTH}/projects?ids={q}")
            if isinstance(r, list):
                for p in r:
                    out[p["id"]] = p
        return out

    def _mr_best(self, pid, allowed):
        lo = urllib.parse.quote(json.dumps([self.loader]))
        gv = urllib.parse.quote(json.dumps([self.mc]))
        _, r = _http_json(
            f"{MODRINTH}/project/{pid}/version?loaders={lo}&game_versions={gv}"
        )
        vs = [v for v in (r or []) if v.get("version_type") in allowed]
        vs.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        return vs[0] if vs else None

    def _cf_fingerprints(self, fps):
        _, r = _http_json(
            f"{CURSEFORGE}/fingerprints",
            "POST",
            headers={"x-api-key": self.cf_key},
            body={"fingerprints": fps},
        )
        matches = {}
        if r:
            for m in r.get("data", {}).get("exactMatches", []):
                f = m.get("file", {})
                matches[f.get("fileFingerprint")] = {"modId": m.get("id"), "file": f}
        return matches

    def _cf_best(self, mod_id, allowed):
        url = (
            f"{CURSEFORGE}/mods/{mod_id}/files"
            f"?gameVersion={self.mc}&modLoaderType={self.lt}&pageSize=30"
        )
        _, r = _http_json(url, headers={"x-api-key": self.cf_key})
        files = [
            f
            for f in (r.get("data", []) if r else [])
            if CF_RELEASETYPE.get(f.get("releaseType"), "release") in allowed
        ]
        files.sort(key=lambda f: f.get("fileDate", ""), reverse=True)
        return files[0] if files else None

    def _cf_download_url(self, mod_id, file_id):
        _, r = _http_json(
            f"{CURSEFORGE}/mods/{mod_id}/files/{file_id}/download-url",
            headers={"x-api-key": self.cf_key},
        )
        return r.get("data") if r else None

    def _cf_mod(self, mod_id):
        _, r = _http_json(
            f"{CURSEFORGE}/mods/{mod_id}", headers={"x-api-key": self.cf_key}
        )
        return r.get("data") if r else None

    @staticmethod
    def _cf_sha1(file_obj):
        for h in file_obj.get("hashes", []):
            if h.get("algo") == 1:
                return h.get("value")
        return None
