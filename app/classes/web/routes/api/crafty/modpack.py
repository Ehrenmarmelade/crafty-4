import json
import logging
from pathlib import Path

from tornado.ioloop import IOLoop

from app.classes.web.base_api_handler import BaseApiHandler
from app.classes.minecraft.content_manager import ContentManager, get_cf_key
from app.classes.minecraft.modpack_installer import (
    ModpackError,
    ModpackInstaller,
    remember_pack,
)

logger = logging.getLogger(__name__)


class ApiCraftyModpackHandler(BaseApiHandler):
    """Modpack browsing for the server creation wizard (no server exists yet).

    POST /api/v2/crafty/modpack   body: {"action": ...}
      action=modpack_search   query [source] [mc] [loader] [limit]
      action=modpack_versions source id [all]
      action=modpack_resolve  source id [version_id]   -> pack summary + pack_token
      action=modpack_resolve  source=upload archive_name -> same, for uploads
    The pack_token goes into modpack_create_data of POST /api/v2/servers so the
    already downloaded archive is reused. Needs the SERVER_CREATION permission.
    """

    async def post(self):
        auth_data = self.authenticate_user()
        if not auth_data:
            return
        if (
            not self.controller.crafty_perms.can_create_server(auth_data[4]["user_id"])
            and not auth_data[4]["superuser"]
        ):
            return self.finish_json(400, {"status": "error", "error": "NOT_AUTHORIZED"})

        try:
            data = json.loads(self.request.body or b"{}")
        except json.JSONDecodeError:
            return self.finish_json(400, {"status": "error", "error": "INVALID_JSON"})

        action = data.get("action", "")
        cf_key = get_cf_key()
        loop = IOLoop.current()

        async def run(fn, *args):
            return await loop.run_in_executor(None, fn, *args)

        try:
            if action == "modpack_search":
                # No server context: search unfiltered unless the wizard passes
                # mc/loader explicitly.
                cm = ContentManager("", cf_key=cf_key)
                res = await run(
                    cm.modpack_search,
                    data.get("query", ""),
                    int(data.get("limit", 12)),
                    data.get("mc") or None,
                    data.get("loader") or None,
                    data.get("source"),
                )
                return self.finish_json(200, {"status": "ok", "data": res})

            if action == "modpack_versions":
                cm = ContentManager("", cf_key=cf_key)
                vs = await run(
                    cm.versions,
                    data.get("source", "modrinth"),
                    data.get("id") or data.get("slug"),
                    True,
                    "modpack",
                )
                return self.finish_json(200, {"status": "ok", "data": vs})

            if action == "modpack_resolve":
                installer = ModpackInstaller(None, cf_key=cf_key)
                source = data.get("source", "modrinth")
                if source == "upload":
                    upload_dir = Path(self.controller.project_root, "import", "upload")
                    try:
                        archive = self.helper.validate_traversal(
                            upload_dir, data.get("archive_name", "")
                        )
                    except ValueError:
                        return self.finish_json(
                            400, {"status": "error", "error": "TRAVERSAL_DETECTED"}
                        )
                    if not archive.is_file():
                        return self.finish_json(
                            404, {"status": "error", "error": "ARCHIVE_NOT_FOUND"}
                        )
                    pack = await run(installer.resolve_archive, str(archive))
                    pack.source = "upload"
                else:
                    ident = data.get("id") or data.get("slug")
                    if not ident:
                        return self.finish_json(
                            400, {"status": "error", "error": "MISSING_ID"}
                        )
                    pack = await run(
                        installer.resolve, source, ident, data.get("version_id")
                    )
                summary = pack.summary()
                summary["pack_token"] = remember_pack(pack)
                summary["cf_enabled"] = bool(cf_key)
                return self.finish_json(200, {"status": "ok", "data": summary})

            if action == "modpack_list_uploads":
                # Big archives can be dropped straight into import/upload
                # instead of going through the browser.
                upload_dir = Path(self.controller.project_root, "import", "upload")
                files = []
                if upload_dir.is_dir():
                    for f in sorted(upload_dir.iterdir()):
                        if f.is_file() and f.suffix.lower() in (".mrpack", ".zip"):
                            files.append({"name": f.name, "size": f.stat().st_size})
                return self.finish_json(200, {"status": "ok", "data": files})

            if action == "cf_slug":
                cm = ContentManager("", cf_key=cf_key)
                cf_id = await run(cm.cf_slug_to_id, data.get("slug", ""))
                return self.finish_json(
                    200,
                    {"status": "ok", "data": {"id": cf_id, "cf_enabled": bool(cf_key)}},
                )

            return self.finish_json(400, {"status": "error", "error": "UNKNOWN_ACTION"})
        except ModpackError as e:
            return self.finish_json(
                400, {"status": "error", "error": e.code, "detail": e.detail}
            )
        except Exception as e:  # pylint: disable=broad-except
            logger.exception("Crafty modpack action '%s' failed", action)
            return self.finish_json(500, {"status": "error", "error": str(e)})
