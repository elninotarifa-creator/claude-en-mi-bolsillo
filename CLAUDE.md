# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Qué es

Puente personal entre **WhatsApp** y **Claude Code** que permite al usuario hablar con su asistente desde el móvil — por texto o por audio — manteniendo acceso completo a sus MCPs (correo, calendario, Drive, sheets, contratos…).

Optimizado para macOS con Apple Silicon. Funciona en cualquier Mac.

## Stack

- **Python 3.x** (uv para gestión de entorno con `pyproject.toml`)
- **mlx-whisper** local para transcripción de audios (Apple Silicon)
- **SQLite** como fuente de mensajes (alimentada por [`../whatsapp-mcp/`](../whatsapp-mcp))
- **launchd** (`.plist`) para correr `bridge.py` como servicio en background
- Integración con **Claude Code CLI** mediante prompts en `prompts/`

## Arquitectura

```
iPhone WhatsApp ──► whatsapp-mcp (whatsmeow + SQLite)
                        │
                        ▼
              bridge.py (este repo)
                ├─ Watcher: polling SQLite
                ├─ Transcripción: mlx-whisper local (audios)
                └─ Llamada a Claude Code con prompt + contexto
```

## Comandos

```sh
# Instalar dependencias
uv sync

# Arrancar bridge manual
python3 bridge.py

# Como servicio (launchd)
launchctl load com.jaime.whatsapp-claude.plist
launchctl unload com.jaime.whatsapp-claude.plist

# Logs
tail -f logs/bridge.log
```

## Archivos importantes

- `bridge.py` — proceso principal
- `config.json` — credenciales y rutas (no subir a git, usar `config.example.json` como referencia)
- `com.jaime.whatsapp-claude.plist` — agente launchd
- `prompts/` — plantillas de prompt para Claude por tipo de mensaje
- `logs/` — output del servicio

## Relación con `../whatsapp-mcp/`

Este bridge **consume** la SQLite que mantiene `whatsapp-mcp`. Tiene que estar corriendo el `whatsapp-bridge` Go del otro proyecto para que haya mensajes que leer.

## Más detalles

Ver [README.md](./README.md) para instalación completa y troubleshooting.
