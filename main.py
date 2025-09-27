import os
import re
import asyncio
import threading
import traceback
import time
from collections import deque
from datetime import datetime
from urllib.parse import unquote # Para decodificar URL
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession
from telethon.tl.types import PeerUser

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
    return fut.result()

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
# Diccionario para esperar respuestas específicas: {command_id: asyncio.Future}
response_waiters = {} 

# Login pendiente
pending_phone = {"phone": None, "sent_at": None}

# --- Lógica de Limpieza y Extracción de Datos ---

def clean_and_extract(raw_text: str):
    """Limpia el texto de cabeceras/pies y extrae campos clave."""
    if not raw_text:
        return {"text": "", "fields": {}}

    text = raw_text
    # 1. Eliminar cabecera (patrón más robusto)
    header_pattern = r"^\[\#LEDER\_BOT\].*?==============================\s*"
    text = re.sub(header_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 2. Eliminar pie (patrón más robusto para créditos/paginación/warnings al final)
    footer_pattern = r"((\r?\n){1,2}\[|Página\s*\d+\/\d+.*|(\r?\n){1,2}Por favor, usa el formato correcto.*|↞ Anterior|Siguiente ↠.*|Credits\s*:.+|Wanted for\s*:.+)"
    text = re.sub(footer_pattern, "", text, flags=re.IGNORECASE | re.DOTALL)
    
    # 3. Limpiar espacios
    text = text.strip()

    # 4. Extraer datos clave (ajustar si el bot devuelve un formato diferente)
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
            # Si no se encuentra la entidad, puede fallar si el bot nunca ha enviado un mensaje.
            # Se permite continuar si el sender_id parece ser un usuario o canal.
            pass

        raw_text = event.raw_text or ""
        cleaned = clean_and_extract(raw_text)

        msg_obj = {
            "chat_id": getattr(event, "chat_id", None),
            "from_id": event.sender_id,
            "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
            "message": cleaned["text"],
            "fields": cleaned["fields"],
        }
        
        # 2. Manejar archivos (media)
        if getattr(event, "message", None) and getattr(event.message, "media", None):
            try:
                unique_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{event.message.id}_{event.message.file.name or 'file'}"
                saved_path = await event.download_media(file=os.path.join(DOWNLOAD_DIR, unique_filename))
                filename = os.path.basename(saved_path)
                msg_obj["url"] = f"{PUBLIC_URL}/files/{filename}"
            except Exception as e:
                msg_obj["media_error"] = str(e)
        
        # 3. Intentar resolver la espera de la API
        resolved = False
        with _messages_lock:
            # Asumimos que el último Future creado está esperando esta respuesta
            if len(response_waiters) > 0:
                command_id_to_resolve = list(response_waiters.keys())[0] 
                future = response_waiters.pop(command_id_to_resolve, None)
                if future and not future.done():
                    # Usamos call_soon_threadsafe para resolver el Future en el thread de Telethon
                    loop.call_soon_threadsafe(future.set_result, msg_obj)
                    resolved = True
        
        # 4. Agregar a la cola de historial si no se usó para una respuesta específica
        if not resolved:
            with _messages_lock:
                messages.appendleft(msg_obj)
                # print("📥 Nuevo mensaje de bot (historial):", msg_obj)

    except Exception:
        traceback.print_exc() 

client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))

# --- Función Central para Llamadas API (Comandos) ---

async def _call_api_command(command: str, timeout: int = 25):
    """Envía un comando al bot y espera la respuesta."""
    if not await client.is_user_authorized():
        raise Exception("Cliente no autorizado. Por favor, inicie sesión.")

    # print(f"📡 Enviando comando a {LEDERDATA_BOT_ID}: {command}")
    
    # 1. Crear un Future para esperar la respuesta
    command_id = time.time() # ID temporal
    future = loop.create_future()
    
    with _messages_lock:
        response_waiters[command_id] = future # Agregar al diccionario de espera

    try:
        # 2. Enviar el mensaje al bot
        await client.send_message(LEDERDATA_BOT_ID, command)
        
        # 3. Esperar la respuesta
        result = await asyncio.wait_for(future, timeout=timeout)
        # print("✅ Comando ejecutado, respuesta recibida.")
        
        # Si la respuesta es un mensaje de error del bot, lo manejamos
        if isinstance(result, dict) and "Por favor, usa el formato correcto" in result.get("message", ""):
             return {"status": "error_bot_format", "message": result.get("message")}
        
        return result
        
    except asyncio.TimeoutError:
        # print(f"❌ Tiempo de espera agotado ({timeout}s) para el comando: {command}")
        return {"status": "error", "message": f"Tiempo de espera de respuesta agotado ({timeout}s)."}
    except Exception as e:
        # print(f"❌ Error al ejecutar comando: {e}")
        return {"status": "error", "message": f"Error de Telethon/conexión: {str(e)}"}
    finally:
        # 4. Limpieza: Asegurar que el Future se elimine
        with _messages_lock:
            response_waiters.pop(command_id, None)

# --- Rutina de reconexión / ping ---

async def _ensure_connected():
    """Mantiene la conexión y autorización activa."""
    while True:
        try:
            if not client.is_connected():
                await client.connect()
                # print("🔌 Reconectando Telethon...")
            
            # if not await client.is_user_authorized():
                 # print("⚠️ Cliente no autorizado o desconectado. Necesita iniciar sesión.")

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
        "endpoints_doc": "Revisa tu documentación para la lista completa de endpoints.",
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
    # ... (código de login, no modificado) ...
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
    # ... (código de code, no modificado) ...
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
    # ESTA RUTA ES PARA MENSAJES MANUALES. NO RECOMENDADA PARA LA API.
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
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False)

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
        if not param:
            return jsonify({"status": "error", "message": f"Parámetro 'query' es requerido para /{command_name}."}), 400
    elif command_name in ["ce"]:
        param = request.args.get("ce")
        if not param:
            return jsonify({"status": "error", "message": f"Parámetro 'ce' es requerido para /{command_name}."}), 400
    else:
        # Otros comandos que asumen DNI por defecto o simplemente necesitan un parámetro.
        # Por simplicidad, asumiremos que usan 'dni' o 'query' si lo necesitan
        # y si no se pasan, el comando se envía sin parámetros (ej: /osiptel, /seeker, etc. si el bot lo acepta).
        param_dni = request.args.get("dni")
        param_query = request.args.get("query")
        param = param_dni or param_query or ""
        
    # Construir comando
    command = f"/{command_name} {param}".strip() # strip() elimina espacio si param está vacío
    
    # Ejecutar comando
    try:
        result = run_coro(_call_api_command(command))
        if result.get("status", "").startswith("error"):
            return jsonify(result), 500
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
