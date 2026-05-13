# whatsapp-claude-bridge

Puente personal entre **WhatsApp** y **Claude Code** que te permite hablar con tu propio asistente desde el móvil — por texto o por audio — manteniendo acceso completo a tus MCPs (correo, calendario, Drive, sheets, contratos…).

> Optimizado para macOS con Apple Silicon. Funciona en cualquier Mac.

## Cómo funciona

```
iPhone WhatsApp ──► whatsapp-mcp (whatsmeow + SQLite)
                        │
                        ▼
              bridge.py (este repo)
                ├─ Watcher: polling SQLite
                ├─ Transcripción: mlx-whisper local (audios)
                ├─ Orquestador: subprocess al CLI `claude`
                │     · resume por chat → hilo permanente
                │     · acceso a TODOS los MCPs de ~/.claude.json
                └─ Sender: POST al bridge → respuesta llega al iPhone
```

- **Texto**: ~5-15 s por respuesta corta, ~30-60 s si Claude consulta correo o calendario.
- **Audio**: descarga + transcripción local con `mlx-whisper` (Whisper large-v3-turbo). 100 % privado, gratis, ~3 s para audios de 30 s.
- **Kill switch**: tope de gasto diario configurable (por defecto 15 €/día con Opus 4.7).
- **Anti-bucle**: el bot prefija sus respuestas con un zero-width space invisible para no procesarse a sí mismo.

## Requisitos

1. macOS con Apple Silicon (para `mlx-whisper`; en Intel funciona pero sin aceleración).
2. [whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) instalado y corriendo (bridge en `http://localhost:8080`).
3. [Claude Code CLI](https://docs.claude.com/claude-code) instalado y autenticado:
   ```bash
   npm install -g @anthropic-ai/claude-code
   claude login
   ```
4. `uv` (gestor de Python): `brew install uv`
5. `ffmpeg`: `brew install ffmpeg`

## Instalación

```bash
git clone https://github.com/TU_USUARIO/whatsapp-claude-bridge ~/whatsapp-claude-bridge
cd ~/whatsapp-claude-bridge

# Crea tu config a partir del ejemplo
cp config.example.json config.json
cp prompts/ceo.example.md prompts/ceo.md
# Edita ambos: pon tu JID, JIDs de grupo, ajusta el prompt a tu negocio

# Instala dependencias
uv sync
```

### Encontrar tu JID y el JID del grupo

```bash
# Tu propio JID (número de WhatsApp)
sqlite3 ~/whatsapp-mcp/whatsapp-bridge/store/whatsapp.db \
  "SELECT jid FROM whatsmeow_device"

# Lista de grupos donde estás
sqlite3 ~/whatsapp-mcp/whatsapp-bridge/store/messages.db \
  "SELECT jid, name FROM chats WHERE jid LIKE '%@g.us' ORDER BY last_message_time DESC LIMIT 10"
```

### Crear el grupo disparador

Desde tu iPhone:
1. Nuevo grupo en WhatsApp con cualquier contacto.
2. Ponle nombre (ej. `🤖 CEO Bot`).
3. Info del grupo → elimina al otro miembro. Quedas solo tú.
4. Manda un mensaje al grupo para que se registre.
5. Copia su JID a `config.json` → `allowed_chats`.

## Ejecución

### Manual (foreground)

```bash
uv run bridge.py
```

### Auto-arranque al login (LaunchAgent)

```bash
cp com.example.whatsapp-claude.plist ~/Library/LaunchAgents/com.TUUSER.whatsapp-claude.plist
# Edita el plist y sustituye paths absolutos por los tuyos
launchctl load ~/Library/LaunchAgents/com.TUUSER.whatsapp-claude.plist
```

El LaunchAgent envuelve la ejecución con `caffeinate -di` para que el Mac no se duerma.

## Comandos útiles

```bash
# Logs en vivo
tail -f logs/bridge.log

# Gasto del día
cat state/usage.json

# Reiniciar el bridge
launchctl unload ~/Library/LaunchAgents/com.TUUSER.whatsapp-claude.plist
launchctl load   ~/Library/LaunchAgents/com.TUUSER.whatsapp-claude.plist

# Añadir otro grupo: edita config.json → allowed_chats
# (se recarga en caliente; no hace falta reiniciar)
```

## Estructura del proyecto

```
whatsapp-claude-bridge/
├── bridge.py                     # watcher + transcripción + orquestador + sender
├── config.json                   # tu config (gitignored)
├── config.example.json           # plantilla
├── prompts/
│   ├── ceo.md                    # tu prompt (gitignored)
│   └── ceo.example.md            # plantilla
├── com.example.whatsapp-claude.plist  # LaunchAgent ejemplo
├── pyproject.toml
├── state/                        # sesiones + usage + last_seen (gitignored)
└── logs/                         # bridge.log (gitignored)
```

## Seguridad

- **Filtro is_from_me=1**: solo procesa mensajes que envías tú desde tu propia cuenta de WhatsApp. En grupos solo-tú esto es equivalente a "solo yo escribo aquí".
- **Sin almacenamiento remoto**: todo el estado, transcripciones y conversaciones viven en `state/` local. Solo Anthropic ve los prompts que envías al modelo.
- **Kill switch diario**: si superas el tope de gasto, el bridge responde con un aviso y se detiene hasta el día siguiente.

## Coste estimado

Con Claude Opus 4.7 (cambiable en `config.json`):
- Mensaje texto corto: ~0,02 €
- Resumen de correos / calendario: ~0,10-0,30 €
- Tarea compleja con múltiples MCPs: ~0,50-2 €

Audio local: gratis (mlx-whisper en el Mac).

## Licencia

MIT
