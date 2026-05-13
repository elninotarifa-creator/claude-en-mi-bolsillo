"""
WhatsApp <-> Claude bridge.

Polling sobre la SQLite de whatsapp-mcp/whatsapp-bridge; cuando el dueño escribe
en un chat autorizado, lanza el CLI de Claude (que ya tiene todos los MCPs y
agentes configurados en ~/.claude.json), persiste la sesión por chat para
mantener hilo permanente, y devuelve la respuesta vía POST al bridge HTTP.

Kill switch por presupuesto diario en EUR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
USAGE_PATH = STATE_DIR / "usage.json"
LAST_SEEN_PATH = STATE_DIR / "last_seen.txt"
SESSIONS_PATH = STATE_DIR / "sessions.json"

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
(STATE_DIR / "conversations").mkdir(parents=True, exist_ok=True)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bridge.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("bridge")


# Marca invisible (zero-width space) que el bot prefija a sus respuestas para
# que el watcher las distinga de los mensajes del dueño. En grupos solo-yo,
# is_from_me=1 también es cierto para las respuestas del propio bot.
BOT_MARK = "​"


@dataclass
class Config:
    owner_jid: str
    allowed_chats: dict[str, str]
    bridge_url: str
    messages_db: Path
    model: str
    max_turns: int
    poll_interval_seconds: float
    daily_budget_eur: float
    price_in_eur_per_mtok: float
    price_out_eur_per_mtok: float
    system_prompt: str
    mcp_servers: list[str]
    whisper_model: str

    @classmethod
    def load(cls) -> "Config":
        raw = json.loads(CONFIG_PATH.read_text())
        prompt_path = ROOT / raw["system_prompt_file"]
        return cls(
            owner_jid=raw["owner_jid"],
            allowed_chats=raw.get("allowed_chats", {}),
            bridge_url=raw["bridge_url"],
            messages_db=Path(raw["messages_db"]),
            model=raw["model"],
            max_turns=raw.get("max_turns", 25),
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 2)),
            daily_budget_eur=float(raw["daily_budget_eur"]),
            price_in_eur_per_mtok=float(raw["price_per_million_input_tokens_eur"]),
            price_out_eur_per_mtok=float(raw["price_per_million_output_tokens_eur"]),
            system_prompt=prompt_path.read_text(),
            mcp_servers=raw.get("mcp_servers", []),
            whisper_model=raw.get("whisper_model", "mlx-community/whisper-large-v3-turbo"),
        )


# ------------------ Estado: sesiones, usage, last_seen ------------------


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        log.warning("JSON corrupto en %s, reinicio a default", path)
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def now_sqlite_format() -> str:
    """Hora actual en el mismo formato que SQLite guarda los timestamps."""
    return datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")


def load_last_seen(cfg: "Config") -> str:
    if LAST_SEEN_PATH.exists():
        return LAST_SEEN_PATH.read_text().strip()
    # Primera arrancada: usa el MAX timestamp de los chats autorizados para no
    # procesar historial antiguo. Si no hay nada, hora actual.
    ts = None
    if cfg.allowed_chats:
        placeholders = ",".join("?" * len(cfg.allowed_chats))
        con = sqlite3.connect(f"file:{cfg.messages_db}?mode=ro", uri=True)
        try:
            row = con.execute(
                f"SELECT MAX(timestamp) FROM messages WHERE chat_jid IN ({placeholders})",
                list(cfg.allowed_chats.keys()),
            ).fetchone()
            ts = row[0] if row else None
        finally:
            con.close()
    ts = ts or now_sqlite_format()
    LAST_SEEN_PATH.write_text(ts)
    return ts


def save_last_seen(ts: str) -> None:
    LAST_SEEN_PATH.write_text(ts)


def today_key() -> str:
    return date.today().isoformat()


def get_usage_today() -> dict[str, float]:
    data = load_json(USAGE_PATH, {})
    today = today_key()
    if data.get("date") != today:
        data = {"date": today, "spent_eur": 0.0, "calls": 0}
        save_json(USAGE_PATH, data)
    return data


def add_usage(input_tokens: int, output_tokens: int, cfg: Config) -> float:
    cost = (
        input_tokens * cfg.price_in_eur_per_mtok / 1_000_000
        + output_tokens * cfg.price_out_eur_per_mtok / 1_000_000
    )
    data = get_usage_today()
    data["spent_eur"] = round(data.get("spent_eur", 0.0) + cost, 4)
    data["calls"] = data.get("calls", 0) + 1
    save_json(USAGE_PATH, data)
    return data["spent_eur"]


def budget_exceeded(cfg: Config) -> bool:
    return get_usage_today().get("spent_eur", 0.0) >= cfg.daily_budget_eur


# ------------------ Watcher SQLite ------------------


def fetch_new_messages(cfg: Config, since_iso: str) -> list[dict[str, Any]]:
    """Mensajes nuevos en chats autorizados desde since_iso.

    En grupos solo-yo el dueño escribe con is_from_me=1. Para evitar bucles
    con las respuestas del propio bot (que también se guardan con is_from_me=1
    por usar la misma sesión), filtramos las que empiecen por BOT_MARK.
    """
    if not cfg.allowed_chats:
        return []
    chat_jids = list(cfg.allowed_chats.keys())
    placeholders = ",".join("?" * len(chat_jids))
    con = sqlite3.connect(f"file:{cfg.messages_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""
            SELECT id, chat_jid, sender, content, timestamp, is_from_me, media_type
            FROM messages
            WHERE chat_jid IN ({placeholders})
              AND is_from_me = 1
              AND timestamp > ?
            ORDER BY timestamp ASC
            """,
            [*chat_jids, since_iso],
        ).fetchall()
    finally:
        con.close()
    return [r for r in (dict(x) for x in rows) if not (r["content"] or "").startswith(BOT_MARK)]


# ------------------ Claude CLI ------------------


def call_claude(
    cfg: Config, chat_jid: str, user_message: str, sessions: dict[str, str]
) -> tuple[str, dict[str, int]]:
    """Llama al CLI claude. Persiste session_id por chat para mantener hilo."""
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    cmd: list[str] = [
        claude_bin,
        "--model",
        cfg.model,
        "--print",
        "--output-format",
        "json",
        "--max-turns",
        str(cfg.max_turns),
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        cfg.system_prompt,
    ]
    if chat_jid in sessions:
        cmd.extend(["--resume", sessions[chat_jid]])

    cmd.append(user_message)

    log.info("Lanzando claude para %s (resume=%s)", chat_jid, chat_jid in sessions)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        log.error("claude rc=%s stderr=%s", result.returncode, result.stderr[:500])
        raise RuntimeError(f"claude error rc={result.returncode}: {result.stderr[:200]}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        log.error("Salida no-JSON de claude: %s", result.stdout[:500])
        raise

    answer = payload.get("result") or payload.get("response") or ""
    session_id = payload.get("session_id")
    if session_id:
        sessions[chat_jid] = session_id
        save_json(SESSIONS_PATH, sessions)

    usage = payload.get("usage", {}) or {}
    return answer.strip(), {
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
    }


# ------------------ Audio: descarga + transcripción ------------------


AUDIO_MEDIA_TYPES = {"audio", "ptt", "voice"}


async def download_media(cfg: Config, message_id: str, chat_jid: str) -> str:
    """Pide al bridge que descargue un media; devuelve path absoluto del archivo."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{cfg.bridge_url}/api/download",
            json={"message_id": message_id, "chat_jid": chat_jid},
        )
        r.raise_for_status()
        payload = r.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("message", "download failed"))
    path = payload.get("path")
    if not path or not Path(path).exists():
        raise RuntimeError(f"path no existe: {path}")
    return path


def transcribe_audio(path: str, model_repo: str) -> str:
    """Transcribe con mlx-whisper. Bloqueante; llamar vía asyncio.to_thread."""
    import mlx_whisper  # import diferido: el modelo carga la primera vez

    result = mlx_whisper.transcribe(
        path,
        path_or_hf_repo=model_repo,
        language="es",
        fp16=True,
    )
    return (result.get("text") or "").strip()


# ------------------ Sender HTTP al bridge ------------------


async def send_whatsapp(cfg: Config, chat_jid: str, text: str, mark: bool = True) -> None:
    if not text:
        return
    payload_text = (BOT_MARK + text) if mark else text
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{cfg.bridge_url}/api/send",
            json={"recipient": chat_jid, "message": payload_text},
        )
        if r.status_code >= 300:
            log.error("Bridge respondió %s: %s", r.status_code, r.text[:200])


# ------------------ Loop principal ------------------


async def handle_message(
    cfg: Config, msg: dict[str, Any], sessions: dict[str, str]
) -> None:
    chat_jid = msg["chat_jid"]
    content = (msg["content"] or "").strip()
    media_type = (msg.get("media_type") or "").lower()

    # En grupos solo-yo el filtro real es is_from_me=1 + ausencia de BOT_MARK
    # (ambos ya aplicados en fetch_new_messages). El sender en grupos viene
    # como LID interno de WhatsApp, no como número, por eso no se usa aquí.

    if media_type in AUDIO_MEDIA_TYPES:
        log.info("Audio detectado en %s, descargando + transcribiendo", chat_jid)
        try:
            path = await download_media(cfg, msg["id"], chat_jid)
            transcript = await asyncio.to_thread(
                transcribe_audio, path, cfg.whisper_model
            )
        except Exception as e:
            log.exception("Error procesando audio")
            await send_whatsapp(cfg, chat_jid, f"⚠️ No pude transcribir el audio: {e}")
            return
        if not transcript:
            await send_whatsapp(cfg, chat_jid, "⚠️ El audio no se transcribió a nada.")
            return
        log.info("Transcripción (%d chars): %s", len(transcript), transcript[:120])
        content = transcript

    if not content:
        log.info("Ignorado mensaje vacío en %s (media_type=%s)", chat_jid, media_type)
        return

    if budget_exceeded(cfg):
        await send_whatsapp(
            cfg, chat_jid, "⚠️ Tope diario de gasto alcanzado. Volveré mañana."
        )
        return

    log.info("Procesando: %s | %s", chat_jid, content[:80])
    try:
        answer, usage = await asyncio.to_thread(
            call_claude, cfg, chat_jid, content, sessions
        )
    except Exception as e:
        log.exception("Error llamando a claude")
        await send_whatsapp(cfg, chat_jid, f"⚠️ Error procesando: {e}")
        return

    spent = add_usage(usage["input_tokens"], usage["output_tokens"], cfg)
    log.info(
        "Tokens in=%s out=%s | gasto día=%.4f €",
        usage["input_tokens"],
        usage["output_tokens"],
        spent,
    )

    if answer:
        await send_whatsapp(cfg, chat_jid, answer)
    else:
        log.warning("Claude no devolvió texto para %s", chat_jid)


async def main_loop() -> None:
    cfg = Config.load()
    log.info("Bridge arrancado. Chats autorizados: %s", list(cfg.allowed_chats.keys()))
    if cfg.owner_jid == "PENDIENTE_RELLENAR":
        log.warning("owner_jid sin configurar en config.json — abortando.")
        return
    if not cfg.allowed_chats:
        log.warning("Sin chats autorizados en config.json — el bridge no hará nada.")

    sessions: dict[str, str] = load_json(SESSIONS_PATH, {})
    last_seen = load_last_seen(cfg)

    while True:
        try:
            cfg = Config.load()  # recarga en caliente
            msgs = fetch_new_messages(cfg, last_seen)
            for m in msgs:
                await handle_message(cfg, m, sessions)
                last_seen = m["timestamp"]
                save_last_seen(last_seen)
        except Exception:
            log.exception("Error en loop principal")
        await asyncio.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        log.info("Interrumpido por usuario")
