import os
import json
import base64
import hashlib
import random
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from uuid import UUID
from app.database import get_db
from app.models import Evaluacion, Pregunta, HojaRespuesta, Seccion
from app.schemas import GenerarHojaRequest, GenerarHojaResponse
from app.services.sheet_generator import generar_hoja_respuestas, generar_hoja_preguntas, exportar_coordenadas_json
from app.config import STORAGE_PATH

router = APIRouter(prefix="/api/v1", tags=["Generación de hojas de examen"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def autogenerar_identificador(db: Session, evaluacion_id: UUID) -> str:
    """Genera un identificador único {short_id}-{n} (D4)."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")
    base = ev.short_id
    for n in range(1, 10000):
        candidate = f"{base}-{n}"
        exists = db.query(HojaRespuesta).filter(
            HojaRespuesta.evaluacion_id == evaluacion_id,
            HojaRespuesta.identificador == candidate
        ).first()
        if not exists:
            return candidate
    raise HTTPException(status_code=500, detail="No se pudo generar identificador único")


def _obtener_preguntas_ordenadas(db: Session, evaluacion_id: UUID) -> list:
    """Preguntas ordenadas por (Seccion.orden, Pregunta.orden)."""
    return db.query(Pregunta).filter(
        Pregunta.evaluacion_id == evaluacion_id
    ).outerjoin(Seccion, Seccion.id == Pregunta.seccion_id).order_by(
        Seccion.orden, Pregunta.orden
    ).all()


def _construir_preguntas_list(preguntas) -> list:
    """Construye la lista de dicts para los generadores PDF.
    Se renumera 'orden' por posición impresa (1..N)."""
    preguntas_list = []
    for idx, p in enumerate(preguntas, start=1):
        opciones = [{"key": o.key, "label": o.label, "es_correcta": o.es_correcta} for o in p.opciones]
        preguntas_list.append({
            "nombre": p.nombre,
            "enunciado": p.enunciado,
            "tipo": p.tipo.value if hasattr(p.tipo, "value") else p.tipo,
            "opciones": opciones,
            "orden": idx,
            "puntos": p.puntos,
            "seccion": p.seccion or (p.seccion_obj.nombre if p.seccion_obj else "")
        })
    return preguntas_list


def _extraer_correctas(preguntas_list: list) -> dict:
    """Mapa {num_impreso: [idx_opciones_correctas]} para la hoja de resultados/clave."""
    correctas = {}
    for num, p in enumerate(preguntas_list, start=1):
        idxs = [j for j, o in enumerate(p["opciones"][:5]) if o.get("es_correcta")]
        if idxs:
            correctas[num] = idxs
    return correctas


def _aplicar_snapshot(db: Session, evaluacion_id: UUID, preguntas: list, preguntas_orden: list) -> list:
    """Reordena preguntas según la lista de IDs del snapshot."""
    if not preguntas_orden:
        return preguntas
    by_id = {str(p.id): p for p in preguntas}
    ordenadas = [by_id[pid] for pid in preguntas_orden if str(pid) in by_id]
    return ordenadas if ordenadas else preguntas


def _obtener_hoja_preguntas_base64(ev, identificador, absolute_dir, db, hoja=None):
    """Devuelve la hoja de preguntas en base64 (regenera si falta)."""
    absolute_preguntas_path = os.path.join(absolute_dir, "hoja_preguntas.pdf")
    if not os.path.exists(absolute_preguntas_path):
        preguntas = _obtener_preguntas_ordenadas(db, ev.id)
        if not preguntas:
            return None
        if hoja is not None and hoja.preguntas_orden:
            preguntas = _aplicar_snapshot(db, ev.id, preguntas, hoja.preguntas_orden)
        try:
            preguntas_pdf_bytes, _ = generar_hoja_preguntas(
                evaluacion_nombre=ev.nombre,
                identificador=identificador,
                preguntas=_construir_preguntas_list(preguntas),
                descripcion=ev.descripcion or ""
            )
        except Exception:
            return None
        with open(absolute_preguntas_path, "wb") as f:
            f.write(preguntas_pdf_bytes)
    else:
        with open(absolute_preguntas_path, "rb") as f:
            preguntas_pdf_bytes = f.read()
    return base64.b64encode(preguntas_pdf_bytes).decode("utf-8")


# ── Selección por secciones (Caso 1: template) ───────────────────────────────

def _seleccionar_template(
    preguntas_all: list,
    cantidad: int,
    secciones_quota=None,
    aleatorio: bool = False,
) -> tuple:
    """
    Selección template (caso 1).
    1. Cantidad + porcentaje por sección → reparto (mayor resto / Hamilton).
    2. Resto de secciones no mencionadas llenan proporcionalmente.
    3. aleatorio=True:shuffle global; False:orden por sección→pregunta.

    Retorna: (seleccionadas, config_dict)
    """
    total_available = len(preguntas_all)
    if cantidad < 1 or cantidad > total_available:
        raise HTTPException(
            status_code=400,
            detail=f"cantidad_preguntas ({cantidad}) inválida (máximo {total_available})"
        )

    # Agrupar preguntas por sección
    por_seccion: dict[str, list] = {}
    for p in preguntas_all:
        sid = str(p.seccion_id) if p.seccion_id else "_sin_seccion"
        por_seccion.setdefault(sid, []).append(p)

    # Sin distribución por sección → tomar directo
    if not secciones_quota:
        pool = list(preguntas_all)
        if aleatorio:
            random.shuffle(pool)
        seleccionadas = pool[:cantidad]
        config = {"tipo": "template", "cantidad_preguntas": cantidad, "secciones": None, "aleatorio": aleatorio}
        return seleccionadas, config

    # ── Fase 1: cantidades explícitas ──
    counts: dict[str, int] = {}
    for q in secciones_quota:
        sid = str(q.seccion_id)
        if sid not in por_seccion:
            raise HTTPException(status_code=400, detail=f"Sección {q.seccion_id} no tiene preguntas en el banco")
        if q.cantidad is not None:
            avail = len(por_seccion[sid])
            if q.cantidad > avail:
                raise HTTPException(
                    status_code=400,
                    detail=f"Sección {q.seccion_id}: cantidad ({q.cantidad}) > disponible ({avail})"
                )
            counts[sid] = q.cantidad

    # ── Fase 2: porcentajes (Hamilton) ──
    fixed_total = sum(counts.values())
    remaining = cantidad - fixed_total

    pct_quotas = [q for q in secciones_quota if q.porcentaje is not None and str(q.seccion_id) not in counts]

    if pct_quotas and remaining > 0:
        # Valores exactos (floor) + residuos
        floors: dict[str, int] = {}
        fracs: dict[str, float] = {}
        for q in pct_quotas:
            sid = str(q.seccion_id)
            avail = len(por_seccion[sid])
            exact = remaining * q.porcentaje / 100.0
            floors[sid] = int(exact)
            fracs[sid] = exact - floors[sid]

        sum_floors = sum(floors.values())
        leftover = remaining - sum_floors

        # Asignar +1 a los residuos más grandes
        for sid in sorted(fracs, key=lambda s: fracs[s], reverse=True):
            if leftover <= 0:
                break
            avail = len(por_seccion[sid])
            if floors[sid] < avail:
                floors[sid] += 1
                leftover -= 1

        counts.update(floors)

    # ── Fase 3: completar hasta cantidad_preguntas ──
    assigned_total = sum(counts.values())
    deficit = cantidad - assigned_total

    if deficit > 0:
        # 3a. Seciones no mencionadas (proporcional a lo disponible)
        unmentioned = [(sid, len(ps)) for sid, ps in por_seccion.items() if sid not in counts]
        total_avail = sum(a for _, a in unmentioned)
        for sid, avail in unmentioned:
            if deficit <= 0:
                break
            share = round(deficit * avail / total_avail) if total_avail > 0 else 0
            add = min(share, avail, deficit)
            counts[sid] = add
            deficit -= add
        # 3b. Cualquier sección con capacidad restante (incl. mencionadas)
        for sid in por_seccion:
            if deficit <= 0:
                break
            avail = len(por_seccion[sid])
            cur = counts.get(sid, 0)
            add = min(deficit, avail - cur)
            if add > 0:
                counts[sid] = cur + add
                deficit -= add

    elif assigned_total > cantidad:
        excess = assigned_total - cantidad
        for sid in sorted(counts, key=lambda s: counts[s], reverse=True):
            if excess <= 0:
                break
            reduce = min(excess, counts[sid])
            counts[sid] -= reduce
            excess -= reduce

    # ── Seleccionar ──
    reparto = dict(counts)  # snapshot para config
    seleccionadas = []
    if aleatorio:
        for sid, cnt in counts.items():
            pool = list(por_seccion[sid])
            random.shuffle(pool)
            seleccionadas.extend(pool[:cnt])
        random.shuffle(seleccionadas)
    else:
        # Orden determinista: sección → pregunta.orden
        for p in preguntas_all:
            sid = str(p.seccion_id) if p.seccion_id else "_sin_seccion"
            if counts.get(sid, 0) > 0:
                seleccionadas.append(p)
                counts[sid] -= 1

    config_secciones = [
        {"seccion_id": str(q.seccion_id), "cantidad": reparto.get(str(q.seccion_id), 0), "porcentaje": q.porcentaje}
        for q in secciones_quota
    ]
    config = {
        "tipo": "template",
        "cantidad_preguntas": len(seleccionadas),
        "secciones": config_secciones,
        "aleatorio": aleatorio,
    }
    return seleccionadas, config


# ── Subconjunto ordenado explícito (Caso 2: estructurada) ───────────────────

def _seleccionar_estructurada(
    preguntas_all: list,
    preguntas_orden_uuids=None,
) -> tuple:
    """Banco completo en orden (o subconjunto si se pasa lista explícita)."""
    if preguntas_orden_uuids:
        by_id = {str(p.id): p for p in preguntas_all}
        seleccionadas = []
        for pid in preguntas_orden_uuids:
            key = str(pid)
            if key not in by_id:
                raise HTTPException(status_code=400, detail=f"Pregunta {pid} no pertenece a este banco")
            seleccionadas.append(by_id[key])
        config = {"tipo": "estructurada", "preguntas_orden": [str(x) for x in preguntas_orden_uuids]}
    else:
        seleccionadas = list(preguntas_all)
        config = {"tipo": "estructurada"}
    return seleccionadas, config


# ── Resolución de selección (back-compat) ───────────────────────────────────

def _resolve_seleccion(data: GenerarHojaRequest, preguntas_all, evaluacion_id, db):
    """Despacha la selección según data.seleccion o los campos legacy."""
    if data.seleccion is not None:
        if data.seleccion.tipo == "template":
            return _seleccionar_template(
                preguntas_all,
                data.seleccion.cantidad_preguntas,
                data.seleccion.secciones,
                data.seleccion.aleatorio,
            )
        if data.seleccion.tipo == "estructurada":
            return _seleccionar_estructurada(
                preguntas_all,
                data.seleccion.preguntas_orden,
            )
    # Back-compat: campos legacy
    if data.cantidad_preguntas is not None or data.aleatorio:
        cantidad = data.cantidad_preguntas or len(preguntas_all)
        return _seleccionar_template(preguntas_all, cantidad, None, data.aleatorio)
    # Default: banco completo estructurado
    return _seleccionar_estructurada(preguntas_all, None)


# ── Endpoint principal: generar hoja de examen ──────────────────────────────

@router.post("/generar/{evaluacion_id}", response_model=GenerarHojaResponse)
def generar_hoja(evaluacion_id: UUID, data: GenerarHojaRequest, db: Session = Depends(get_db)):
    """Genera las hojas de un examen a partir del banco.

    **Selección**
    - `seleccion.tipo = "template"`: reparto por secciones con `cantidad` (exacto) o
      `porcentaje` (proporción del total, redondeo por mayor resto). El total no puede
      exceder el banco y siempre es exacto.
    - `seleccion.tipo = "estructurada"`: banco completo en su orden, o solo las preguntas
      de `preguntas_orden` si se indica.

    **Identificador (D4)** — opcional. Si falta se genera `{short_id}-{n}`. Llamar de nuevo
    con el mismo `identificador` devuelve la misma variante (reimpresión estable).

    **Respuesta**: PDF de burbujas + hoja de preguntas en base64, y `config_seleccion`
    con el snapshot de la selección aplicada."""
    ev = db.query(Evaluacion).filter(Evaluacion.id == evaluacion_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Evaluación no encontrada")

    # --- D4: identificador auto-gen si falta ---
    identificador = data.identificador
    if not identificador:
        try:
            identificador = autogenerar_identificador(db, evaluacion_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Reimpresión estable: misma (evaluación, identificador) → misma variante
    existing_hoja = db.query(HojaRespuesta).filter(
        HojaRespuesta.evaluacion_id == evaluacion_id,
        HojaRespuesta.identificador == identificador,
    ).first()

    if existing_hoja and existing_hoja.pdf_path:
        absolute_pdf_path = os.path.join(STORAGE_PATH, existing_hoja.pdf_path)
        pdf_dir = os.path.dirname(absolute_pdf_path)
        if os.path.exists(absolute_pdf_path):
            with open(absolute_pdf_path, "rb") as f:
                pdf_bytes = f.read()
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
            try:
                qr_data_dict = json.loads(existing_hoja.qr_data)
            except (json.JSONDecodeError, TypeError):
                qr_data_dict = {"short_id": existing_hoja.qr_data}

            hoja_preguntas_base64 = _obtener_hoja_preguntas_base64(
                ev, identificador, pdf_dir, db, hoja=existing_hoja
            )
            return GenerarHojaResponse(
                hoja_id=existing_hoja.id,
                identificador=existing_hoja.identificador,
                qr_data=qr_data_dict,
                pdf_base64=pdf_base64,
                hoja_preguntas_base64=hoja_preguntas_base64,
                cantidad_preguntas=len(existing_hoja.preguntas_orden or []),
                config_seleccion=existing_hoja.config_seleccion,
            )

    # --- Seleccionar preguntas ---
    preguntas_all = _obtener_preguntas_ordenadas(db, evaluacion_id)
    if not preguntas_all:
        raise HTTPException(status_code=400, detail="La evaluación no tiene preguntas")

    seleccionadas, config_seleccion = _resolve_seleccion(data, preguntas_all, evaluacion_id, db)
    if not seleccionadas:
        raise HTTPException(status_code=400, detail="La selección devolvió 0 preguntas")

    snapshot_orden = [str(p.id) for p in seleccionadas]
    preguntas_list = _construir_preguntas_list(seleccionadas)

    # --- Generar PDFs ---
    pdf_bytes, coords = generar_hoja_respuestas(
        evaluacion_nombre=ev.nombre,
        identificador=identificador,
        preguntas=preguntas_list,
        descripcion=ev.descripcion or "",
        short_id=ev.short_id,
    )
    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
    coords_json = exportar_coordenadas_json(coords)

    pdf_clave_bytes, _ = generar_hoja_respuestas(
        evaluacion_nombre=ev.nombre,
        identificador=identificador,
        preguntas=preguntas_list,
        descripcion=ev.descripcion or "",
        short_id=ev.short_id,
        respuestas_correctas=_extraer_correctas(preguntas_list),
        es_clave=True,
    )

    # --- Storage ---
    file_hash = hashlib.sha256(f"{evaluacion_id}:{identificador}".encode()).hexdigest()[:16]
    relative_dir = os.path.join("hojas", file_hash)
    absolute_dir = os.path.join(STORAGE_PATH, relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)

    for name, data_bytes in [
        ("hoja_respuestas.pdf", pdf_bytes),
        ("coordenadas.json", coords_json.encode()),
        ("hojas_resultado.pdf", pdf_clave_bytes),
    ]:
        with open(os.path.join(absolute_dir, name), "wb" if not isinstance(data_bytes, str) else "w") as f:
            f.write(data_bytes)

    # Snapshot en storage
    with open(os.path.join(absolute_dir, "preguntas_orden.json"), "w") as f:
        json.dump({
            "evaluacion_id": str(evaluacion_id),
            "identificador": identificador,
            "config_seleccion": config_seleccion,
            "cantidad_preguntas": len(seleccionadas),
            "preguntas_orden": snapshot_orden,
        }, f, indent=2)

    # --- Guardar en BD ---
    qr_data_dict = {"short_id": ev.short_id, "identificador": identificador}
    relative_pdf_path = os.path.join(relative_dir, "hoja_respuestas.pdf")

    if existing_hoja:
        existing_hoja.qr_data = json.dumps(qr_data_dict)
        existing_hoja.pdf_path = relative_pdf_path
        existing_hoja.preguntas_orden = snapshot_orden
        existing_hoja.config_seleccion = config_seleccion
        db.commit()
        db.refresh(existing_hoja)
        hoja = existing_hoja
    else:
        hoja = HojaRespuesta(
            evaluacion_id=evaluacion_id,
            identificador=identificador,
            qr_data=json.dumps(qr_data_dict),
            pdf_path=relative_pdf_path,
            preguntas_orden=snapshot_orden,
            config_seleccion=config_seleccion,
        )
        db.add(hoja)
        db.commit()
        db.refresh(hoja)

    # --- Hoja de preguntas ---
    preguntas_pdf_bytes, _ = generar_hoja_preguntas(
        evaluacion_nombre=ev.nombre,
        identificador=identificador,
        preguntas=preguntas_list,
        descripcion=ev.descripcion or "",
    )
    hoja_preguntas_base64 = base64.b64encode(preguntas_pdf_bytes).decode("utf-8")
    with open(os.path.join(absolute_dir, "hoja_preguntas.pdf"), "wb") as f:
        f.write(preguntas_pdf_bytes)

    return GenerarHojaResponse(
        hoja_id=hoja.id,
        identificador=identificador,
        qr_data=qr_data_dict,
        pdf_base64=pdf_base64,
        hoja_preguntas_base64=hoja_preguntas_base64,
        cantidad_preguntas=len(seleccionadas),
        config_seleccion=config_seleccion,
    )
