import logging

import orjson
from jsonschema import ValidationError, validate

from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler

logger = logging.getLogger(__name__)

VALID_LOADERS = [
    "vanilla",
    "fabric",
    "forge-installer",
    "neoforge-installer",
    "purpur",
    "paper",
    "folia",
]

change_loader_schema = {
    "type": "object",
    "properties": {
        "loader_type": {"type": "string"},
        "version": {"type": "string"},
        "build": {"type": "string"},
    },
    "required": ["loader_type", "version"],
    "additionalProperties": False,
}


class ApiServersServerLoaderHandler(BaseApiHandler):
    """Per-server loader (NeoForge / Forge / Fabric / vanilla) version manager.

    GET  -> current loader/version + all available versions from the big bucket.
    POST -> replace the loader with the chosen type + Minecraft version.
    """

    def _not_authorized(self, auth_data):
        return self.finish_json(
            400,
            {
                "status": "error",
                "error": "NOT_AUTHORIZED",
                "error_data": self.helper.translation.translate(
                    "validators", "insufficientPerms", auth_data[4]["lang"]
                ),
            },
        )

    def get(self, server_id: str):
        auth_data = self.authenticate_user()
        if not auth_data:
            return

        if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
            return self._not_authorized(auth_data)

        # Sub-query: concrete builds for a loader + MC version (official sources).
        if self.get_query_argument("builds", None) == "true":
            loader = self.get_query_argument("loader", "")
            mc_version = self.get_query_argument("mc", "")
            return self.finish_json(
                200,
                {
                    "status": "ok",
                    "data": {
                        "builds": self.controller.get_loader_builds(loader, mc_version)
                    },
                },
            )

        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {
                    "current": self.controller.detect_server_loader(server_id),
                    "available": self.controller.get_loader_versions(),
                },
            },
        )

    def post(self, server_id: str):
        auth_data = self.authenticate_user()
        if not auth_data:
            return

        if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
            return self._not_authorized(auth_data)

        # Requires the same permission as issuing server commands.
        mask = self.controller.server_perms.get_lowest_api_perm_mask(
            self.controller.server_perms.get_user_permissions_mask(
                auth_data[4]["user_id"], server_id
            ),
            auth_data[5],
        )
        server_permissions = self.controller.server_perms.get_permissions(mask)
        if EnumPermissionsServer.COMMANDS not in server_permissions:
            return self._not_authorized(auth_data)

        try:
            data = orjson.loads(self.request.body)
        except orjson.JSONDecodeError as why:
            return self.finish_json(
                400,
                {"status": "error", "error": "INVALID_JSON", "error_data": str(why)},
            )

        try:
            validate(data, change_loader_schema)
        except ValidationError as why:
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID_JSON_SCHEMA",
                    "error_data": str(why),
                },
            )

        loader_type = data["loader_type"]
        version = data["version"]

        if loader_type not in VALID_LOADERS:
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID_LOADER",
                    "error_data": f"Unknown loader type: {loader_type}",
                },
            )

        # The server must be stopped before we swap its loader.
        server = self.controller.servers.get_server_instance_by_id(server_id)
        if server.check_running():
            return self.finish_json(
                409,
                {
                    "status": "error",
                    "error": "SERVER_RUNNING",
                    "error_data": "The server must be stopped first.",
                },
            )

        # Make sure the requested version actually exists in the bucket.
        available = self.controller.get_loader_versions().get(loader_type, [])
        if version not in [v["version"] for v in available]:
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID_VERSION",
                    "error_data": (
                        f"Version {version} is not available for {loader_type}"
                    ),
                },
            )

        self.controller.change_server_loader(
            server_id,
            loader_type,
            version,
            auth_data[4]["user_id"],
            self.get_remote_ip(),
            build=data.get("build") or None,
        )

        self.controller.management.add_to_audit_log(
            auth_data[4]["user_id"],
            f"requested loader change to {loader_type} {version}",
            server_id=server_id,
            source_ip=self.get_remote_ip(),
        )

        self.finish_json(200, {"status": "ok"})
