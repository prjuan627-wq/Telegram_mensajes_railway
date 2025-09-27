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

# El chat ID/nombre del bot al que enviar los comandos
LEDERDATA_BOT_ID = "@LEDERDATA_OFC_BOT" 

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
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    # Espera hasta 30 segundos más allá del timeout de la API
    return fut.result(timeout=35) 

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
# {command_id: {"future": asyncio.Future, "messages": list, "timer": asyncio.TimerHandle}}
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
    dni_match = re.search(r"DNI\s*:\s*(\d+)", text, re.IGNORECASE)
    if dni_match: fields["dni"] = dni_match.group(1)
    
    return {"text": text, "fields": fields}

# --- Handler de nuevos mensajes ---

async def _on_new_message(event):
    """Intercepta mensajes y resuelve las esperas de API si aplica."""
    try:
        # 1. Verificar si el mensaje viene del bot LEDER DATA
        try:
            bot_entity = await client.get_entity(LEDERDATA_BOT_ID)
            if event.sender_id != bot_entity.id:
                return # Ignorar mensajes que no sean del bot
        except Exception:
            pass

        raw_text = event.raw_text or ""
        cleaned = clean_and_extract(raw_text)

        msg_obj = {
            "chat_id": getattr(event, "chat_id", None),
            "from_id": event.sender_id,
            "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
            "message": cleaned["text"],
            "fields": cleaned["fields"],
            "urls": [] # Ahora es una lista
        }
        
        # 2. Manejar archivos (media): Descarga TODOS los archivos adjuntos
        if getattr(event, "message", None) and getattr(event.message, "media", None):
            media_list = []
            if isinstance(event.message.media, (MessageMediaDocument, MessageMediaPhoto)):
                media_list.append(event.message.media)
            elif hasattr(event.message.media, 'webpage') and event.message.media.webpage and hasattr(event.message.media.webpage, 'photo'):
                 # Esto podría ser una imagen de preview, la ignoramos o la manejamos si es necesario.
                 pass
            
            # Buscar otros archivos si el mensaje es un álbum o tiene más de un archivo.
            # Telethon normalmente maneja los álbumes con un solo evento de mensaje.
            
            if media_list or event.message.grouped_id:
                try:
                    # Usar datetime.now(timezone.utc) para un nombre de archivo consistente
                    timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                    
                    # Si hay un grouped_id (álbum), el evento puede contener todos los mensajes
                    # Pero en la práctica, para un bot, cada foto/archivo suele ser un evento NewMessage.
                    # El siguiente código está optimizado para un solo archivo por mensaje,
                    # pero maneja cualquier media que se encuentre.
                    
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
                        
                        unique_filename = f"{timestamp_str}_{event.message.id}{dni_part}_{i}{file_ext}"
                        
                        # Descargar el medio
                        saved_path = await client.download_media(event.message, file=os.path.join(DOWNLOAD_DIR, unique_filename))
                        filename = os.path.basename(saved_path)
                        msg_obj["urls"].append(f"{PUBLIC_URL}/files/{filename}")
                        
                        # Manejo especial para /dnivaz (anverso/reverso)
                        if "DNI VIRTUAL AZUL" in raw_text:
                            if "ANVERSO" not in raw_text.upper() and "REVERSO" not in raw_text.upper():
                                # Intentar adivinar, si el bot no lo pone
                                side_match = re.search(r"(ANVERSO|REVERSO)", unique_filename.upper())
                                if side_match:
                                    msg_obj["urls"][-1] = {"url": msg_obj["urls"][-1], "side": side_match.group(1)}
                                else:
                                    msg_obj["urls"][-1] = {"url": msg_obj["urls"][-1], "side": f"UNKNOWN_{i+1}"}
                            
                except Exception as e:
                    msg_obj["media_error"] = str(e)
                    
        # 3. Intentar resolver la espera de la API
        resolved = False
        with _messages_lock:
            # Iterar sobre las esperas activas. Solo hay una, pero por seguridad.
            keys_to_check = list(response_waiters.keys())
            for command_id in keys_to_check:
                waiter_data = response_waiters.get(command_id)
                if not waiter_data: continue

                # Condición de resolución: El DNI en el mensaje coincide con el DNI del comando.
                # Para comandos sin DNI (ej. /tel) se debe confiar en el orden de llegada.
                command_dni = waiter_data.get("dni")
                message_dni = cleaned["fields"].get("dni")
                
                # Para /dnivaz, es crucial obtener ambos. Usaremos un timeout corto.
                if command_dni and command_dni == message_dni:
                    waiter_data["messages"].append(msg_obj)
                    
                    # Lógica de finalización para DNI VIRTUAL AZUL (2 mensajes esperados)
                    if len(waiter_data["messages"]) >= 2 and waiter_data["command"].startswith("/dnivaz"):
                        loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"])
                        waiter_data["timer"].cancel() # Cancelar el timer de timeout
                        response_waiters.pop(command_id, None)
                        resolved = True
                        break
                        
                    # Lógica de finalización para otros comandos (1 mensaje esperado)
                    elif not waiter_data["command"].startswith("/dnivaz") and len(waiter_data["messages"]) >= 1:
                        loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"][0])
                        waiter_data["timer"].cancel() # Cancelar el timer de timeout
                        response_waiters.pop(command_id, None)
                        resolved = True
                        break
                        
                # Si no hay DNI, asumimos que el primer mensaje es la respuesta
                elif not command_dni and not waiter_data["command"].startswith("/dnivaz"):
                    waiter_data["messages"].append(msg_obj)
                    loop.call_soon_threadsafe(waiter_data["future"].set_result, waiter_data["messages"][0])
                    waiter_data["timer"].cancel() # Cancelar el timer de timeout
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
    """Envía un comando al bot y espera la respuesta(s)."""
    if not await client.is_user_authorized():
        raise Exception("Cliente no autorizado. Por favor, inicie sesión.")

    # 1. Crear un Future para esperar la respuesta
    command_id = time.time() # ID temporal
    future = loop.create_future()
    
    # Extraer DNI del comando si existe
    dni_match = re.search(r"/\w+\s+(\d{8})", command)
    dni = dni_match.group(1) if dni_match else None
    
    waiter_data = {
        "future": future,
        "messages": [],
        "dni": dni,
        "command": command,
        "timer": None # Se asigna después
    }
    
    # Función de timeout para el Future
    def _on_timeout():
        with _messages_lock:
            waiter_data = response_waiters.pop(command_id, None)
            if waiter_data and not waiter_data["future"].done():
                loop.call_soon_threadsafe(
                    waiter_data["future"].set_result, 
                    {"status": "error", "message": f"Tiempo de espera de respuesta agotado ({timeout}s) para el comando: {command}."}
                )

    # Establecer el timer de timeout en el loop de Telethon
    waiter_data["timer"] = loop.call_later(timeout, _on_timeout)

    with _messages_lock:
        response_waiters[command_id] = waiter_data

    try:
        # 2. Enviar el mensaje al bot
        await client.send_message(LEDERDATA_BOT_ID, command)
        
        # 3. Esperar la respuesta (o lista de respuestas)
        result = await future
        
        # Si la respuesta es un mensaje de error del bot, lo manejamos
        if isinstance(result, dict) and "Por favor, usa el formato correcto" in result.get("message", ""):
             return {"status": "error_bot_format", "message": result.get("message")}
        
        # Manejar el caso de /dnivaz para consolidar en un solo JSON
        if isinstance(result, list) and command.startswith("/dnivaz"):
            # Deberían ser 2 mensajes. Si es 1 o 0, el timeout ya lo manejó o está incompleto.
            if len(result) < 2:
                return {"status": "error", "message": f"Solo se recibió {len(result)} de 2 partes para /dnivaz. Puede haber expirado el tiempo de espera."}

            final_result = result[0].copy() # Clonar el primer resultado
            final_result["full_response"] = [] # Para el texto completo
            final_result["urls"] = [] # Para las URLs consolidadas
            
            # Recorrer los mensajes y consolidar
            for msg in result:
                final_result["full_response"].append(msg["message"])
                final_result["urls"].extend(msg["urls"])
                
                # Se espera que el primer mensaje tenga los campos (DNI, Nombres, etc.)
                # Pero si no los tiene, los copiamos del que los tenga.
                if not final_result["fields"].get("dni") and msg["fields"].get("dni"):
                    final_result["fields"] = msg["fields"]

            final_result["message"] = " | ".join(final_result["full_response"])
            final_result.pop("full_response")
            return final_result
            
        # Para todos los demás comandos, el resultado es un único mensaje (dict)
        return result
        
    except Exception as e:
        # Si hay un error, el futuro se canceló o falló.
        return {"status": "error", "message": f"Error de Telethon/conexión/timeout: {str(e)}"}
    finally:
        # 4. Limpieza: Asegurar que el Future y el Timer se eliminen si no se hizo en _on_new_message
        with _messages_lock:
            if command_id in response_waiters:
                waiter_data = response_waiters.pop(command_id, None)
                if waiter_data and waiter_data["timer"]:
                    waiter_data["timer"].cancel()


# --- Rutina de reconexión / ping ---

async def _ensure_connected():
    """Mantiene la conexión y autorización activa."""
    while True:
        try:
            if not client.is_connected():
                print("🔌 Intentando reconectar Telethon...")
                await client.connect()
                # Intentar obtener la entidad del bot de nuevo después de la reconexión
                await client.get_entity(LEDERDATA_BOT_ID) 
                print("✅ Reconexión exitosa.")
            elif not await client.is_user_authorized():
                 print("⚠️ Telethon conectado, pero no autorizado.")
            # Un ping simple para mantener viva la conexión
            await client.get_dialogs(limit=1) 

        except Exception:
            tracebox.print_exc()
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

@app.route("/files/<path:filename>")
def files(filename):
    """
    Ruta para descargar archivos. Se añade as_attachment=True para forzar la descarga.
    Esta es la solución del backend para la descarga automática.
    """
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)

# ----------------------------------------------------------------------
# --- Rutas HTTP de API (Comandos LEDER DATA) ----------------------------
# ----------------------------------------------------------------------

# --- 1. Handlers para comandos basados en DNI (8 dígitos) o 1 parámetro simple ---

@app.route("/dni", methods=["GET"])
@app.route("/dnif", methods=["GET"])
@app.route("/dnidb", methods=["GET"])
@app.route("/dnifdb", methods=["GET"])
@app.route("/c4", methods=["GET"])
@app.route("/dnivaz", methods=["GET"])
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
    if command_name in ["dni", "dnif", "dnidb", "dnifdb", "c4", "dnivaz", "dnivam", "dnivel", "dniveln", "fa", "fadb", "fb", "fbdb", "cnv", "cdef", "antpen", "antpol", "antjud"]:
        param = request.args.get("dni")
        if not param or not param.isdigit() or len(param) != 8:
            return jsonify({"status": "error", "message": f"Parámetro 'dni' es requerido y debe ser un número de 8 dígitos para /{command_name}."}), 400
    elif command_name in ["tel", "telp", "cor", "antpenv", "dend", "dence", "denpas", "nmv", "cedula"]:
        param = request.args.get("query")
        # En /dence y /denpas el bot espera un documento, si no se pasa, puede fallar el comando.
        # Asumiremos que 'query' es el parámetro para todos ellos.
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
        # Aumentar el timeout para /dnivaz y /dnif (más tráfico, más tiempo de descarga)
        timeout = 30 if command_name in ["dnivaz", "dnif"] else 25
        result = run_coro(_call_api_command(command, timeout=timeout))
        
        if result.get("status", "").startswith("error"):
            # Devolver 500 si hay un error de conexión/timeout, 400 para error de formato del bot
            status_code = 500 if "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() else 400
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
        result = run_coro(_call_api_command(command))
        if result.get("status", "").startswith("error"):
            return jsonify(result), 500
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
        result = run_coro(_call_api_command(command))
        if result.get("status", "").startswith("error"):
            return jsonify(result), 500
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno: {str(e)}"}), 500
        
# ----------------------------------------------------------------------
# --- Inicio de la Aplicación ------------------------------------------
# ----------------------------------------------------------------------

if __name__ == "__main__":
    try:
        run_coro(client.connect())
        # Esto ayuda a Telethon a resolver la entidad del bot al inicio
        run_coro(client.get_entity(LEDERDATA_BOT_ID)) 
    except Exception:
        pass
    print(f"🚀 App corriendo en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
