import pytest
from jsonschema import validate, ValidationError

from app.classes.web.routes.api.servers.index import new_server_schema


def _body(java_create):
    return {
        "name": "pack",
        "roles": [],
        "stop_command": "stop",
        "log_location": "./logs/latest.log",
        "crashdetection": False,
        "autostart": False,
        "autostart_delay": 10,
        "monitoring_type": "minecraft_java",
        "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": 25565},
        "create_type": "minecraft_java",
        "minecraft_java_create_data": java_create,
    }


def test_modpack_create_type_accepted():
    validate(
        _body(
            {
                "create_type": "modpack",
                "modpack_create_data": {
                    "source": "modrinth",
                    "project_id": "AuI3VLGI",
                    "version_id": "VjrstANB",
                    "pack_token": "abc",
                    "mem_min": 1,
                    "mem_max": 4,
                    "server_properties_port": 25565,
                    "agree_to_eula": True,
                },
            }
        ),
        new_server_schema,
    )


def test_modpack_upload_source_accepted():
    validate(
        _body(
            {
                "create_type": "modpack",
                "modpack_create_data": {
                    "source": "upload",
                    "archive_name": "pack.mrpack",
                    "mem_min": 1,
                    "mem_max": 4,
                    "server_properties_port": 25565,
                },
            }
        ),
        new_server_schema,
    )


def test_modpack_requires_its_data_block():
    with pytest.raises(ValidationError):
        validate(_body({"create_type": "modpack"}), new_server_schema)


def test_modpack_rejects_unknown_source():
    with pytest.raises(ValidationError):
        validate(
            _body(
                {
                    "create_type": "modpack",
                    "modpack_create_data": {
                        "source": "ftp",
                        "mem_min": 1,
                        "mem_max": 4,
                        "server_properties_port": 25565,
                    },
                }
            ),
            new_server_schema,
        )
