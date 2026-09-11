import secrets
import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from uuid import UUID
from typing import List
from app.database import get_db
from app.models import Evaluacion, Pregunta, Opcion, Seccion, HojaRespuesta
from app.schemas import (
    EvaluacionCreate, EvaluacionResponse,
    PreguntaBatchCreate, PreguntaResponse,
    SeccionCreate, SeccionUpdate, SeccionBatchCreate, SeccionResponse,
    HojaRespuestaInfo
)

router = APIRouter(prefix="/api/v1", tags=["Banco de preguntas"])


def _generate_short_id(db: Session) -> str:
    """Genera un short_id único de 8 caracteres hex."""
    for _ in range(20):
        short_id = secrets.token_hex(4)  # 8 chars
        exists = db.query(Evaluacion).filter(Evaluacion.short_id == short_id).first()
        if not exists:
            return short_id
    raise ValueError("No se pudo generar short_id único")


def _validar_opciones(opciones, nombre_pregunta: str):
    """D6: una pregunta tiene entre 2 y 5 opciones."""
    if not (2 <= len(opciones) <= 5):
        raise HTTPException(
            status_code=400,
            detail=f"La pregunta '{nombre_pregunta}' debe tener entre 2 y 5 opciones (recibió {len(opciones)})"
        )


@router.post("/evaluacion", response_model=EvaluacionResponse)
def crear_evaluacion(data: EvaluacionCreate, db: Session = Depends(get_db)):
    """Crea una evaluación (banco de preguntas). Genera un `short_id` único usado
    como prefijo del identificador autogenerado de las hojas (D4)."""
    short_id = _generate_short_id(db)
    evaluacion = Evaluacion(nombre=data.nombre, descripcion=data.descripcion, short_id=short_id)
    db.add(evaluacion)
    db.commit()
    db.refresh(evaluacion)
    return evaluacion


@router.get("/evaluaciones", response_model=List[EvaluacionResponse])
def listar_evaluaciones(db: Session = Depends(get_db)):
    """Lista todas las evaluaciones (bancos de preguntas)."""
    return db.query(Evaluacion).all()


@router.get("/evaluacion/{evaluacion_id}", response_model=EvaluacionResponse)
def obtener_evaluacion(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Obtiene una evaluación por su id."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    return ev


@router.delete("/evaluacion/{evaluacion_id}")
def eliminar_evaluacion(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Elimina una evaluación y todo lo asociado (preguntas, secciones, hojas, resultados)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    db.delete(ev)
    db.commit()
    return {"detail": "Eliminada"}


# --- Secciones del banco ---


@router.post("/evaluacion/{evaluacion_id}/secciones", response_model=List[SeccionResponse])
def crear_secciones_batch(evaluacion_id: UUID, data: SeccionBatchCreate, db: Session = Depends(get_db)):
    """Crea varias secciones en el banco de una evaluación (caso 1: reparto por secciones)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")

    secciones_creadas = []
    for s_data in data.secciones:
        seccion = Seccion(
            evaluacion_id=evaluacion_id,
            nombre=s_data.nombre,
            descripcion=s_data.descripcion,
            orden=s_data.orden
        )
        db.add(seccion)
        db.flush()
        secciones_creadas.append(seccion)

    db.commit()
    for s in secciones_creadas:
        db.refresh(s)
    return secciones_creadas


@router.get("/evaluacion/{evaluacion_id}/secciones", response_model=List[SeccionResponse])
def listar_secciones(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Lista las secciones de un banco, ordenadas por su `orden`."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    return db.query(Seccion).filter(
        Seccion.evaluacion_id == evaluacion_id
    ).order_by(Seccion.orden).all()


@router.get("/seccion/{seccion_id}", response_model=SeccionResponse)
def obtener_seccion(seccion_id: UUID, db: Session = Depends(get_db)):
    """Obtiene una sección del banco."""
    seccion = db.query(Seccion).filter(Seccion.id == seccion_id).first()
    if not seccion:
        raise HTTPException(status_code=404, detail="Sección no encontrada")
    return seccion


@router.put("/seccion/{seccion_id}", response_model=SeccionResponse)
def actualizar_seccion(seccion_id: UUID, data: SeccionUpdate, db: Session = Depends(get_db)):
    """Actualiza nombre/descripción/orden de una sección."""
    seccion = db.query(Seccion).filter(Seccion.id == seccion_id).first()
    if not seccion:
        raise HTTPException(status_code=404, detail="Sección no encontrada")
    if data.nombre is not None:
        seccion.nombre = data.nombre
    if data.descripcion is not None:
        seccion.descripcion = data.descripcion
    if data.orden is not None:
        seccion.orden = data.orden
    db.commit()
    db.refresh(seccion)
    return seccion


@router.delete("/seccion/{seccion_id}")
def eliminar_seccion(seccion_id: UUID, db: Session = Depends(get_db)):
    """Elimina una sección y sus preguntas (cascade)."""
    seccion = db.query(Seccion).filter(Seccion.id == seccion_id).first()
    if not seccion:
        raise HTTPException(status_code=404, detail="Sección no encontrada")
    db.delete(seccion)
    db.commit()
    return {"detail": "Eliminada"}


# --- Hojas generadas de una evaluación ---


@router.get("/evaluacion/{evaluacion_id}/hojas", response_model=List[HojaRespuestaInfo])
def listar_hojas(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Lista las hojas de respuestas generadas para una evaluación (recientes primero)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")

    hojas = db.query(HojaRespuesta).filter(
        HojaRespuesta.evaluacion_id == evaluacion_id
    ).order_by(HojaRespuesta.created_at.desc()).all()

    resultado = []
    for h in hojas:
        try:
            qr_data = json.loads(h.qr_data)
        except (TypeError, ValueError):
            qr_data = {"raw": h.qr_data}
        resultado.append(HojaRespuestaInfo(
            hoja_id=h.id,
            evaluacion_id=h.evaluacion_id,
            identificador=h.identificador,
            qr_data=qr_data,
            pdf_path=h.pdf_path,
            cantidad_preguntas=len(h.preguntas_orden or []),
            config_seleccion=h.config_seleccion,
            created_at=h.created_at,
        ))
    return resultado


# --- Preguntas ---


def _resolver_seccion(db: Session, evaluacion_id: UUID, p_data) -> tuple:
    """Resuelve la sección de una pregunta. Retorna (seccion_obj|None, seccion_name|None).
    Prioriza seccion_id (FK); si no, usa el texto legacy buscando/creando la Seccion."""
    if p_data.seccion_id:
        seccion = db.query(Seccion).filter(
            Seccion.id == p_data.seccion_id,
            Seccion.evaluacion_id == evaluacion_id
        ).first()
        if not seccion:
            raise HTTPException(
                status_code=400,
                detail=f"La sección {p_data.seccion_id} no pertenece a esta evaluación/banco"
            )
        return seccion, seccion.nombre

    nombre = (p_data.seccion or "").strip()
    if not nombre:
        return None, None

    seccion = db.query(Seccion).filter(
        Seccion.evaluacion_id == evaluacion_id,
        Seccion.nombre == nombre
    ).first()
    if not seccion:
        next_orden = (db.query(func.max(Seccion.orden)).filter(Seccion.evaluacion_id == evaluacion_id).scalar() or 0) + 1
        seccion = Seccion(evaluacion_id=evaluacion_id, nombre=nombre, orden=next_orden)
        db.add(seccion)
        db.flush()
    return seccion, seccion.nombre


@router.post("/evaluacion/{evaluacion_id}/preguntas/batch", response_model=List[PreguntaResponse])
def crear_preguntas_batch(evaluacion_id: UUID, data: PreguntaBatchCreate, db: Session = Depends(get_db)):
    """Carga preguntas en el banco. Cada pregunta debe tener entre 2 y 5 opciones (D6)
    y puede asociarse a una sección con `seccion_id` (o texto legacy `seccion`)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")

    preguntas_creadas = []
    for p_data in data.preguntas:
        _validar_opciones(p_data.opciones, p_data.nombre)
        seccion_obj, seccion_str = _resolver_seccion(db, evaluacion_id, p_data)

        pregunta = Pregunta(
            evaluacion_id=evaluacion_id,
            seccion_id=seccion_obj.id if seccion_obj else None,
            nombre=p_data.nombre,
            tipo=p_data.tipo,
            enunciado=p_data.enunciado,
            orden=p_data.orden,
            puntos=p_data.puntos,
            requerida=p_data.requerida,
            seccion=seccion_str
        )
        db.add(pregunta)
        db.flush()  # Get the ID

        for idx, opt_data in enumerate(p_data.opciones):
            opcion = Opcion(
                pregunta_id=pregunta.id,
                key=opt_data.key,
                label=opt_data.label,
                es_correcta=opt_data.es_correcta,
                orden=idx + 1
            )
            db.add(opcion)

        preguntas_creadas.append(pregunta)

    db.commit()
    for p in preguntas_creadas:
        db.refresh(p)

    return preguntas_creadas


@router.get("/evaluacion/{evaluacion_id}/preguntas", response_model=List[PreguntaResponse])
def listar_preguntas(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Lista las preguntas de un banco, ordenadas por (sección, orden)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    return db.query(Pregunta).filter(
        Pregunta.evaluacion_id == evaluacion_id
    ).outerjoin(Seccion, Seccion.id == Pregunta.seccion_id).order_by(
        Seccion.orden, Pregunta.orden
    ).all()


@router.delete("/pregunta/{pregunta_id}")
def eliminar_pregunta(pregunta_id: UUID, db: Session = Depends(get_db)):
    """Elimina una pregunta del banco junto con sus opciones."""
    pregunta = db.query(Pregunta).filter(Pregunta.id == pregunta_id).first()
    if not pregunta:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")
    db.delete(pregunta)
    db.commit()
    return {"detail": "Eliminada"}