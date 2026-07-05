#!/usr/bin/env python3
"""
register_existing.py - Registra un servidor YA EXISTENTE en Crafty apuntando a su
carpeta real, SIN copiar archivos (no duplica el world). Reutiliza el propio
register_server() de Crafty. Ejecutar con Crafty PARADO.

Uso:  .venv\\Scripts\\python.exe register_existing.py "D:\\Minecraft" "Cobblemon Server"
"""
import os
import sys
import uuid as uuidlib

os.chdir(os.path.dirname(os.path.abspath(__file__)))

import peewee
from app.classes.helpers.helpers import Helpers
from app.classes.helpers.file_helpers import FileHelpers
from app.classes.shared.import_helper import ImportHelpers
from app.classes.models.base_model import database_proxy
from app.classes.shared.main_controller import Controller

SERVER_DIR = sys.argv[1] if len(sys.argv) > 1 else r"D:\Minecraft"
SERVER_NAME = sys.argv[2] if len(sys.argv) > 2 else "Cobblemon Server"

helper = Helpers()
database = peewee.SqliteDatabase(
    helper.db_path,
    pragmas={"journal_mode": "wal", "cache_size": -1024 * 10,
             "busy_timeout": 5000, "synchronous": 1},
)
database_proxy.initialize(database)
file_helper = FileHelpers(helper)
import_helper = ImportHelpers(helper, file_helper)
controller = Controller(database, helper, file_helper, import_helper)

# --- usuario admin (superuser -> verá el servidor) ---
uid = controller.users_helper.get_user_id_by_name("admin") or 1

# --- comando de arranque NeoForge (autodetecta versión y usa el JRE portable) ---
neo_base = os.path.join(SERVER_DIR, "libraries", "net", "neoforged", "neoforge")
ver = sorted(os.listdir(neo_base))[-1]
java = os.path.join(SERVER_DIR, "jre", "bin", "java.exe")
java = java if os.path.exists(java) else "java"
cmd = (f'"{java}" @user_jvm_args.txt '
       f'@libraries/net/neoforged/neoforge/{ver}/win_args.txt nogui')

print(f"Servidor : {SERVER_DIR}")
print(f"NeoForge : {ver}")
print(f"Comando  : {cmd}")
print(f"admin uid: {uid}")

# --- ¿ya está registrado? (evita duplicados) ---
from app.classes.models.servers import Servers
existing = [s.server_name for s in Servers.select().where(Servers.path == SERVER_DIR)]
if existing:
    print(f"YA registrado como: {existing}. No hago nada.")
    sys.exit(0)

new_id = controller.register_server(
    name=SERVER_NAME,
    server_uuid=str(uuidlib.uuid4()),
    server_dir=SERVER_DIR,
    server_command=cmd,
    server_file="user_jvm_args.txt",
    server_log_file=os.path.join("logs", "latest.log"),
    server_stop="stop",
    server_port=25565,
    created_by=uid,
    server_type="minecraft-java",
)
print(f"REGISTRADO. server_id = {new_id}")

# verificación directa en BD
row = Servers.select().where(Servers.server_id == new_id).first()
print(f"Verificación BD -> name={row.server_name} path={row.path}")
