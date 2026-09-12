import hashlib
import json
import zipfile

import pytest

from app.classes.minecraft import modpack_installer as mi
from app.classes.minecraft.modpack_installer import (
    ModpackError,
    ModpackInstaller,
    PackInfo,
    parse_loader_id,
    safe_join,
    safe_relative_path,
)


# ----------------------------------------------------------------- fixtures
def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


JAR_A = b"jar-a-bytes"
JAR_B = b"jar-b-bytes"
URL_A = "https://cdn.modrinth.com/data/aaa/versions/1/a.jar"
URL_B = "https://cdn.modrinth.com/data/bbb/versions/1/b.jar"
URL_EVIL = "https://evil.example.com/x.jar"


def _mrpack_index(extra_files=None):
    files = [
        {
            "path": "mods/a.jar",
            "hashes": {"sha1": _sha1(JAR_A)},
            "env": {"client": "required", "server": "required"},
            "downloads": [URL_A],
            "fileSize": len(JAR_A),
        },
        {
            "path": "mods/b.jar",
            "hashes": {"sha1": _sha1(JAR_B)},
            "downloads": [URL_B],
            "fileSize": len(JAR_B),
        },
        {  # client-only -> skipped
            "path": "mods/client.jar",
            "hashes": {"sha1": "00"},
            "env": {"client": "required", "server": "unsupported"},
            "downloads": [URL_A],
        },
        {  # traversal -> skipped
            "path": "../outside.jar",
            "hashes": {"sha1": "00"},
            "downloads": [URL_A],
        },
        {  # bad host -> skipped
            "path": "mods/evil.jar",
            "hashes": {"sha1": "00"},
            "downloads": [URL_EVIL],
        },
    ] + (extra_files or [])
    return {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1.2.3",
        "name": "Test Pack",
        "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.9"},
        "files": files,
    }


@pytest.fixture
def mrpack(tmp_path):
    path = tmp_path / "test.mrpack"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(_mrpack_index()))
        zf.writestr("overrides/config/a.toml", "a=1")
        zf.writestr("server-overrides/server.properties", "motd=hi")
        zf.writestr("overrides/../escape.txt", "nope")
        # symlink entry (external_attr marks it as a link)
        info = zipfile.ZipInfo("overrides/link")
        info.external_attr = 0o120777 << 16
        zf.writestr(info, "/etc/passwd")
    return str(path)


@pytest.fixture
def cf_zip(tmp_path):
    manifest = {
        "minecraft": {
            "version": "1.20.1",
            "modLoaders": [{"id": "forge-47.3.0", "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "name": "CF Pack",
        "version": "9",
        "overrides": "overrides",
        "files": [
            {"projectID": 1, "fileID": 11, "required": True},
            {"projectID": 2, "fileID": 22, "required": True},
        ],
    }
    path = tmp_path / "cf.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("overrides/config/cf.cfg", "x")
    return str(path)


@pytest.fixture
def fake_net(monkeypatch):
    """Replace HTTP with an in-memory map of url -> bytes and json responses."""
    store = {URL_A: JAR_A, URL_B: JAR_B}
    calls = {"json": [], "download": []}

    def fake_download(url, dest, expected_sha1=None):
        calls["download"].append(url)
        data = store.get(url)
        if data is None:
            raise OSError("404")
        got = _sha1(data)
        if expected_sha1 and got != expected_sha1.lower():
            return False, got
        import os

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return True, got

    responses = {}

    def fake_http_json(url, method="GET", headers=None, body=None):
        calls["json"].append((method, url, body))
        for key, value in responses.items():
            if key in url:
                return 200, value
        return 404, None

    monkeypatch.setattr(mi, "_download", fake_download)
    monkeypatch.setattr(mi, "_http_json", fake_http_json)
    return {"store": store, "responses": responses, "calls": calls}


# -------------------------------------------------------------- unit tests
def test_parse_loader_id():
    assert parse_loader_id("forge-47.3.0") == ("forge", "47.3.0")
    assert parse_loader_id("NeoForge-21.1.65") == ("neoforge", "21.1.65")
    assert parse_loader_id("fabric") == ("fabric", None)
    assert parse_loader_id(None) == ("", None)


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../../b", "C:\\x", "", None])
def test_safe_relative_path_rejects(bad):
    with pytest.raises(ModpackError):
        safe_relative_path(bad)


def test_safe_relative_path_normalises():
    assert safe_relative_path("mods\\sub\\a.jar") == "mods/sub/a.jar"


def test_safe_join_stays_inside(tmp_path):
    inside = safe_join(str(tmp_path), "mods/a.jar")
    assert inside.startswith(str(tmp_path.resolve()))
    with pytest.raises(ModpackError):
        safe_join(str(tmp_path), "../a.jar")


def test_resolve_mrpack_parses_and_filters(mrpack, tmp_path, fake_net):
    inst = ModpackInstaller(str(tmp_path / "srv"))
    pack = inst.resolve_archive(mrpack)
    assert pack.format == "mrpack"
    assert (pack.mc, pack.loader, pack.loader_build) == ("1.21.1", "fabric", "0.16.9")
    assert [f.path for f in pack.files] == ["mods/a.jar", "mods/b.jar"]
    reasons = {s["reason"] for s in pack.skipped}
    assert reasons == {"client_only", "bad_path", "bad_host"}
    assert pack.summary()["file_count"] == 2
    # round-trips through dict for handing to a background thread
    again = PackInfo.from_dict(pack.to_dict())
    assert again.files[0].path == "mods/a.jar"


def test_resolve_rejects_unknown_archive(tmp_path):
    path = tmp_path / "x.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(ModpackError) as exc:
        ModpackInstaller(str(tmp_path)).resolve_archive(str(path))
    assert exc.value.code == "unknown_format"


def test_resolve_rejects_quilt_free_missing_deps(tmp_path):
    path = tmp_path / "x.mrpack"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps({"dependencies": {}}))
    with pytest.raises(ModpackError) as exc:
        ModpackInstaller(str(tmp_path)).resolve_archive(str(path))
    assert exc.value.code == "missing_dependencies"


def test_install_merge_downloads_and_extracts_overrides(mrpack, tmp_path, fake_net):
    srv = tmp_path / "srv"
    srv.mkdir()
    progress = []
    inst = ModpackInstaller(str(srv), progress_cb=progress.append)
    pack = inst.resolve_archive(mrpack)
    summary = inst.install(pack, mode="merge")

    assert (srv / "mods" / "a.jar").read_bytes() == JAR_A
    assert (srv / "mods" / "b.jar").read_bytes() == JAR_B
    assert (srv / "config" / "a.toml").read_text() == "a=1"
    assert (srv / "server.properties").read_text() == "motd=hi"
    assert not (srv / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert not (srv / "link").exists()
    assert summary["errors"] == []
    assert sorted(summary["installed"])[:2] == ["config/a.toml", "mods/a.jar"]

    status = ModpackInstaller.read_status(str(srv))
    assert status["status"] == "done"
    assert status["done"] == status["total"] == 4
    assert progress[-1]["status"] == "done"


def test_install_skips_identical_and_reports_hash_mismatch(mrpack, tmp_path, fake_net):
    srv = tmp_path / "srv"
    (srv / "mods").mkdir(parents=True)
    (srv / "mods" / "a.jar").write_bytes(JAR_A)  # already present, same hash
    fake_net["store"][URL_B] = b"corrupted"  # server sends wrong bytes

    inst = ModpackInstaller(str(srv))
    summary = inst.install(inst.resolve_archive(mrpack), mode="merge")

    assert summary["skipped"] == ["mods/a.jar"]
    assert URL_A not in fake_net["calls"]["download"]
    assert summary["errors"][0]["name"] == "mods/b.jar"
    assert "hash_mismatch" in summary["errors"][0]["reason"]
    assert not (srv / "mods" / "b.jar").exists()


def test_install_replace_backs_up_existing_mods(mrpack, tmp_path, fake_net):
    srv = tmp_path / "srv"
    (srv / "mods").mkdir(parents=True)
    (srv / "mods" / "old.jar").write_bytes(b"old")

    inst = ModpackInstaller(str(srv))
    summary = inst.install(inst.resolve_archive(mrpack), mode="replace")

    assert summary["backup"].startswith("mods/_backup/")
    assert not (srv / "mods" / "old.jar").exists()
    backups = list((srv / "mods" / "_backup").iterdir())
    assert len(backups) == 1 and (backups[0] / "old.jar").read_bytes() == b"old"
    assert (srv / "mods" / "a.jar").exists()


def test_curseforge_manifest_requires_key(cf_zip, tmp_path, fake_net):
    with pytest.raises(ModpackError) as exc:
        ModpackInstaller(str(tmp_path)).resolve_archive(cf_zip)
    assert exc.value.code == "curseforge_key_required"


def test_curseforge_manifest_resolves_files_and_blocked(cf_zip, tmp_path, fake_net):
    fake_net["responses"]["/mods/files"] = {
        "data": [
            {
                "id": 11,
                "modId": 1,
                "fileName": "one.jar",
                "downloadUrl": "https://edge.forgecdn.net/files/1/11/one.jar",
                "fileLength": 10,
                "hashes": [{"algo": 1, "value": "ab"}],
            },
            {
                "id": 22,
                "modId": 2,
                "fileName": "two.jar",
                "downloadUrl": None,  # allowModDistribution=false
                "fileLength": 20,
                "hashes": [],
            },
        ]
    }
    fake_net["responses"]["/mods"] = {
        "data": [{"id": 2, "links": {"websiteUrl": "https://www.curseforge.com/x/two"}}]
    }
    inst = ModpackInstaller(str(tmp_path), cf_key="k")
    pack = inst.resolve_archive(cf_zip)

    assert pack.format == "curseforge"
    assert (pack.mc, pack.loader, pack.loader_build) == ("1.20.1", "forge", "47.3.0")
    assert [f.path for f in pack.files] == ["mods/one.jar"]
    assert pack.files[0].sha1 == "ab"
    assert pack.blocked == [
        {
            "name": "two.jar",
            "project_id": 2,
            "file_id": 22,
            "url": "https://www.curseforge.com/x/two",
        }
    ]
    # the key must be sent to CurseForge
    posted = [c for c in fake_net["calls"]["json"] if c[0] == "POST"]
    assert posted and posted[0][2] == {"fileIds": [11, 22]}


def test_resolve_modrinth_downloads_mrpack(mrpack, tmp_path, fake_net):
    archive_bytes = open(mrpack, "rb").read()
    url = "https://cdn.modrinth.com/data/p1/versions/v1/test.mrpack"
    fake_net["store"][url] = archive_bytes
    fake_net["responses"]["/version/v1"] = {
        "id": "v1",
        "project_id": "p1",
        "version_number": "1.2.3",
        "files": [
            {
                "url": url,
                "filename": "test.mrpack",
                "primary": True,
                "hashes": {"sha1": _sha1(archive_bytes)},
            }
        ],
    }
    fake_net["responses"]["/project/p1"] = {"title": "Test Pack", "slug": "test-pack"}

    inst = ModpackInstaller(None, work_dir=str(tmp_path / "work"))
    pack = inst.resolve("modrinth", "p1", "v1")
    assert pack.source == "modrinth"
    assert pack.name == "Test Pack"
    assert pack.url == "https://modrinth.com/modpack/test-pack"
    assert pack.archive_path.endswith("test.mrpack")
    inst.cleanup(pack)
    assert not (tmp_path / "work" / "test.mrpack").exists()


def test_resolve_rejects_archive_from_unknown_host(tmp_path, fake_net):
    fake_net["responses"]["/version/v1"] = {
        "id": "v1",
        "project_id": "p1",
        "files": [{"url": URL_EVIL, "filename": "x.mrpack", "primary": True}],
    }
    with pytest.raises(ModpackError) as exc:
        ModpackInstaller(None, work_dir=str(tmp_path)).resolve("modrinth", "p1", "v1")
    assert exc.value.code == "bad_host"
