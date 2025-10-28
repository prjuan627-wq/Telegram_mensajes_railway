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
from telethon.errors.rpcerrorlist import UserBlockedError # Importar el error específico

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

# ID/Nombre del canal de películas (Necesitas ser miembro)
# Usando el nombre corto que se ve en el link: '+qE1c6fb3l0w2ODlh'
MOVIE_CHANNEL_ENTITY = "c/1507924325" # Si usas el ID numérico es mejor, si no, el username o el hash
# Usaremos el hash/ID numérico para Telethon si la sesión de usuario ya se ha unido. 
# Si el hash es 'qE1c6fb3l0w2ODlh', en la URL de 't.me/+' se resuelve.
# Para Telethon, se puede usar el ID numérico largo (con o sin el prefijo -100).
MOVIE_CHANNEL_ID_FOR_TELETHON = -1001507924325 
MOVIE_CHANNEL_NAME = "𝗘𝗹 𝗖𝗶𝗻𝗲́𝗳𝗶𝗹𝗼 𝘗𝘦𝘭𝘪𝘤𝘶𝘭𝘢𝘴 𝘊𝘰𝘮𝘱𝘭𝘦𝘵𝘢𝘴"

# Lista de bots para verificar en el handler
ALL_BOT_IDS = [LEDERDATA_BOT_ID, LEDERDATA_BACKUP_BOT_ID]

# Tiempo de espera (en segundos) para el bot principal antes de intentar con el de respaldo.
# ESTE ES EL TIEMPO MÁXIMO PARA RECIBIR *TODOS* LOS MENSAJES DEL BOT PRINCIPAL antes del failover
TIMEOUT_FAILOVER = 25 
# Tiempo de espera total para la llamada a la API. ESTE DEFINE CUANTO ESPERA POR TODOS LOS MENSAJES
# Si el bot de respaldo se usa, tiene 40 segundos.
TIMEOUT_TOTAL = 40 

# --- Manejo de Fallos por Bot (Implementación de tu lógica) ---

# Diccionario para rastrear los fallos por timeout/bloqueo: {bot_id: datetime_of_failure}
bot_fail_tracker = {}
BOT_FAIL_TIMEOUT_HOURS = 6 # Tiempo de bloqueo de 6 horas

def is_bot_blocked(bot_id: str) -> bool:
    """Verifica si el bot está temporalmente bloqueado por fallos previos."""
    last_fail_time = bot_fail_tracker.get(bot_id)
    if not last_fail_time:
        return False

    # Usamos la hora actual para la verificación
    now = datetime.now()
    six_hours_ago = now - timedelta(hours=BOT_FAIL_TIMEOUT_HOURS)

    # Si la última falla fue más reciente que 'six_hours_ago', está bloqueado
    if last_fail_time > six_hours_ago:
        # Imprimir el tiempo restante para depuración
        time_left = last_fail_time + timedelta(hours=BOT_FAIL_TIMEOUT_HOURS) - now
        print(f"🚫 Bot {bot_id} bloqueado. Restan: {time_left}")
        return True
    
    # Si ya pasó el tiempo, eliminamos el registro y permitimos el intento
    print(f"✅ Bot {bot_id} ha cumplido su tiempo de bloqueo. Desbloqueado.")
    bot_fail_tracker.pop(bot_id, None)
    return False

def record_bot_failure(bot_id: str):
    """Registra la hora actual como la última hora de fallo del bot."""
    print(f"🚨 Bot {bot_id} ha fallado y será BLOQUEADO por {BOT_FAIL_TIMEOUT_HOURS} horas.")
    # Usamos datetime.now() para que coincida con la verificación en is_bot_blocked
    bot_fail_tracker[bot_id] = datetime.now()

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
    # Se agrega un margen de 5 segundos al TIMEOUT_TOTAL para la espera externa
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
# {command_id: {"future": asyncio.Future, "messages": list, "dni": str, "command": str, "timer": asyncio.TimerHandle, "sent_to_bot": str, "has_response": bool}}
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
    # NOTA: Se usan 'rostro', 'huella', 'firma', 'adverso', 'reverso' del bot original
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
        if not hasattr(_on_new_message, 'bot_ids'):
            _on_new_message.bot_ids = {}
            # Necesitamos resolver la entidad AQUI. Esto debe ser SÍNCRONO al cargar el handler
            # pero dado que telethon requiere await, se hace la primera vez
            for bot_name in ALL_BOT_IDS:
                try:
                    entity = await client.get_entity(bot_name)
                    _on_new_message.bot_ids[bot_name] = entity.id
                except Exception as e:
                    print(f"Error al obtener entidad para {bot_name}: {e}")


        if event.sender_id in _on_new_message.bot_ids.values():
            sender_is_bot = True
        
        # OJO: Se IGNORA el canal de películas aquí para no interferir con la lógica de bots.
        # Si quisieras monitorear el canal de películas, deberías agregarlo a una lógica SEPARADA
        # para que no interfiera con los comandos de los bots.

        if not sender_is_bot:
            return # Ignorar mensajes que no sean de los bots
            
        raw_text = event.raw_text or ""
        cleaned = clean_and_extract(raw_text)
        
        # Inicializar la lista de URLs para cada mensaje
        msg_urls = []

        # 2. Manejar archivos (media): Descarga TODOS los archivos adjuntos
        if getattr(event, "message", None) and getattr(event.message, "media", None):
            media_list = []
            if isinstance(event.message.media, (MessageMediaDocument, MessageMediaPhoto)):
                media_list.append(event.message.media)
            elif hasattr(event.message.media, 'webpage') and event.message.media.webpage and hasattr(event.message.media.webpage, 'photo'):
                 pass
            
            # Si hay media, proceder a la descarga
            if media_list:
                try:
                    # Usar datetime.now(timezone.utc) para un nombre de archivo consistente
                    timestamp_str = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
                    
                    for i, media in enumerate(media_list):
                        # Obtener la extensión original si es posible, o usar 'file'
                        file_ext = '.file'
                        if hasattr(media, 'document') and hasattr(media.document, 'attributes'):
                            # Intentar obtener la extensión de file_name
                            file_ext = os.path.splitext(getattr(media.document, 'file_name', 'file'))[1]
                        elif isinstance(media, MessageMediaPhoto) or (hasattr(media, 'photo') and media.photo):
                            file_ext = '.jpg' # La foto de Telegram suele ser JPG
                            
                        # Si hay un DNI, lo incluimos en el nombre
                        dni_part = f"_{cleaned['fields'].get('dni')}" if cleaned["fields"].get("dni") else ""
                        
                        # Incluir el tipo de foto/documento en el nombre para depuración
                        type_part = f"_{cleaned['fields'].get('photo_type')}" if cleaned['fields'].get('photo_type') else ""
                        
                        # Usar el ID del mensaje para unicidad
                        unique_filename = f"{timestamp_str}_{event.message.id}{dni_part}{type_part}_{i}{file_ext}"
                        
                        # Descargar el medio
                        saved_path = await client.download_media(event.message, file=os.path.join(DOWNLOAD_DIR, unique_filename))
                        filename = os.path.basename(saved_path)
                        
                        # Estructura de URL mejorada
                        url_obj = {
                            "url": f"{PUBLIC_URL}/files/{filename}", 
                            # Si es un PDF de denuncia de placa, el 'type' será 'file', lo dejamos así
                            "type": cleaned['fields'].get('photo_type', 'file'),
                            "text_context": raw_text.split('\n')[0].strip() # Cabecera del mensaje
                        }
                        msg_urls.append(url_obj)
                        
                except Exception as e:
                    print(f"Error al descargar media: {e}")
        
        msg_obj = {
            "chat_id": getattr(event, "chat_id", None),
            "from_id": event.sender_id,
            "date": event.message.date.isoformat() if getattr(event, "message", None) else datetime.utcnow().isoformat(),
            "message": cleaned["text"],
            "fields": cleaned["fields"],
            "urls": msg_urls # Usar la lista de URLs construida
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
                
                # Coincidencia de DNI o si el comando no es por DNI 
                dni_match = command_dni and command_dni == message_dni
                no_dni_command = not command_dni 
                
                # Lógica simplificada: si el mensaje viene del bot al que se envió (sent_to_bot)
                # O si el comando no es por DNI, solo verificamos que venga de CUALQUIER bot
                sender_bot_name = next((name for name, id_ in _on_new_message.bot_ids.items() if id_ == event.sender_id), None)
                sent_to_match = sender_bot_name and sender_bot_name == waiter_data.get("sent_to_bot")


                # Solo procesamos si:
                # 1. La respuesta viene del bot al que se le envió el comando (sent_to_match)
                # 2. El comando es por DNI y el DNI coincide (dni_match)
                # 3. O el comando NO es por DNI (no_dni_command) y solo necesitamos que sea del bot correcto.
                
                # La lógica debe ser más permisiva si el comando NO es por DNI, pero el bot SI debe ser el correcto.
                if sent_to_match and (dni_match or no_dni_command):
                    
                    # Lógica de acumulación: Agregar el mensaje y marcar que HUBO respuesta
                    waiter_data["messages"].append(msg_obj)
                    waiter_data["has_response"] = True
                    
                    # El único caso de resolución forzada que dejamos es el de error de formato del bot
                    if "Por favor, usa el formato correcto" in msg_obj["message"]:
                        # Si es un error de formato, resolvemos de inmediato para no esperar el timeout
                        loop.call_soon_threadsafe(waiter_data["future"].set_result, msg_obj)
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

async def _call_api_command(command: str, timeout: int = TIMEOUT_TOTAL):
    """Envía un comando al bot y espera la respuesta(s), con lógica de respaldo y bloqueo por fallo."""
    if not await client.is_user_authorized():
        raise Exception("Cliente no autorizado. Por favor, inicie sesión.")

    command_id = time.time() # ID temporal
    
    # Extraer DNI del comando si existe
    dni_match = re.search(r"/\w+\s+(\d{8})", command)
    dni = dni_match.group(1) if dni_match else None
    
    # Lista de bots a intentar
    bots_to_try = [LEDERDATA_BOT_ID, LEDERDATA_BACKUP_BOT_ID]
    
    # ----------------------------------------------------------------------
    # Lógica de Intento con Verificación de Bloqueo
    # ----------------------------------------------------------------------
    
    for attempt, current_bot_id in enumerate(bots_to_try, 1):
        
        # 1. Verificar si el bot está bloqueado
        if is_bot_blocked(current_bot_id) and attempt == 1:
            print(f"🚫 Bot {current_bot_id} está BLOQUEADO temporalmente. Saltando al bot de respaldo.")
            continue # Saltar al siguiente bot (el de respaldo)
        elif is_bot_blocked(current_bot_id) and attempt == 2:
            print(f"🚫 Bot de Respaldo {current_bot_id} también está BLOQUEADO. No hay bots disponibles.")
            break # Salir del bucle, ambos fallaron

        # 2. Preparar el Future y Waiter
        future = loop.create_future()
        waiter_data = {
            "future": future,
            "messages": [], # Aquí se acumularán todos los mensajes
            "dni": dni,
            "command": command,
            "timer": None, 
            "sent_to_bot": current_bot_id,
            "has_response": False # CRUCIAL: Indica si se recibió *al menos un* mensaje
        }
        
        # El tiempo de espera será el de failover para el bot principal, y el total para el de respaldo.
        current_timeout = TIMEOUT_FAILOVER if attempt == 1 else TIMEOUT_TOTAL
        
        # Función de timeout para el Future
        def _on_timeout(bot_id_on_timeout=current_bot_id, command_id_on_timeout=command_id):
            with _messages_lock:
                waiter_data = response_waiters.pop(command_id_on_timeout, None)
                if waiter_data and not waiter_data["future"].done():
                    
                    # Lógica de Failover/Bloqueo
                    if waiter_data["messages"]:
                        # LLEGÓ RESPUESTA(S). Se devuelve la lista de mensajes acumulados (EXITO)
                        # YA NO SE INTENTA EN EL OTRO BOT (si fuera intento 1)
                        print(f"✅ Timeout alcanzado para acumulación en {bot_id_on_timeout}. Devolviendo {len(waiter_data['messages'])} mensaje(s).")
                        loop.call_soon_threadsafe(
                            waiter_data["future"].set_result, 
                            waiter_data["messages"] # 👈 DEVUELVE LA LISTA COMPLETA
                        )
                    else:
                        # NO LLEGÓ NINGÚN mensaje (Fallo de NO RESPUESTA).
                        # 1. Registrar la falla del bot (solo si no se recibió NINGÚN mensaje)
                        if not waiter_data["has_response"]:
                            record_bot_failure(bot_id_on_timeout)
                        
                        # 2. Resolver el future con un indicador de fallo
                        loop.call_soon_threadsafe(
                            waiter_data["future"].set_result, 
                            {"status": "error_timeout", "message": f"Tiempo de espera de respuesta agotado ({current_timeout}s). No se recibió NINGÚN mensaje para el comando: {command}.", "bot": bot_id_on_timeout, "fail_recorded": not waiter_data["has_response"]}
                        )

        # Establecer el timer de timeout en el loop de Telethon
        waiter_data["timer"] = loop.call_later(current_timeout, _on_timeout)

        with _messages_lock:
            # 3. Usamos el mismo command_id pero actualizamos el waiter_data
            response_waiters[command_id] = waiter_data

        print(f"📡 Enviando comando (Intento {attempt}) a {current_bot_id} [Timeout: {current_timeout}s]: {command}")
        
        try:
            # 4. Enviar el mensaje al bot
            await client.send_message(current_bot_id, command)
            
            # 5. Esperar la respuesta (que será una lista de mensajes o un dict de error)
            result = await future
            
            # 6. Lógica de Failover
            # Si el resultado es un fallo por NO RESPUESTA y estamos en el intento 1, pasamos al siguiente bot.
            if isinstance(result, dict) and result.get("status") == "error_timeout" and attempt == 1:
                print(f"⌛ Timeout de NO RESPUESTA de {LEDERDATA_BOT_ID}. Intentando con {LEDERDATA_BACKUP_BOT_ID}.")
                continue # Pasa al siguiente intento/bot
            elif isinstance(result, dict) and result.get("status") == "error_timeout" and attempt == 2:
                # El bot de respaldo falló también. Retornar el error final.
                return result 
            
            # Si el resultado es un error de formato del bot (dict, si solo llegó uno de error de formato)
            if isinstance(result, dict) and "Por favor, usa el formato correcto" in result.get("message", ""):
                 return {"status": "error_bot_format", "message": result.get("message"), "bot_used": current_bot_id}

            # Si llega aquí con un resultado (lista de mensajes), YA NO SE INTENTA EL OTRO BOT.

            # 7. Lógica de Consolidación de Respuestas
            
            # El resultado debe ser una lista de mensajes recibidos (lista_de_mensajes).
            list_of_messages = result if isinstance(result, list) else [] # Debe ser una lista
            
            if isinstance(list_of_messages, list) and len(list_of_messages) > 0:
                
                # Usamos el primer mensaje como base para la respuesta final
                final_result = list_of_messages[0].copy() 
                
                # Lista de mensajes de texto completos (limpios)
                final_result["full_messages"] = [msg["message"] for msg in list_of_messages] 
                
                # Consolidar todas las URLs
                consolidated_urls = {} 
                
                # Mapeo de tipos de foto a claves de URL para PRESERVAR EL JSON ORIGINAL
                type_map = {
                    "rostro": "ROSTRO", 
                    "huella": "HUELLA", 
                    "firma": "FIRMA", 
                    "adverso": "ADVERSO", 
                    "reverso": "REVERSO"
                }
                
                for msg in list_of_messages:
                    for url_obj in msg.get("urls", []):
                        # Usar el tipo de foto/documento como clave (mayúsculas)
                        key = type_map.get(url_obj["type"].lower())
                        
                        if key:
                            # Si ya existe una foto con ese tipo, no la sobreescribimos
                            if key not in consolidated_urls:
                                consolidated_urls[key] = url_obj["url"]
                        else:
                            # Para otros archivos (pdfs, etc.), usar la clave 'FILE'. 
                            # Si es un comando que devuelve varios PDFs (como /denp), se usa una enumeración.
                            base_key = "FILE"
                            i = 1
                            # Si ya existe 'FILE', probamos con 'FILE_1', 'FILE_2', etc.
                            if base_key in consolidated_urls:
                                while f"{base_key}_{i}" in consolidated_urls:
                                    i += 1
                                consolidated_urls[f"{base_key}_{i}"] = url_obj["url"]
                            else:
                                consolidated_urls[base_key] = url_obj["url"]

                    # Asegurarnos de que los fields (como DNI) se capturen si no vinieron en el primer mensaje
                    if not final_result["fields"].get("dni") and msg["fields"].get("dni"):
                        final_result["fields"] = msg["fields"]
                        
                final_result["urls"] = consolidated_urls 
                
                # Unimos todos los mensajes de texto para la clave principal 'message'
                # Mantenemos el formato de unir por '\n---\n' para simular un único mensaje grande
                final_result["message"] = "\n---\n".join(final_result["full_messages"])
                final_result.pop("full_messages")
                
                # Limpiar campos no necesarios para el JSON final
                final_result.pop("chat_id", None)
                final_result.pop("from_id", None)
                final_result.pop("date", None)
                
                # Reconstruir el JSON para que se parezca al original (message + fields + urls)
                final_json = {
                    "message": final_result["message"],
                    "fields": final_result["fields"],
                    "urls": final_result["urls"],
                }
                
                # Si el campo 'dni' está en fields, lo movemos al nivel superior para compatibilidad
                if final_json["fields"].get("dni"):
                    final_json["dni"] = final_json["fields"]["dni"]
                    final_json["fields"].pop("dni")
                
                # Si la consulta fue exitosa con al menos 1 mensaje
                final_json["status"] = "ok"
                final_json["bot_used"] = current_bot_id
                
                return final_json
                
            # Si list_of_messages está vacío
            else: 
                # Esto debería ser cubierto por el error_timeout, pero por si acaso.
                return {"status": "error", "message": f"Respuesta vacía o inesperada del bot {current_bot_id}.", "bot_used": current_bot_id}
            
        # --- CAPTURA DE ERROR CLAVE: UserBlockedError ---
        except UserBlockedError as e:
            error_msg = f"Error de Telethon/conexión/fallo: You blocked this user (caused by SendMessageRequest)"
            print(f"❌ Error de BLOQUEO en {current_bot_id}: {error_msg}. Registrando fallo y pasando al siguiente bot.")
            
            # Registrar la falla por bloqueo inmediatamente
            record_bot_failure(current_bot_id)
            
            # Limpiar el waiter y cancelar el timer ANTES de pasar al siguiente intento
            with _messages_lock:
                 if command_id in response_waiters:
                    waiter_data = response_waiters.pop(command_id, None)
                    if waiter_data and waiter_data["timer"]:
                        waiter_data["timer"].cancel()
                        
            if attempt == 1:
                continue # Pasa al bot de respaldo
            else:
                # Si falla el intento 2 por bloqueo, retornamos el error final.
                return {"status": "error", "message": error_msg, "bot_used": current_bot_id}
            
        except Exception as e:
            # Si hay un error de Telethon/conexión GENERAL (diferente a UserBlockedError).
            error_msg = f"Error de Telethon/conexión/fallo: {str(e)}"
            if attempt == 1:
                print(f"❌ Error en {LEDERDATA_BOT_ID}: {error_msg}. Intentando con {LEDERDATA_BACKUP_BOT_ID}.")
                # Registrar la falla por error de conexión
                record_bot_failure(LEDERDATA_BOT_ID)
                
                # Limpiar el waiter y cancelar el timer ANTES de pasar al siguiente intento
                with _messages_lock:
                     if command_id in response_waiters:
                        waiter_data = response_waiters.pop(command_id, None)
                        if waiter_data and waiter_data["timer"]:
                            waiter_data["timer"].cancel()
                            
                continue
            else:
                # Si falla el intento 2, retornamos el error final.
                return {"status": "error", "message": error_msg, "bot_used": current_bot_id}
        finally:
            # 8. Limpieza final: Asegurar que el Future y el Timer se eliminen si no se hizo antes
            with _messages_lock:
                if command_id in response_waiters:
                    waiter_data = response_waiters.pop(command_id, None)
                    if waiter_data and waiter_data["timer"]:
                        waiter_data["timer"].cancel()

    # Si se llegó aquí es porque ambos bots fallaron o estaban bloqueados.
    final_bot = LEDERDATA_BOT_ID
    if is_bot_blocked(LEDERDATA_BACKUP_BOT_ID) or not is_bot_blocked(LEDERDATA_BOT_ID):
         final_bot = LEDERDATA_BOT_ID
    elif is_bot_blocked(LEDERDATA_BOT_ID) and not is_bot_blocked(LEDERDATA_BACKUP_BOT_ID):
         final_bot = LEDERDATA_BACKUP_BOT_ID
    
    return {"status": "error", "message": f"Falló la consulta después de 2 intentos. Ambos bots están bloqueados o agotaron el tiempo de espera.", "bot_used": final_bot}


# --- Rutina de reconexión / ping ---

async def _ensure_connected():
    """Mantiene la conexión y autorización activa."""
    while True:
        try:
            if not client.is_connected():
                print("🔌 Intentando reconectar Telethon...")
                await client.connect()
            
            if client.is_connected() and not await client.is_user_authorized():
                 print("⚠️ Telethon conectado, pero no autorizado. Reintentando auth...")
                 # Si la sesión es de StringSession, no puede re-auth si no hay 2FA/login
                 # Pero si es un archivo de sesión, intentar start() podría ayudar.
                 try:
                    await client.start()
                 except Exception:
                     pass

            # Intentar obtener la entidad de ambos bots después de la reconexión/auth
            if await client.is_user_authorized():
                await client.get_entity(LEDERDATA_BOT_ID) 
                await client.get_entity(LEDERDATA_BACKUP_BOT_ID) 
                # Un ping simple para mantener viva la conexión
                await client.get_dialogs(limit=1) 
                print("✅ Reconexión y verificación de bots exitosa.")
            else:
                 print("🔴 Cliente no autorizado. Requerido /login.")


        except Exception:
            traceback.print_exc()
        await asyncio.sleep(300) # Dormir 5 minutos

asyncio.run_coroutine_threadsafe(_ensure_connected(), loop)

# --- Funciones para la NUEVA API de Películas ---

async def _search_movie_in_channel(query: str, limit: int = 10):
    """
    Busca una película en el canal por nombre/descripción y descarga el archivo.
    Requiere que la cuenta de Telethon sea miembro del canal.
    """
    if not await client.is_user_authorized():
        return {"status": "error", "message": "Cliente no autorizado. Por favor, inicie sesión."}

    try:
        # Intenta obtener la entidad del canal (debe estar unido)
        channel_entity = await client.get_entity(MOVIE_CHANNEL_ID_FOR_TELETHON)
    except Exception:
        return {"status": "error", "message": f"No se pudo acceder al canal {MOVIE_CHANNEL_NAME}. Confirma que tu sesión de Telethon está unida al canal y usa el ID correcto."}
    
    found_movies = []
    
    # Se usa client.iter_messages con 'search' para que Telegram haga la búsqueda interna
    async for message in client.iter_messages(channel_entity, search=query, limit=limit):
        if message.media and isinstance(message.media, MessageMediaDocument):
            document = message.media.document
            
            # Solo consideramos videos (tipo de mime 'video/mp4' o similar)
            is_video = any(attr.to_dict().get('_') == 'DocumentAttributeVideo' for attr in document.attributes)
            if not is_video:
                continue

            # Obtener el nombre del archivo (si está disponible) o generar uno
            file_name = next((attr.file_name for attr in document.attributes if hasattr(attr, 'file_name')), f"movie_{message.id}.mp4")
            
            # Para evitar conflictos y simplificar, se usará un nombre de archivo único basado en el ID del mensaje
            unique_filename = f"movie_{message.id}_{file_name}"
            
            # --- NOTA CRUCIAL: Solo recopilamos los datos, la descarga se hace AL ELEGIR ---
            # La descarga es pesada, no queremos descargar 10 películas.
            
            movie_info = {
                "id": message.id,
                "title": file_name,
                "description": message.message,
                "date": message.date.isoformat(),
                "size_mb": round(document.size / (1024 * 1024), 2),
                "download_status": "PENDING" # Marcamos que está pendiente de descarga
            }
            found_movies.append(movie_info)

    return {"status": "ok", "query": query, "channel": MOVIE_CHANNEL_NAME, "results": found_movies}

async def _download_and_get_url(message_id: int):
    """
    Descarga un mensaje específico y devuelve la URL pública.
    """
    if not await client.is_user_authorized():
        return {"status": "error", "message": "Cliente no autorizado. Por favor, inicie sesión."}

    try:
        # Obtener el mensaje por ID
        message = await client.get_messages(MOVIE_CHANNEL_ID_FOR_TELETHON, ids=message_id)
        
        if not message or not message.media or not isinstance(message.media, MessageMediaDocument):
            return {"status": "error", "message": f"Mensaje (ID: {message_id}) no encontrado o no contiene un documento/video válido."}
            
        document = message.media.document
        
        # Generar nombre de archivo único
        file_name = next((attr.file_name for attr in document.attributes if hasattr(attr, 'file_name')), f"movie_{message.id}.mp4")
        unique_filename = f"movie_{message.id}_{file_name}"
        
        # Path de descarga
        saved_path = os.path.join(DOWNLOAD_DIR, unique_filename)
        
        # Verificar si ya existe para evitar la descarga
        if os.path.exists(saved_path):
            print(f"🎬 Archivo ya existe: {unique_filename}. Omitiendo descarga.")
        else:
            print(f"⬇️ Descargando película (ID: {message_id}) a {unique_filename}...")
            # Descargar el medio (ESTO PUEDE TARDAR MUCHO)
            saved_path = await client.download_media(message, file=saved_path)
            
        # Devolver la URL pública
        public_url = f"{PUBLIC_URL}/files/{unique_filename}"
        
        return {
            "status": "ok",
            "message": "Descarga completada y URL generada.",
            "movie_id": message_id,
            "title": file_name,
            "download_url": public_url,
            "telegram_link": f"https://t.me/c/{str(MOVIE_CHANNEL_ID_FOR_TELETHON)[4:]}/{message_id}"
        }

    except Exception as e:
        traceback.print_exc()
        return {"status": "error", "message": f"Error al descargar la película: {str(e)}"}

# --- Rutas HTTP Base (Login/Status/General) ---

@app.route("/")
def root():
    return jsonify({
        "status": "ok",
        "message": "Gateway API para LEDER DATA Bot y Películas (Experimental) activo. Consulta /status para la sesión.",
    })

# [EL RESTO DE RUTAS BASE Y COMANDOS DE LEDER DATA (/status, /login, /code, /send, /get, /files/<path:filename>, /dni, /dni_nombres, etc.) PERMANECEN INTACTAS AQUÍ]

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
    
    # Agregar estado de bloqueo de bots
    bot_status = {}
    for bot_id in ALL_BOT_IDS:
        is_blocked = is_bot_blocked(bot_id)
        bot_status[bot_id] = {
            "blocked": is_blocked,
            "last_fail": bot_fail_tracker.get(bot_id).isoformat() if bot_fail_tracker.get(bot_id) else None
        }

    return jsonify({
        "authorized": bool(is_auth),
        "pending_phone": pending_phone["phone"],
        "session_loaded": True if SESSION_STRING else False,
        "session_string": current_session,
        "bot_status": bot_status,
        "movie_channel_id": MOVIE_CHANNEL_ID_FOR_TELETHON,
        "movie_channel_name": MOVIE_CHANNEL_NAME,
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
    Ruta para descargar archivos. Se añade as_attachment=True para forzar la descarga 
    en lugar de visualizar el archivo, lo que es ideal para appcreator24.
    """
    # Se debe decodificar el nombre del archivo, ya que Telethon podría incluir caracteres especiales en el nombre
    decoded_filename = unquote(filename)
    return send_from_directory(DOWNLOAD_DIR, decoded_filename, as_attachment=True)


# ----------------------------------------------------------------------
# --- RUTAS NUEVAS PARA PELÍCULAS (EXPERIMENTAL) -----------------------
# ----------------------------------------------------------------------

@app.route("/peliculas/buscar", methods=["GET"])
def api_buscar_pelicula():
    """
    Busca películas en el canal privado usando la función de búsqueda interna de Telegram.
    Devuelve una lista de resultados, pero NO las descarga.
    """
    query = request.args.get("query")
    if not query:
        return jsonify({"status": "error", "message": "Parámetro 'query' (nombre o descripción de la película) es requerido."}), 400

    limit = request.args.get("limit", 10, type=int)
    
    try:
        # Llama a la función asíncrona de búsqueda
        result = run_coro(_search_movie_in_channel(query, limit))
        
        if result.get("status") == "error":
            return jsonify(result), 400
            
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno en la búsqueda: {str(e)}"}), 500

@app.route("/peliculas/descargar", methods=["GET"])
def api_descargar_pelicula():
    """
    Descarga la película por su ID de mensaje y devuelve la URL pública.
    ADVERTENCIA: Esta operación puede ser muy lenta y consumir mucho ancho de banda.
    """
    message_id = request.args.get("message_id", type=int)
    if not message_id:
        return jsonify({"status": "error", "message": "Parámetro 'message_id' (ID del mensaje en el canal) es requerido. Obténlo de /peliculas/buscar."}), 400
        
    # Agregamos un timeout más largo para la descarga (ej: 5 minutos = 300 segundos)
    try:
        result = run_coro(
            asyncio.wait_for(_download_and_get_url(message_id), timeout=300)
        )
        
        if result.get("status") == "error":
            return jsonify(result), 500
            
        return jsonify(result)
        
    except asyncio.TimeoutError:
        return jsonify({"status": "error", "message": "Tiempo de espera agotado (5 minutos). La descarga de la película es demasiado lenta o falló la conexión."}), 500
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error interno en la descarga: {str(e)}"}), 500


# ----------------------------------------------------------------------
# --- Rutas HTTP de API (Comandos LEDER DATA) ----------------------------
# ----------------------------------------------------------------------

# [EL RESTO DE RUTAS DE COMANDO (/dni, /dnif, /c4, etc.) VAN AQUÍ]

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
@app.route("/meta", methods=["GET"]) # METADATA COMPLETA 
@app.route("/fis", methods=["GET"]) # FISCALIA 
@app.route("/fisdet", methods=["GET"]) # FISCALIA DETALLADO (Para cubrir tu mención, se envía como comando)
@app.route("/det", methods=["GET"]) # DETENIDOS 
@app.route("/rqh", methods=["GET"]) # REQUISITORIAS HISTORICAS 
@app.route("/antpenv", methods=["GET"]) # ANTECEDENTES PENALES VERIFICADOR
@app.route("/dend", methods=["GET"]) # DENUNCIAS POLICIALES (DNI)
@app.route("/dence", methods=["GET"]) # DENUNCIAS POLICIALES (CE) (AGREGADO)
@app.route("/denpas", methods=["GET"]) # DENUNCIAS POLICIALES (PASAPORTE) (AGREGADO)
@app.route("/denci", methods=["GET"]) # DENUNCIAS POLICIALES (CEDULA) (AGREGADO)
@app.route("/denp", methods=["GET"]) # DENUNCIAS POLICIALES (PLACA) (AGREGADO)
@app.route("/denar", methods=["GET"]) # DENUNCIAS POLICIALES (ARMAMENTO) (AGREGADO)
@app.route("/dencl", methods=["GET"]) # DENUNCIAS POLICIALES (CLAVE) (AGREGADO)
@app.route("/agv", methods=["GET"]) # ÁRBOL GENEALÓGICO VISUAL (AGREGADO)
@app.route("/agvp", methods=["GET"]) # ÁRBOL GENEALÓGICO VISUAL PROFESIONAL (AGREGADO)
@app.route("/cedula", methods=["GET"]) # VENEZOLANOS CEDULA (AGREGADO)
def api_dni_based_command():
    """
    Maneja comandos que solo requieren un DNI o un parámetro simple.
    El parámetro se espera bajo el nombre 'query'. Para RENIEC es 'dni'.
    """
    
    command_name = request.path.lstrip('/') 
    
    # Comandos que esperan DNI de 8 dígitos
    dni_required_commands = [
        "dni", "dnif", "dnidb", "dnifdb", "c4", "dnivaz", "dnivam", "dnivel", "dniveln", 
        "fa", "fadb", "fb", "fbdb", "cnv", "cdef", "antpen", "antpol", "antjud", 
        "actancc", "actamcc", "actadcc", "tra", "sue", "cla", "sune", "cun", "colp", 
        "mine", "afp", "antpenv", "dend", "meta", "fis", "det", "rqh", "agv", "agvp"
    ]
    
    # Comandos que esperan un parámetro de consulta genérico (query)
    query_required_commands = [
        "tel", "telp", "cor", "nmv", "tremp", 
        # LOS 7 NUEVOS COMANDOS (excepto /rqh y /fisdet, que ya están arriba o se manejan especial)
        "fisdet", # DETALLADO
        "dence", "denpas", "denci", "denp", "denar", "dencl", 
        "cedula", # Venezolanos Cédula
    ]
    
    # Comandos que toman DNI o query, o pueden ir sin nada (ej: /osiptel sin query da info general)
    optional_commands = ["osiptel", "claro", "entel", "pro", "sen", "sbs", "pasaporte", "seeker", "bdir"]
    
    param = ""

    if command_name in dni_required_commands:
        param = request.args.get("dni")
        if not param or not param.isdigit() or len(param) != 8:
            return jsonify({"status": "error", "message": f"Parámetro 'dni' es requerido y debe ser un número de 8 dígitos para /{command_name}."}), 400
    
    elif command_name in query_required_commands:
        
        param_value = None
        
        # --- Lógica específica para los 7 comandos ---
        if command_name == "fisdet":
            # Formato: /fisdet <caso|distritojudicial>
            # Buscamos 'dni' (o caso/distritojudicial) o 'query'
            param_value = request.args.get("caso") or request.args.get("distritojudicial") or request.args.get("query")
            
            # Si el usuario usa el formato 'dni|detalle' (aunque el nuevo formato pide caso/distrito)
            if not param_value:
                dni_val = request.args.get("dni")
                det_val = request.args.get("detalle")
                if dni_val and det_val:
                    param_value = f"{dni_val}|{det_val}"
                elif dni_val:
                    param_value = dni_val # Usar solo DNI si detalle no está
        
        elif command_name == "dence":
            param_value = request.args.get("carnet_extranjeria")
        elif command_name == "denpas":
            param_value = request.args.get("pasaporte")
        elif command_name == "denci":
            param_value = request.args.get("cedula_identidad")
        elif command_name == "denp":
            param_value = request.args.get("placa")
        elif command_name == "denar":
            param_value = request.args.get("serie_armamento")
        elif command_name == "dencl":
            param_value = request.args.get("clave_denuncia")
            
        # --- Lógica para otros comandos de query ---
        elif command_name == "cedula":
            param_value = request.args.get("cedula")
        
        # Si no se encontró el parámetro específico, usamos 'query' (o dni de fallback)
        param = param_value or request.args.get("dni") or request.args.get("query")
             
        if not param:
            return jsonify({"status": "error", "message": f"Parámetro de consulta es requerido para /{command_name}."}), 400
    
    elif command_name in optional_commands:
        param_dni = request.args.get("dni")
        param_query = request.args.get("query")
        param_pasaporte = request.args.get("pasaporte") if command_name == "pasaporte" else None
        
        param = param_dni or param_query or param_pasaporte or ""
        
    else:
        # En caso de que se añadan nuevos comandos no cubiertos por DNI o Query
        param = request.args.get("dni") or request.args.get("query") or ""

        
    # Construir comando
    command = f"/{command_name} {param}".strip() # strip() elimina espacio si param está vacío
    
    # Ejecutar comando
    try:
        # Usamos el timeout de failover para el bot principal. El de respaldo usará TIMEOUT_TOTAL
        result = run_coro(_call_api_command(command, timeout=TIMEOUT_FAILOVER))
        
        if result.get("status", "").startswith("error"):
            # Si el error es un timeout o de telethon, devolvemos 500, sino 400
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            status_code = 500 if is_timeout_or_connection_error else 400
            # Mantenemos la estructura de respuesta de error simple
            result.pop("bot_used", None)
            return jsonify(result), status_code
            
        # Si es exitoso, el JSON ya viene en el formato esperado
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
        result = run_coro(_call_api_command(command, timeout=TIMEOUT_FAILOVER))
        if result.get("status", "").startswith("error"):
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            result.pop("bot_used", None)
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
        result = run_coro(_call_api_command(command, timeout=TIMEOUT_FAILOVER))
        if result.get("status", "").startswith("error"):
            is_timeout_or_connection_error = "timeout" in result.get("message", "").lower() or "telethon" in result.get("message", "").lower() or result.get("status") == "error_timeout"
            result.pop("bot_used", None)
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
        # Intentar iniciar la sesión (si es persistente)
        if not run_coro(client.is_user_authorized()):
             run_coro(client.start())
             
        # Esto ayuda a Telethon a resolver la entidad de ambos bots al inicio
        run_coro(client.get_entity(LEDERDATA_BOT_ID)) 
        run_coro(client.get_entity(LEDERDATA_BACKUP_BOT_ID)) 
        
        # Intenta resolver la entidad del canal de películas (si ya estás unido)
        try:
             run_coro(client.get_entity(MOVIE_CHANNEL_ID_FOR_TELETHON))
             print(f"✅ Entidad del canal de películas resuelta: {MOVIE_CHANNEL_NAME}")
        except Exception as e:
             print(f"⚠️ No se pudo resolver la entidad del canal de películas. Asegúrate de que la sesión de Telethon esté unida. Error: {e}")
             
    except Exception:
        pass
    print(f"🚀 App corriendo en http://0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)

