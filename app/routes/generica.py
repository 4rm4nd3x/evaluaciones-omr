import os
import json
import base64
import hashlib
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import HojaGenerica
from app.schemas import (
    GenerarGenericaRequest, GenerarGenericaResponse,
    EvaluarRequest, LecturaGenericaLoteResponse, LecturaGenericaItem, RespuestaLecturaItem,
)
from app.services.sheet_generator import (
    generar_hoja_generica, exportar_coordenadas_json, cargar_coordenadas
)
from app.services.omr_processor import (
    detect_qr_codes, pdf_to_images, decode_image_base64, enderezar_imagen,
    leer_respuestas_con_coords, leer_id_con_coords
)
from app.config import STORAGE_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Hoja genérica y lectura"])

OPTION_LABELS = ["A", "B", "C", "D", "E"]


# ── Parse opciones field ─────────────────────────────────────────────────────

def _parse_opciones(value) -> int:
    """Convierte 'ABCDE' / 'ABCD' / ['A','B','C'] a num_opciones (int)."""
    if isinstance(value, list):
        return max(1, min(len(value), 5))
    if isinstance(value, str):
        return max(1, min(len(value.strip()), 5))
    raise HTTPException(status_code=400, detail="Formato de 'opciones' no válido")


# ── Generar hoja genérica (caso 4) ─────────────────────────────────────────

@router.post("/generar/generica", response_model=GenerarGenericaResponse)
def generar_hoja_generica_endpoint(data: GenerarGenericaRequest, db: Session = Depends(get_db)):
    identificador = data.identificador
    cantidad = data.cantidad_preguntas
    if cantidad < 1:
        raise HTTPException(status_code=400, detail="cantidad_preguntas debe ser ≥ 1")
    num_opciones = _parse_opciones(data.opciones)

    # Reimpresión estable
    existing = db.query(HojaGenerica).filter(HojaGenerica.identificador == identificador).first()
    if existing and existing.pdf_path:
        absolute_pdf_path = os.path.join(STORAGE_PATH, existing.pdf_path)
        if os.path.exists(absolute_pdf_path):
            with open(absolute_pdf_path, "rb") as f:
                pdf_bytes = f.read()
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
            try:
                qr_data_dict = json.loads(existing.qr_data)
            except (json.JSONDecodeError, TypeError):
                qr_data_dict = {"tipo": "generica", "identificador": identificador}
            return GenerarGenericaResponse(
                hoja_id=existing.id,
                identificador=identificador,
                num_opciones=existing.num_opciones,
                qr_data=qr_data_dict,
                pdf_base64=pdf_base64,
            )

    # Generar PDF
    pdf_bytes, coords = generar_hoja_generica(identificador, cantidad, num_opciones)
    coords_json = exportar_coordenadas_json(coords)

    # Storage
    file_hash = hashlib.sha256(f"generica:{identificador}".encode()).hexdigest()[:16]
    relative_dir = os.path.join("hojas_generica", file_hash)
    absolute_dir = os.path.join(STORAGE_PATH, relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)
    relative_pdf_path = os.path.join(relative_dir, "hoja_respuestas.pdf")

    with open(os.path.join(absolute_dir, "hoja_respuestas.pdf"), "wb") as f:
        f.write(pdf_bytes)
    with open(os.path.join(absolute_dir, "coordenadas.json"), "w") as f:
        f.write(coords_json)

    # Snapshot
    with open(os.path.join(absolute_dir, "snapshot.json"), "w") as f:
        json.dump({
            "identificador": identificador,
            "cantidad_preguntas": cantidad,
            "num_opciones": num_opciones,
        }, f, indent=2)

    # BD
    qr_data_dict = {"tipo": "generica", "identificador": identificador}
    hoja = HojaGenerica(
        identificador=identificador,
        qr_data=json.dumps(qr_data_dict),
        pdf_path=relative_pdf_path,
        cantidad_preguntas=cantidad,
        num_opciones=num_opciones,
    )
    db.add(hoja)
    db.commit()
    db.refresh(hoja)

    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    return GenerarGenericaResponse(
        hoja_id=hoja.id,
        identificador=identificador,
        num_opciones=num_opciones,
        qr_data=qr_data_dict,
        pdf_base64=pdf_base64,
    )


@router.get("/generica/{identificador}", response_model=GenerarGenericaResponse)
def obtener_hoja_generica(identificador: str, db: Session = Depends(get_db)):
    """Recupera una hoja genérica por identificador (PDF en base64, reimpresión)."""
    hoja = db.query(HojaGenerica).filter(HojaGenerica.identificador == identificador).first()
    if not hoja or not hoja.pdf_path:
        raise HTTPException(status_code=404, detail="Hoja genérica no encontrada")
    absolute_pdf_path = os.path.join(STORAGE_PATH, hoja.pdf_path)
    if not os.path.exists(absolute_pdf_path):
        raise HTTPException(status_code=404, detail="El PDF de la hoja ya no existe en storage")
    with open(absolute_pdf_path, "rb") as f:
        pdf_bytes = f.read()
    try:
        qr_data_dict = json.loads(hoja.qr_data)
    except (json.JSONDecodeError, TypeError):
        qr_data_dict = {"tipo": "generica", "identificador": identificador}
    return GenerarGenericaResponse(
        hoja_id=hoja.id,
        identificador=identificador,
        num_opciones=hoja.num_opciones,
        qr_data=qr_data_dict,
        pdf_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
    )


@router.delete("/generica/{identificador}")
def eliminar_hoja_generica(identificador: str, db: Session = Depends(get_db)):
    """Elimina una hoja genérica y sus archivos de storage."""
    hoja = db.query(HojaGenerica).filter(HojaGenerica.identificador == identificador).first()
    if not hoja:
        raise HTTPException(status_code=404, detail="Hoja genérica no encontrada")
    if hoja.pdf_path:
        pdf_dir = os.path.dirname(os.path.join(STORAGE_PATH, hoja.pdf_path))
        import shutil
        if os.path.isdir(pdf_dir):
            try:
                shutil.rmtree(pdf_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Error eliminando archivos de hoja genérica: {e}")
    db.delete(hoja)
    db.commit()
    return {"detail": "Eliminada"}


# ── Leer hoja genérica escaneada (caso 4) ──────────────────────────────────

def _cargar_coords_generica(db: Session, identificador: str) -> tuple:
    """Busca la hoja genérica y retorna (coords_dict, hoja_obj) o lanza 404."""
    hoja = db.query(HojaGenerica).filter(HojaGenerica.identificador == identificador).first()
    if not hoja or not hoja.pdf_path:
        return None, None
    pdf_dir = os.path.dirname(os.path.join(STORAGE_PATH, hoja.pdf_path))
    coords_path = os.path.join(pdf_dir, "coordenadas.json")
    if not os.path.exists(coords_path):
        return None, hoja
    try:
        with open(coords_path, "r") as f:
            coords = json.load(f)
        return coords, hoja
    except Exception as e:
        logger.error(f"Error cargando coordenadas genérica: {e}")
        return None, hoja


def _leer_pagina_generica(image, coords, num_preguntas, num_opciones):
    """Lee una página de hoja genérica usando coordenadas conocidas.
    Retorna dict con identificador_persona y respuestas."""
    image_original = image

    # Enderezar (transform) para mapear hoja->imagen con la inversa de A
    _, transform = enderezar_imagen(image_original)

    # Respuestas
    detected_multi = None
    try:
        detected_multi = leer_respuestas_con_coords(image_original, coords, transform, num_preguntas)
    except Exception as e:
        logger.error(f"Lectura coords genérica falló: {e}")
        detected_multi = None

    if detected_multi is None:
        respuestas_raw = [[] for _ in range(num_preguntas)]
    else:
        respuestas_raw = detected_multi

    # ID persona
    identificador_persona = ""
    try:
        identificador_persona = leer_id_con_coords(image_original, coords, transform)
    except Exception:
        pass

    # Mapear índices a letras
    respuestas = []
    for idx, opciones_marcadas in enumerate(respuestas_raw):
        letras = [OPTION_LABELS[i] for i in sorted(set(opciones_marcadas)) if i < num_opciones]
        respuestas.append(RespuestaLecturaItem(pregunta=idx + 1, opciones_seleccionadas=letras))

    return identificador_persona, respuestas


def _leer_pagina_generica_vacia(num_preguntas):
    """Sin coordenadas: devuelve respuestas vacías."""
    return "", [RespuestaLecturaItem(pregunta=i + 1, opciones_seleccionadas=[]) for i in range(num_preguntas)]


@router.post("/leer", response_model=LecturaGenericaLoteResponse)
def leer_hoja_generica(data: EvaluarRequest, db: Session = Depends(get_db)):
    """Lee una hoja genérica escaneada y devuelve lo llenado (sin contrastar)."""
    if not data.pdf_escaneado_base64 and not data.imagen_escaneada_base64:
        raise HTTPException(status_code=400, detail="Proporcione pdf_escaneado_base64 o imagen_escaneada_base64")

    is_pdf = bool(data.pdf_escaneado_base64)
    raw_data = data.pdf_escaneado_base64 if is_pdf else data.imagen_escaneada_base64

    try:
        if is_pdf:
            paginas = pdf_to_images(raw_data)
        else:
            unica = decode_image_base64(raw_data)
            paginas = [unica] if unica is not None else []
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error procesando archivo: {e}")
    if not paginas:
        raise HTTPException(status_code=400, detail="No se pudo decodificar el escaneo")

    lecturas = []
    errores = []
    for num_pagina, image in enumerate(paginas, start=1):
        try:
            # QR
            qr_list = detect_qr_codes(image)
            if not qr_list:
                errores.append({"pagina": num_pagina, "detalle": "Página sin código QR"})
                continue

            # Resolver QR → hoja genérica
            identificador = data.identificador
            for qr_str in qr_list:
                qr_str = qr_str.strip()
                try:
                    qr = json.loads(qr_str)
                    if qr.get("tipo") == "generica" and "identificador" in qr:
                        identificador = qr["identificador"]
                        break
                except (json.JSONDecodeError, ValueError):
                    continue

            if not identificador:
                errores.append({"pagina": num_pagina, "detalle": "No se pudo resolver identificador del QR"})
                continue

            coords, hoja = _cargar_coords_generica(db, identificador)
            num_preguntas = hoja.cantidad_preguntas if hoja else 50
            num_opciones = hoja.num_opciones if hoja else 5

            if coords:
                identificador_persona, respuestas = _leer_pagina_generica(
                    image, coords, num_preguntas, num_opciones
                )
            else:
                identificador_persona, respuestas = _leer_pagina_generica_vacia(
                    num_preguntas
                )

            lecturas.append(LecturaGenericaItem(
                identificador=identificador,
                identificador_persona=identificador_persona,
                cantidad_preguntas=num_preguntas,
                num_opciones=num_opciones,
                respuestas=respuestas,
                qr_codes=qr_list,
            ))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error leyendo página {num_pagina}: {e}")
            errores.append({"pagina": num_pagina, "detalle": str(e)})

    if not lecturas:
        resumen = "; ".join(f"pág {e['pagina']}: {e['detalle']}" for e in errores)
        raise HTTPException(status_code=400, detail=f"No se pudo leer ninguna página ({resumen})")

    return LecturaGenericaLoteResponse(
        total_paginas=len(paginas),
        paginas_leidas=len(lecturas),
        paginas_ignoradas=len(paginas) - len(lecturas),
        lecturas=lecturas,
        errores=errores,
    )
