import mimetypes
import os
import logging
import shutil
import asyncio
import pathlib
from pathlib import Path
import anyio
from PIL import Image
from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.web.base_api_handler import BaseApiHandler
from app.classes.web.websocket_handler import WebSocketManager

logger = logging.getLogger(__name__)
IMAGE_MIME_TYPES = [
    "image/bmp",
    "image/cis-cod",
    "image/gif",
    "image/ief",
    "image/jpeg",
    "image/pipeg",
    "image/svg+xml",
    "image/tiff",
    "image/x-cmu-raster",
    "image/x-cmx",
    "image/x-icon",
    "image/x-portable-anymap",
    "image/x-portable-bitmap",
    "image/x-portable-graymap",
    "image/x-portable-pixmap",
    "image/x-rgb",
    "image/x-xbitmap",
    "image/x-xpixmap",
    "image/x-xwindowdump",
    "image/png",
    "image/webp",
]

MRPACK_MIME_TYPE = "application/x-modrinth-modpack+zip"
# .mrpack is a zip; Python's mimetypes does not know the extension.
mimetypes.add_type(MRPACK_MIME_TYPE, ".mrpack")
ARCHIVE_MIME_TYPES = [
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
    MRPACK_MIME_TYPE,
]


class ApiFilesUploadHandler(BaseApiHandler):

    upload_locks = {}

    def get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a lock for the given key."""
        if key not in self.upload_locks:
            self.upload_locks[key] = asyncio.Lock()
        return self.upload_locks[key]

    async def post(self, server_id=None):
        auth_data = self.authenticate_user()
        if not auth_data:
            return

        upload_type = self.request.headers.get("type")
        accepted_types = []

        if server_id:
            # Check to make sure user is authorized for the server
            if server_id not in [str(x["server_id"]) for x in auth_data[0]]:
                # if the user doesn't have access to the server, return an error
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
            mask = self.controller.server_perms.get_lowest_api_perm_mask(
                self.controller.server_perms.get_user_permissions_mask(
                    auth_data[4]["user_id"], server_id
                ),
                auth_data[5],
            )
            # Make sure user has file access for the server
            server_permissions = self.controller.server_perms.get_permissions(mask)
            if EnumPermissionsServer.FILES not in server_permissions:
                # if the user doesn't have Files permission, return an error
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

            u_type = "server_upload"
        # Make sure user is a super user if they're changing panel settings
        elif auth_data[4]["superuser"] and upload_type == "background":
            u_type = "admin_config"
            self.upload_dir = os.path.join(
                self.controller.project_root,
                "app/frontend/static/assets/images/auth/custom",
            )
            accepted_types = IMAGE_MIME_TYPES
        elif upload_type == "import":
            # Check that user can make servers
            if (
                not self.controller.crafty_perms.can_create_server(
                    auth_data[4]["user_id"]
                )
                and not auth_data[4]["superuser"]
            ):
                return self.finish_json(
                    400,
                    {
                        "status": "error",
                        "error": "NOT_AUTHORIZED",
                        "data": {"message": ""},
                    },
                )
            # Set directory to upload import dir
            self.upload_dir = Path(self.controller.project_root, "import", "upload")
            u_type = "server_import"
            accepted_types = ARCHIVE_MIME_TYPES
        else:
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "NOT_AUTHORIZED",
                    "data": {"message": ""},
                },
            )
        # Get the headers from the request
        self.chunk_hash = self.request.headers.get("chunkHash", 0)
        self.file_id = self.request.headers.get("fileId")
        self.chunked = self.request.headers.get("chunked", False)
        self.filename = self.request.headers.get("fileName", None)
        try:
            file_size = int(self.request.headers.get("fileSize", None))
            total_chunks = int(self.request.headers.get("totalChunks", 0))
        except TypeError as why:
            return self.finish_json(
                400, {"status": "error", "error": "TYPE ERROR", "error_data": {why}}
            )
        self.chunk_index = self.request.headers.get("chunkId")
        self.temp_dir = os.path.join(self.controller.project_root, "temp", self.file_id)

        if u_type == "server_upload":
            # Check for absolute or relative path. Absolute paths should be deprecated
            self.upload_dir = self.request.headers.get("location", None)
            # Check for absolute or relative path. Absolute paths should be deprecated
            server_path = self.controller.servers.get_server_data_by_id(server_id)[
                "path"
            ]
            self.upload_dir = pathlib.Path(
                self.file_helper.get_absolute_path(server_path, self.upload_dir)
            ).resolve()
            # Check to make sure the requested path is inside the server's directory
            try:
                self.helper.validate_traversal(
                    server_path, pathlib.Path(self.upload_dir, self.filename).resolve()
                )
            except ValueError:
                return self.finish_json(
                    500,
                    {
                        "status": "error",
                        "error": "TRAVERSAL_DETECTED",
                        "error_data": (
                            "Attempted traversal detected. "
                            "Requested upload must go to server directory"
                        ),
                    },
                )
        # Check to make sure the file type we're being sent is what we're expecting
        if (
            self.file_helper.check_mime_types(self.filename) not in accepted_types
            and u_type != "server_upload"
        ):
            return self.finish_json(
                422,
                {
                    "status": "error",
                    "error": "INVALID FILE TYPE",
                    "data": {
                        "message": f"Invalid File Type only accepts {accepted_types}"
                    },
                },
            )
        _total, _used, free = shutil.disk_usage(self.upload_dir)

        # Check to see if we have enough space
        if free <= file_size:
            return self.finish_json(
                507,
                {
                    "status": "error",
                    "error": "NO STORAGE SPACE",
                    "data": {"message": "Out Of Space!"},
                },
            )

        # If this has no chunk index we know it's the inital request
        if self.chunked and not self.chunk_index:
            return self.finish_json(
                200, {"status": "ok", "data": {"file-id": self.file_id}}
            )
        # Create the upload and temp directories if they don't exist
        os.makedirs(self.upload_dir, exist_ok=True)

        # Check for chunked header. We will handle this request differently
        # if it doesn't exist
        if not self.chunked:
            # Write the file directly to the upload dir
            async with await anyio.open_file(
                os.path.join(self.upload_dir, self.filename), "wb"
            ) as file:
                chunk = self.request.body
                if chunk:
                    await file.write(chunk)
            # We'll check the file hash against the sent hash once the file is
            # written. We cannot check this buffer.
            calculated_hash = self.helper.crypto_helper.calculate_file_hash_sha256(
                os.path.join(self.upload_dir, self.filename)
            )
            logger.info(
                f"File upload completed. Filename: {self.filename} Type: {u_type}"
            )
            return self.finish_json(
                200,
                {
                    "status": "completed",
                    "data": {"message": "File uploaded successfully"},
                },
            )
        # Since this is a chunked upload we'll create the temp dir for parts.
        os.makedirs(self.temp_dir, exist_ok=True)

        # Read headers and query parameters
        content_length = int(self.request.headers.get("Content-Length"))
        if content_length <= 0:
            logger.error(
                f"File upload failed. Filename: {self.filename}"
                f"Type: {u_type} Error: INVALID CONTENT LENGTH"
            )
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID CONTENT LENGTH",
                    "data": {"message": "Invalid content length"},
                },
            )

        # At this point filename, chunk index and total chunks are required
        # in the request
        if not self.filename or self.chunk_index is None:
            logger.error(
                f"File upload failed. Filename: {self.filename}"
                f"Type: {u_type} Error: CHUNK INDEX NOT FOUND"
            )
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INDEX ERROR",
                    "data": {
                        "message": "Filename, chunk_index,"
                        " and total_chunks are required"
                    },
                },
            )

        # Calculate the hash of the buffer and compare it against the expected hash
        calculated_hash = self.helper.crypto_helper.calculate_buffer_hash(
            self.request.body
        )
        if str(self.chunk_hash) != str(calculated_hash):
            logger.error(
                f"File upload failed. Filename: {self.filename}"
                f"Type: {u_type} Error: INVALID HASH"
            )
            return self.finish_json(
                400,
                {
                    "status": "error",
                    "error": "INVALID_HASH",
                    "data": {
                        "message": "Hash recieved does not match reported sent hash.",
                        "chunk_id": self.chunk_index,
                    },
                },
            )

        # File paths
        file_path = os.path.join(self.upload_dir, self.filename)
        chunk_path = os.path.join(
            self.temp_dir, f"{self.filename}.part{self.chunk_index}"
        )

        lock = self.get_lock(self.file_id)  # Capture async lock to avoid race condition

        async with lock:
            # Save the chunk
            async with await anyio.open_file(chunk_path, "wb") as f:
                await f.write(self.request.body)

            # Check if all chunks are received
            received_chunks = [
                f
                for f in os.listdir(self.temp_dir)
                if f.startswith(f"{self.filename}.part")
            ]
            # When we've reached the total chunks we'll
            # Compare the hash and write the file
            if len(received_chunks) == total_chunks:
                async with await anyio.open_file(file_path, "wb") as outfile:
                    for i in range(total_chunks):
                        WebSocketManager().broadcast_user(
                            auth_data[4]["user_id"],
                            "upload_process",
                            {
                                "cur_file": i,
                                "total_files": total_chunks,
                                "type": u_type,
                                "file_id": self.file_id,
                            },
                        )
                        chunk_file = os.path.join(
                            self.temp_dir, f"{self.filename}.part{i}"
                        )
                        async with await anyio.open_file(chunk_file, "rb") as infile:
                            await outfile.write(await infile.read())
                        try:
                            await anyio.Path(chunk_file).unlink(missing_ok=True)
                        except OSError as why:
                            logger.exception(
                                "Failed to remove chunk file with error: %s", why
                            )
                try:
                    self.file_helper.del_dirs(self.temp_dir)
                except OSError as why:
                    logger.exception(
                        "Failed to import remove temp dir with error: %s", why
                    )
                if upload_type == "background":
                    # Strip EXIF data
                    image_path = os.path.join(file_path)
                    logger.debug("Stripping exif data from image")
                    image = Image.open(image_path)

                    # Get current raw pixel data from image
                    image_data = list(image.getdata())
                    # Create new image
                    image_no_exif = Image.new(image.mode, image.size)
                    # Restore pixel data
                    image_no_exif.putdata(image_data)

                    image_no_exif.save(image_path)

                logger.info(
                    f"File upload completed. Filename: {self.filename}"
                    f" Path: {file_path} Type: {u_type}"
                )
                self.controller.management.add_to_audit_log(
                    auth_data[4]["user_id"],
                    f"Uploaded file {self.filename}",
                    server_id,
                    self.get_remote_ip(),
                )
                self.finish_json(
                    200,
                    {
                        "status": "completed",
                        "data": {"message": "File uploaded successfully"},
                    },
                )
            else:
                self.finish_json(
                    200,
                    {
                        "status": "partial",
                        "data": {"message": f"Chunk {self.chunk_index} received"},
                    },
                )
