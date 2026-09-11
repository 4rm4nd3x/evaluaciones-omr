from datetime import datetime
from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Union
from uuid import UUID


# --- Evaluacion ---
class EvaluacionCreate(BaseModel):
    nombre: str
    descripcion: Optional[str] = None


class EvaluacionResponse(BaseModel):
    id: UUID
    short_id: str
    nombre: str
    descripcion: Optional[str]

    class Config:
        from_attributes = True


# --- Opcion ---
class OpcionCreate(BaseModel):
    key: str
    label: str
    es_correcta: bool


class OpcionResponse(BaseModel):
    id: UUID
    key: str
    label: str
    es_correcta: bool
    orden: int

    class Config:
        from_attributes = True


# --- Seccion ---
class SeccionCreate(BaseModel):
    nombre: str = Field(..., description="Nombre de la sección (p. ej. 'Álgebra')")
    descripcion: Optional[str] = Field(None, description="Descripción libre de la sección")
    orden: int = Field(1, description="Posición en que se imprime la sección dentro del banco")


class SeccionUpdate(BaseModel):
    nombre: Optional[str] = Field(None, description="Nuevo nombre de la sección")
    descripcion: Optional[str] = Field(None, description="Nueva descripción")
    orden: Optional[int] = Field(None, description="Nueva posición (secciones mayores se desplazan)")


class SeccionBatchCreate(BaseModel):
    secciones: List[SeccionCreate] = Field(..., description="Lista de secciones a crear")


class SeccionResponse(BaseModel):
    id: UUID
    nombre: str
    descripcion: Optional[str]
    orden: int

    class Config:
        from_attributes = True


# --- Pregunta ---
class PreguntaCreate(BaseModel):
    nombre: str = Field(..., description="Nombre corto de la pregunta (p. ej. 'pregunta_1')")
    tipo: str = Field("single", description="Tipo: 'single' (una respuesta) o 'multiple' (varias)")
    enunciado: Optional[str] = Field(None, description="Enunciado completo de la pregunta")
    opciones: List[OpcionCreate] = Field(..., description="Opciones de respuesta (entre 2 y 5, D6)")
    orden: int = Field(1, description="Orden dentro de la sección/banco")
    puntos: float = Field(1.0, description="Puntos asignados si la pregunta es correcta")
    requerida: bool = Field(False, description="Indica si la pregunta es obligatoria")
    seccion: Optional[str] = Field(None, description="Legacy: texto de sección (se crea/enlaza automáticamente)")
    seccion_id: Optional[UUID] = Field(None, description="Preferido: UUID de la sección (tabla 'secciones')")


class PreguntaBatchCreate(BaseModel):
    preguntas: List[PreguntaCreate]


class PreguntaResponse(BaseModel):
    id: UUID
    nombre: str
    tipo: str
    enunciado: Optional[str]
    orden: int
    puntos: float
    requerida: bool
    seccion: Optional[str]          # legacy
    seccion_id: Optional[UUID] = None
    opciones: List[OpcionResponse]

    class Config:
        from_attributes = True


# --- Selección por secciones (caso 1: template) ---
class SeccionQuota(BaseModel):
    seccion_id: UUID = Field(..., description="UUID de la sección del banco")
    cantidad: Optional[int] = Field(None, description="Número exacto de preguntas de esta sección")
    porcentaje: Optional[float] = Field(None, description="Porcentaje del total para esta sección (redondeo por mayor resto)")


class SeleccionTemplate(BaseModel):
    tipo: Literal["template"] = Field(..., description="Selección por secciones (cantidad/porcentaje)")
    cantidad_preguntas: int = Field(..., description="Total de preguntas a imprimir (debe estar entre 1 y el tamaño del banco)")
    secciones: Optional[List[SeccionQuota]] = Field(None, description="Reparto por sección: cada item con 'cantidad' o 'porcentaje'")
    aleatorio: bool = Field(False, description="Si true, selección y orden aleatorios (secciones intercaladas)")


# --- Subconjunto ordenado explícito (caso 2: estructurada) ---
class SeleccionEstructurada(BaseModel):
    tipo: Literal["estructurada"] = Field(..., description="Banco completo en su orden (o subconjunto ordenado)")
    preguntas_orden: Optional[List[UUID]] = Field(None, description="Subconjunto explícito de preguntas en orden; si falta, se usa todo el banco")


Seleccion = Union[SeleccionTemplate, SeleccionEstructurada]


# --- Generar Hoja de examen ---
class GenerarHojaRequest(BaseModel):
    identificador: Optional[str] = Field(None, description="Identificador de la hoja; si falta se genera '{short_id}-{n}' (D4). Misma (evaluación, identificador) → misma variante (reimpresión estable).")
    seleccion: Optional[Seleccion] = Field(None, description="Configuración de selección (template | estructurada). Si se omite, se usa el banco completo en orden.")
    # Backward-compat: si seleccion es None, se usan estos campos legacy
    cantidad_preguntas: Optional[int] = Field(None, description="Legacy: recorte del banco (equivale a un template sin secciones)")
    aleatorio: bool = Field(False, description="Legacy: selección/orden aleatorios")
    hojaPreguntas: Optional[str] = Field(None, description="PDF de preguntas (base64) cargado externamente — aún sin uso")
    hojaRespuestas: Optional[str] = Field(None, description="PDF de respuestas (base64) cargado externamente — aún sin uso")


class GenerarHojaResponse(BaseModel):
    hoja_id: UUID
    identificador: str
    qr_data: dict
    pdf_base64: str
    hoja_preguntas_base64: Optional[str] = None
    cantidad_preguntas: int = 0
    config_seleccion: Optional[dict] = None


# --- Generar Hoja Genérica (caso 4) ---
class GenerarGenericaRequest(BaseModel):
    identificador: str = Field(..., description="Identificador de la hoja genérica (único; la reimpresión con el mismo id devuelve la misma hoja)")
    cantidad_preguntas: int = Field(..., description="Número de preguntas (filas de burbujas)")
    opciones: str | List[str] = Field("ABCDE", description="Letras de opciones: 'ABCDE' o ['A','B','C'] (2-5 opciones)")


class GenerarGenericaResponse(BaseModel):
    hoja_id: UUID
    identificador: str
    num_opciones: int = Field(..., description="Cantidad de burbujas por pregunta (2-5)")
    qr_data: dict = Field(..., description='JSON embebido en el QR, p. ej. {"tipo": "generica", "identificador": "GEN-001"}')
    pdf_base64: str = Field(..., description="PDF de la hoja de respuestas en base64")


# --- Información de hojas generadas ---
class HojaRespuestaInfo(BaseModel):
    hoja_id: UUID
    evaluacion_id: UUID
    identificador: str
    qr_data: dict
    pdf_path: Optional[str]
    cantidad_preguntas: int = 0
    config_seleccion: Optional[dict] = None
    created_at: datetime

    class Config:
        from_attributes = True


# --- Evaluar ---
class EvaluarRequest(BaseModel):
    pdf_escaneado_base64: Optional[str] = None
    imagen_escaneada_base64: Optional[str] = None
    identificador: Optional[str] = None


class HojaResultadoResponse(BaseModel):
    identificador: str
    evaluacion_id: UUID
    cantidad_preguntas: int = 0
    pdf_base64: str


class RespuestaDetalleResponse(BaseModel):
    nombre: str
    pregunta_id: UUID
    respuesta: Optional[str]
    es_correcta: bool
    puntos_obtenidos: float

    class Config:
        from_attributes = True


class ResultadoResponse(BaseModel):
    resultado_id: UUID
    identificador: Optional[str]
    identificador_persona: Optional[str] = None
    puntuacion_total: float
    total_preguntas: int
    respuestas_correctas: int
    respuestas: List[RespuestaDetalleResponse]
    pdf_revisado_base64: Optional[str] = None
    imagen_anotada_base64: Optional[str] = None
    qr_codes: List[str]
    storage_path: Optional[str] = None


class ResultadoLoteResponse(BaseModel):
    """Resultado de evaluar un escaneo: cada página con QR válido produce
    un resultado propio; las páginas sin QR se reportan en errores."""
    total_paginas: int
    paginas_evaluadas: int
    paginas_ignoradas: int
    resultados: List[ResultadoResponse]
    errores: List[dict] = []


# --- Leer Hoja Genérica (caso 4) ---
class RespuestaLecturaItem(BaseModel):
    pregunta: int
    opciones_seleccionadas: List[str]


class LecturaGenericaItem(BaseModel):
    identificador: Optional[str] = None
    identificador_persona: Optional[str] = None
    cantidad_preguntas: int
    num_opciones: int
    respuestas: List[RespuestaLecturaItem]
    imagen_anotada_base64: Optional[str] = None
    qr_codes: List[str]


class LecturaGenericaLoteResponse(BaseModel):
    total_paginas: int
    paginas_leidas: int
    paginas_ignoradas: int
    lecturas: List[LecturaGenericaItem]
    errores: List[dict] = []


# --- Resultados (consulta / CRUD) ---
class ResultadoResumenResponse(BaseModel):
    """Resumen de un resultado (para listados)."""
    resultado_id: UUID
    evaluacion_id: UUID
    identificador: Optional[str] = None
    identificador_persona: Optional[str] = None
    puntuacion_total: float
    total_preguntas: int
    respuestas_correctas: int
    created_at: datetime

    class Config:
        from_attributes = True


class ResultadoCompletoResponse(ResultadoResumenResponse):
    """Resultado completo con el detalle por pregunta y los archivos en base64."""
    respuestas: List[RespuestaDetalleResponse]
    pdf_revisado_base64: Optional[str] = None
    imagen_anotada_base64: Optional[str] = None
