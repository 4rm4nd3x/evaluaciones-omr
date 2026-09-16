import io
import json
import textwrap
import qrcode
from datetime import date, datetime
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.colors import black, white
from reportlab.lib.utils import ImageReader
from typing import Dict, List, Tuple

# US Letter
PAGE_W, PAGE_H = letter  # 612 x 792

ML = 36
MR = 36
MT = 30
MB = 30

BUBBLE_R = 6
BUBBLE_GAP = 22

FH = "Helvetica-Bold"
FB = "Helvetica"

# Scale factor: PDF points to 3x resolution pixels (for scanning at 300dpi)
# 3x is used in pdf_to_images for QR detection
SCALE_3X = 3

CORNER_SIZE = 12
CORNER_MARGIN = 18


# ============================================================
#  HOJA DE RESPUESTAS (solo burbujas, limpia)
# ============================================================

def _draw_section_chip(c, x, y_baseline, text: str, size: float = 7):
    """Etiqueta de sección tipo chip: banda negra con texto blanco."""
    t = text.upper()
    tw = c.stringWidth(t, FH, size)
    pad_x, pad_y = 5, 2.5
    c.setFillColor(black)
    c.rect(x - pad_x, y_baseline - pad_y, tw + 2 * pad_x, size + 2 * pad_y + 1, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont(FH, size)
    c.drawString(x, y_baseline, t)
    c.setFillColor(black)


def _fecha_impresa(fecha: str = None) -> str:
    """Fecha legible DD/MM/YYYY para el encabezado de la hoja.
    Acepta 'YYYY-MM-DD' (o cualquier valor ISO; se toma la parte de la fecha)
    o 'DD/MM/YYYY'. Si falta o es inválida, usa la fecha del día."""
    if fecha:
        txt = str(fecha)[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(txt, fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
    return date.today().strftime("%d/%m/%Y")


def generar_hoja_respuestas(evaluacion_nombre: str, identificador: str, preguntas: list, descripcion: str = "", short_id: str = "", respuestas_correctas: Dict = None, es_clave: bool = False, qr_dict: dict = None, fecha: str = None, recuadro_firma: bool = False, lineas_separadoras: bool = True) -> Tuple[bytes, Dict]:
    """
    Genera hoja de respuestas PDF + diccionario de coordenadas.
    Si es_clave=True (con respuestas_correctas={num_pregunta: [idx_opciones]}),
    las burbujas correctas se dibujan rellenas: hoja de resultados/clave.
    `fecha` permite imprimir una fecha distinta a la del día (YYYY-MM-DD o DD/MM/YYYY).
    Si recuadro_firma=True se dibuja al pie un recuadro para que el postulante
    anote su Nombre y Firma (no se lee por OMR; solo informativo).
    Si lineas_separadoras=True (default) se dibuja una línea gris sutil entre
    las filas de burbujas de cada columna (ayuda visual; no interfiere con la
    lectura OMR: va a mitad de camino entre filas y en gris claro).
    Retorna: (pdf_bytes, coordenadas_dict)
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(f"Hoja de respuestas - {evaluacion_nombre}")

    # Coordenadas para exportar
    coords = {
        "page": {"width": PAGE_W, "height": PAGE_H},
        "scale_factor": SCALE_3X,
        "id_grid": [],       # [{"row": 0, "col": 0, "cx": x, "cy": y, "r": 4}, ...]
        "id_grid_bounds": {},  # bounding box of ID grid
        "answer_bubbles": [], # [{"question": 1, "option": "A", "cx": x, "cy": y, "r": 6}, ...]
        "answer_bounds": {},  # bounding box of answer section
        "header_boundary_y": 0,  # Y line separating header from answers (PDF coords)
        "corner_marks": [],   # [{"cx": x, "cy": y, "size": 12}, ...]
        "section_markers": [] # [{"cx": x, "cy": y, "label": "..."}, ...] alignment markers
    }

    # === CORNER MARKS (4 esquinas para alineación OMR) ===
    _draw_corner_marks(c)
    coords["corner_marks"] = [
        {"cx": CORNER_MARGIN + CORNER_SIZE/2, "cy": PAGE_H - CORNER_MARGIN - CORNER_SIZE/2, "size": CORNER_SIZE},
        {"cx": PAGE_W - CORNER_MARGIN - CORNER_SIZE/2, "cy": PAGE_H - CORNER_MARGIN - CORNER_SIZE/2, "size": CORNER_SIZE},
        {"cx": CORNER_MARGIN + CORNER_SIZE/2, "cy": CORNER_MARGIN + CORNER_SIZE/2, "size": CORNER_SIZE},
        {"cx": PAGE_W - CORNER_MARGIN - CORNER_SIZE/2, "cy": CORNER_MARGIN + CORNER_SIZE/2, "size": CORNER_SIZE},
    ]

    y = PAGE_H - MT

    # === ROW 1: QR (izq) + Nombre evaluación + ID persona (der) ===
    qr_size = 50
    qr_x = ML
    qr_y = y - qr_size

    # QR arriba a la izquierda
    _draw_qr(c, qr_x, qr_y, short_id, identificador, qr_dict=qr_dict)

    # Nombre de evaluación al lado del QR
    text_x = qr_x + qr_size + 10
    c.setFont(FH, 14)
    name = evaluacion_nombre
    max_name_w = PAGE_W - MR - text_x - 200  # leave space for ID
    while c.stringWidth(name, FH, 14) > max_name_w and len(name) > 5:
        name = name[:-1]
    c.drawString(text_x, qr_y + qr_size - 12, name)

    if descripcion:
        c.setFont(FB, 8)
        c.drawString(text_x, qr_y + qr_size - 24, descripcion[:65])

    c.setFont(FB, 8)
    fecha_str = _fecha_impresa(fecha)
    c.drawString(text_x, qr_y + qr_size - 36, f"Fecha: {fecha_str}     ID: {identificador}")

    # Banner identificador cuando es hoja de resultados/clave
    if es_clave:
        c.setFont(FH, 9)
        c.drawString(text_x, qr_y + qr_size - 50, "CLAVE DE RESULTADOS · LLENADO CORRECTO")

    # === ID PERSONA (der) ===
    id_x = PAGE_W - MR - 170
    id_y_top = y - 4
    id_col_sp = 15
    id_bubble_r = 4
    id_row_sp = 10.5
    grid_x = id_x
    digits = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

    # --- Title bar "ID PERSONA" above the rectangle ---
    id_label_y = id_y_top
    _draw_section_chip(c, id_x, id_label_y, "ID PERSONA")

    # --- Rectangle starts below the label ---
    id_grid_content_top = id_label_y - 14  # digit headers
    id_grid_content_bottom = id_grid_content_top - 7 - 10 * id_row_sp - id_bubble_r

    id_box_pad_x = 8
    id_box_pad_top = 10  # space above digit headers
    id_box_pad_bot = 6
    id_bounds = {
        "x1": grid_x - 20 - id_box_pad_x,
        "y1": id_grid_content_top + id_box_pad_top,
        "x2": grid_x + 10 * id_col_sp + id_bubble_r + id_box_pad_x,
        "y2": id_grid_content_bottom - id_box_pad_bot
    }
    coords["id_grid_bounds"] = id_bounds

    # Draw rectangle frame
    c.setStrokeColor(black)
    c.setLineWidth(1.0)
    c.rect(
        id_bounds["x1"], id_bounds["y2"],
        id_bounds["x2"] - id_bounds["x1"],
        id_bounds["y1"] - id_bounds["y2"],
        stroke=1, fill=0
    )

    # Draw content inside the rectangle
    id_y = id_grid_content_top
    c.setFont(FH, 5.5)
    for i, d in enumerate(digits):
        cx = grid_x + i * id_col_sp + id_bubble_r
        c.drawCentredString(cx, id_y, d)
    id_y -= 7

    for row in range(10):
        c.setFont(FB, 5)
        c.drawRightString(grid_x - 2, id_y + id_bubble_r - 2, str(row + 1))
        for col in range(10):
            cx = grid_x + col * id_col_sp + id_bubble_r
            _draw_bubble(c, cx, id_y, r=id_bubble_r)
            coords["id_grid"].append({
                "row": row,
                "col": col,
                "digit": col,
                "cx": cx,
                "cy": id_y,
                "r": id_bubble_r
            })
        id_y -= id_row_sp

    id_bottom = id_y

    # Record the 4 corners as alignment markers
    coords["section_markers"].extend([
        {"cx": id_bounds["x1"], "cy": id_bounds["y1"], "label": "id_top_left"},
        {"cx": id_bounds["x2"], "cy": id_bounds["y1"], "label": "id_top_right"},
        {"cx": id_bounds["x1"], "cy": id_bounds["y2"], "label": "id_bottom_left"},
        {"cx": id_bounds["x2"], "cy": id_bounds["y2"], "label": "id_bottom_right"},
    ])

    # === INSTRUCCIONES DE LLENADO (debajo del QR, con ejemplo visual de ID) ===
    instrucciones_hoja = [
        ("LÁPIZ", "Use lápiz HB o #2. Rellene los círculos por completo."),
        ("ID PERSONA", "Una columna por cada dígito."),
        ("SIMPLES", "Marque SOLO UNA burbuja (A-E) por pregunta."),
        ("MÚLTIPLES", "Marque TODAS las burbujas correctas."),
    ]
    ex_id = "45689"
    ins_x0 = ML
    ins_x1 = id_bounds["x1"] - 12
    ins_top = qr_y - 8

    # zona derecha reservada para el ejemplo visual
    ex_col_sp, ex_r, ex_row_sp = 9.0, 2.4, 7.0
    ex_zone_w = 10 + 10 * ex_col_sp + 8
    ex_x0 = ins_x1 - ex_zone_w - 6

    left_x = ins_x0 + 9
    left_max = ex_x0 - left_x - 14

    ins_groups = []
    for pre, txt in instrucciones_hoja:
        pw = c.stringWidth(f"{pre}:", FH, 6.5)
        rest = _wrap_pdf(c, txt, FB, 6.5, left_max - pw - 4)
        ins_groups.append((pre, pw, rest))

    title_h = 11
    left_h = title_h + sum(len(r) * 9 for _, _, r in ins_groups) + (len(ins_groups) - 1) * 2
    ex_h = 9 + 6.5 + len(ex_id) * ex_row_sp
    ins_h = max(left_h, ex_h) + 11

    c.setFillColorRGB(0.95, 0.95, 0.95)
    c.setStrokeColorRGB(0.65, 0.65, 0.65)
    c.setLineWidth(0.5)
    c.roundRect(ins_x0, ins_top - ins_h, ins_x1 - ins_x0, ins_h, 4, stroke=1, fill=1)
    c.setFillColor(black)

    # --- Título del bloque ---
    yy = ins_top - 12
    c.setFont(FH, 7)
    c.drawString(left_x, yy, "INSTRUCCIONES DE LLENADO")
    yy -= 11

    # --- Viñetas con prefijo en negrilla ---
    for pre, pw, rest_lines in ins_groups:
        c.setFont(FH, 6.5)
        c.drawString(left_x, yy, f"{pre}:")
        c.setFont(FB, 6.5)
        c.drawString(left_x + pw + 4, yy, rest_lines[0])
        yy -= 9
        for wl in rest_lines[1:]:
            c.setFont(FB, 6.5)
            c.drawString(left_x, yy, wl)
            yy -= 9
        yy -= 2

    # --- Ejemplo visual de ID PERSONA (derecha) ---
    ty2 = ins_top - 12
    c.setFont(FH, 6.5)
    c.drawString(ex_x0, ty2, f"EJEMPLO - ID {ex_id}")
    ty2 -= 8.5
    for di in range(10):
        cx = ex_x0 + 10 + di * ex_col_sp + ex_r
        c.setFont(FH, 5)
        c.drawCentredString(cx, ty2, str(di))
    ty2 -= 7
    for ri in range(len(ex_id)):
        c.setFont(FB, 5)
        c.drawRightString(ex_x0 + 6, ty2 + ex_r - 1.5, str(ri + 1))
        for di in range(10):
            cx = ex_x0 + 10 + di * ex_col_sp + ex_r
            c.setFillColor(black if di == int(ex_id[ri]) else white)
            c.setStrokeColor(black)
            c.setLineWidth(0.5)
            c.circle(cx, ty2, ex_r, stroke=1, fill=1)
        c.setFillColor(black)
        ty2 -= ex_row_sp

    # === Línea separadora debajo de TODO el header (QR + ID) ===
    header_boundary_y = min(qr_y, id_bottom) - 12
    coords["header_boundary_y"] = header_boundary_y
    y = header_boundary_y
    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    c.line(ML, y, PAGE_W - MR, y)
    y -= 12

    # === Respuestas en 4 columnas ===
    # --- Title bar "RESPUESTAS" above the rectangle ---
    _draw_section_chip(c, ML, y, "RESPUESTAS")
    y -= 18

    # --- Rectangle starts below the label ---
    ans_box_pad_x = 8
    ans_box_pad_top = 14  # space for A B C D E headers
    ans_box_pad_bot = 8
    ans_box_x1 = ML - ans_box_pad_x
    ans_box_x2 = id_bounds["x2"]  # alinear borde derecho con ID persona
    ans_usable_w = ans_box_x2 - ans_box_x1 - 2 * ans_box_pad_x

    total = len(preguntas)
    N_ANS_COLS = 4
    per_col = (total + N_ANS_COLS - 1) // N_ANS_COLS
    rows_in_tallest_col = per_col

    # Paso vertical adaptativo: si hay demasiadas filas, se compacta hasta 14pt
    row_step = 18.0
    firma_reserva = 44 if recuadro_firma else 0
    avail_h = y - (MB + 24 + firma_reserva)
    if rows_in_tallest_col > 1:
        max_step = (avail_h - 18 - 12) / (rows_in_tallest_col - 1)
        row_step = max(14.0, min(row_step, max_step))
        row_step = round(row_step * 2) / 2.0

    answer_top_y = y
    answer_bottom_y = answer_top_y - 18 - (rows_in_tallest_col - 1) * row_step - 12

    ans_bounds = {
        "x1": ans_box_x1,
        "y1": answer_top_y + ans_box_pad_top,
        "x2": ans_box_x2,
        "y2": answer_bottom_y - ans_box_pad_bot
    }
    coords["answer_bounds"] = ans_bounds

    # Draw rectangle frame
    c.setStrokeColor(black)
    c.setLineWidth(1.0)
    c.rect(
        ans_bounds["x1"], ans_bounds["y2"],
        ans_bounds["x2"] - ans_bounds["x1"],
        ans_bounds["y1"] - ans_bounds["y2"],
        stroke=1, fill=0
    )

    # Draw bubbles inside the rectangle
    _draw_answers_cols(c, ML, y, preguntas, coords, width=ans_usable_w, n_cols=N_ANS_COLS, row_step=row_step, respuestas_correctas=respuestas_correctas, lineas_separadoras=lineas_separadoras)

    # Record the 4 corners as alignment markers
    coords["section_markers"].extend([
        {"cx": ans_bounds["x1"], "cy": ans_bounds["y1"], "label": "ans_top_left"},
        {"cx": ans_bounds["x2"], "cy": ans_bounds["y1"], "label": "ans_top_right"},
        {"cx": ans_bounds["x1"], "cy": ans_bounds["y2"], "label": "ans_bottom_left"},
        {"cx": ans_bounds["x2"], "cy": ans_bounds["y2"], "label": "ans_bottom_right"},
    ])

    # Recuadro de Nombre y Firma del postulante (informativo, no se lee por OMR)
    if recuadro_firma:
        _draw_recuadro_firma(c)

    c.save()
    return buffer.getvalue(), coords


def _draw_recuadro_firma(c):
    """Recuadro al pie de la hoja para que el postulante anote su Nombre y
    Firma. Es solo informativo: el OMR ignora esta zona (queda fuera del
    área de burbujas y de las marcas de esquina)."""
    box_h = 34
    y0 = MB
    x1 = ML
    x2 = PAGE_W - MR
    c.setStrokeColor(black)
    c.setLineWidth(1.0)
    c.rect(x1, y0, x2 - x1, box_h, stroke=1, fill=0)

    mitad = x1 + (x2 - x1) / 2
    label_y = y0 + box_h - 10
    line_y = y0 + 9
    c.setFont(FH, 7)
    c.drawString(x1 + 8, label_y, "NOMBRE:")
    c.drawString(mitad + 8, label_y, "FIRMA:")
    c.setLineWidth(0.5)
    c.line(x1 + 52, line_y, mitad - 12, line_y)
    c.line(mitad + 46, line_y, x2 - 12, line_y)
    c.setFillColor(black)


def _draw_corner_marks(c, size=CORNER_SIZE):
    """Dibuja 4 marcas de esquina (cuadrados negros rellenos) para alinear
   /enderezar la hoja escaneada. Los centros quedan exactamente en
   (CORNER_MARGIN+size/2, CORNER_MARGIN+size/2) y simetricos, coincidiendo
   con lo registrado en coords['corner_marks']."""
    margin = CORNER_MARGIN
    c.setFillColor(black)
    for x0, y0 in [
        (margin, PAGE_H - margin - size),          # superior izq (crece hacia arriba desde y0)
        (PAGE_W - margin - size, PAGE_H - margin - size),  # superior der
        (margin, margin),                           # inferior izq
        (PAGE_W - margin - size, margin),           # inferior der
    ]:
        c.rect(x0, y0, size, size, stroke=0, fill=1)


def _draw_answers_cols(c, x_start, y_start, preguntas, coords: dict = None, width: float = None, n_cols: int = 4, row_step: float = 18.0, respuestas_correctas: Dict = None, lineas_separadoras: bool = True):
    """Respuestas en N columnas con headers dinámicos (máx. 5).
    Cada pregunta dibuja solo las burbujas que realmente tiene (2–5),
    alineadas a la izquierda en los slots fijos A–E.
    Si coords se proporciona, registra las coordenadas de cada burbuja.
    Si respuestas_correctas={num: [idx,...]} se proporciona, esas burbujas se
    dibujan rellenas (hoja de resultados/clave).
    Si lineas_separadoras=True, entre filas consecutivas se dibuja una línea
    gris clara a mitad de camino: queda fuera del interior muestreado (0.75r)
    y apenas roza el anillo (1.30r–1.85r), sin afectar la decisión de
    contraste del lector (umbral 22; el aporte de la línea es <3 niveles)."""
    total = len(preguntas)
    per_col = (total + n_cols - 1) // n_cols

    usable_w = width if width else (PAGE_W - ML - MR)
    col_w = usable_w / n_cols

    all_labels = ["A", "B", "C", "D", "E"]
    num_col_w = 24
    opt_gap = 19.0  # separación centro-centro entre burbujas (4 col requieren menos que 22)

    for col_idx in range(n_cols):
        col_x = x_start + col_idx * col_w
        start = col_idx * per_col
        end = min(start + per_col, total)
        col_qs = preguntas[start:end]
        if not col_qs:
            continue

        # Máximo número de opciones en esta columna (para el header)
        col_max_opts = max((len(q.get("opciones") or []) for q in col_qs), default=5)
        col_max_opts = max(1, min(col_max_opts, 5))

        header_y = y_start
        opt_x = col_x + num_col_w

        # Header: solo letras hasta el máximo de esta columna
        c.setFont(FH, 9)
        for i in range(col_max_opts):
            lx = opt_x + i * opt_gap + BUBBLE_R
            c.drawCentredString(lx, header_y, all_labels[i])

        c.setStrokeColor(black)
        c.setLineWidth(0.3)
        c.line(col_x, header_y - 5, col_x + col_w - 6, header_y - 5)

        # Líneas separadoras sutiles entre filas de preguntas (ayuda visual).
        for q_idx in range(max(0, len(col_qs) - 1)):
            sep_y = header_y - 18 - q_idx * row_step - row_step / 2.0
            c.setStrokeColorRGB(0.82, 0.82, 0.82)
            c.setLineWidth(0.3)
            c.line(col_x, sep_y, col_x + col_w - 6, sep_y)
        c.setStrokeColor(black)
        c.setLineWidth(0.5)

        for q_idx in range(len(col_qs)):
            global_num = start + q_idx + 1
            by = header_y - 18 - q_idx * row_step
            num_opt_q = min(len(col_qs[q_idx].get("opciones") or []), 5)
            num_opt_q = max(1, num_opt_q)  # mínimo 1

            c.setFont(FB, 8)
            c.drawRightString(col_x + num_col_w - 5, by + 1, f"{global_num}.")

            for opt_idx in range(num_opt_q):
                bx = opt_x + opt_idx * opt_gap + BUBBLE_R
                es_correcta_burbuja = opt_idx in (respuestas_correctas or {}).get(global_num, ())
                _draw_bubble(c, bx, by, r=BUBBLE_R, filled=es_correcta_burbuja)
                # Record answer bubble coordinates
                if coords is not None:
                    q_opts = col_qs[q_idx].get("opciones") or []
                    opt_key = q_opts[opt_idx].get("key", all_labels[opt_idx]) if opt_idx < len(q_opts) else all_labels[opt_idx]
                    coords["answer_bubbles"].append({
                        "question": global_num,
                        "question_index": global_num - 1,  # 0-based
                        "option": opt_key,
                        "option_index": opt_idx,  # 0-based
                        "col": col_idx,
                        "cx": bx,
                        "cy": by,
                        "r": BUBBLE_R
                    })


def _draw_bubble(c, cx, cy, r=BUBBLE_R, filled=False):
    c.setStrokeColor(black)
    c.setFillColor(black if filled else white)
    c.setLineWidth(0.5)
    c.circle(cx, cy, r, stroke=1, fill=1)
    c.setFillColor(black)


def exportar_coordenadas_json(coords: dict) -> str:
    """Exporta las coordenadas a JSON string.
    Convierte las coordenadas PDF a coordenadas de imagen 3x."""
    scale = coords.get("scale_factor", SCALE_3X)
    page_h = coords["page"]["height"]

    export = {
        "page": coords["page"],
        "scale_factor": scale,
        "header_boundary_y_pdf": coords["header_boundary_y"],
        "header_boundary_y_img": int((page_h - coords["header_boundary_y"]) * scale),
        "id_grid": [],
        "id_grid_bounds_img": {},
        "answer_bubbles": [],
        "answer_bounds_img": {},
        "corner_marks_img": [],
        "section_markers_img": [],
    }

    # Convert ID grid to image coords (Y is inverted: PDF bottom-up -> image top-down)
    for b in coords["id_grid"]:
        export["id_grid"].append({
            "row": b["row"],
            "col": b["col"],
            "digit": b["digit"],
            "cx_img": int(b["cx"] * scale),
            "cy_img": int((page_h - b["cy"]) * scale),
            "r_img": int(b["r"] * scale),
        })

    # Convert ID grid bounds
    ib = coords["id_grid_bounds"]
    export["id_grid_bounds_img"] = {
        "x1": int(ib["x1"] * scale),
        "y1": int((page_h - ib["y1"]) * scale),
        "x2": int(ib["x2"] * scale),
        "y2": int((page_h - ib["y2"]) * scale),
    }

    # Convert answer bubbles
    for b in coords["answer_bubbles"]:
        export["answer_bubbles"].append({
            "question": b["question"],
            "question_index": b["question_index"],
            "option": b["option"],
            "option_index": b["option_index"],
            "col": b["col"],
            "cx_img": int(b["cx"] * scale),
            "cy_img": int((page_h - b["cy"]) * scale),
            "r_img": int(b["r"] * scale),
        })

    # Convert answer bounds
    ab = coords["answer_bounds"]
    export["answer_bounds_img"] = {
        "x1": int(ab["x1"] * scale),
        "y1": int((page_h - ab["y1"]) * scale),
        "x2": int(ab["x2"] * scale),
        "y2": int((page_h - ab["y2"]) * scale),
    }

    # Convert corner marks
    for cm in coords["corner_marks"]:
        export["corner_marks_img"].append({
            "cx_img": int(cm["cx"] * scale),
            "cy_img": int((page_h - cm["cy"]) * scale),
            "size_img": int(cm["size"] * scale),
        })

    # Convert section markers
    for sm in coords["section_markers"]:
        export["section_markers_img"].append({
            "cx_img": int(sm["cx"] * scale),
            "cy_img": int((page_h - sm["cy"]) * scale),
            "label": sm["label"],
        })

    return json.dumps(export, indent=2)


def cargar_coordenadas(json_str: str) -> dict:
    """Carga coordenadas desde JSON string."""
    return json.loads(json_str)


def _draw_qr(c, x, y, short_id="", identificador="", qr_dict=None):
    # El QR incluye el identificador de la hoja para que /evaluar pueda
    # localizar sus coordenadas conocidas sin informacion adicional.
    if qr_dict is not None:
        qr_data = json.dumps(qr_dict)
    elif identificador:
        qr_data = json.dumps({"short_id": short_id, "identificador": identificador})
    else:
        qr_data = short_id
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=5,
        border=2,
    )
    qr.add_data(qr_data)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    # Resize to high resolution for sharp rendering
    qr_img = qr_img.resize((400, 400))
    img_buffer = io.BytesIO()
    qr_img.save(img_buffer, format="PNG")
    img_buffer.seek(0)
    qr_size = 55
    img = ImageReader(img_buffer)
    c.drawImage(img, x, y, width=qr_size, height=qr_size)


# ============================================================
#  HOJA DE PREGUNTAS (2 columnas balanceadas, enunciado en negrilla)
# ============================================================

Q_NUM_GUTTER = 13   # número alineado a la derecha en este ancho
Q_TEXT_X = 17       # indent del enunciado dentro de la columna
OPT_LETTER_W = 11   # ancho reservado para "A)"
Q_LINE_H = 10       # interlineado enunciado
O_LINE_H = 9.5      # interlineado opciones
Q_GAP_AFTER = 7     # espacio después de cada pregunta
SECTION_H = 22      # alto del bloque de sección (banda + aire inferior)


def _wrap_pdf(c, text: str, font: str, size: float, max_w: float) -> List[str]:
    """Wrap por ancho real (stringWidth), no por conteo de caracteres."""
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or c.stringWidth(trial, font, size) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def _build_question_blocks(c, preguntas: list, col_w: float) -> List[dict]:
    """Pre-calcula bloques de sección/pregunta con altura exacta."""
    blocks = []
    last_sec = None
    enum_max = col_w - Q_TEXT_X - 2
    opt_text_x = Q_TEXT_X + OPT_LETTER_W
    opt_max = col_w - opt_text_x - 2
    fallback = ["A", "B", "C", "D", "E"]

    for q in preguntas:
        sec = (q.get("seccion") or "").strip()
        if sec and sec != last_sec:
            last_sec = sec
            blocks.append({"type": "section", "text": sec.upper(), "h": SECTION_H})

        tipo = q.get("tipo", "single")
        num = q.get("orden", "?")
        texto = q.get("enunciado") or q.get("nombre") or ""
        if tipo == "multiple":
            texto = f"{texto}  [MÚLTIPLE]"
        enum_lines = _wrap_pdf(c, texto, FH, 8, enum_max)

        opts = []
        for i, opt in enumerate(q.get("opciones", [])[:5]):
            lbl = (opt.get("key") or fallback[i]).upper()
            t = opt.get("label", opt.get("texto", "")) or ""
            opts.append((lbl, _wrap_pdf(c, t, FB, 7.5, opt_max)))

        h = len(enum_lines) * Q_LINE_H + sum(len(l) * O_LINE_H for _, l in opts) + Q_GAP_AFTER
        blocks.append({"type": "q", "num": num, "enum": enum_lines, "opts": opts, "h": h})

    return blocks


def generar_hoja_preguntas(evaluacion_nombre: str, identificador: str, preguntas: list, descripcion: str = "", fecha: str = None) -> Tuple[bytes, Dict]:
    """Genera hoja de preguntas con instrucciones de llenado + opciones en vertical.

    Layout: las columnas se llenan por página (izquierda completa, luego derecha),
    y sólo después se pasa a la página siguiente. La última página se balancea
    entre ambas columnas para que no queden vacías o muy desparejas."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(f"Hoja de preguntas - {evaluacion_nombre}")

    # Coordenadas para exportar
    # (la hoja de preguntas no se escanea: sin marcas de esquina)
    coords = {
        "page": {"width": PAGE_W, "height": PAGE_H},
        "scale_factor": SCALE_3X,
        "header_boundary_y": 0,
        "corner_marks": [],
        "section_markers": []
    }

    y = PAGE_H - MT

    # === HEADER ===
    c.setFont(FH, 13)
    c.drawString(ML, y - 10, evaluacion_nombre)
    if descripcion:
        c.setFont(FB, 8)
        c.drawString(ML, y - 22, descripcion[:70])
    c.setFont(FB, 8)
    c.drawString(ML, y - 34, f"Identificador: {identificador}     Fecha: {_fecha_impresa(fecha)}")
    y -= 42

    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    c.line(ML, y, PAGE_W - MR, y)
    y -= 12

    # === INSTRUCCIONES DE LLENADO (caja con ejemplo visual de ID) ===
    c.setFont(FH, 8)
    c.drawString(ML, y, "INSTRUCCIONES DE LLENADO")
    y -= 12

    instrucciones = [
        "Use lápiz oscuro (HB o #2). No use tinta ni bolígrafo.",
        "ID PERSONA: Marque 1 columna por cada dígito de su número, fila por fila, como en el ejemplo.",
        "RESPUESTAS SIMPLES: Marque SOLO UNA burbuja (A-E). Si marca más de una, será incorrecta.",
        "RESPUESTAS MÚLTIPLES: Marque TODAS las burbujas correctas. Si no marca todas, será incorrecta."
    ]
    box_w = PAGE_W - ML - MR
    ex_id = "45689"
    grid_zone_w = 130            # zona derecha reservada para el ejemplo
    left_max_w = box_w - grid_zone_w - 24
    left_x = ML + 8

    groups = [_wrap_pdf(c, ln, FB, 6.5, left_max_w) for ln in instrucciones]
    left_h = sum(len(g) for g in groups) * 8.5 + (len(groups) - 1) * 3

    # Alto del ejemplo: título + cabecera de dígitos + filas del ID
    ex_col_sp = 9.5
    ex_r = 2.6
    ex_row_sp = 7.5
    ex_h = 9 + 7 + len(ex_id) * ex_row_sp
    ins_h = max(left_h, ex_h) + 12

    c.setFillColorRGB(0.95, 0.95, 0.95)
    c.setStrokeColorRGB(0.65, 0.65, 0.65)
    c.setLineWidth(0.5)
    c.roundRect(ML, y - ins_h, box_w, ins_h, 4, stroke=1, fill=1)
    c.setFillColor(black)

    # --- Texto (izquierda de la caja), prefijos en negrilla ---
    yy = y - 12
    for group in groups:
        for j, wl in enumerate(group):
            if j == 0 and ":" in wl:
                pre, rest = wl.split(":", 1)
                c.setFont(FH, 6.5)
                c.drawString(left_x, yy, pre + ":")
                pw = c.stringWidth(pre + ":", FH, 6.5)
                c.setFont(FB, 6.5)
                c.drawString(left_x + pw + 2, yy, rest.strip())
            else:
                c.setFont(FB, 6.5)
                c.drawString(left_x, yy, wl)
            yy -= 8.5
        yy -= 3

    # --- Ejemplo visual de ID PERSONA (derecha de la caja) ---
    ex_x0 = ML + box_w - grid_zone_w
    ty2 = y - 10
    c.setFont(FH, 6.5)
    c.drawString(ex_x0, ty2, f"EJEMPLO - ID {ex_id}")
    ty2 -= 9

    for di in range(10):
        cx = ex_x0 + 10 + di * ex_col_sp + ex_r
        c.setFont(FH, 5)
        c.drawCentredString(cx, ty2, str(di))
    ty2 -= 7

    for ri in range(len(ex_id)):
        c.setFont(FB, 5)
        c.drawRightString(ex_x0 + 6, ty2 + ex_r - 1.5, str(ri + 1))
        for di in range(10):
            cx = ex_x0 + 10 + di * ex_col_sp + ex_r
            if di == int(ex_id[ri]):
                c.setFillColor(black)
            else:
                c.setFillColor(white)
            c.setStrokeColor(black)
            c.setLineWidth(0.5)
            c.circle(cx, ty2, ex_r, stroke=1, fill=1)
        c.setFillColor(black)
        ty2 -= ex_row_sp

    y = y - ins_h - 8
    c.setStrokeColor(black)
    c.setLineWidth(0.3)
    c.line(ML, y, PAGE_W - MR, y)
    y -= 12

    # === PREGUNTAS (2 columnas por página, flujo izq -> der -> pág. siguiente) ===
    col_w = (PAGE_W - ML - MR) / 2
    blocks = _build_question_blocks(c, preguntas, col_w)
    top_q = y                    # pág. 1: debajo de encabezado e instrucciones
    cont_top = PAGE_H - MT - 18  # págs. siguientes: margen superior compacto
    bottom_limit = 64
    divider_x = ML + col_w

    def _col_top(pg):
        return top_q if pg == 0 else cont_top

    # --- Colocación greedy: (page, col, y_top, block) ---
    placements = []
    page, col = 0, 0
    cy = _col_top(0)
    for b in blocks:
        if cy - b["h"] < bottom_limit:
            col += 1
            if col > 1:
                page += 1
                col = 0
            cy = _col_top(page)
        placements.append([page, col, cy, b])
        cy -= b["h"]
    n_pages = page + 1

    # --- Balancear la última página entre sus dos columnas ---
    last_idx = [i for i, p in enumerate(placements) if p[0] == page]
    if len(last_idx) > 1:
        heights = [placements[i][3]["h"] for i in last_idx]
        half = sum(heights) / 2
        best_k, best_d = 1, None
        for k in range(1, len(heights)):
            d = abs(sum(heights[:k]) - half)
            if best_d is None or d < best_d:
                best_d, best_k = d, k
        yy = _col_top(page)
        for j, i in enumerate(last_idx[:best_k]):
            placements[i][1], placements[i][2] = 0, yy
            yy -= heights[j]
        yy = _col_top(page)
        for j, i in enumerate(last_idx[best_k:], start=best_k):
            placements[i][1], placements[i][2] = 1, yy
            yy -= heights[j]

    # --- Render ---
    for p in range(n_pages):
        if p > 0:
            c.showPage()

        # Divisor vertical tenue entre columnas
        c.setStrokeColorRGB(0.75, 0.75, 0.75)
        c.setLineWidth(0.5)
        c.line(divider_x, _col_top(p) + 4, divider_x, bottom_limit + 6)

        # Pie de página
        c.setFillGray(0.35)
        c.setFont(FB, 7)
        c.drawCentredString(PAGE_W / 2, 40, f"Página {p + 1} de {n_pages}")
        c.setFillGray(0)

        for pg, cl, ty, b in placements:
            if pg != p:
                continue
            bx = ML + cl * col_w
            if b["type"] == "section":
                band_h = SECTION_H - 10
                c.setFillColorRGB(0.87, 0.87, 0.87)
                c.rect(bx, ty - band_h, col_w - 8, band_h, stroke=0, fill=1)
                c.setFillColor(black)
                c.setFont(FH, 7)
                c.drawString(bx + 6, ty - band_h + 3, b["text"])
            else:
                yy = ty
                c.setFont(FH, 8)
                c.drawRightString(bx + Q_NUM_GUTTER, yy, f'{b["num"]}.')
                for ln in b["enum"]:
                    c.setFont(FH, 8)
                    c.drawString(bx + Q_TEXT_X, yy, ln)
                    yy -= Q_LINE_H
                for lbl, lines in b["opts"]:
                    if not lines:
                        continue
                    c.setFont(FH, 7.5)
                    c.drawString(bx + Q_TEXT_X, yy, f"{lbl})")
                    c.setFont(FB, 7.5)
                    c.drawString(bx + Q_TEXT_X + OPT_LETTER_W, yy, lines[0])
                    yy -= O_LINE_H
                    for ln2 in lines[1:]:
                        c.drawString(bx + Q_TEXT_X + OPT_LETTER_W, yy, ln2)
                        yy -= O_LINE_H

    c.save()
    return buffer.getvalue(), coords


# ============================================================
#  HOJA DE RESULTADOS (REVISADA)
# ============================================================

def generar_pdf_revisado(respuestas_detalle: list, evaluacion_nombre: str, identificador: str) -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(f"Resultados - {evaluacion_nombre}")

    y = PAGE_H - MT

    c.setFont(FH, 14)
    c.drawString(ML, y - 10, f"RESULTADOS - {evaluacion_nombre}")
    y -= 18
    c.setFont(FB, 10)
    c.drawString(ML, y - 8, f"Identificador: {identificador}")
    y -= 16

    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    c.line(ML, y, PAGE_W - MR, y)
    y -= 16

    c.setFont(FH, 9)
    headers = ["#", "Nombre", "Respuesta", "Estado", "Puntos"]
    xp = [ML, ML + 30, ML + 240, ML + 330, ML + 420]
    for h, x in zip(headers, xp):
        c.drawString(x, y, h)
    y -= 4
    c.line(ML, y, PAGE_W - MR, y)
    y -= 14

    c.setFont(FB, 8)
    for idx, resp in enumerate(respuestas_detalle):
        if y < 60:
            c.showPage()
            y = PAGE_H - MT - 20

        c.drawString(xp[0], y, str(idx + 1))
        c.drawString(xp[1], y, resp.get("nombre", "")[:30])
        c.drawString(xp[2], y, resp.get("respuesta", "N/A") or "N/A")

        if resp.get("es_correcta"):
            c.setFillColorRGB(0, 0.5, 0)
            c.drawString(xp[3], y, "✓ Correcta")
        else:
            c.setFillColorRGB(0.8, 0, 0)
            c.drawString(xp[3], y, "✗ Incorrecta")
        c.setFillColor(black)

        c.drawString(xp[4], y, f"{resp.get('puntos_obtenidos', 0):.1f}")
        y -= 14

    y -= 12
    c.line(ML, y, PAGE_W - MR, y)
    y -= 16
    c.setFont(FH, 11)
    total = sum(r.get("puntos_obtenidos", 0) for r in respuestas_detalle)
    correctas = sum(1 for r in respuestas_detalle if r.get("es_correcta"))
    c.drawString(ML, y, f"Total: {total:.1f} puntos  |  Correctas: {correctas}/{len(respuestas_detalle)}")

    c.save()
    return buffer.getvalue()


# ============================================================
#  HOJA GENÉRICA (sin examen asociado)
# ============================================================

def generar_hoja_generica(identificador: str, cantidad_preguntas: int, num_opciones: int = 5) -> Tuple[bytes, Dict]:
    """Genera una hoja de respuestas genérica (N preguntas × M opciones) sin
    asociarla a ninguna evaluación. El QR embebe {"tipo":"generica", ...}.

    Retorna: (pdf_bytes, coordenadas_dict)"""
    num_opciones = max(1, min(int(num_opciones), 5))
    all_labels = ["A", "B", "C", "D", "E"]
    preguntas = []
    for i in range(cantidad_preguntas):
        preguntas.append({
            "nombre": f"pregunta_{i + 1}",
            "enunciado": "",
            "tipo": "single",
            "opciones": [
                {"key": all_labels[j], "label": "", "es_correcta": False}
                for j in range(num_opciones)
            ],
            "orden": i + 1,
            "puntos": 1.0,
            "seccion": ""
        })

    qr_dict = {"tipo": "generica", "identificador": identificador}
    return generar_hoja_respuestas(
        evaluacion_nombre="Hoja de respuestas genérica",
        identificador=identificador,
        preguntas=preguntas,
        descripcion=f"{cantidad_preguntas} preguntas · opciones {''.join(all_labels[:num_opciones])}",
        short_id="",
        qr_dict=qr_dict
    )
