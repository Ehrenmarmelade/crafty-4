import hashlib
import json
import zipfile

from app.classes.minecraft import modpack_installer as mi
from app.classes.minecraft.server_pack_export import export_server_pack


def _sha1(b):
    return hashlib.sha1(b).hexdigest()


def test_export_server_pack_contents(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    (srv / "mods" / "_backup" / "x").mkdir(parents=True)
    (srv / "mods" / "server.jar").write_bytes(b"srv")
    (srv / "mods" / "client.jar").write_bytes(b"cli")
    (srv / "mods" / "old.jar.disabled").write_bytes(b"old")
    (srv / "mods" / "_backup" / "x" / "gone.jar").write_bytes(b"gone")
    (srv / "config").mkdir()
    (srv / "config" / "a.toml").write_text("a=1")
    (srv / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.248").mkdir(
        parents=True
    )
    (srv / "world").mkdir()
    (srv / "world" / "level.dat").write_bytes(b"world")
    (srv / "neoforge-installer-21.1.248.jar").write_bytes(b"installer")
    (srv / "run.sh").write_text("java ...")
    (srv / "server.properties").write_text("server-port=25565")
    (srv / "ops.json").write_text("[]")

    def fake_http_json(url, method="GET", headers=None, body=None):
        if "/version_files" in url:
            return 200, {_sha1(b"cli"): {"project_id": "c"}}
        if "/projects?ids=" in url:
            return 200, [{"id": "c", "title": "Client", "server_side": "unsupported"}]
        return 404, None

    monkeypatch.setattr(mi, "_http_json", fake_http_json)

    rel, summary = export_server_pack(str(srv), "My Pack!")
    assert rel == ".content_cache/exports/My-Pack-server-pack.zip"
    assert summary["removed"] == ["client.jar"]
    assert summary["mods"] == 1
    assert (summary["loader"], summary["loader_build"], summary["mc"]) == (
        "neoforge",
        "21.1.248",
        "1.21.1",
    )
    names = set(zipfile.ZipFile(srv / rel).namelist())
    assert "mods/server.jar" in names
    assert "config/a.toml" in names
    assert "neoforge-installer-21.1.248.jar" in names
    assert "run.sh" in names and "eula.txt" in names and "README-SERVER.txt" in names
    for absent in (
        "mods/client.jar",
        "mods/old.jar.disabled",
        "mods/_backup/x/gone.jar",
        "world/level.dat",
        "server.properties",
        "ops.json",
    ):
        assert absent not in names, absent
    manifest = json.loads(zipfile.ZipFile(srv / rel).read("server-pack.json"))
    assert manifest["client_only_removed"] == ["client.jar"]


def test_exported_pack_round_trips_through_installer(tmp_path, monkeypatch):
    from app.classes.minecraft.modpack_installer import ModpackInstaller

    monkeypatch.setattr(mi, "_http_json", lambda *a, **k: (404, None))
    src = tmp_path / "src"
    (src / "mods").mkdir(parents=True)
    (src / "mods" / "a.jar").write_bytes(b"a")
    (src / "config").mkdir()
    (src / "config" / "x.cfg").write_text("x")
    (src / "libraries" / "net" / "neoforged" / "neoforge" / "21.1.248").mkdir(
        parents=True
    )
    (src / "neoforge-installer-21.1.248.jar").write_bytes(b"inst")
    rel, _ = export_server_pack(str(src), "Round Trip")

    dst = tmp_path / "dst"
    dst.mkdir()
    inst = ModpackInstaller(str(dst))
    pack = inst.resolve_archive(str(src / rel))
    assert pack.format == "serverpack"
    assert (pack.mc, pack.loader, pack.loader_build) == (
        "1.21.1",
        "neoforge",
        "21.1.248",
    )
    summary = inst.install(pack, mode="merge")
    assert (dst / "mods" / "a.jar").read_bytes() == b"a"
    assert (dst / "config" / "x.cfg").exists()
    for absent in (
        "server-pack.json",
        "README-SERVER.txt",
        "eula.txt",
        "neoforge-installer-21.1.248.jar",
    ):
        assert not (dst / absent).exists(), absent
    assert summary["errors"] == []
