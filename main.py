import os
import re
import asyncio
import threading
import traceback
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import unquote
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

# --- Configuración ---

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
# Se sugiere un tiempo menor que el timeout total de la API.
TIMEOUT_PRIMARY_BOT_FAILOVER = 25 
# Tiempo de espera total para la llamada a la API.
TIMEOUT_TOTAL = 40 

# --- Aplicación Flask ---

app = Flask(__name__)
CORS(app)

# --- Bucle Asíncrono para Telethon ---

loop = asyncio.new_event_loop()
threading.Thread(
    target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True
).start()

def run_coro(coro):
    """Ejecuta una corrutina en el bucle principal y espera el resultado."""
    # Usamos el TIMEOUT_TOTAL para la espera externa
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=TIMEOUT_TOTAL + 5) 

# --- Configuración del Cliente Telegram ---

if SESSION_STRING and SESSION_STRING.strip() and SESSION_STRING != "consulta_pe_bot":
    session = StringSession(SESSION_STRING)
    print("🔑 Usando SESSION_STRING desde variables de entorno")
else:
    # Usa un nombre de archivo si quieres persistencia local sin SESSION_STRING
    session = "consulta_pe_session" 
    print("📂 Usando sesión 'consulta_pe_session'")

client = TelegramClient(session, API_ID, API_HASH, loop=loop)

# Mensajes en memoria (usaremos esto como caché de respuestas)
messages = deque(maxlen=2000)
_messages_lock = threading.Lock()

# Diccionario para esperar respuestas específicas: 
# {command_id: {"future": asyncio.Future, "messages": list, "dni": str, "command": str, "timer": asyncio.TimerHandle, "sent_to_bot": str}}
response_waiters = {} 

# Login pendiente
pending_phone = {"phone": None, "sent_at": None}

# --- Lógica de Limpieza y Extracción de Datos ---

def clean_and_extract(raw_text: str):
    """Limpia el texto de cabeceras/pies y extrae campos clave. REEMPLAZA MARCA LEDER BOT."""
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text
    
    # 1. Reemplazar la marca LEDER_BOT por CONSULTA PE
    # Esto busca y reemplaza la primera ocurrencia de [#LEDER_BOT]
    text = re.sub(r"^\[\#LEDER\_BOT\]", "[CONSULTA PE]", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 2. Eliminar cabecera (patrón más robusto)
    # Buscamos la cabecera hasta "==============================\s*"
    # Eliminamos el encabezado que contiene [CONSULTA PE] o [#LEDER_BOT]
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
    # Extracción de DNI de 8 dígitos
    dni_match = re.search(r"DNI\s*:\s*(\d{8})", text, re.IGNORECASE)
    if dni_match: fields["dni"] = dni_match.group(1)
    
    # Extracción de tipo de foto para /dnif y /dnivaz (para etiquetar las URLs)
    photo_type_match = re.search(r"Foto\s*:\s*(rostro|huella|firma|adverso|reverso).*", text, re.IGNORECASE)
    if photo_type_match: fields["photo_type"] = photo_type_match.group(1).lower()

    return {"text": text, "fields": fields}

# --- Handler de nuevos mensajes ---

async def _on_new_message(event):
    """Intercepta mensajes y resuelve las esperas de API si aplica."""
    try:
        # 1. Verificar si el mensaje viene de alguno de los bots
        sender_is_bot = False
        
        # Obtenemos los IDs de los bots al inicio si es posible, para evitar llamadas a la API en cada mensaje
        bot_entities = {}
        try:
             # Usamos una lista global para guardar las entidades de los bots si ya las obtuvimos
            if not hasattr(_on_new_message, 'bot_ids'):
                _on_new_message.bot_ids = {}
                for bot_name in ALL_BOT_IDS:
                    entity = await client.get_entity(bot_name)
                    _on_new_message.bot_ids[bot_name] = entity.id

            if event.sender_id in _on_new_message.bot_ids.values():
                sender_is_bot = True
        except Exception:
            # Si falla, simplemente ignoramos este mensaje
            return 
            
        if not sender_is_bot:
            return # Ignorar mensajes que no sean de los bots
            
        raw_text = event.raw_text or ""
        cleaned = clean_and_extract(raw_text)
        
        # 🚨 Inicializar la lista de URLs para cada mensaje
        msg_urls = []

        # 2. Manejar archivos (media): Descarga TODOS los archivos adjuntos
        if getattr(event, "message", None) and getattr(event.message, "media", None):
            media_list = []
            if isinstance(event.message.media, (MessageMediaDocument, MessageMediaPhoto)):
                media_list.append(event.message.media)
            elif hasattr(event.message.media, 'webpage') and event.message.media.webpage and hasattr(event.message.media.webpage, 'photo'):
                 # Esto podría ser una imagen de preview, la ignoramos o la manejamos si es necesario.
                 pass
            
            # Si hay media, proceder a la descarga
            if media_list:
                try:
                    # Usar datetime.now(timezone.utc) para un nombre de archivo consistente
                    timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                    
                    for i, media in enumerate(media_list):
                        # Obtener la extensión original si es posible, o usar 'file'
                        if hasattr(media, 'document') and hasattr(media.document, 'attributes'):
                            file_ext = os.path.splitext(getattr(media.document, 'file_name', 'file'))[1]
                        elif isinstance(media, MessageMediaPhoto) or (hasattr(media, 'photo') and media.photo):
                            file_ext = '.jpg' # La foto de Telegram suele ser JPG
                        else:
                            file_ext = '.file'
                            
                        # Si hay un DNI, lo incluimos en el nombre
                        dni_part = f"_{cleaned['fields'].get('dni')}" if cleaned["fields"].get("dni") else ""
                        
                        # 🚨 Incluir el tipo de foto/documento en el nombre para depuración
                        type_part = f"_{cleaned['fields'].get('photo_type')}" if cleaned['fields'].get('photo_type') else ""
                        
                        unique_filename = f"{timestamp_str}_{event.message.id}{dni_part}{type_part}_{i}{file_ext}"
                        
                        # Descargar el medio
                        saved_path = await client.download_media(event.message, file=os.path.join(DOWNLOAD_DIR, unique_filename))
                        filename = os.path.basename(saved_path)
                        
                        # 🚨 Estructura de URL mejorada para facilitar el uso y la identificación
                        url_obj = {
                            "url": f"{PUBLIC_URL}/files/{filename}", 
                            "type": cleaned['fields'].get('photo_type', 'file'),
                            "text_context": raw_text.split('\n')[0].strip() # Cabecera del mensaje
                        }
                        msg_urls.append(url_obj)
                        
                except Exception as e:
                    print(f"Error al descargar media: {e}")
                    # El error se loguea pero el proceso continúa
        
        msg_obj = {
            "chat_id": getattr(event, "chat_id", None),
            "from_id": event.sender_id,
            "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
            "message": cleaned["text"],
            "fields": cleaned["fields"],
            "urls": msg_urls # 🚨 Usar la lista de URLs construida
        }

        # 3. Intentar resolver la espera de la API
        resolved = False
        with _messages_lock:
            keys_to_check = list(response_waiters.keys())
            for command_id in keys_to_check:
                waiter_data = response_waiters.get(command_id)
                if not waiter_data: continue

                command_dni = waiter_data.get("dni")
                message_dni = cleaned["fields"].get("dni")
                command = waiter_data["command"]

                # Lógica para resolver la espera si los DNI coinciden O no hay DNI y el bot respondió.
                dni_match = command_dni and command_dni == message_dni
                no_dni_command = not command_dni and not command.startswith(("/dnivaz", "/dnif"))

                if dni_match or no_dni_command:
                    # 🚨 Lógica de acumulación para /dnif (4 fotos esperadas)
                    if command.startswith("/dnif"):
                        waiter_data["messages"].append(msg_obj)
                        if len(waiter_data["messages"]) >= 4:
                            loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"])
                            waiter_data["timer"].cancel()
                            response_waiters.pop(command_id, None)
                            resolved = True
                            break
                            
                    # 🚨 Lógica de acumulación para /dnivaz (2 fotos esperadas)
                    elif command.startswith("/dnivaz"):
                        waiter_data["messages"].append(msg_obj)
                        if len(waiter_data["messages"]) >= 2:
                            loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"])
                            waiter_data["timer"].cancel()
                            response_waiters.pop(command_id, None)
                            resolved = True
                            break
                            
                    # Lógica de finalización para otros comandos (1 mensaje esperado)
                    else:
                        waiter_data["messages"].append(msg_obj)
                        loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"][0])
                        waiter_data["timer"].cancel()
                        response_waiters.pop(command_id, None)
                        resolved = True
                        break

        # 4. Agregar a la cola de historial si no se usó para una respuesta específica
        if not resolved:
            with _messages_lock:
                messages.appendleft(msg_obj)

    except Exception:
        traceback.print_exc() 

client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))

# --- Función Central para Llamadas API (Comandos) ---

async def _call_api_command(command: str, timeout: int = 25):
    """Envía un comando al bot y espera la respuesta(s), con lógica de respaldo."""
    if not await client.is_user_authorized():
        raise Exception("Cliente no autorizado. Por favor, inicie sesión.")

    command_id = time.time() # ID temporal
    
    # Extraer DNI del comando si existe
    dni_match = re.search(r"/\w+\s+(\d{8})", command)
    dni = dni_match.group(1) if dni_match else None
    
    # ----------------------------------------------------------------------
    # Lógica de Intento 1: Bot Principal
    # ----------------------------------------------------------------------
    
    current_bot_id = LEDERDATA_BOT_ID
    
    for attempt in range(1, 3): # Intentamos con el bot principal, y luego con el de respaldo si falla por timeout.
        future = loop.create_future()
        
        waiter_data = {
            "future": future,
            "messages": [],
            "dni": dni,
            "command": command,
            "timer": None, 
            "sent_to_bot": current_bot_id
        }
        
        # Usamos un tiempo de espera más corto para el primer intento, 
        # y el timeout total para el segundo (si se usa el de respaldo).
        current_timeout = timeout if attempt == 1 else TIMEOUT_TOTAL
        
        # Función de timeout para el Future
        def _on_timeout():
            with _messages_lock:
                waiter_data = response_waiters.pop(command_id, None)
                if waiter_data and not waiter_data["future"].done():
                    # Si falla el intento 1 por timeout, el resultado será un error de timeout.
                    # Si falla el intento 2 (respaldo), el resultado será un error final.
                    loop.call_soon_threadsafe(
                        waiter_data["future"].set_result, 
                        {"status": "error_timeout", "message": f"Tiempo de espera de respuesta agotado ({current_timeout}s) para el comando: {command}.", "bot": current_bot_id}
                    )

        # Establecer el timer de timeout en el loop de Telethon
        waiter_data["timer"] = loop.call_later(current_timeout, _on_timeout)

        with _messages_lock:
            response_waiters[command_id] = waiter_data

        print(f"📡 Enviando comando ({attempt}/2) a {current_bot_id}: {command}")
        
        try:
            # 2. Enviar el mensaje al bot
            await client.send_message(current_bot_id, command)
            
            # 3. Esperar la respuesta (o lista de respuestas)
            result = await future
            
            # Si el resultado es un timeout del intento 1, pasamos al intento 2.
            if isinstance(result, dict) and result.get("status") == "error_timeout" and attempt == 1:
                # ----------------------------------------------------------------------
                # Lógica de Intento 2: Bot de Respaldo
                # ----------------------------------------------------------------------
                print(f"⌛ Timeout de {LEDERDATA_BOT_ID}. Intentando con {LEDERDATA_BACKUP_BOT_ID}.")
                current_bot_id = LEDERDATA_BACKUP_BOT_ID
                # El bucle for continuará a la siguiente iteración (attempt=2)
                continue 
            
            # Si la respuesta es un mensaje de error del bot, lo manejamos
            if isinstance(result, dict) and "Por favor, usa el formato correcto" in result.get("message", ""):
                 return {"status": "error_bot_format", "message": result.get("message"), "bot": current_bot_id}

            # 🚨 Manejar el caso de /dnif para consolidar en un solo JSON
            if isinstance(result, list) and command.startswith("/dnif"):
                expected_count = 4
                if len(result) < expected_count:
                    return {"status": "error", "message": f"Solo se recibió {len(result)} de {expected_count} partes para /dnif. Puede haber expirado el tiempo de espera.", "bot": current_bot_id}

                final_result = result[0].copy() 
                final_result["full_response"] = [] 
                
                consolidated_urls = {} 
                
                for msg in result:
                    final_result["full_response"].append(msg["message"])
                    
                    for url_obj in msg.get("urls", []):
                        photo_type = msg["fields"].get("photo_type")
                        if photo_type:
                            key = photo_type
                            if key in consolidated_urls:
                                 key = f"{photo_type}_2" if photo_type in ["huella", "huella_2"] else f"{photo_type}_1" 
                                 if key in consolidated_urls: key = f"{key}_dup" 
                            
                            consolidated_urls[key.upper()] = url_obj["url"]
                        else:
                            consolidated_urls[f"UNKNOWN_PHOTO_{len(consolidated_urls)+1}"] = url_obj["url"]

                    if not final_result["fields"].get("dni") and msg["fields"].get("dni"):
                        final_result["fields"] = msg["fields"]

                final_result["urls"] = consolidated_urls 
                final_result["message"] = " | ".join(final_result["full_response"])
                final_result.pop("full_response")
                final_result["bot"] = current_bot_id
                return final_result

            # 🚨 Manejar el caso de /dnivaz para consolidar en un solo JSON
            elif isinstance(result, list) and command.startswith("/dnivaz"):
                if len(result) < 2:
                    return {"status": "error", "message": f"Solo se recibió {len(result)} de 2 partes para /dnivaz. Puede haber expirado el tiempo de espera.", "bot": current_bot_id}

                final_result = result[0].copy() 
                final_result["full_response"] = [] 
                
                consolidated_urls = {} 
                
                for msg in result:
                    final_result["full_response"].append(msg["message"])
                    
                    for url_obj in msg.get("urls", []):
                        photo_type = msg["fields"].get("photo_type")
                        if photo_type:
                            consolidated_urls[photo_type.upper()] = url_obj["url"]
                        else:
                            consolidated_urls[f"UNKNOWN_DNI_VIRTUAL_{len(consolidated_urls)+1}"] = url_obj["url"]

                    if not final_result["fields"].get("dni") and msg["fields"].get("dni"):
                        final_result["fields"] = msg["fields"]

                final_result["urls"] = consolidated_urls 
                final_result["message"] = " | ".join(final_result["full_response"])
                final_result.pop("full_response")
                final_result["bot"] = current_bot_id
                return final_result
                
            # Para todos los demás comandos, el resultado es un único mensaje (dict)
            result["bot"] = current_bot_id
            return result
            
        except Exception as e:
            # Si hay un error, el futuro se canceló o falló.
            error_msg = f"Error de Telethon/conexión/fallo: {str(e)}"
            if attempt == 1:
                print(f"❌ Error en {LEDERDATA_BOT_ID}: {error_msg}. Intentando con {LEDERDATA_BACKUP_BOT_ID}.")
                current_bot_id = LEDERDATA_BACKUP_BOT_ID
                # El bucle for continuará a la siguiente iteración (attempt=2)
                continue
            else:
                # Si falla el intento 2, retornamos el error final.
                return {"status": "error", "message": error_msg, "bot": current_bot_id}
        finally:
            # 4. Limpieza: Asegurar que el Future y el Timer se eliminen si no se hizo en _on_new_message
            with _messages_lock:
                if command_id in response_waiters:
                    waiter_data = response_waiters.pop(command_id, None)
                    if waiter_data and waiter_data["timer"]:
                        waiter_data["timer"].cancel()

    # Si se llegó aquí es porque el loop terminó después de los 2 intentos.
    return {"status": "error", "message": f"Falló la consulta después de 2 intentos (Bot principal y de respaldo). Último intento con {current_bot_id} agotó el tiempo de espera.", "bot": current_bot_id}


# --- Rutina de reconexión / ping ---

async def _ensure_connected():
    """Mantiene la conexión y autorización activa."""
    while True:
        try:
            if not client.is_connected():
                print("🔌 Intentando reconectar Telethon...")
                await client.connect()
                # Intentar obtener la entidad de ambos bots después de la reconexión
                await client.get_entity(LEDERDATA_BOT_ID) 
                await client.get_entity(LEDERDATA_BACKUP_BOT_ID) 
                print("✅ Reconexión exitosa.")
            elif not await client.is_user_authorized():
                 print("⚠️ Telethon conectado, pero no autorizado.")
            # Un ping simple para mantener viva la conexión
            await client.get_dialogs(limit=1) 

        except Exception:
            traceback.print_exc()
        await asyncio.sleep(300) # Dormir 5 minutos

asyncio.run_coroutine_threadsafe(_ensure_connected(), loop)

# --- Rutas HTTP Base (Login/Status/General) ---

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Gateway API para LEDER DATA Bot activo. Consulta /status para la sesión.",
    })

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
    
    return jsonify({
        "authorized": bool(is_auth),
        "pending_phone": pending_phone["phone"],
        "session_loaded": True if SESSION_STRING else False,
        "session_string": current_session,
    })

@app.route("/login")
def login():
    phone = request.args.get("phone")
    if not phone: return jsonify({"error": "Falta parámetro phone"}), 400

    async def _send_code():
        await client.connect()
        if await client.is_user_authorized(): return {"status": "already_authorized"}
        try:
            await client.send_code_request(phone)
            pending_phone["phone"] = phone
            pending_phone["sent_at"] = datetime.utcnow().isoformat()
            return {"status": "code_sent", "phone": phone}
        except Exception as e: return {"status": "error", "error": str(e)}

    result = run_coro(_send_code())
    return jsonify(result)

@app.route("/code")
def code():
    code = request.args.get("code")
    if not code: return jsonify({"error": "Falta parámetro code"}), 400
    if not pending_phone["phone"]: return jsonify({"error": "No hay login pendiente"}), 400

    phone = pending_phone["phone"]
    async def _sign_in():
        try:
            await client.sign_in(phone, code)
            await client.start()
            pending_phone["phone"] = None
            pending_phone["sent_at"] = None
            new_string = client.session.save()
            return {"status": "authenticated", "session_string": new_string}
        except errors.SessionPasswordNeededError: return {"status": "error", "error": "2FA requerido"}
        except Exception as e: return {"status": "error", "error": str(e)}

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
        return jsonify({
            "message": "found data" if data else "no data",
            "result": {"quantity": len(data), "coincidences": data},
        })

# 🚨 CAMBIO CRUCIAL: Agregar as_attachment=True para forzar la descarga
@app.route("/files/<path:filename>")
def files(filename):
    """
    Ruta para descargar archivos. Se añade as_attachment=True para forzar la descarga 
    en lugar de visualizar el archivo, lo que es ideal para appcreator24.
    """
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

# ----------------------------------------------------------------------
# --- Rutas HTTP de API (Comandos LEDER DATA) ----------------------------
# ----------------------------------------------------------------------

# --- 1. Handlers para comandos basados en DNI (8 dígitos) o 1 parámetro simple ---

@app.route("/dni", methods=["GET"])
@app.route("/dnif", methods=["GET"]) # 🚨 Este es el comando que ahora espera 4 fotos
@app.route("/dnidb", methods=["GET"])
@app.route("/dnifdb", methods=["GET"])
@app.route("/c4", methods=["GET"])
@app.route("/dnivaz", methods=["GET"]) # 🚨 Este es el comando que ahora espera 2 fotos
@app.route("/dnivam", methods=["GET"])
@app.route("/dnivel", methods=["GET"])
@app.route("/dniveln", methods=["GET"])
@app.route("/fa", methods=["GET"])
@app.route("/fadb", methods=["GET"])
@app.route("/fb", methods=["GET"])
@app.route("/fbdb", methods=["GET"])
@app.route("/cnv", methods=["GET"])
@app.route("/cdef", methods=["GET"])
@app.route("/antpen", methods=["GET"])
@app.route("/antpol", methods=["GET"])
@app.route("/antjud", methods=["GET"])
@app.route("/actancc", methods=["GET"])
@app.route("/actamcc", methods=["GET"])
@app.route("/actadcc", methods=["GET"])
@app.route("/osiptel", methods=["GET"])
@app.route("/claro", methods=["GET"])
@app.route("/entel", methods=["GET"])
@app.route("/pro", methods=["GET"]) # SUNARP
@app.route("/sen", methods=["GET"]) # SENTINEL
@app.route("/sbs", methods=["GET"]) # SBS
@app.route("/tra", methods=["GET"]) # TRABAJOS
@app.route("/tremp", methods=["GET"]) # TRABAJADORES POR EMPRESA
@app.route("/sue", methods=["GET"]) # SUELDOS
@app.route("/cla", methods=["GET"]) # CONSTANCIA DE LOGROS
@app.route("/sune", methods=["GET"]) # TITULOS UNIVERSITARIOS
@app.route("/cun", methods=["GET"]) # CARNET UNIVERSITARIO
@app.route("/colp", methods=["GET"]) # COLEGIADOS
@app.route("/mine", methods=["GET"]) # TITULOS INSTITUTOS
@app.route("/pasaporte", methods=["GET"]) # PASAPORTE
@app.route("/seeker", methods=["GET"]) # SEEKER
@app.route("/afp", methods=["GET"]) # AFPS
@app.route("/bdir", methods=["GET"]) # DIRECCION INVERSA
@app.route("/antpenv", methods=["GET"]) # ANTECEDENTES PENALES VERIFICADOR
@app.route("/dend", methods=["GET"]) # DENUNCIAS POLICIALES (DNI)
@app.route("/dence", methods=["GET"]) # DENUNCIAS POLICIALES (CE)
@app.route("/denpas", methods=["GET"]) # DENUNCIAS POLICIALES (PASAPORTE)
def api_dni_based_command():
    """
    Maneja comandos que solo requieren un DNI o un parámetro simple.
    El parámetro se espera bajo el nombre 'query'. Para RENIEC es 'dni'.
    """
    
    command_name = request.path.lstrip('/') 
    
    # Parámetro genérico. Se usa 'dni' para RENIEC, 'query' para /tel, etc.
    dni_required_commands = ["dni", "dnif", "dnidb", "dnifdb", "c4", "dnivaz", "dnivam", "dnivel", "dniveln", "fa", "fadb", "fb", "fbdb", "cnv", "cdef", "antpen", "antpol", "antjud"]
    
    if command_name in dni_required_commands:
        param = request.args.get("dni")
        if not param or not param.isdigit() or len(param) != 8:
            return jsonify({"status": "error", "message": f"Parámetro 'dni' es requerido y debe ser un número de 8 dígitos para /{command_name}."}), 400
    elif command_name in ["tel", "telp", "cor", "antpenv", "dend", "dence", "denpas", "nmv", "cedula"]:
        param = request.args.get("query")
        if not param and command_name not in ["osiptel", "claro", "entel", "seeker", "bdir", "pasaporte"]:
            return jsonify({"status": "error", "message": f"Parámetro 'query' es requerido para /{command_name}."}), 400
    elif command_name in ["ce"]:
        param = request.args.get("ce")
        if not param:
            return jsonify({"status": "error", "message": f"Parámetro 'ce' es requerido para /{command_name}."}), 400
    else:
        # Comandos que pueden ir sin parámetro (ej: /osiptel, /seeker, etc.)
        param_dni = request.args.get("dni")
        param_query = request.args.get("query")
        param = param_dni or param_query or ""
        
    # Construir comando
    command = f"/{command_name} {param}".strip() # strip() elimina espacio si param está vacío
    
    # Ejecutar comando
    try:
        # Usamos el timeout más corto para el primer intento
        timeout_primary = TIMEOUT_PRIMARY_BOT_FAILOVER 
        if command_name in ["dnivaz", "dnif"]:
             # Damos más tiempo al bot primario si tiene descargas
             timeout_primary = 30
        
        result = run_coro(_call_api_command(command, timeout=timeout_primary))
        
        if result.get("status", "").startswith("error"):
            # Si el error es un timeout o de telethon, devolvemos 500, sino 400
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            status_code = 500 if is_timeout_or_connection_error else 400
            return jsonify(result), status_code
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno: {str(e)}"}), 500

# --- 2. Handler dedicado para Nombres (Formato complejo) ---

@app.route("/dni_nombres", methods=["GET"])
def api_dni_nombres():
    """Maneja la consulta por nombres: /nm nombres(s)|apellidopaterno|apellidomaterno"""
    
    nombres = unquote(request.args.get("nombres", "")).strip()
    ape_paterno = unquote(request.args.get("apepaterno", "")).strip()
    ape_materno = unquote(request.args.get("apematerno", "")).strip()

    if not ape_paterno or not ape_materno:
        return jsonify({"status": "error", "message": "Faltan parámetros: 'apepaterno' y 'apematerno' son obligatorios."}), 400

    # 1. Formatear Nombres: reemplazar espacios con "," (si hay más de 1 palabra)
    formatted_nombres = nombres.replace(" ", ",")
    
    # 2. Formatear Apellidos: reemplazar espacios con "+"
    formatted_apepaterno = ape_paterno.replace(" ", "+")
    formatted_apematerno = ape_materno.replace(" ", "+")

    # 3. Construir comando
    command = f"/nm {formatted_nombres}|{formatted_apepaterno}|{formatted_apematerno}"
    
    # 4. Ejecutar comando
    try:
        result = run_coro(_call_api_command(command, timeout=TIMEOUT_PRIMARY_BOT_FAILOVER))
        if result.get("status", "").startswith("error"):
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            return jsonify(result), 500 if is_timeout_or_connection_error else 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno: {str(e)}"}), 500

# --- 3. Handler dedicado para Venezolanos Nombres (Formato nombres simples) ---

@app.route("/venezolanos_nombres", methods=["GET"])
def api_venezolanos_nombres():
    """Maneja la consulta por nombres venezolanos: /nmv <nombres_apellidos>"""
    
    query = unquote(request.args.get("query", "")).strip()
    
    if not query:
        return jsonify({"status": "error", "message": "Parámetro 'query' (nombres_apellidos) es requerido para /venezolanos_nombres."}), 400

    # Se asume que /nmv toma una cadena simple de nombres/apellidos
    command = f"/nmv {query}"
    
    try:
        result = run_coro(_call_api_command(command, timeout=TIMEOUT_PRIMARY_BOT_FAILOVER))
        if result.get("status", "").startswith("error"):
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            return jsonify(result), 500 if is_timeout_or_connection_error else 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno: {str(e)}"}), 500
        
# ----------------------------------------------------------------------
# --- Inicio de la Aplicación ------------------------------------------
# ----------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_coro(client.connect())
        # Esto ayuda a Telethon a resolver la entidad de ambos bots al inicio
        run_coro(client.get_entity(LEDERDATA_BOT_ID)) 
        run_coro(client.get_entity(LEDERDATA_BACKUP_BOT_ID)) 
    except Exception:
        pass
    print(f"🚀 App corriendo en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
