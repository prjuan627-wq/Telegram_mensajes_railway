# Usa la imagen oficial de Python como base
# Es mejor usar la variante 'slim' para un tamaño más pequeño
FROM python:3.11.9-slim

# Establecer el directorio de trabajo dentro del contenedor
WORKDIR /app

# Instalar gunicorn y las dependencias (se asume que requirements.txt ya está actualizado)
# Instalar dependencias del sistema operativo que puedan ser necesarias (ej: para Telethon)
RUN apt-get update && apt-get install -y \
    gcc \
    # Dependencias comunes para Python
    libpq-dev \
    # Limpiar caché para reducir el tamaño de la imagen
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements.txt e instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código de la aplicación
COPY . .

# Crear el directorio de descargas si no existe y asegurar permisos
RUN mkdir -p /app/downloads

# El comando de inicio está en Procfile, pero se define el punto de entrada para Fly.io
# CMD está por defecto para Procfile
# El puerto ya se define en la configuración de gunicorn en Procfile
# El Procfile ya es suficiente para el ENTRYPOINT
