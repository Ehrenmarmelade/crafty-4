# Módulo "Content" — gestor de mods/packs (fork)

Añade a Crafty la gestión de contenido del servidor: identifica los mods por
hash en **Modrinth** (sha1) y **CurseForge** (murmur2 fingerprint), detecta
actualizaciones para la versión de MC + loader, y las aplica con verificación de
hash + backup. Fuente: **Modrinth primero, CurseForge como fallback**
(su buscador de texto `/v1/mods/search` está capado para keys estándar).

## Qué se ha añadido

| Archivo | Rol |
|---|---|
| `app/classes/minecraft/content_manager.py` | Motor (solo stdlib): `scan / plan / apply / search` |
| `app/classes/web/routes/api/servers/server/content.py` | Handler Tornado `ApiServersServerContentHandler` |
| `app/classes/web/routes/api/api_handlers.py` | Import + ruta `…/content/?` registrados |
| `app/config/content.json.example` | Plantilla para la CurseForge API key |

## Configurar la CurseForge API key

Variable de entorno (tiene prioridad):
```
set CURSEFORGE_API_KEY=tu_key   REM Windows
```
o `cp app/config/content.json.example app/config/content.json` y rellénala.
Solo hace falta para los mods exclusivos de CurseForge; Modrinth no necesita key.

## Endpoint

`POST /api/v2/servers/<server_id>/content` (requiere permiso **FILES** del servidor)

| body `action` | efecto |
|---|---|
| `scan` | inventario unificado por mod (source, side, update, channel…) |
| `plan` + `channel` (`release`/`beta`/`alpha`) | lista de updates (no toca nada) |
| `apply` + `channel` + `confirm:true` | descarga→verifica hash→backup en `mods/_backup/<ts>/`→sustituye |
| `search` + `query` + `type` (`mod`/`resourcepack`/`shader`/`datapack`) | busca en Modrinth |

Contexto opcional en el body: `mc` (def. `1.21.1`), `loader` (def. `neoforge`).
De momento se pasan por petición; un TODO es derivarlos de los metadatos del
servidor en Crafty.

Ejemplo:
```bash
curl -X POST https://TU_CRAFTY/api/v2/servers/<id>/content \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"action":"plan","channel":"release"}'
```

## Frontend (pendiente)

El backend está listo y probado. Falta la UI:
1. Añadir pestaña "Content" en `app/frontend/templates/server/` (junto a `files`).
2. JS que llame al endpoint. Esqueleto:
```js
async function loadContent(serverId, token) {
  const r = await fetch(`/api/v2/servers/${serverId}/content`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ action: "scan" })
  });
  const { data } = await r.json();          // pinta tabla: nombre | source | side | update
}
```
3. Botones → `plan` (previsualizar), `apply` (con `confirm:true`), y buscador → `search`.

## Notas de diseño

- **Sin downgrades**: `plan` solo propone si la candidata es más nueva por fecha
  de publicación (no solo "distinta").
- **Canal por defecto `release`**; beta/alpha son opt-in.
- **Reversible**: cada sustitución guarda el jar viejo en `mods/_backup/<timestamp>/`.
- **allowModDistribution=false**: si CurseForge no da URL, el item se marca
  `blocked` y se omite (descarga manual).
- Tras `apply`, reiniciar el servidor para cargar los cambios.

El motor original y su CLI (`scan/apply/loader/search`) viven fuera del fork en
`D:\mc-content-manager\` (`mccm.py`), útil para pruebas sin levantar el panel.
