import os
import json
import base64
import hashlib
import logging
import math
import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import or_
from uuid import UUID
from typing import List
from app.database import get_db
from app.models import Evaluacion, Pregunta, Opcion, Resultado, RespuestaDetalle, HojaRespuesta
from app.schemas import EvaluarRequest, ResultadoResponse, RespuestaDetalleResponse, HojaResultadoResponse, ResultadoLoteResponse, ResultadoResumenResponse, ResultadoCompletoResponse
from app.services.omr_processor import (
    detect_identifier_section,
    detect_answer_bubbles, decode_image_base64, pdf_to_images,
    preprocess_image, detect_corner_marks, apply_perspective_transform,
    _find_header_boundary, _group_into_rows, is_bubble_filled,
    detect_qr_codes, enderezar_imagen,
    leer_respuestas_con_coords, leer_id_con_coords, leer_id_con_circulos
)
from app.services.sheet_generator import generar_pdf_revisado, cargar_coordenadas, generar_hoja_respuestas
from app.routes.generar import _aplicar_snapshot, _construir_preguntas_list, _extraer_correctas
from app.config import STORAGE_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Evaluación de escaneos"])


def _compute_scale(coords: dict, image_shape: tuple) -> float:
    """
    Compute the actual scale factor between coordinates and the scanned image.
    The coords were generated at SCALE_3X, but the actual scan might be different.
    """
    h_img, w_img = image_shape[:2]
    page_w = coords.get("page", {}).get("width", 612)
    page_h = coords.get("page", {}).get("height", 792)
    exported_scale = coords.get("scale_factor", 3)

    # The actual scale is: image_size / page_size
    actual_scale_x = w_img / page_w
    actual_scale_y = h_img / page_h
    actual_scale = (actual_scale_x + actual_scale_y) / 2

    return actual_scale


def _detect_id_from_coords(image: np.ndarray, coords: dict) -> str:
    """
    Detecta el ID persona usando coordenadas conocidas del JSON.
    Re-escala las coordenadas para que coincidan con la imagen real.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h_img, w_img = gray.shape

    id_grid_pdf = coords.get("id_grid", [])
    if not id_grid_pdf:
        return ""

    # Recompute coordinates at actual image scale
    page_w = coords.get("page", {}).get("width", 612)
    page_h = coords.get("page", {}).get("height", 792)
    scale = _compute_scale(coords, image.shape)

    # Group by row
    rows = {}
    for b in id_grid_pdf:
        row = b["row"]
        if row not in rows:
            rows[row] = []
        rows[row].append(b)

    identifier = ""
    for row_idx in range(10):
        if row_idx not in rows:
            identifier += " "
            continue

        row_bubbles = sorted(rows[row_idx], key=lambda b: b["cx_img"])
        filled_col = -1
        best_score = 0

        for b in row_bubbles:
            # Recompute position at actual scale
            cx = int(b["cx_img"] * scale / coords.get("scale_factor", 3))
            cy = int(b["cy_img"] * scale / coords.get("scale_factor", 3))
            r = max(3, int(b["r_img"] * scale / coords.get("scale_factor", 3)))

            # Clamp to image bounds
            y1 = max(0, cy - r - 2)
            y2 = min(h_img, cy + r + 2)
            x1 = max(0, cx - r - 2)
            x2 = min(w_img, cx + r + 2)
            roi = gray[y1:y2, x1:x2]
            if roi.size == 0:
                continue

            mean_val = np.mean(roi)
            if mean_val < 160:
                score = 256 - mean_val
                if score > best_score:
                    best_score = score
                    filled_col = b["col"]

        if filled_col >= 0:
            identifier += str(filled_col)
        else:
            # Empty row: use space to preserve position for middle digits
            identifier += " "

    # Trim trailing spaces (unmarked rows at the end)
    return identifier.rstrip()


def _detect_answers_from_coords(image: np.ndarray, coords: dict, num_questions: int) -> list:
    """
    Detecta las respuestas usando coordenadas conocidas del JSON.
    Re-escala las coordenadas para la imagen real.
    Retorna lista de respuestas (0-based index o None) por pregunta.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h_img, w_img = gray.shape

    answer_bubbles_pdf = coords.get("answer_bubbles", [])
    if not answer_bubbles_pdf:
        return [None] * num_questions

    scale_factor = coords.get("scale_factor", 3)
    scale = _compute_scale(coords, image.shape)
    ratio = scale / scale_factor

    # Group by question_index
    questions = {}
    for b in answer_bubbles_pdf:
        qi = b["question_index"]
        if qi not in questions:
            questions[qi] = []
        questions[qi].append(b)

    answers = []
    for qi in range(num_questions):
        if qi not in questions:
            answers.append(None)
            continue

        q_bubbles = sorted(questions[qi], key=lambda b: b["option_index"])
        filled_options = []

        for b in q_bubbles:
            cx = int(b["cx_img"] * ratio)
            cy = int(b["cy_img"] * ratio)
            r = max(3, int(b["r_img"] * ratio))

            y1 = max(0, cy - r - 2)
            y2 = min(h_img, cy + r + 2)
            x1 = max(0, cx - r - 2)
            x2 = min(w_img, cx + r + 2)
            roi = gray[y1:y2, x1:x2]
            if roi.size == 0:
                continue

            mean_val = np.mean(roi)
            if mean_val < 160:
                filled_options.append(b["option_index"])

        if len(filled_options) == 1:
            answers.append(filled_options[0])
        elif len(filled_options) > 1:
            answers.append(filled_options[0])
        else:
            answers.append(None)

    return answers


def _detect_multiple_answers_from_coords(image: np.ndarray, coords: dict, num_questions: int) -> list:
    """
    Detecta respuestas (incluyendo múltiples) usando coordenadas conocidas.
    Retorna lista de listas de índices marcados por pregunta.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h_img, w_img = gray.shape

    answer_bubbles_pdf = coords.get("answer_bubbles", [])
    if not answer_bubbles_pdf:
        return [[] for _ in range(num_questions)]

    scale_factor = coords.get("scale_factor", 3)
    scale = _compute_scale(coords, image.shape)
    ratio = scale / scale_factor

    # Group by question_index
    questions = {}
    for b in answer_bubbles_pdf:
        qi = b["question_index"]
        if qi not in questions:
            questions[qi] = []
        questions[qi].append(b)

    all_answers = []
    for qi in range(num_questions):
        if qi not in questions:
            all_answers.append([])
            continue

        q_bubbles = sorted(questions[qi], key=lambda b: b["option_index"])
        filled = []

        for b in q_bubbles:
            cx = int(b["cx_img"] * ratio)
            cy = int(b["cy_img"] * ratio)
            r = max(3, int(b["r_img"] * ratio))

            y1 = max(0, cy - r - 2)
            y2 = min(h_img, cy + r + 2)
            x1 = max(0, cx - r - 2)
            x2 = min(w_img, cx + r + 2)
            roi = gray[y1:y2, x1:x2]
            if roi.size == 0:
                continue

            mean_val = np.mean(roi)
            if mean_val < 160:
                filled.append(b["option_index"])

        all_answers.append(filled)

    return all_answers


def _find_coords_for_hoja(db: Session, evaluacion_id: UUID, identificador: str) -> dict:
    """
    Busca las coordenadas JSON para una hoja de respuestas.
    Primero busca por evaluación + identificador, luego por QR short_id.
    """
    # Find the hoja in DB
    hoja = db.query(HojaRespuesta).filter(
        HojaRespuesta.evaluacion_id == evaluacion_id,
        HojaRespuesta.identificador == identificador
    ).first()

    if not hoja or not hoja.pdf_path:
        return None

    # The coords file is in the same directory as the PDF
    pdf_dir = os.path.dirname(os.path.join(STORAGE_PATH, hoja.pdf_path))
    coords_path = os.path.join(pdf_dir, "coordenadas.json")

    if not os.path.exists(coords_path):
        return None

    try:
        with open(coords_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading coords: {e}")
        return None


def _find_coords_by_qr(db: Session, qr_data: str) -> dict:
    """
    Busca coordenadas por short_id del QR.
    """
    evaluacion = db.query(Evaluacion).filter(Evaluacion.short_id == qr_data).first()
    if not evaluacion:
        return None

    hoja = db.query(HojaRespuesta).filter(
        HojaRespuesta.evaluacion_id == evaluacion.id
    ).order_by(HojaRespuesta.created_at.desc()).first()

    if not hoja or not hoja.pdf_path:
        return None

    pdf_dir = os.path.dirname(os.path.join(STORAGE_PATH, hoja.pdf_path))
    coords_path = os.path.join(pdf_dir, "coordenadas.json")

    if not os.path.exists(coords_path):
        return None

    try:
        with open(coords_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading coords: {e}")
        return None


@router.post("/evaluar", response_model=ResultadoLoteResponse)
def evaluar_hoja(data: EvaluarRequest, db: Session = Depends(get_db)):
    """
    Evalúa un escaneo (PDF de varias hojas o imagen) en base64.
    Procesa TODAS las páginas que contengan un QR válido (cada hoja se evalúa
    por separado, incluso si mezclan evaluaciones distintas); las páginas
    sin QR se ignoran y se reportan.
    """
    if not data.pdf_escaneado_base64 and not data.imagen_escaneada_base64:
        raise HTTPException(
            status_code=400,
            detail="Debe proporcionar pdf_escaneado_base64 o imagen_escaneada_base64"
        )

    is_pdf = bool(data.pdf_escaneado_base64)
    raw_data = data.pdf_escaneado_base64 if is_pdf else data.imagen_escaneada_base64

    # Decodificar todas las páginas
    try:
        if is_pdf:
            paginas = pdf_to_images(raw_data)
        else:
            unica = decode_image_base64(raw_data)
            paginas = [unica] if unica is not None else []
    except Exception as e:
        logger.error(f"Error decodificando escaneo: {e}")
        raise HTTPException(status_code=400, detail=f"Error procesando archivo: {str(e)}")

    if not paginas:
        raise HTTPException(status_code=400, detail="No se pudo decodificar el escaneo")

    resultados = []
    errores = []
    for numero_pagina, image in enumerate(paginas, start=1):
        try:
            resultados.append(_evaluar_pagina(db, data, raw_data, is_pdf, image, numero_pagina))
        except HTTPException as e:
            errores.append({"pagina": numero_pagina, "detalle": e.detail})
        except Exception as e:
            logger.error(f"Error evaluando página {numero_pagina}: {e}")
            errores.append({"pagina": numero_pagina, "detalle": str(e)})

    if not resultados:
        resumen = "; ".join(f"pág {e['pagina']}: {e['detalle']}" for e in errores)
        raise HTTPException(status_code=400, detail=f"No se pudo evaluar ninguna página ({resumen})")

    return ResultadoLoteResponse(
        total_paginas=len(paginas),
        paginas_evaluadas=len(resultados),
        paginas_ignoradas=len(paginas) - len(resultados),
        resultados=resultados,
        errores=errores
    )


def _evaluar_pagina(db: Session, data: EvaluarRequest, raw_data: str, is_pdf: bool, image, numero_pagina: int) -> ResultadoResponse:
    """Evalúa una sola página ya decodificada. Lanza HTTPException si la página
    no contiene un QR resoluble."""
    # QR de esta página
    qr_data_list = detect_qr_codes(image)
    logger.info(f"[eval] pág {numero_pagina}: img={image.shape[1]}x{image.shape[0]}, QR detectado(s)={qr_data_list}")

    if not qr_data_list:
        raise HTTPException(status_code=400, detail="Página sin código QR")

    # Hoja genérica (sin examen): debe leerse con /leer
    for qr_str in qr_data_list:
        try:
            qr = json.loads(qr_str.strip())
            if qr.get("tipo") == "generica":
                raise HTTPException(
                    status_code=400,
                    detail="Página de hoja genérica: use el endpoint /leer (no /evaluar)",
                )
        except (json.JSONDecodeError, ValueError):
            continue

    # Find evaluation QR
    evaluacion = None
    identificador = None
    for qr_str in qr_data_list:
        qr_str = qr_str.strip()
        # Try as short_id (8 chars hex)
        if len(qr_str) == 8:
            evaluacion = db.query(Evaluacion).filter(Evaluacion.short_id == qr_str).first()
            if evaluacion:
                break
        # Try as UUID
        try:
            evaluacion = db.query(Evaluacion).filter(Evaluacion.id == UUID(qr_str)).first()
            if evaluacion:
                break
        except ValueError:
            pass
        # Try as JSON (legacy format)
        try:
            qr = json.loads(qr_str)
            if qr.get("tipo") == "evaluacion" and "evaluacion_id" in qr:
                evaluacion = db.query(Evaluacion).filter(Evaluacion.id == UUID(qr["evaluacion_id"])).first()
                identificador = qr.get("identificador")
                if evaluacion:
                    break
            if "short_id" in qr:
                evaluacion = db.query(Evaluacion).filter(Evaluacion.short_id == qr["short_id"]).first()
                identificador = qr.get("identificador") or identificador
                if evaluacion:
                    break
        except (json.JSONDecodeError, ValueError):
            continue

    if not evaluacion:
        raise HTTPException(
            status_code=400,
            detail="No se encontró evaluación válida en el QR"
        )

    # El identificador explícito del request tiene prioridad (permite desambiguar variantes)
    identificador = data.identificador or identificador
    logger.info(
        f"[eval] pág {numero_pagina}: evaluación='{evaluacion.nombre}' ({evaluacion.id}), "
        f"identificador='{identificador}'"
    )

    evaluacion_id = evaluacion.id
    if not evaluacion:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada en BD")

    # Get questions and correct answers
    preguntas = db.query(Pregunta).filter(
        Pregunta.evaluacion_id == evaluacion_id
    ).order_by(Pregunta.orden).all()

    if not preguntas:
        raise HTTPException(status_code=400, detail="La evaluación no tiene preguntas")

    # --- Resolver la variante de la hoja: si esta hoja se generó con orden
    # aleatorio, reordenar las preguntas según el snapshot guardado para que
    # las posiciones de las burbujas coincidan con las preguntas correctas ---
    hoja_escaneada = None
    orden_personalizado = False
    if identificador:
        hoja_escaneada = db.query(HojaRespuesta).filter(
            HojaRespuesta.evaluacion_id == evaluacion_id,
            HojaRespuesta.identificador == identificador
        ).first()
        if hoja_escaneada and hoja_escaneada.preguntas_orden:
            preguntas = _aplicar_snapshot(db, evaluacion_id, preguntas, hoja_escaneada.preguntas_orden)
            orden_personalizado = True

    logger.info(
        f"[eval] pág {numero_pagina}: preguntas_tras_snapshot={len(preguntas)}, "
        f"orden_personalizado={orden_personalizado}, hoja_en_bd={hoja_escaneada is not None}"
    )

    # Build correct answers map: question_index -> list of correct option indices
    correct_answers = {}
    option_labels = ["A", "B", "C", "D", "E"]
    for idx, p in enumerate(preguntas):
        correct_indices = []
        for opt in p.opciones:
            if opt.es_correcta:
                key = opt.key.upper().strip()
                if key in option_labels:
                    correct_indices.append(option_labels.index(key))
        correct_answers[idx] = correct_indices
    # Enderezado visual + estimacion de transformacion por marcas de esquina.
    # La lectura por coordenadas se hace sobre la imagen ORIGINAL (sin
    # interpolaciones) mapeando hoja->imagen con la similitud A invertida.
    image_original = image
    image, transform = enderezar_imagen(image)
    _A = transform.get("A")
    if _A is not None:
        _escala = math.hypot(_A[0, 0], _A[0, 1])
        _traslacion = (round(_A[0, 2], 1), round(_A[1, 2], 1))
        _matriz_str = "[" + ",".join(f"{v:.4f}" for v in _A.reshape(-1)) + "]"
    else:
        _escala, _traslacion, _matriz_str = None, None, "None"
    logger.info(
        f"[eval] pág {numero_pagina}: img_original={image_original.shape[1]}x{image_original.shape[0]}, "
        f"img_nivelada={image.shape[1]}x{image.shape[0]}, "
        f"enderezada={transform['enderezada']}, origen={transform.get('origen')}, "
        f"ángulo={transform.get('angulo', 0):.2f}°, escala={_escala}, traslación={_traslacion}, "
        f"A=[{_matriz_str}]"
    )

    # Coordenadas conocidas de esta hoja (identificada por QR)
    coords_hoja = None
    if hoja_escaneada and hoja_escaneada.pdf_path:
        _coords_path = os.path.join(
            os.path.dirname(os.path.join(STORAGE_PATH, hoja_escaneada.pdf_path)),
            "coordenadas.json"
        )
        if os.path.exists(_coords_path):
            try:
                with open(_coords_path) as f:
                    coords_hoja = json.load(f)
                logger.info(f"[eval] pág {numero_pagina}: coords cargadas desde {_coords_path} "
                            f"(page={coords_hoja.get('page')}, scale={coords_hoja.get('scale_factor')}, "
                            f"burbujas={len(coords_hoja.get('answer_bubbles', []))}, "
                            f"id_grid={len(coords_hoja.get('id_grid', []))})")
            except Exception as e:
                logger.error(f"Error cargando coordenadas de la hoja: {e}")
        else:
            logger.warning(f"[eval] pág {numero_pagina}: no existe coordenadas.json en {_coords_path}")

    # Lectura determinista muestreando las coordenadas conocidas; fallback a
    # detección genérica (HoughCircles/contornos) si no hay coordenadas
    detected_multi = None
    identificador_persona = ""
    if coords_hoja:
        try:
            detected_multi = leer_respuestas_con_coords(
                image_original, coords_hoja, transform, len(preguntas)
            )
            # ID: primero por detección de círculos en el grid (independiente de
            # la traslación del QR, que desvía la malla tupida); replicamos por
            # coordenadas y preferimos el que lea más dígitos concretos.
            id_por_circulos = leer_id_con_circulos(image_original)
            id_por_coords = leer_id_con_coords(image_original, coords_hoja, transform)
            def _dto_score(id_): return sum(1 for ch in id_ if ch.isdigit())
            if _dto_score(id_por_circulos) > _dto_score(id_por_coords):
                identificador_persona = id_por_circulos
            else:
                identificador_persona = id_por_coords
            logger.info(
                f"[eval] pág {numero_pagina}: lectura por coords -> ID(círculos)='{id_por_circulos}', "
                f"ID(coords)='{id_por_coords}' -> usado='{identificador_persona}', marcas={detected_multi}"
            )
        except Exception as e:
            logger.error(f"Lectura por coordenadas falló: {e}")
            detected_multi = None

    if detected_multi is not None:
        logger.info(f"[eval] pág {numero_pagina}: lectura por coordenadas conocidas")
    else:
        num_options = max(len(p.opciones) for p in preguntas) if preguntas else 5
        detected_answers = detect_answer_bubbles(image, len(preguntas), num_options)
        detected_multi = [[d] if d is not None else [] for d in detected_answers]
        identificador_persona = detect_identifier_section(image)
        logger.info(
            f"[eval] pág {numero_pagina}: detección genérica (HoughCircles) -> ID='{identificador_persona}', "
            f"marcas={detected_multi}"
        )

    detected_answers = [m[0] if m else None for m in detected_multi]
    logger.info(f"[eval] pág {numero_pagina}: resultado ID='{identificador_persona}', "
                f"answers={sum(1 for a in detected_answers if a is not None)}/{len(detected_answers)}")

    # Grade each question
    respuestas_detalle = []
    errores_revision: List[str] = []
    total_puntos = 0.0
    respuestas_correctas = 0

    for idx, p in enumerate(preguntas):
        detected = detected_answers[idx] if idx < len(detected_answers) else None
        detected_set = set(detected_multi[idx]) if idx < len(detected_multi) else set()
        correct = correct_answers.get(idx, [])
        correct_set = set(correct)

        n_correctas = len(correct_set)
        n_marcadas = len(detected_set)
        puntos = 0.0
        es_correcta = False

        if correct_set and detected_set:
            if n_marcadas > n_correctas:
                # Marcó más opciones de las esperadas (p.ej. todas las burbujas):
                # la respuesta queda inválida y se avisa para revisión manual.
                marcadas_txt = ",".join(
                    option_labels[i] for i in sorted(detected_set) if i < len(option_labels)
                )
                errores_revision.append(
                    f"{p.nombre}: se marcaron {n_marcadas} opciones ({marcadas_txt}) "
                    f"cuando se esperan {n_correctas} - revisión manual"
                )
            elif n_correctas == 1:
                # Single answer: only exact match counts (nada de superset)
                es_correcta = detected_set == correct_set
            else:
                # Multiple: full points only if exact; otherwise prorrateo por aciertos
                correctas_marcadas = len(detected_set & correct_set)
                es_correcta = detected_set == correct_set
                if correctas_marcadas:
                    puntos = p.puntos * (correctas_marcadas / n_correctas)
                else:
                    puntos = 0.0

        puntos = p.puntos if es_correcta else puntos

        respuesta_texto = ",".join(option_labels[i] for i in sorted(detected_set) if i < len(option_labels)) if detected_set else None

        ambigua = False
        if detected_set and n_marcadas > n_correctas:
            ambigua = True

        correctas_txt = ",".join(option_labels[i] for i in sorted(correct_set) if i < len(option_labels)) if correct_set else "-"
        logger.info(
            f"[eval] pág {numero_pagina}: pregunta {idx + 1} '{p.nombre}': "
            f"correcta(s)=[{correctas_txt}], marcada(s)=[{respuesta_texto or '-'}], "
            f"n={n_marcadas}/{n_correctas}, es_correcta={es_correcta}, puntos={puntos}, ambigua={ambigua}"
        )

        total_puntos += puntos
        if es_correcta:
            respuestas_correctas += 1

        respuestas_detalle.append(RespuestaDetalle(
            pregunta_id=p.id,
            nombre=p.nombre,
            respuesta=respuesta_texto,
            es_correcta=es_correcta,
            puntos_obtenidos=puntos,
            ambigua=ambigua
        ))

    # Create resultado
    resultado = Resultado(
        evaluacion_id=evaluacion_id,
        identificador=identificador,
        identificador_persona=identificador_persona,
        puntuacion_total=total_puntos,
        total_preguntas=len(preguntas),
        respuestas_correctas=respuestas_correctas,
        errores=errores_revision or None
    )
    db.add(resultado)
    db.flush()

    # Save detail
    for rd in respuestas_detalle:
        rd.resultado_id = resultado.id
        db.add(rd)

    db.commit()
    db.refresh(resultado)

    logger.info(
        f"[eval] pág {numero_pagina}: persistido resultado {resultado.id} - "
        f"puntos={total_puntos}, correctas={respuestas_correctas}/{len(preguntas)}, "
        f"errores_revision={errores_revision or '-'}"
    )

    # Generate reviewed PDF
    respuestas_data = []
    for rd in respuestas_detalle:
        respuestas_data.append({
            "nombre": rd.nombre,
            "respuesta": rd.respuesta,
            "es_correcta": rd.es_correcta,
            "puntos_obtenidos": rd.puntos_obtenidos
        })

    try:
        pdf_bytes = generar_pdf_revisado(
            respuestas_data, evaluacion.nombre, identificador or ""
        )
        pdf_revisado_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    except Exception as e:
        logger.error(f"Error generando PDF revisado: {e}")
        pdf_revisado_base64 = None
        pdf_bytes = None

    # Generate annotated image with green marks
    annotated_base64 = None
    try:
        annotated_base64 = _generate_annotated_image(
            image_original, detected_answers, correct_answers, identificador_persona,
            coords=coords_hoja, transform=transform, detected_multi=detected_multi,
            puntos_detalle=[rd.puntos_obtenidos for rd in respuestas_detalle],
            total_puntos=total_puntos,
        )
    except Exception as e:
        logger.error(f"Error generando imagen anotada: {e}")

    # Save files to STORAGE_PATH/{hash}/ (hash por página del escaneo)
    file_hash = hashlib.sha256(f"{raw_data}:{numero_pagina}".encode()).hexdigest()[:16]
    storage_dir = os.path.join(STORAGE_PATH, file_hash)
    os.makedirs(storage_dir, exist_ok=True)

    # Save original file
    try:
        if is_pdf:
            original_path = os.path.join(storage_dir, "original.pdf")
            with open(original_path, "wb") as f:
                f.write(base64.b64decode(raw_data))
        else:
            original_path = os.path.join(storage_dir, "original.png")
            with open(original_path, "wb") as f:
                f.write(base64.b64decode(raw_data))
    except Exception as e:
        logger.error(f"Error guardando archivo original: {e}")

    # Save annotated image
    if annotated_base64:
        try:
            annotated_path = os.path.join(storage_dir, "annotated.png")
            with open(annotated_path, "wb") as f:
                f.write(base64.b64decode(annotated_base64))
        except Exception as e:
            logger.error(f"Error guardando imagen anotada: {e}")

    # Save reviewed PDF (si fue generado) y registrar su ruta en el resultado
    pdf_revisado_relpath = None
    if pdf_bytes:
        try:
            pdf_revisado_path = os.path.join(storage_dir, "pdf_revisado.pdf")
            with open(pdf_revisado_path, "wb") as f:
                f.write(pdf_bytes)
            pdf_revisado_relpath = os.path.join(file_hash, "pdf_revisado.pdf")
        except Exception as e:
            logger.error(f"Error guardando PDF revisado: {e}")

    if pdf_revisado_relpath:
        resultado.pdf_revisado_path = pdf_revisado_relpath
        db.add(resultado)
        db.commit()
        db.refresh(resultado)

    # Save metadata
    try:
        metadata = {
            "resultado_id": str(resultado.id),
            "identificador": identificador,
            "identificador_persona": identificador_persona,
            "evaluacion_id": str(evaluacion_id),
            "evaluacion_nombre": evaluacion.nombre,
            "puntuacion_total": total_puntos,
            "total_preguntas": len(preguntas),
            "respuestas_correctas": respuestas_correctas,
            "orden_personalizado": orden_personalizado,
            "pagina": numero_pagina,
            "enderezada": transform["enderezada"],
            "lectura_por_coords": coords_hoja is not None and detected_multi is not None,
            "qr_codes": qr_data_list,
            "errores": errores_revision
        }
        metadata_path = os.path.join(storage_dir, "metadata.json")
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error guardando metadata: {e}")

    logger.info(
        f"[eval] pág {numero_pagina}: Ø completo -> storage={storage_dir}, "
        f"lectura_por_coords={coords_hoja is not None and detected_multi is not None}"
    )

    return ResultadoResponse(
        resultado_id=resultado.id,
        identificador=resultado.identificador,
        identificador_persona=identificador_persona,
        puntuacion_total=resultado.puntuacion_total,
        total_preguntas=resultado.total_preguntas,
        respuestas_correctas=resultado.respuestas_correctas,
        respuestas=[
            RespuestaDetalleResponse(
                nombre=rd.nombre,
                pregunta_id=rd.pregunta_id,
                respuesta=rd.respuesta,
                es_correcta=rd.es_correcta,
                puntos_obtenidos=rd.puntos_obtenidos,
                ambigua=rd.ambigua
            )
            for rd in respuestas_detalle
        ],
        pdf_revisado_base64=pdf_revisado_base64,
        imagen_anotada_base64=annotated_base64,
        qr_codes=qr_data_list,
        storage_path=storage_dir,
        errores=errores_revision
    )


@router.get("/resultados/{referencia}", response_model=HojaResultadoResponse)
def obtener_hoja_resultados(
    referencia: str,
    evaluacion_id: UUID = None,
    short_id: str = None,
    db: Session = Depends(get_db)
):
    """Devuelve en base64 la hoja de resultados (clave con el llenado correcto
    de la hoja de respuestas) generada junto con las hojas.
    `referencia` acepta el identificador de la hoja ('Eva-2026-001') o su hoja_id (UUID)."""
    filtros = [HojaRespuesta.identificador == referencia]
    try:
        ref_uuid = UUID(referencia)
        filtros.append(HojaRespuesta.id == ref_uuid)
    except (ValueError, AttributeError):
        pass

    query = db.query(HojaRespuesta).filter(or_(*filtros))

    if evaluacion_id:
        query = query.filter(HojaRespuesta.evaluacion_id == evaluacion_id)
    elif short_id:
        ev = db.query(Evaluacion).filter(Evaluacion.short_id == short_id).first()
        if not ev:
            raise HTTPException(status_code=404, detail="Evaluación no encontrada")
        query = query.filter(HojaRespuesta.evaluacion_id == ev.id)

    hojas = query.all()
    if not hojas:
        raise HTTPException(
            status_code=404,
            detail=(f"No existe hoja para '{referencia}'. Debe ser el identificador de la hoja "
                    f"(el texto enviado al generar, p.ej. 'Eva-2026-001') o el hoja_id; "
                    f"también puede desambiguar con ?short_id= o ?evaluacion_id=")
        )
    if len(hojas) > 1:
        raise HTTPException(status_code=400, detail="Referencia ambigua: especifique evaluacion_id o short_id")

    hoja = hojas[0]

    # Localizar hojas_resultado.pdf; si falta, se regenera desde el snapshot
    clave_path = None
    if hoja.pdf_path:
        dir_path = os.path.dirname(os.path.join(STORAGE_PATH, hoja.pdf_path))
        clave_path = os.path.join(dir_path, "hojas_resultado.pdf")

    if not clave_path or not os.path.exists(clave_path):
        ev = db.query(Evaluacion).filter(Evaluacion.id == hoja.evaluacion_id).first()
        preguntas = db.query(Pregunta).filter(
            Pregunta.evaluacion_id == hoja.evaluacion_id
        ).order_by(Pregunta.orden).all()
        if not ev or not preguntas:
            raise HTTPException(status_code=404, detail="No se puede regenerar la hoja de resultados")
        if hoja.preguntas_orden:
            preguntas = _aplicar_snapshot(db, hoja.evaluacion_id, preguntas, hoja.preguntas_orden)
        preguntas_list = _construir_preguntas_list(preguntas)
        pdf_bytes, _ = generar_hoja_respuestas(
            evaluacion_nombre=ev.nombre,
            identificador=hoja.identificador,
            preguntas=preguntas_list,
            descripcion=ev.descripcion or "",
            short_id=ev.short_id,
            respuestas_correctas=_extraer_correctas(preguntas_list),
            es_clave=True
        )
        if clave_path is None:
            file_hash = hashlib.sha256(f"{hoja.evaluacion_id}:{hoja.identificador}".encode()).hexdigest()[:16]
            dir_path = os.path.join(STORAGE_PATH, "hojas", file_hash)
            os.makedirs(dir_path, exist_ok=True)
            clave_path = os.path.join(dir_path, "hojas_resultado.pdf")
        with open(clave_path, "wb") as f:
            f.write(pdf_bytes)

    with open(clave_path, "rb") as f:
        pdf_base64 = base64.b64encode(f.read()).decode("utf-8")

    return HojaResultadoResponse(
        identificador=hoja.identificador,
        evaluacion_id=hoja.evaluacion_id,
        cantidad_preguntas=len(hoja.preguntas_orden or []),
        pdf_base64=pdf_base64
    )


@router.get("/resultado/{resultado_id}", response_model=ResultadoCompletoResponse)
def obtener_resultado(resultado_id: UUID, db: Session = Depends(get_db)):
    """Devuelve un resultado completo: puntuación, detalle por pregunta (en el
    orden del banco) y los archivos pdf_revisado / imagen_anotada en base64."""
    resultado = db.query(Resultado).filter(Resultado.id == resultado_id).first()
    if not resultado:
        raise HTTPException(status_code=404, detail="Resultado no encontrado")

    respuestas = (
        db.query(RespuestaDetalle)
        .filter(RespuestaDetalle.resultado_id == resultado.id)
        .join(Pregunta, Pregunta.id == RespuestaDetalle.pregunta_id)
        .order_by(Pregunta.orden)
        .all()
    )

    # Archivos guardados junto al resultado (ruta relativa a STORAGE_PATH)
    pdf_revisado_base64 = None
    imagen_anotada_base64 = None
    if resultado.pdf_revisado_path:
        base_dir = os.path.dirname(os.path.join(STORAGE_PATH, resultado.pdf_revisado_path))
        pdf_revisado_path = os.path.join(base_dir, "pdf_revisado.pdf")
        if os.path.exists(pdf_revisado_path):
            with open(pdf_revisado_path, "rb") as f:
                pdf_revisado_base64 = base64.b64encode(f.read()).decode("utf-8")
        annotated_path = os.path.join(base_dir, "annotated.png")
        if os.path.exists(annotated_path):
            with open(annotated_path, "rb") as f:
                imagen_anotada_base64 = base64.b64encode(f.read()).decode("utf-8")

    return ResultadoCompletoResponse(
        resultado_id=resultado.id,
        evaluacion_id=resultado.evaluacion_id,
        identificador=resultado.identificador,
        identificador_persona=resultado.identificador_persona,
        puntuacion_total=resultado.puntuacion_total,
        total_preguntas=resultado.total_preguntas,
        respuestas_correctas=resultado.respuestas_correctas,
        created_at=resultado.created_at,
        respuestas=[
            RespuestaDetalleResponse(
                nombre=rd.nombre,
                pregunta_id=rd.pregunta_id,
                respuesta=rd.respuesta,
                es_correcta=rd.es_correcta,
                puntos_obtenidos=rd.puntos_obtenidos,
                ambigua=rd.ambigua
            )
            for rd in respuestas
        ],
        pdf_revisado_base64=pdf_revisado_base64,
        imagen_anotada_base64=imagen_anotada_base64,
        errores=resultado.errores or []
    )


@router.get("/evaluacion/{evaluacion_id}/resultados", response_model=List[ResultadoResumenResponse])
def listar_resultados(evaluacion_id: UUID, db: Session = Depends(get_db)):
    """Lista los resultados de una evaluación (sin detalle), más recientes primero."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    resultados = db.query(Resultado).filter(
        Resultado.evaluacion_id == evaluacion_id
    ).order_by(Resultado.created_at.desc()).all()
    return [
        ResultadoResumenResponse(
            resultado_id=r.id,
            evaluacion_id=r.evaluacion_id,
            identificador=r.identificador,
            identificador_persona=r.identificador_persona,
            puntuacion_total=r.puntuacion_total,
            total_preguntas=r.total_preguntas,
            respuestas_correctas=r.respuestas_correctas,
            created_at=r.created_at,
        )
        for r in resultados
    ]


@router.delete("/resultado/{resultado_id}")
def eliminar_resultado(resultado_id: UUID, db: Session = Depends(get_db)):
    """Elimina un resultado y su detalle (los archivos en storage se conservan)."""
    resultado = db.query(Resultado).filter(Resultado.id == resultado_id).first()
    if not resultado:
        raise HTTPException(status_code=404, detail="Resultado no encontrado")
    db.delete(resultado)
    db.commit()
    return {"detail": "Eliminado"}


def _generate_annotated_image(
    image: np.ndarray,
    detected_answers: list,
    correct_answers: dict,
    identificador_persona: str,
    coords: dict = None,
    transform: dict = None,
    detected_multi: list = None,
    puntos_detalle: list = None,
    total_puntos: float = None,
) -> str:
    """
    Genera imagen anotada con V/X sobre cada burbuja.
    Si coords está disponible, usa coordenadas conocidas para posicionar marcas.
    Muestra el puntaje por pregunta bajo cada fila y el total (suma) centrado
    arriba si se provee puntos_detalle/total_puntos.
    """
    annotated = image.copy()
    h, w = annotated.shape[:2]

    GREEN = (0, 180, 0)
    RED = (0, 0, 220)
    YELLOW = (0, 200, 255)
    option_labels = ["A", "B", "C", "D", "E"]

    total = len(detected_answers)
    correctas = sum(
        1 for i, d in enumerate(detected_answers)
        if d is not None and d in correct_answers.get(i, [])
    )

    # Banner superior
    overlay = annotated.copy()
    cv2.rectangle(overlay, (0, 0), (w, 50), (40, 40, 40), -1)
    cv2.addWeighted(overlay, 0.7, annotated, 0.3, 0, annotated)
    cv2.putText(annotated, f"ID: {identificador_persona}", (15, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, GREEN, 2)
    cv2.putText(annotated, f"Correctas: {correctas}/{total}",
                (w - 300, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, GREEN, 2)
    if total_puntos is not None:
        total_txt = f"TOTAL: {total_puntos:g} pts"
        (tw, th), _ = cv2.getTextSize(total_txt, cv2.FONT_HERSHEY_SIMPLEX, 1.1, 3)
        cv2.putText(annotated, total_txt,
                    ((w - tw) // 2, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.1, GREEN, 3)

    def _dibujar_puntaje_pregunta(qi: int, x_izq: int, y_centro: int):
        """Dibuja el puntaje al lado izquierdo del bloque de burbujas, un poco
        más abajo que el centro, con fuente grande sobre fondo negro."""
        if puntos_detalle is None or qi >= len(puntos_detalle):
            return
        pts = puntos_detalle[qi]
        txt = f"{pts:g}pt"
        color = GREEN if pts > 0 else RED
        font_scale = 0.8
        thickness = 2
        (tw_, th_), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
        text_x = x_izq - tw_ - 6
        text_y = int(y_centro + 28 + th_ / 2)
        if text_x < 4:
            # Sin espacio a la izquierda: dibujar a la derecha del bloque
            text_x = x_izq + 6
        cv2.putText(annotated, txt, (text_x, text_y),
                    cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, thickness)

    if coords and coords.get("answer_bubbles"):
        # === USAR COORDENADAS CONOCIDAS ===
        from app.services.omr_processor import _inv_A
        A = (transform or {}).get("A")

        # Group answer bubbles by question_index
        questions = {}
        for b in coords["answer_bubbles"]:
            qi = b["question_index"]
            if qi not in questions:
                questions[qi] = []
            questions[qi].append(b)

        # Mark each bubble
        for qi in range(total):
            detected_set = set(detected_multi[qi]) if detected_multi and qi < len(detected_multi) else set()
            correct_set = set(correct_answers.get(qi, []))

            if qi not in questions:
                continue

            q_bubbles = sorted(questions[qi], key=lambda b: b["option_index"])
            xs_q, ys_q, rs_q = [], [], []
            for b in q_bubbles:
                if A is not None:
                    cx, cy = _inv_A(A, b["cx_img"], b["cy_img"])
                    cx, cy = int(round(cx)), int(round(cy))
                    r_img = max(5, int(b["r_img"] / math.hypot(A[0, 0], A[0, 1])))
                else:
                    scale_factor = coords.get("scale_factor", 3)
                    scale = _compute_scale(coords, image.shape)
                    ratio = scale / scale_factor
                    cx = int(b["cx_img"] * ratio)
                    cy = int(b["cy_img"] * ratio)
                    r_img = max(5, int(b["r_img"] * ratio))
                opt_idx = b["option_index"]

                is_correct = opt_idx in correct_set
                is_marked = opt_idx in detected_set

                if is_marked and is_correct:
                    # Marca correcta: verde + V
                    cv2.circle(annotated, (cx, cy), r_img + 6, GREEN, 3)
                    cv2.putText(annotated, "V", (cx - 8, cy + 6),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 2)
                elif is_marked and not is_correct:
                    # Marca incorrecta: rojo + X
                    cv2.circle(annotated, (cx, cy), r_img + 6, RED, 3)
                    cv2.putText(annotated, "X", (cx - 7, cy + 6),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.5, RED, 2)
                elif not is_marked and is_correct and detected_set:
                    # Respuesta correcta no marcada: amarillo
                    cv2.circle(annotated, (cx, cy), r_img + 4, YELLOW, 2)

                xs_q.append(cx)
                ys_q.append(cy)
                rs_q.append(r_img)

            # Puntaje de la pregunta al lado izquierdo del bloque de burbujas
            if xs_q and puntos_detalle is not None and qi < len(puntos_detalle):
                _dibujar_puntaje_pregunta(
                    qi,
                    min(xs_q) - max(rs_q),
                    int(sum(ys_q) / len(ys_q)),
                )
    else:
        # === FALLBACK: usar HoughCircles para encontrar burbujas ===
        from app.services.omr_processor import _detect_bubbles_hough, _find_header_boundary, _group_into_rows
        boundary = _find_header_boundary(image)
        answer_bubbles = _detect_bubbles_hough(image, boundary)
        answer_bubbles = [b for b in answer_bubbles if b[1] >= boundary + 20]

        if answer_bubbles:
            x_vals = sorted(set(b[0] for b in answer_bubbles))
            x_gaps = []
            for i in range(1, len(x_vals)):
                gap = x_vals[i] - x_vals[i - 1]
                if gap > 100:
                    x_gaps.append((x_vals[i - 1], x_vals[i], gap))

            if len(x_gaps) >= 2:
                x_gaps.sort(key=lambda g: g[2], reverse=True)
                gap_thresholds = sorted([g[1] for g in x_gaps[:2]])
                col_boundaries = [0] + gap_thresholds + [w]
            else:
                col_boundaries = [0, w]

            q_idx = 0
            for ci in range(len(col_boundaries) - 1):
                x_min = col_boundaries[ci]
                x_max = col_boundaries[ci + 1]
                col_bubbles = [b for b in answer_bubbles if x_min <= b[0] < x_max]
                rows = _group_into_rows(col_bubbles, y_threshold=15)
                rows = [r for r in rows if len(r) >= 3]  # filtrar ruido

                for row in rows:
                    row_sorted = sorted(row, key=lambda b: b[0])
                    if q_idx >= total:
                        break

                    detected = detected_answers[q_idx] if q_idx < len(detected_answers) else None
                    correct = correct_answers.get(q_idx, [])

                    for i, (cx, cy, area) in enumerate(row_sorted[:5]):
                        is_correct_answer = i in correct
                        is_detected = detected == i

                        if is_detected and is_correct_answer:
                            cv2.circle(annotated, (cx, cy), 18, GREEN, 3)
                            cv2.putText(annotated, "V", (cx - 8, cy + 6),
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, GREEN, 2)
                        elif is_detected and not is_correct_answer:
                            cv2.circle(annotated, (cx, cy), 18, RED, 3)
                            cv2.putText(annotated, "X", (cx - 7, cy + 6),
                                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, RED, 2)
                        elif is_correct_answer and detected is not None:
                            cv2.circle(annotated, (cx, cy), 18, YELLOW, 2)

                    _dibujar_puntaje_pregunta(
                        q_idx,
                        int(row_sorted[0][0]) - 18,
                        int(row_sorted[0][1]),
                    )
                    q_idx += 1

    _, buffer = cv2.imencode(".png", annotated)
    return base64.b64encode(buffer).decode("utf-8")
