from fastapi import FastAPI
from sqlalchemy import text
from app.database import engine, Base
from app.routes import evaluaciones, generar, evaluar, generica
from app.config import DEBUG, STORAGE_PATH

# Create tables
Base.metadata.create_all(bind=engine)

# Migración ligera e idempotente para BDs existentes
try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE hojas_respuesta ADD COLUMN IF NOT EXISTS preguntas_orden JSONB"))
        conn.execute(text("ALTER TABLE hojas_respuesta ADD COLUMN IF NOT EXISTS config_seleccion JSONB"))
        conn.execute(text("ALTER TABLE preguntas ADD COLUMN IF NOT EXISTS seccion_id UUID"))
        conn.execute(text("ALTER TABLE preguntas ADD COLUMN IF NOT EXISTS activa BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.commit()
except Exception:
    pass

tags_metadata = [
    {
        "name": "Banco de preguntas",
        "description": "Evaluaciones (bancos), secciones y preguntas. Las secciones agrupan "
                       "preguntas y permiten seleccionar por cantidad/porcentaje al generar una hoja.\n"
                       "- **Crear pregunta**: `POST /evaluacion/{id}/preguntas/batch`.\n"
                       "- **Actualizar pregunta** (nombre, tipo, opciones, sección, puntos, etc.): "
                       "`PUT /pregunta/{id}`.\n"
                       "- **Desactivar pregunta** (excluirla de hojas nuevas sin borrar su historial): "
                       "`PUT /pregunta/{id}` con `\"activa\": false`.\n"
                       "- **Eliminar** definitivamente solo si no tiene resultados ni hojas generadas; "
                       "en caso contrario devuelva 409 indicando que use desactivación.",
    },
    {
        "name": "Generación de hojas de examen",
        "description": "Genera hojas de respuestas (con su hoja de preguntas y clave) a partir de "
                       "un banco, con selección **template** (cantidad/porcentaje por sección) o "
                       "**estructurada** (subconjunto ordenado o banco completo).",
    },
    {
        "name": "Hoja genérica y lectura",
        "description": "Hoja de respuestas genérica (solo burbujas N×M, sin examen) y lectura de "
                       "lo llenado en un escaneo, sin contrastar contra un banco.",
    },
    {
        "name": "Evaluación de escaneos",
        "description": "Evalúa hojas escaneadas contra el banco (coordenadas conocidas) y consulta "
                       "de resultados.",
    },
]

app = FastAPI(
    title="Evaluaciones OMR API",
    description="""Servicio de evaluaciones con reconocimiento óptico de marcas (OMR).

**Flujo principal**
1. Crear una evaluación (banco de preguntas) → agregar secciones → cargar preguntas con `seccion_id`.
2. Generar hojas: `POST /generar/{evaluacion_id}` con una selección **template** (reparto por
   cantidad/porcentaje por sección) o **estructurada** (banco completo o subconjunto ordenado).
   El `identificador` es opcional: si falta se autogenera (`{short_id}-{n}`) y la reimpresión
   con el mismo identificador devuelve la misma variante.
3. Escanear la hoja llena y evaluarla con `POST /evaluar` (califica contra el banco).

**Hoja genérica (sin examen)**
- `POST /generar/generica` genera solo burbujas (N preguntas × M opciones, 2–5).
- `POST /leer` devuelve lo llenado (identificador persona + burbujas marcadas) sin contrastar nada.

Ver **Redoc** (`/redoc`) para la referencia completa o el esquema OpenAPI en `/openapi.json`.""",
    version="1.1.0",
    openapi_tags=tags_metadata,
    contact={
        "name": "Evaluaciones OMR",
    },
    license_info={
        "name": "Proprietary",
    },
    debug=DEBUG,
)

# Include routers (generica antes que generar: /generar/generica es una ruta
# estática y debe resolverse antes que /generar/{evaluacion_id})
app.include_router(evaluaciones.router)
app.include_router(generica.router)
app.include_router(generar.router)
app.include_router(evaluar.router)


@app.get("/")
def root():
    return {
        "message": "Evaluaciones OMR API",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "version": "1.1.0",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
