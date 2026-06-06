# Stage 1: Imagen base con Python 3.13 para construccion.
FROM python:3.13-slim AS builder

# Mantiene el contenedor actualizado y define variables de entorno
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/workspace/app

# Crear directorios de trabajo
WORKDIR ${APP_HOME}

# Instalar dependencias del sistema necesarias para OpenCV y PDF parsing
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libpoppler-cpp-dev \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip

# Crear un ambiente virtual
RUN python -m venv /opt/venv

# Forzar al contenedor a usar los binarios del ambiente virtual automaticamente.
ENV PATH="/opt/venv/bin:$PATH"

# Copiar archivo de requerimientos del proyecto
COPY requirements.txt ${APP_HOME}

#Instalar librerias adicionales necesitadas por el proyecto
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Imagen minima final de runtime ---
FROM python:3.13-slim AS runner

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_HOME=/workspace/app
ENV PATH="/opt/venv/bin:$PATH"

# Crear directorios de trabajo
WORKDIR ${APP_HOME}

# Crear grupo y usuario no root. y cambiar a ese usuario
RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid appgroup --shell /bin/false --create-home appuser

# Copiar el ambiente virtual de la imagen constructora.
COPY --from=builder /opt/venv /opt/venv

# Copiar archivos del proyecto
COPY --chown=appuser:appgroup . ${APP_HOME}

USER appuser
# Exponer puerto 8000 para Flask
EXPOSE 8000

# Comando de ejecución
CMD ["gunicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]