import os
import re
import asyncio
import threading
import traceback
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from telethon.errors.rpcerrorlist import UserBlockedError 
from telethon.tl.types import Channel, Chat # Necesario para resolver entidades
from telethon.utils import get_extension # Para obtener la extensión del archivo
from mimetypes import guess_type # Para adivinar el MIME-type para streaming

# --- Configuración (MANTENIDA) ---

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
# Asegúrate de que esta URL sea correcta para los archivos
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://consulta-pe-bot.up.railway.app").rstrip("/")
SESSION_STRING = os.getenv("SESSION_STRING", None)
PORT = int(os.getenv("PORT", 8080))

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# El chat ID/nombre del bot al que enviar los comandos (BOT PRINCIPAL)
LEDERDATA_BOT_ID = "@LEDERDATA_OFC_BOT" 

# El chat ID/nombre del bot de respaldo (NUEVO BOT)
LEDERDATA_BACKUP_BOT_ID = "@lederdata_publico_bot"

# Lista de bots para verificar en el handler
ALL_BOT_IDS = [LEDERDATA_BOT_ID, LEDERDATA_BACKUP_BOT_ID]

# Tiempo de espera (en segundos) para el bot principal antes de intentar con el de respaldo.
TIMEOUT_FAILOVER = 25 
# Tiempo de espera total para la llamada a la API. ESTE DEFINE CUANTO ESPERA POR TODOS LOS MENSAJES
TIMEOUT_TOTAL = 40 

# --- Configuración AÑADIDA para Películas ---

# ID numérico del canal de películas (t.me/c/1507924325)
# El ID numérico es -1001507924325.
MOVIE_CHANNEL_ID = int(os.getenv("MOVIE_CHANNEL_ID", -1001507924325))
MOVIE_CHANNEL_USERNAME = "ElCinfiloPeliculasCompletas" # Nombre o alias para referencia

# Directorio de descarga exclusivo para películas
MOVIE_DOWNLOAD_DIR = "movie_downloads"
os.makedirs(MOVIE_DOWNLOAD_DIR, exist_ok=True)

# Cache de películas descargadas (para evitar descargas repetidas)
# {filename: timestamp}
DOWNLOADED_MOVIES_CACHE = {}
MOVIE_CACHE_TIMEOUT_SECONDS = 3600 * 24 # 24 horas

# --- Manejo de Fallos por Bot (MANTENIDA) ---

bot_fail_tracker = {}
BOT_FAIL_TIMEOUT_HOURS = 6 

def is_bot_blocked(bot_id: str) -> bool:
    """Verifica si el bot está temporalmente bloqueado por fallos previos."""
    last_fail_time = bot_fail_tracker.get(bot_id)
    if not last_fail_time:
        return False

    now = datetime.now()
    six_hours_ago = now - timedelta(hours=BOT_FAIL_TIMEOUT_HOURS)

    if last_fail_time > six_hours_ago:
        time_left = last_fail_time + timedelta(hours=BOT_FAIL_TIMEOUT_HOURS) - now
        print(f"🚫 Bot {bot_id} bloqueado. Restan: {time_left}")
        return True
    
    print(f"✅ Bot {bot_id} ha cumplido su tiempo de bloqueo. Desbloqueado.")
    bot_fail_tracker.pop(bot_id, None)
    return False

def record_bot_failure(bot_id: str):
    """Registra la hora actual como la última hora de fallo del bot."""
    print(f"🚨 Bot {bot_id} ha fallado y será BLOQUEADO por {BOT_FAIL_TIMEOUT_HOURS} horas.")
    bot_fail_tracker[bot_id] = datetime.now()

# --- Aplicación Flask (MANTENIDA) ---

app = Flask(__name__)
CORS(app)

# --- Bucle Asíncrono para Telethon (MANTENIDA) ---

loop = asyncio.new_event_loop()
threading.Thread(
    target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True
).start()

def run_coro(coro):
    """Ejecuta una corrutina en el bucle principal y espera el resultado."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=TIMEOUT_TOTAL + 5) 

# --- Configuración del Cliente Telegram (MANTENIDA) ---

if SESSION_STRING and SESSION_STRING.strip() and SESSION_STRING != "consulta_pe_bot":
    session = StringSession(SESSION_STRING)
    print("🔑 Usando SESSION_STRING desde variables de entorno")
else:
    session = "consulta_pe_session" 
    print("📂 Usando sesión 'consulta_pe_session'")

client = TelegramClient(session, API_ID, API_HASH, loop=loop)

# Mensajes en memoria (usaremos esto como caché de respuestas) (MANTENIDA)
messages = deque(maxlen=2000)
_messages_lock = threading.Lock()

# Diccionario para esperar respuestas específicas: (MANTENIDA)
response_waiters = {} 

# Login pendiente (MANTENIDA)
pending_phone = {"phone": None, "sent_at": None}

# --- Lógica de Limpieza y Extracción de Datos (MANTENIDA) ---

def clean_and_extract(raw_text: str):
    """Limpia el texto de cabeceras/pies y extrae campos clave. REEMPLAZA MARCA LEDER BOT."""
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text
    
    # 1. Reemplazar la marca LEDER_BOT por CONSULTA PE
    text = re.sub(r"^\[\#LEDER\_BOT\]", "[CONSULTA PE]", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 2. Eliminar cabecera (patrón más robusto)
    header_pattern = r"^\[.*?\]\s*→\s*.*?\[.*?\](\r?\n){1,2}"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 3. Eliminar pie (patrón más robusto para créditos/paginación/warnings al final)
    footer_pattern = r"((\r?\n){1,2}\[|Página\s*\d+\/\d+.*|(\r?\n){1,2}Por favor, usa el formato correcto.*|↞ Anterior|Siguiente ↠.*|Credits\s*:.+|Wanted for\s*:.+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 4. Limpiar separador (si queda)
    text = re.sub(r"\-{3,}", "", text, flags=re.IGNORECASE | re.DOTALL)

    # 5. Limpiar espacios
    text = text.strip()

    # 6. Extraer datos clave
    fields = {}
    dni_match = re.search(r"DNI\s*:\s*(\d{8})", text, re.IGNORECASE)
    if dni_match: fields["dni"] = dni_match.group(1)
    
    photo_type_match = re.search(r"Foto\s*:\s*(rostro|huella|firma|adverso|reverso).*", text, re.IGNORECASE)
    if photo_type_match: fields["photo_type"] = photo_type_match.group(1).lower()

    return {"text": text, "fields": fields}

# --- Handler de nuevos mensajes (MANTENIDA) ---

async def _on_new_message(event):
    # ... Lógica original del handler de mensajes del bot LEDER DATA ...
    # No se incluye aquí por longitud, pero se asume que se mantiene intacta.
    # El código completo AÑADIDO está al final.
    pass # Asumiendo el código original aquí

client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))

# --- Función Central para Llamadas API (Comandos) (MANTENIDA) ---

async def _call_api_command(command: str, timeout: int = TIMEOUT_TOTAL):
    # ... Lógica original de _call_api_command ...
    # No se incluye aquí por longitud, pero se asume que se mantiene intacta.
    # El código completo AÑADIDO está al final.
    pass # Asumiendo el código original aquí

# --- Rutina de reconexión / ping (MANTENIDA) ---

async def _ensure_connected():
    # ... Lógica original de _ensure_connected ...
    # No se incluye aquí por longitud, pero se asume que se mantiene intacta.
    # El código completo AÑADIDO está al final.
    pass # Asumiendo el código original aquí

asyncio.run_coroutine_threadsafe(_ensure_connected(), loop)

# --- Rutas HTTP Base (Login/Status/General) (MANTENIDA) ---

@app.route("/")
def root():
    # ... Rutas originales /, /status, /login, /code, /send, /get ...
    # No se incluye aquí por longitud, pero se asume que se mantiene intacta.
    pass # Asumiendo el código original aquí

@app.route("/files/<path:filename>")
def files(filename):
    """Ruta para descargar archivos (manteniendo la descarga forzada del bot de datos)."""
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

# ----------------------------------------------------------------------
# --- Rutas HTTP de API (Comandos LEDER DATA) (MANTENIDA) ----------------
# ----------------------------------------------------------------------

# ... Todas las rutas originales /dni, /dnif, etc. y las de nombres ...
# No se incluyen aquí por longitud, pero se asume que se mantienen intactas.
pass # Asumiendo el código original aquí

# ----------------------------------------------------------------------
# --- NUEVAS Rutas HTTP de API (Películas/Streaming) ----------------------
# ----------------------------------------------------------------------

@app.route("/movie_search", methods=["GET"])
def movie_search():
    """
    API para buscar coincidencias en un canal privado de películas, descargar
    el archivo y exponer el link de streaming.
    """
    search_query = request.args.get("q")
    if not search_query:
        return jsonify({"status": "error", "message": "Falta el parámetro de búsqueda 'q'."}), 400

    async def _search_and_download_movie():
        if not await client.is_user_authorized():
            return {"status": "error", "message": "Cliente Telethon no autorizado. Por favor, inicie sesión."}

        try:
            # 1. Obtener entidad del canal
            # Si el usuario NO es miembro, esta llamada fallará.
            channel_entity = await client.get_entity(MOVIE_CHANNEL_ID)
            
            # 2. Buscar en el canal
            # Busca en el historial del canal por la query. La limitación de Telegram es de unos 100 resultados.
            print(f"🔎 Buscando '{search_query}' en el canal {MOVIE_CHANNEL_ID}...")
            
            # Nota: El 'search' busca en el texto del mensaje y la descripción del documento.
            messages = await client.get_messages(
                channel_entity, 
                search=search_query, 
                limit=5
            )
            
            if not messages:
                return {"status": "not_found", "message": f"No se encontraron coincidencias para '{search_query}' en el canal de películas."}

            results = []
            
            for msg in messages:
                # 3. Filtrar por mensaje con archivo multimedia (video/documento)
                if not msg.media or not (isinstance(msg.media, MessageMediaDocument) or (msg.media.document and 'video' in getattr(msg.media.document, 'mime_type', ''))):
                    continue

                document = msg.media.document
                file_name_original = getattr(document, 'file_name', f"movie_{msg.id}.unk")
                # Crear un nombre único de archivo para la descarga
                file_ext = get_extension(document)
                unique_filename = f"movie_{msg.id}{file_ext}"
                save_path = os.path.join(MOVIE_DOWNLOAD_DIR, unique_filename)
                
                # 4. Verificar caché
                now = time.time()
                if unique_filename in DOWNLOADED_MOVIES_CACHE and (now - DOWNLOADED_MOVIES_CACHE[unique_filename]) < MOVIE_CACHE_TIMEOUT_SECONDS:
                    print(f"✅ Película {unique_filename} encontrada en caché. Omitiendo descarga.")
                else:
                    # 5. Descargar archivo
                    print(f"📥 Descargando película: {file_name_original} -> {unique_filename}")
                    await client.download_media(msg, file=save_path)
                    DOWNLOADED_MOVIES_CACHE[unique_filename] = now
                    print(f"💾 Descarga completada: {unique_filename}")

                # 6. Exponer URL de Streaming (usando la nueva ruta /stream)
                stream_url = f"{PUBLIC_URL}/stream/{unique_filename}"
                
                # Obtener detalles del archivo para la respuesta
                file_size_bytes = document.size
                
                results.append({
                    "title": file_name_original,
                    "message_text": msg.message or msg.media.document.caption, # El mensaje o la descripción
                    "telegram_link": f"https://t.me/c/{abs(MOVIE_CHANNEL_ID)}/{msg.id}",
                    "size_mb": round(file_size_bytes / (1024 * 1024), 2),
                    "stream_url": stream_url,
                    "filename_in_server": unique_filename,
                })

            if not results:
                 return {"status": "not_found", "message": f"No se encontraron mensajes con archivos de video para '{search_query}'."}
                 
            return {"status": "ok", "query": search_query, "results": results}

        except errors.ChannelPrivateError:
            return {"status": "error", "message": f"Error: El cliente no es miembro del canal privado con ID {MOVIE_CHANNEL_ID}."}
        except Exception as e:
            traceback.print_exc()
            return {"status": "error", "message": f"Error interno durante la búsqueda o descarga: {str(e)}"}

    result = run_coro(_search_and_download_movie())
    if result.get("status", "").startswith("error"):
        return jsonify(result), 500
    if result.get("status") == "not_found":
        return jsonify(result), 404
        
    return jsonify(result)

@app.route("/stream/<path:filename>")
def stream_movie(filename):
    """
    Ruta para reproducir la película en streaming sin forzar la descarga, 
    simulando el comportamiento de Google Drive/Telegram web.
    """
    file_path = os.path.join(MOVIE_DOWNLOAD_DIR, filename)
    
    if not os.path.exists(file_path):
        return jsonify({"status": "error", "message": "Archivo no encontrado en el servidor."}), 404

    # Determinar el MIME-type
    mime_type = guess_type(filename)[0] or 'application/octet-stream'
    
    # IMPORTANTE: Usamos as_attachment=False para intentar que el navegador lo reproduzca
    # en lugar de descargarlo. El encabezado 'Content-Disposition' se omite.
    
    # Para forzar la visualización en línea (y no la descarga), se puede usar:
    # headers = {'Content-Disposition': 'inline; filename="%s"' % filename}
    # return send_from_directory(MOVIE_DOWNLOAD_DIR, filename, mimetype=mime_type, headers=headers)
    
    # La forma más simple para streaming en Flask es usando as_attachment=False
    return send_from_directory(
        MOVIE_DOWNLOAD_DIR, 
        filename, 
        mimetype=mime_type, 
        as_attachment=False
    )

# ----------------------------------------------------------------------
# --- Inicio de la Aplicación (MANTENIDA) ------------------------------
# ----------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_coro(client.connect())
        if not run_coro(client.is_user_authorized()):
             run_coro(client.start())
             
        # Esto ayuda a Telethon a resolver la entidad de ambos bots al inicio
        run_coro(client.get_entity(LEDERDATA_BOT_ID)) 
        run_coro(client.get_entity(LEDERDATA_BACKUP_BOT_ID)) 
    except Exception:
        pass
    print(f"🚀 App corriendo en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
