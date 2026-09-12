import os
import json
import logging
import threading

from tornado.ioloop import IOLoop

from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler
from app.classes.minecraft.content_manager import ContentManager
from app.classes.minecraft.modpack_installer import (
    ModpackError,
    ModpackInstaller,
    PackInfo,
)
from app.classes.shared.websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

# One modpack install job per server at a time; last resolved pack per server
# so install does not have to download the archive twice.
_MODPACK_JOBS = {}
_MODPACK_RESOLVED = {}
_MODPACK_LOCK = threading.Lock()


def _modpack_job_running(server_id):
    t = _MODPACK_JOBS.get(server_id)
    return bool(t and t.is_alive())


def _t_modpack_install(server_id, server_path, cf_key, pack_dict, mode):
    """Thread target: (re)resolve if needed, install, clean up, notify."""
    installer = ModpackInstaller(server_path, cf_key=cf_key)
    try:
        pack = PackInfo.from_dict(pack_dict)
        if not pack.archive_path or not os.path.exists(pack.archive_path):
            pack = installer.resolve(pack.source, pack.project_id, pack.version_id)
        result = installer.install(pack, mode=mode)
        installer.cleanup(pack)
        logger.info(
            "Modpack '%s' installed on server %s (%s): %d files, %d errors",
            pack.name,
            server_id,
            mode,
            len(result["installed"]),
            len(result["errors"]),
        )
    except (ModpackError, OSError) as exc:
        logger.error("Modpack install failed on server %s: %s", server_id, exc)
        ModpackInstaller.write_status(
            server_path,
            {"status": "error", "error": str(exc), "current": "", "errors": []},
        )
    except Exception:  # pylint: disable=broad-except
        logger.exception("Modpack install crashed on server %s", server_id)
        ModpackInstaller.write_status(
            server_path,
            {"status": "error", "error": "internal_error", "current": "", "errors": []},
        )
    finally:
        _MODPACK_RESOLVED.pop(server_id, None)
        WebSocketManager().broadcast_to_server_users(server_id, "send_start_reload", {})


def _get_cf_key():
    """CurseForge API key: variable de entorno o app/config/content.json."""
    key = os.environ.get("CURSEFORGE_API_KEY", "")
    if key:
        return key
    cfg_path = os.path.join("app", "config", "content.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                return json.load(f).get("curseforge_api_key", "")
        except (OSError, json.JSONDecodeError):
            return ""
    return ""


class ApiServersServerContentHandler(BaseApiHandler):
    """
    Gestión de contenido (mods/packs) de un servidor.

    POST /api/v2/servers/<id>/content   body: {"action": ...}
      action=scan                                -> inventario unificado
      action=plan   [channel]                    -> updates disponibles (no aplica)
      action=apply  [channel] confirm=true       -> descarga+verifica+backup+sustituye
      action=search query [type] [limit]         -> buscar en Modrinth
    Parametros opcionales de contexto: mc (1.21.1), loader (neoforge).
    """

    def _authorize(self, server_id):
        auth_data = self.authenticate_user()
        if not auth_data:
            return None
        if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
            self.finish_json(400, {"status": "error", "error": "NOT_AUTHORIZED"})
            return None
        mask = self.controller.server_perms.get_lowest_api_perm_mask(
            self.controller.server_perms.get_user_permissions_mask(
                auth_data[4]["user_id"], server_id
            ),
            auth_data[5],
        )
        perms = self.controller.server_perms.get_permissions(mask)
        if EnumPermissionsServer.FILES not in perms:
            self.finish_json(400, {"status": "error", "error": "NOT_AUTHORIZED"})
            return None
        return auth_data

    async def post(self, server_id: str):
        auth_data = self._authorize(server_id)
        if not auth_data:
            return

        try:
            data = json.loads(self.request.body or b"{}")
        except json.JSONDecodeError:
            return self.finish_json(400, {"status": "error", "error": "INVALID_JSON"})

        server = self.controller.servers.get_server_data_by_id(server_id)
        if not server:
            return self.finish_json(
                404, {"status": "error", "error": "SERVER_NOT_FOUND"}
            )

        cm = ContentManager(
            server_path=server["path"],
            mc=data.get("mc"),  # None -> autodetecta del servidor
            loader=data.get("loader"),  # None -> autodetecta del servidor
            cf_key=_get_cf_key(),
        )

        action = data.get("action", "scan")
        loop = IOLoop.current()

        # Ejecuta el trabajo bloqueante (red/disco) en un hilo para NO congelar
        # el IOLoop de Tornado (y con él, todo el panel) durante escaneos largos.
        async def run(fn, *args):
            return await loop.run_in_executor(None, fn, *args)

        try:
            if action == "cached":
                return self.finish_json(
                    200, {"status": "ok", "data": await run(cm.load_cache)}
                )

            if action == "context":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": {
                            "mc": cm.mc,
                            "loader": cm.loader,
                            "mc_detected": cm.mc_detected,
                            "loader_detected": cm.loader_detected,
                            "cf_enabled": bool(cm.cf_key),
                        },
                    },
                )

            if action == "save_cache":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": await run(cm.save_cache, data.get("inventory", [])),
                    },
                )

            if action == "scan_one":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": await run(
                            cm.scan_one,
                            data.get("filename", ""),
                            data.get("content_type", "mod"),
                        ),
                    },
                )

            if action == "scan":
                return self.finish_json(
                    200, {"status": "ok", "data": await run(cm.scan)}
                )

            if action == "plan":
                inv = data.get("inventory") or await run(cm.scan)
                plan = await run(cm.plan, inv, data.get("channel", "release"))
                return self.finish_json(200, {"status": "ok", "data": plan})

            if action == "apply":
                if not data.get("confirm"):
                    return self.finish_json(
                        400, {"status": "error", "error": "CONFIRM_REQUIRED"}
                    )
                inv = data.get("inventory") or await run(cm.scan)
                plan = await run(cm.plan, inv, data.get("channel", "release"))
                result = await run(cm.apply, plan)
                logger.info(
                    "Content update applied on server %s: %s", server_id, result
                )
                return self.finish_json(200, {"status": "ok", "data": result})

            if action == "search":
                hits = await run(
                    cm.search,
                    data.get("query", ""),
                    data.get("type", "mod"),
                    int(data.get("limit", 10)),
                )
                return self.finish_json(200, {"status": "ok", "data": hits})

            if action == "install":
                res = await run(
                    cm.install,
                    data.get("id") or data.get("slug"),
                    data.get("type", "mod"),
                    data.get("channel", "release"),
                )
                logger.info("Content install on server %s: %s", server_id, res)
                return self.finish_json(200, {"status": "ok", "data": res})

            if action == "detail":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": await run(
                            cm.detail,
                            data.get("source"),
                            data.get("id") or data.get("slug"),
                        ),
                    },
                )

            if action == "versions":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": await run(
                            cm.versions,
                            data.get("source"),
                            data.get("id") or data.get("slug"),
                            bool(data.get("all")),
                            data.get("type", "mod"),
                        ),
                    },
                )

            if action == "install_version":
                res = await run(
                    cm.install_version,
                    data.get("source"),
                    data.get("id") or data.get("slug"),
                    data.get("version_id"),
                    data.get("current_filename"),
                    data.get("type", "mod"),
                )
                logger.info("Content install_version on server %s: %s", server_id, res)
                return self.finish_json(200, {"status": "ok", "data": res})

            if action == "toggle":
                return self.finish_json(
                    200,
                    {
                        "status": "ok",
                        "data": await run(
                            cm.toggle,
                            data.get("filename", ""),
                            data.get("content_type", "mod"),
                        ),
                    },
                )

            if action == "remove":
                res = await run(
                    cm.remove, data.get("filename", ""), data.get("content_type", "mod")
                )
                logger.info("Content remove on server %s: %s", server_id, res)
                return self.finish_json(200, {"status": "ok", "data": res})

            if action == "update_one":
                res = await run(
                    cm.update_one,
                    data.get("filename", ""),
                    data.get("channel", "release"),
                    data.get("content_type", "mod"),
                )
                logger.info("Content update_one on server %s: %s", server_id, res)
                return self.finish_json(200, {"status": "ok", "data": res})

            if action.startswith("modpack_"):
                return await self._modpack_action(
                    action, data, cm, server_id, server["path"], run
                )

            return self.finish_json(400, {"status": "error", "error": "UNKNOWN_ACTION"})
        except ModpackError as e:
            return self.finish_json(
                400, {"status": "error", "error": e.code, "detail": e.detail}
            )
        except Exception as e:  # pragma: no cover - defensivo
            logger.error("Content action '%s' failed: %s", action, e)
            return self.finish_json(500, {"status": "error", "error": str(e)})

    # ---------------------------------------------------------- modpacks
    async def _modpack_action(self, action, data, cm, server_id, server_path, run):

        if action == "modpack_search":
            res = await run(
                cm.modpack_search,
                data.get("query", ""),
                int(data.get("limit", 12)),
                data.get("mc"),
                data.get("loader"),
                data.get("source"),
            )
            return self.finish_json(200, {"status": "ok", "data": res})

        if action == "modpack_versions":
            vs = await run(
                cm.versions,
                data.get("source", "modrinth"),
                data.get("id") or data.get("slug"),
                bool(data.get("all", True)),
                "modpack",
            )
            return self.finish_json(200, {"status": "ok", "data": vs})

        if action == "modpack_status":
            status = ModpackInstaller.read_status(server_path)
            status["running"] = _modpack_job_running(server_id)
            return self.finish_json(200, {"status": "ok", "data": status})

        source = data.get("source", "modrinth")
        ident = data.get("id") or data.get("slug")
        version_id = data.get("version_id")
        if not ident:
            return self.finish_json(400, {"status": "error", "error": "MISSING_ID"})

        if action == "modpack_resolve":
            installer = ModpackInstaller(server_path, cf_key=cm.cf_key)
            pack = await run(installer.resolve, source, ident, version_id)
            with _MODPACK_LOCK:
                _MODPACK_RESOLVED[server_id] = pack.to_dict()
            summary = pack.summary()
            summary["mismatch"] = {
                "mc": bool(cm.mc_detected and pack.mc != cm.mc),
                "loader": bool(cm.loader_detected and pack.loader != cm.loader),
                "server_mc": cm.mc if cm.mc_detected else None,
                "server_loader": cm.loader if cm.loader_detected else None,
            }
            return self.finish_json(200, {"status": "ok", "data": summary})

        if action == "modpack_install":
            if not data.get("confirm"):
                return self.finish_json(
                    400, {"status": "error", "error": "CONFIRM_REQUIRED"}
                )
            mode = data.get("mode", "merge")
            if mode not in ("merge", "replace"):
                return self.finish_json(400, {"status": "error", "error": "BAD_MODE"})
            with _MODPACK_LOCK:
                if _modpack_job_running(server_id):
                    return self.finish_json(
                        409, {"status": "error", "error": "JOB_RUNNING"}
                    )
                cached = _MODPACK_RESOLVED.get(server_id)
                if not (
                    cached
                    and cached.get("source") == source
                    and str(cached.get("project_id")) == str(ident)
                    and (
                        not version_id
                        or str(cached.get("version_id")) == str(version_id)
                    )
                ):
                    cached = {
                        "source": source,
                        "project_id": ident,
                        "version_id": version_id,
                        "name": "",
                        "version": "",
                        "mc": "",
                        "loader": "",
                    }
                ModpackInstaller.write_status(
                    server_path,
                    {"status": "queued", "mode": mode, "current": "", "errors": []},
                )
                thread = threading.Thread(
                    target=_t_modpack_install,
                    name=f"modpack-install-{server_id}",
                    daemon=True,
                    args=(server_id, server_path, cm.cf_key, cached, mode),
                )
                _MODPACK_JOBS[server_id] = thread
                thread.start()
            logger.info(
                "Modpack install queued on server %s: %s/%s@%s mode=%s",
                server_id,
                source,
                ident,
                version_id,
                mode,
            )
            return self.finish_json(200, {"status": "ok", "data": {"job": "started"}})

        return self.finish_json(400, {"status": "error", "error": "UNKNOWN_ACTION"})
