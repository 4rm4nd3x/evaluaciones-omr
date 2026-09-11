import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Text, Float, Boolean, Integer,
    DateTime, ForeignKey, Enum as SAEnum
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class TipoPregunta(str, enum.Enum):
    SINGLE = "single"
    MULTIPLE = "multiple"


class Evaluacion(Base):
    __tablename__ = "evaluaciones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    short_id = Column(String(8), unique=True, nullable=False, index=True)
    nombre = Column(String(255), nullable=False)
    descripcion = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    preguntas = relationship("Pregunta", back_populates="evaluacion", cascade="all, delete-orphan")
    secciones = relationship("Seccion", back_populates="evaluacion", cascade="all, delete-orphan")
    hojas = relationship("HojaRespuesta", back_populates="evaluacion", cascade="all, delete-orphan")
    resultados = relationship("Resultado", back_populates="evaluacion", cascade="all, delete-orphan")


class Seccion(Base):
    """Sección de preguntas dentro del banco (evaluación)."""
    __tablename__ = "secciones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluacion_id = Column(UUID(as_uuid=True), ForeignKey("evaluaciones.id"), nullable=False)
    nombre = Column(String(255), nullable=False)
    descripcion = Column(Text, nullable=True)
    orden = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)

    evaluacion = relationship("Evaluacion", back_populates="secciones")
    preguntas = relationship("Pregunta", back_populates="seccion_obj", cascade="all, delete-orphan")


class Pregunta(Base):
    __tablename__ = "preguntas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluacion_id = Column(UUID(as_uuid=True), ForeignKey("evaluaciones.id"), nullable=False)
    seccion_id = Column(UUID(as_uuid=True), ForeignKey("secciones.id"), nullable=True)
    nombre = Column(String(255), nullable=False)
    tipo = Column(SAEnum(TipoPregunta), nullable=False, default=TipoPregunta.SINGLE)
    enunciado = Column(Text, nullable=True)
    orden = Column(Integer, nullable=False, default=1)
    puntos = Column(Float, nullable=False, default=1.0)
    requerida = Column(Boolean, nullable=False, default=False)
    seccion = Column(String(255), nullable=True)  # legacy: texto; prefiera seccion_obj

    evaluacion = relationship("Evaluacion", back_populates="preguntas")
    seccion_obj = relationship("Seccion", back_populates="preguntas")
    opciones = relationship("Opcion", back_populates="pregunta", cascade="all, delete-orphan")


class Opcion(Base):
    __tablename__ = "opciones"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pregunta_id = Column(UUID(as_uuid=True), ForeignKey("preguntas.id"), nullable=False)
    key = Column(String(10), nullable=False)
    label = Column(Text, nullable=False)
    es_correcta = Column(Boolean, nullable=False, default=False)
    orden = Column(Integer, nullable=False, default=1)

    pregunta = relationship("Pregunta", back_populates="opciones")


class HojaRespuesta(Base):
    __tablename__ = "hojas_respuesta"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluacion_id = Column(UUID(as_uuid=True), ForeignKey("evaluaciones.id"), nullable=False)
    identificador = Column(String(255), nullable=False)
    qr_data = Column(Text, nullable=False)
    pdf_path = Column(Text, nullable=True)
    preguntas_orden = Column(JSONB, nullable=True)  # snapshot: IDs de preguntas en el orden impreso de esta hoja
    config_seleccion = Column(JSONB, nullable=True)  # snapshot de selección: tipo, cantidad, secciones, aleatorio
    created_at = Column(DateTime, default=datetime.utcnow)

    evaluacion = relationship("Evaluacion", back_populates="hojas")


class Resultado(Base):
    __tablename__ = "resultados"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluacion_id = Column(UUID(as_uuid=True), ForeignKey("evaluaciones.id"), nullable=False)
    identificador = Column(String(255), nullable=True)
    identificador_persona = Column(String(20), nullable=True)
    puntuacion_total = Column(Float, nullable=False, default=0.0)
    total_preguntas = Column(Integer, nullable=False, default=0)
    respuestas_correctas = Column(Integer, nullable=False, default=0)
    pdf_revisado_path = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    evaluacion = relationship("Evaluacion", back_populates="resultados")
    respuestas_detalle = relationship("RespuestaDetalle", back_populates="resultado", cascade="all, delete-orphan")


class RespuestaDetalle(Base):
    __tablename__ = "respuestas_detalle"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resultado_id = Column(UUID(as_uuid=True), ForeignKey("resultados.id"), nullable=False)
    pregunta_id = Column(UUID(as_uuid=True), ForeignKey("preguntas.id"), nullable=False)
    nombre = Column(String(255), nullable=False)
    respuesta = Column(String(10), nullable=True)
    es_correcta = Column(Boolean, nullable=False, default=False)
    puntos_obtenidos = Column(Float, nullable=False, default=0.0)

    resultado = relationship("Resultado", back_populates="respuestas_detalle")


class HojaGenerica(Base):
    """Hoja de respuestas genérica (no asociada a una evaluación)."""
    __tablename__ = "hojas_genericas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    identificador = Column(String(255), nullable=False, unique=True, index=True)
    qr_data = Column(Text, nullable=False)
    pdf_path = Column(Text, nullable=True)
    cantidad_preguntas = Column(Integer, nullable=False)
    num_opciones = Column(Integer, nullable=False, default=5)
    created_at = Column(DateTime, default=datetime.utcnow)
