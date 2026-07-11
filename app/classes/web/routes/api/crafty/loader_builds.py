import logging

from app.classes.web.base_api_handler import BaseApiHandler

logger = logging.getLogger(__name__)


class ApiCraftyLoaderBuildsHandler(BaseApiHandler):
    """Crafty-level loader build listing (used by the creation wizard, where no
    server exists yet). Returns concrete builds for a loader + MC version from
    the official sources (NeoForge maven / Fabric meta)."""

    def get(self):
        auth_data = self.authenticate_user()
        if not auth_data:
            return

        loader = self.get_query_argument("loader", "")
        mc_version = self.get_query_argument("mc", "")

        self.finish_json(
            200,
            {
                "status": "ok",
                "data": {
                    "builds": self.controller.get_loader_builds(loader, mc_version)
                },
            },
        )
