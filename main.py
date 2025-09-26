import os
import re
import asyncio
import threading
import traceback
from collections import deque
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

# --- Config ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://consulta-pe-bot.up.railway.app").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", None)
PORT = int(os.getenv("PORT", 8080))

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Flask app ---
app = Flask(__name__)
CORS(app)

# --- Async loop for Telethon ---
loop = asyncio.new_event_loop()
threading.Thread(
    target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True
).start()


def run_coro(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


# --- Setup Telegram Client ---
if SESSION_STRING and SESSION_STRING.strip() and SESSION_STRING != "consulta_pe_bot":
    session = StringSession(SESSION_STRING)
    print("🔑 Usando SESSION_STRING desde variables de entorno")
else:
    session = "consulta_pe_bot"
    print("📂 Usando sesión temporal (no persistente)")

client = TelegramClient(session, API_ID, API_HASH, loop=loop)

# Mensajes en memoria
messages = deque(maxlen=2000)
_messages_lock = threading.Lock()

# Login pendiente
pending_phone = {"phone": None, "sent_at": None}


# Limpieza de cabecera/pie y extracción de campos
def clean_and_extract(raw_text: str):
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text

    # --- 1. Eliminar cabecera ---
    header_pattern = r"^\s*\[#LEDER_BOT\].*?resultado\.\s*"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)

    # --- 2. Eliminar pie ---
    footer_pattern = r"(↞.*|Credits\s*:.+|Wanted for\s*:.+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)

    # --- 3. Limpiar espacios ---
    text = text.strip()

    # --- 4. Extraer datos clave ---
    fields = {}
    dni_match = re.search(r"DNI\s*:\s*(\d+)", text, re.IGNORECASE)
    if dni_match:
        fields["dni"] = dni_match.group(1)

    ape_match = re.search(r"APELLIDOS?\s*:\s*([A-ZÁÉÍÓÚÑ\s]+)", text, re.IGNORECASE)
    if ape_match:
        fields["apellidos"] = ape_match.group(1).strip()

    nom_match = re.search(r"NOMBRES?\s*:\s*([A-ZÁÉÍÓÚÑ\s]+)", text, re.IGNORECASE)
    if nom_match:
        fields["nombres"] = nom_match.group(1).strip()

    edad_match = re.search(r"EDAD\s*:\s*(\d+)", text, re.IGNORECASE)
    if edad_match:
        fields["edad"] = edad_match.group(1)

    return {"text": text, "fields": fields}


# --- Handler de nuevos mensajes ---
async def _on_new_message(event):
    try:
        raw_text = event.raw_text or ""
        cleaned = clean_and_extract(raw_text)

        msg_obj = {
            "chat_id": getattr(event, "chat_id", None),
            "from_id": event.sender_id,
            "date": event.message.date.isoformat()
            if getattr(event, "message", None)
            else datetime.utcnow().isoformat(),
            # Siempre JSON limpio
            "message": cleaned["text"],
            "fields": cleaned["fields"],
        }

        # Si tiene media, guardar e incluir URL
        if getattr(event, "message", None) and getattr(event.message, "media", None):
            try:
                saved_path = await event.download_media(file=DOWNLOAD_DIR)
                filename = os.path.basename(saved_path)
                msg_obj["url"] = f"{PUBLIC_URL}/files/{filename}"
            except Exception as e:
                msg_obj["media_error"] = str(e)

        with _messages_lock:
            messages.appendleft(msg_obj)

        print("📥 Nuevo mensaje interceptado:", msg_obj)
    except Exception:
        traceback.print_exc()


client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))


# --- Rutina de reconexión / ping ---
async def _ensure_connected():
    while True:
        try:
            if not client.is_connected():
                await client.connect()
                print("🔌 Reconectando Telethon...")
            if await client.is_user_authorized():
                pass
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(300)


asyncio.run_coroutine_threadsafe(_ensure_connected(), loop)

# --- Rutas HTTP ---


@app.route("/")
def root():
    return jsonify(
        {
            "status": "ok",
            "public_url": PUBLIC_URL,
            "endpoints": {
                "/login?phone=+51...": "Solicita código",
                "/code?code=12345": "Confirma código",
                "/send?chat_id=@user&msg=hola": "Enviar mensaje",
                "/get": "Obtener mensajes interceptados (JSON limpio)",
                "/files/": "Descargar archivos",
            },
        }
    )


@app.route("/status")
def status():
    try:
        is_auth = run_coro(client.is_user_authorized())
    except Exception:
        is_auth = False

    current_session = None
    try:
        if is_auth:
            current_session = client.session.save()
    except Exception:
        pass

    return jsonify(
        {
            "authorized": bool(is_auth),
            "pending_phone": pending_phone["phone"],
            "session_loaded": True if SESSION_STRING else False,
            "session_string": current_session,
        }
    )


@app.route("/login")
def login():
    phone = request.args.get("phone")
    if not phone:
        return jsonify({"error": "Falta parámetro phone"}), 400

    async def _send_code():
        await client.connect()
        if await client.is_user_authorized():
            return {"status": "already_authorized"}
        try:
            await client.send_code_request(phone)
            pending_phone["phone"] = phone
            pending_phone["sent_at"] = datetime.utcnow().isoformat()
            return {"status": "code_sent", "phone": phone}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    result = run_coro(_send_code())
    return jsonify(result)


@app.route("/code")
def code():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Falta parámetro code"}), 400
    if not pending_phone["phone"]:
        return jsonify({"error": "No hay login pendiente"}), 400

    phone = pending_phone["phone"]

    async def _sign_in():
        try:
            await client.sign_in(phone, code)
            await client.start()
            pending_phone["phone"] = None
            pending_phone["sent_at"] = None
            new_string = client.session.save()
            return {"status": "authenticated", "session_string": new_string}
        except errors.SessionPasswordNeededError:
            return {"status": "error", "error": "2FA requerido"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    result = run_coro(_sign_in())
    return jsonify(result)


@app.route("/send")
def send_msg():
    chat_id = request.args.get("chat_id")
    msg = request.args.get("msg")
    if not chat_id or not msg:
        return jsonify({"error": "Faltan parámetros"}), 400

    async def _send():
        target = int(chat_id) if chat_id.isdigit() else chat_id
        entity = await client.get_entity(target)
        await client.send_message(entity, msg)
        return {"status": "sent", "to": chat_id, "msg": msg}

    try:
        result = run_coro(_send())
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/get")
def get_msgs():
    with _messages_lock:
        data = list(messages)
    return jsonify(
        {
            "message": "found data" if data else "no data",
            "result": {"quantity": len(data), "coincidences": data},
        }
    )


@app.route("/files/<path:filename>")
def files(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    try:
        run_coro(client.connect())
    except Exception:
        pass
    print(f"🚀 App corriendo en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
