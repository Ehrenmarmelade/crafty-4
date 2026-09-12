import os
import json
import logging

from tornado.ioloop import IOLoop

from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler
from app.classes.minecraft.content_manager import ContentManager

logger = logging.getLogger(__name__)


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
                    200, {"status": "ok", "data": {"mc": cm.mc, "loader": cm.loader}}
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

            return self.finish_json(400, {"status": "error", "error": "UNKNOWN_ACTION"})
        except Exception as e:  # pragma: no cover - defensivo
            logger.error("Content action '%s' failed: %s", action, e)
            return self.finish_json(500, {"status": "error", "error": str(e)})
