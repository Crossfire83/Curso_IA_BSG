# Imagen base con Python 3.13
FROM python:3.13-slim

# Mantiene el contenedor actualizado y define variables de entorno
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/workspace/app

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
    && rm -rf /var/lib/apt/lists/*

# Crear directorios de trabajo
WORKDIR ${APP_HOME}

# Crear grupo y usuario no root.
RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid appgroup --shell /bin/false --create-home appuser

# Copiar archivos del proyecto
COPY --chown=appuser:appgroup . ${APP_HOME}

# Instalar dependencias Python
RUN pip install --upgrade pip && pip install -r requirements.txt

# Exponer puerto 8000 para Flask
EXPOSE 8000

# Comando de ejecución
CMD ["gunicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]