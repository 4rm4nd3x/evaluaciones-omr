import cv2
import numpy as np
import base64
import io
import json
from typing import List, Tuple, Optional, Dict


def decode_image_base64(base64_str: str) -> np.ndarray:
    """Decodifica una imagen en base64 a un array de numpy."""
    if "," in base64_str:
        base64_str = base64_str.split(",", 1)[1]

    img_bytes = base64.b64decode(base64_str)
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    return img


def pdf_to_images(pdf_base64: str) -> List[np.ndarray]:
    """Convierte un PDF en base64 a lista de imágenes numpy."""
    try:
        import fitz  # PyMuPDF
        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        images = []
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
            img_bytes = pix.tobytes("png")
            nparr = np.frombuffer(img_bytes, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is not None:
                images.append(img)
        return images
    except ImportError:
        return [decode_image_base64(pdf_base64)]


def detect_qr_codes(image: np.ndarray) -> List[str]:
    """Detecta códigos QR en la imagen y retorna sus contenidos."""
    from pyzbar import pyzbar

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    qr_data_list = []

    decoded_objects = pyzbar.decode(gray)
    for obj in decoded_objects:
        data = obj.data.decode("utf-8")
        if data not in qr_data_list:
            qr_data_list.append(data)

    if qr_data_list:
        return qr_data_list

    _, thresh = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    for obj in pyzbar.decode(thresh):
        data = obj.data.decode("utf-8")
        if data not in qr_data_list:
            qr_data_list.append(data)

    if qr_data_list:
        return qr_data_list

    for scale in [0.5, 1.5, 2.0, 3.0]:
        new_w = int(w * scale)
        new_h = int(h * scale)
        if new_w < 50 or new_h < 50:
            continue
        resized = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        for obj in pyzbar.decode(resized):
            data = obj.data.decode("utf-8")
            if data not in qr_data_list:
                qr_data_list.append(data)
        if qr_data_list:
            return qr_data_list

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    adaptive = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )
    for obj in pyzbar.decode(adaptive):
        data = obj.data.decode("utf-8")
        if data not in qr_data_list:
            qr_data_list.append(data)

    return qr_data_list


def preprocess_image(image: np.ndarray) -> np.ndarray:
    """
    Preprocesa la imagen para detección de burbujas.
    Adaptive threshold + limpieza morfológica.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    # Adaptive threshold: se adapta a variaciones locales de iluminación
    thresh = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 15, 8
    )
    # Limpiar ruido pequeño
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
    return cleaned


def to_monochrome(image: np.ndarray) -> np.ndarray:
    """
    Convierte imagen a binario monocrómico (blanco/negro puro).
    Ideal para mejorar contraste de marcaciones en escaneos reales.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    # Otsu para binarización automática
    _, mono = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return mono


def is_bubble_filled(image: np.ndarray, center: Tuple[int, int], radius: int = 12) -> bool:
    """
    Determina si una burbuja está marcada (rellenada).
    Usa Otsu + intensidad media + contraste con fondo.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    x, y = center
    h_img, w_img = gray.shape

    pad = max(2, radius // 3)
    y1 = max(0, y - radius - pad)
    y2 = min(h_img, y + radius + pad)
    x1 = max(0, x - radius - pad)
    x2 = min(w_img, x + radius + pad)

    roi = gray[y1:y2, x1:x2]
    if roi.size == 0:
        return False

    # Otsu threshold on ROI
    roi_blur = cv2.GaussianBlur(roi, (3, 3), 0)
    _, roi_otsu = cv2.threshold(
        roi_blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    # Circular mask
    mask = np.zeros_like(roi_otsu)
    roi_cy = radius + pad
    roi_cx = radius + pad
    cv2.circle(mask, (roi_cx, roi_cy), radius, 255, -1)

    masked_otsu = cv2.bitwise_and(roi_otsu, mask)
    mask_area = np.count_nonzero(mask)
    if mask_area == 0:
        return False

    fill_ratio_otsu = np.count_nonzero(masked_otsu) / mask_area

    # Mean intensity inside bubble
    masked_gray = cv2.bitwise_and(roi, roi, mask=mask)
    mean_intensity = cv2.mean(masked_gray, mask=mask)[0]

    # Background estimation from border ring (outside bubble but inside ROI)
    border_mask = cv2.bitwise_and(mask, mask, mask=mask)  # same as mask
    inner_mask = np.zeros_like(mask)
    cv2.circle(inner_mask, (roi_cx, roi_cy), max(1, radius - 3), 255, -1)
    ring_mask = cv2.subtract(border_mask, inner_mask)
    ring_pixels = cv2.bitwise_and(roi, roi, mask=ring_mask)
    ring_count = np.count_nonzero(ring_mask)
    bg_mean = cv2.mean(roi, mask=ring_mask)[0] if ring_count > 0 else 200

    contrast = bg_mean - mean_intensity

    return (
        fill_ratio_otsu > 0.30
        or (mean_intensity < 160 and contrast > 20)
        or fill_ratio_otsu > 0.50
    )


def _group_into_rows(
    bubbles: List[Tuple[int, int, float]],
    y_threshold: int = 15,
) -> List[List[Tuple[int, int, float]]]:
    """Agrupa burbujas en filas basándose en coordenada Y."""
    if not bubbles:
        return []

    sorted_bubbles = sorted(bubbles, key=lambda b: (b[1], b[0]))
    rows = []
    current_row = [sorted_bubbles[0]]

    for b in sorted_bubbles[1:]:
        row_y = current_row[0][1]
        if abs(b[1] - row_y) <= y_threshold:
            current_row.append(b)
        else:
            rows.append(sorted(current_row, key=lambda x: x[0]))
            current_row = [b]

    if current_row:
        rows.append(sorted(current_row, key=lambda x: x[0]))

    return rows


def _find_header_boundary(image: np.ndarray) -> int:
    """
    Encuentra la línea divisoria entre el header (QR + ID persona)
    y la sección de respuestas.
    Usa la brecha Y más grande entre filas de burbujas como separador.
    """
    h_img, w_img = image.shape[:2]
    processed = preprocess_image(image)
    contours, _ = cv2.findContours(
        processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    # Find all circular bubbles
    all_bubbles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 50 or area > 5000:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)
        if circularity > 0.4:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                all_bubbles.append((cx, cy, area))

    if not all_bubbles:
        return int(h_img * 0.25)

    # Group into rows and find the first significant gap
    # (the gap between header/ID section and answer section)
    rows = _group_into_rows(all_bubbles, y_threshold=10)
    row_ys = [int(np.mean([b[1] for b in row])) for row in rows]
    row_ys_sorted = sorted(row_ys)

    # Find the largest gap in the top 60% of the image
    # This should be the gap between ID grid and answer bubbles
    max_gap = 0
    gap_pos = int(h_img * 0.25)
    search_limit = int(h_img * 0.6)
    for i in range(1, len(row_ys_sorted)):
        if row_ys_sorted[i] > search_limit:
            break
        gap = row_ys_sorted[i] - row_ys_sorted[i - 1]
        if gap > max_gap:
            max_gap = gap
            gap_pos = (row_ys_sorted[i] + row_ys_sorted[i - 1]) // 2

    return gap_pos


def detect_identifier_section(
    image: np.ndarray,
    num_digits: int = 10,
    num_options: int = 10
) -> str:
    """
    Detecta la sección de identificador de persona (grid 10x10).
    El grid ID tiene burbujas más pequeñas (área ~250) en la parte
    superior derecha de la imagen, con 10 columnas (dígitos 0-9) y hasta 10 filas.
    """
    h_img, w_img = image.shape[:2]

    # Find all bubbles in the image
    processed = preprocess_image(image)
    contours, _ = cv2.findContours(
        processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    all_bubbles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 50 or area > 5000:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)
        if circularity > 0.5:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                all_bubbles.append((cx, cy, area))

    if not all_bubbles:
        return ""

    # Find the header boundary to limit ID search
    boundary = _find_header_boundary(image)

    # The ID grid is in the TOP-RIGHT of the image.
    # ID bubbles: header region, area ~250, X > 50% of width
    header_bubbles = [b for b in all_bubbles if b[1] < boundary]

    # Check if contour detection found enough ID-size bubbles
    right_bubbles = [b for b in header_bubbles if b[0] > w_img * 0.45]
    has_enough = False
    if right_bubbles:
        areas = [b[2] for b in right_bubbles]
        med = float(np.median(areas))
        id_size = [b for b in right_bubbles if b[2] < med * 2]
        rows_test = _group_into_rows(id_size, y_threshold=8)
        has_enough = any(8 <= len(r) <= 12 for r in rows_test)

    # Fallback: if contour detection found too few, try HoughCircles
    if not has_enough:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
        header_region = gray[0:boundary, int(w_img * 0.4):]
        blurred = cv2.GaussianBlur(header_region, (5, 5), 1)
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT,
            dp=1.2, minDist=15, param1=50, param2=20,
            minRadius=5, maxRadius=15
        )
        if circles is not None:
            hough_bubbles = []
            for c in circles[0]:
                cx = int(c[0]) + int(w_img * 0.4)
                cy = int(c[1])
                area = np.pi * c[2] * c[2]
                hough_bubbles.append((cx, cy, area))
            # Deduplicate
            deduped = []
            for b in sorted(hough_bubbles, key=lambda b: (b[1], b[0])):
                merged = False
                for dx, dy, da in deduped:
                    if abs(b[0] - dx) < 15 and abs(b[1] - dy) < 15:
                        merged = True
                        break
                if not merged:
                    deduped.append(b)
            header_bubbles = deduped

    if not header_bubbles:
        return ""

    # Split by area to find ID-size bubbles
    areas = [b[2] for b in header_bubbles]
    median_area = float(np.median(areas))

    # ID bubbles are in the right half of the image AND have small area
    id_bubbles = [
        (cx, cy, area)
        for cx, cy, area in header_bubbles
        if cx > w_img * 0.45 and area < median_area * 2.0
    ]

    if not id_bubbles:
        return ""

    # Sort by y then x
    id_bubbles.sort(key=lambda b: (b[1], b[0]))

    # Group into rows (ID bubbles are closely spaced)
    rows = _group_into_rows(id_bubbles, y_threshold=8)

    # ID grid rows must have exactly 10 bubbles (digits 0-9)
    # Allow 8-12 to handle slight detection variations
    id_rows = [r for r in rows if 8 <= len(r) <= 12]

    if len(id_rows) < 1:
        return ""

    identifier = ""
    for row_idx in range(min(num_digits, len(id_rows))):
        row = id_rows[row_idx]
        row_sorted = sorted(row, key=lambda b: b[0])
        xs = [b[0] for b in row_sorted]

        # Mapear cada burbuja a su columna absoluta (digito 0-9) usando el
        # espaciado uniforme del grid, anclado en la ultima columna (9).
        # Asi, si se pierden burbujas a la izquierda (occlusion del chip,
        # union de contornos), las posiciones no se desplazan.
        diffs = np.diff(xs)
        if len(xs) >= 4 and len(diffs) > 0:
            s = float(np.median(diffs))
            if s <= 0 or float(np.std(diffs)) > 0.30 * s:
                continue  # espaciado irregular: no es una fila del grid
            cols = [9]
            for d in reversed(diffs):
                cols.append(cols[-1] - int(round(d / s)))
            cols = list(reversed(cols))
        else:
            cols = list(range(len(xs)))

        filled_col = -1
        best_fill_score = 0
        for i, col in enumerate(cols):
            if col < 0 or col > 9:
                continue
            cx, cy, area = row_sorted[i]
            if is_bubble_filled(image, (cx, cy), radius=5):
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
                y1 = max(0, int(cy) - 5)
                y2 = min(gray.shape[0], int(cy) + 5)
                x1 = max(0, int(cx) - 5)
                x2 = min(gray.shape[1], int(cx) + 5)
                roi = gray[y1:y2, x1:x2]
                if roi.size > 0:
                    mean_val = np.mean(roi)
                    if mean_val > 190:
                        # Burbuja visualmente vacia: falso positivo de Otsu
                        continue
                    score = 256 - mean_val
                    if score > best_fill_score:
                        best_fill_score = score
                        filled_col = col

        if 0 <= filled_col <= 9:
            identifier += str(filled_col)
        elif cols:
            identifier += "?"

    return identifier


def _detect_bubbles_hough(image: np.ndarray, boundary: int) -> List[Tuple[int, int, float]]:
    """
    Detecta burbujas usando HoughCircles (fallback para escaneos reales
    donde la detección por contornos falla).
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h_img, w_img = gray.shape

    # Work on the answer region only
    region_y = max(0, boundary - 50)
    region = gray[region_y:, :]

    # Apply Gaussian blur
    blurred = cv2.GaussianBlur(region, (9, 9), 2)

    # Detect circles with HoughCircles
    # minDist: minimum distance between circle centers
    # param1: Canny edge detection threshold
    # param2: accumulator threshold (lower = more circles detected)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=30,
        param1=50,
        param2=25,
        minRadius=8,
        maxRadius=25
    )

    if circles is None:
        return []

    bubbles = []
    for c in circles[0]:
        cx = int(c[0])
        cy = int(c[1]) + region_y  # offset back to full image
        r = c[2]
        area = np.pi * r * r
        bubbles.append((cx, cy, area))

    # Deduplicate: merge circles that are very close
    deduped = []
    for b in sorted(bubbles, key=lambda b: (b[1], b[0])):
        merged = False
        for i, (dx, dy, da) in enumerate(deduped):
            if abs(b[0] - dx) < 20 and abs(b[1] - dy) < 20:
                merged = True
                break
        if not merged:
            deduped.append(b)

    return deduped


def detect_answer_bubbles(
    image: np.ndarray,
    num_questions: int,
    options_per_question: int = 5
) -> List[Optional[int]]:
    """
    Detecta las respuestas marcadas en la hoja de respuestas.
    Intenta detección por contornos primero, luego HoughCircles como fallback.
    """
    h_img, w_img = image.shape[:2]

    # Find header boundary
    boundary = _find_header_boundary(image)

    # Find all answer bubbles via contours
    processed = preprocess_image(image)
    contours, _ = cv2.findContours(
        processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    all_bubbles_in_image = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 200 or area > 5000:
            continue
        perimeter = cv2.arcLength(cnt, True)
        if perimeter == 0:
            continue
        circularity = 4 * np.pi * area / (perimeter * perimeter)
        if circularity > 0.4:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                all_bubbles_in_image.append((cx, cy, area))

    # Filter answer bubbles (below header boundary, with margin)
    answer_bubbles = [
        b for b in all_bubbles_in_image if b[1] >= boundary + 20
    ]

    # Fallback: if too few bubbles found, try HoughCircles
    if len(answer_bubbles) < num_questions:
        hough_bubbles = _detect_bubbles_hough(image, boundary)
        # Filter HoughCircles to answer region only
        answer_bubbles = [b for b in hough_bubbles if b[1] >= boundary + 20]

    if not answer_bubbles:
        return [None] * num_questions

    # Find natural X-gaps to detect column boundaries
    x_vals = sorted(set(b[0] for b in answer_bubbles))
    x_gaps = []
    for i in range(1, len(x_vals)):
        gap = x_vals[i] - x_vals[i - 1]
        if gap > 100:  # Significant gap between columns
            x_gaps.append((x_vals[i - 1], x_vals[i], gap))

    # If we find 2+ significant X gaps, it's a multi-column layout
    if len(x_gaps) >= 2:
        # Split into columns using the 2 LARGEST gaps (most likely column separators)
        x_gaps.sort(key=lambda g: g[2], reverse=True)
        gap_thresholds = sorted([g[1] for g in x_gaps[:2]])
        col_boundaries = [0] + gap_thresholds + [w_img]

        columns = []
        for ci in range(len(col_boundaries) - 1):
            x_min = col_boundaries[ci]
            x_max = col_boundaries[ci + 1]
            col_bubbles = [
                b for b in answer_bubbles if x_min <= b[0] < x_max
            ]
            columns.append(col_bubbles)

        # Process each column
        all_answers = []
        for col_bubbles in columns:
            col_answers = _process_answer_column(
                col_bubbles, image, options_per_question
            )
            all_answers.extend(col_answers)

        # Pad or trim to num_questions
        while len(all_answers) < num_questions:
            all_answers.append(None)
        return all_answers[:num_questions]
    else:
        # Single column layout
        answers = _process_answer_column(answer_bubbles, image, options_per_question)
        while len(answers) < num_questions:
            answers.append(None)
        return answers[:num_questions]


def _process_answer_column(
    bubbles: List[Tuple[int, int, float]],
    image: np.ndarray,
    options_per_question: int = 5,
) -> List[Optional[int]]:
    """Procesa una columna de respuestas y retorna lista de respuestas detectadas."""
    if not bubbles:
        return []

    # Group into rows
    rows = _group_into_rows(bubbles, y_threshold=15)

    # Filter: keep only rows with enough bubbles (answer rows have options_per_question)
    # Allow some tolerance for detection variations
    min_bubbles = max(3, options_per_question - 2)
    rows = [r for r in rows if len(r) >= min_bubbles]

    answers = []
    for row in rows:
        row_sorted = sorted(row, key=lambda b: b[0])

        filled_idx = -1
        for i, (cx, cy, area) in enumerate(row_sorted[:options_per_question]):
            if is_bubble_filled(image, (cx, cy)):
                filled_idx = i
                break

        if filled_idx >= 0:
            answers.append(filled_idx)
        else:
            answers.append(None)

    return answers


def detect_corner_marks(image: np.ndarray) -> Optional[Dict]:
    """Detecta las 4 marcas de esquina en la imagen."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    h, w = gray.shape

    _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY_INV)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Area maxima adaptativa: las marcas escalan con la resolucion del escaneo
    area_max = max(2000, int(h * w * 0.001))
    candidates = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 100 or area > area_max:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        aspect_ratio = bw / bh if bh > 0 else 0
        if 0.7 < aspect_ratio < 1.3:
            M = cv2.moments(cnt)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
                candidates.append((cx, cy, area))

    if len(candidates) < 4:
        return None

    corner_positions = {
        "top_left": (0, 0),
        "top_right": (w, 0),
        "bottom_left": (0, h),
        "bottom_right": (w, h),
    }

    if len(candidates) < 4:
        return None

    # Rectangulo nominal de las marcas en marco hoja (@SCALE_3X)
    m, s = MARCA_MARGEN_PX, MARCA_LADO_PX
    NW, NH = 1836, 2376
    nominales = np.float32([
        [m + s / 2, m + s / 2],
        [NW - m - s / 2, m + s / 2],
        [NW - m - s / 2, NH - m - s / 2],
        [m + s / 2, NH - m - s / 2],
    ])

    # Candidatos cercanos a cada esquina (radio generoso para inclinaciones
    # grandes); los falsos positivos (QR, burbujas de ejemplo) se descartan
    # luego por consistencia geometrica.
    radio = 0.20 * min(h, w)
    orden_esquinas = ("top_left", "top_right", "bottom_right", "bottom_left")
    por_esquina = []
    for corner_name in orden_esquinas:
        tx_, ty_ = corner_positions[corner_name]
        cerca = [
            (i, (cx - tx_) ** 2 + (cy - ty_) ** 2)
            for i, (cx, cy, _a) in enumerate(candidates)
            if (cx - tx_) ** 2 + (cy - ty_) ** 2 <= radio * radio
        ]
        if not cerca:
            return None
        cerca.sort(key=lambda t: t[1])
        por_esquina.append([i for i, _d in cerca[:4]])

    # Probar combinaciones y quedarse con la de menor error de reproyeccion
    import itertools
    mejor = None
    mejor_err = float("inf")
    for combo in itertools.product(*por_esquina):
        if len(set(combo)) < 4:
            continue
        det_pts = np.float32([[candidates[i][0], candidates[i][1]] for i in combo])
        try:
            A_, _ = cv2.estimateAffinePartial2D(det_pts, nominales)
        except cv2.error:
            continue
        if A_ is None:
            continue
        reproj = (A_[:, :2] @ det_pts.T).T + A_[:, 2]
        err = float(np.mean(np.linalg.norm(reproj - nominales, axis=1)))
        if err < mejor_err:
            mejor_err = err
            mejor = {n: tuple(candidates[i]) for n, i in zip(orden_esquinas, combo)}

    return mejor if mejor is not None and mejor_err <= 8.0 else None


def apply_perspective_transform(
    image: np.ndarray, corners: Optional[Dict] = None
) -> np.ndarray:
    """Aplica transformación de perspectiva para enderezar la imagen."""
    if not corners:
        return image

    h, w = image.shape[:2]
    src_points = np.float32(
        [
            [corners["top_left"][0], corners["top_left"][1]],
            [corners["top_right"][0], corners["top_right"][1]],
            [corners["bottom_right"][0], corners["bottom_right"][1]],
            [corners["bottom_left"][0], corners["bottom_left"][1]],
        ]
    )
    dst_points = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    matrix = cv2.getPerspectiveTransform(src_points, dst_points)
    return cv2.warpPerspective(image, matrix, (w, h))


def process_scan(
    image_or_pdf_base64: str, is_pdf: bool = False
) -> Tuple[np.ndarray, List[str]]:
    """Procesa una imagen o PDF escaneado. Retorna: (image, qr_data_list)"""
    if is_pdf:
        images = pdf_to_images(image_or_pdf_base64)
        if not images:
            raise ValueError("No se pudo decodificar el PDF")
        image = images[0]
    else:
        image = decode_image_base64(image_or_pdf_base64)

    if image is None:
        raise ValueError("No se pudo decodificar la imagen")

    qr_data_list = detect_qr_codes(image)

    return image, qr_data_list


# Banda exterior (fraccion del lado menor) que se recorta tras enderezar:
# ahi viven las marcas de esquina y podrian confundirse con burbujas del ID.
BANDA_MARCAS_RATIO = 0.04

# Geometria nominal de las marcas de esquina de la hoja de respuestas
# (puntos del PDF * SCALE_3X): margen 18pt, lado 12pt
MARCA_MARGEN_PX = 54
MARCA_LADO_PX = 36


def _recortar_banda_marcas(image: np.ndarray, base: int = 0) -> Tuple[np.ndarray, int]:
    """Recorta una banda perimetral donde viven las marcas de esquina.
    Retorna (imagen, banda_aplicada)."""
    h, w = image.shape[:2]
    banda = max(40, min(int(min(h, w) * BANDA_MARCAS_RATIO) + base, h // 2 - 1))
    return image[banda:h - banda, banda:w - banda], banda


def _rotar_expandiendo(image: np.ndarray, angulo: float):
    """Rota la imagen sobre un lienzo expandido (nada se recorta, como en un
    escaneo real de pagina completa).
    Retorna (imagen, matriz_M, crecimiento_por_lado_x, crecimiento_por_lado_y)."""
    h, w = image.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angulo, 1.0)
    cos_a, sin_a = abs(M[0, 0]), abs(M[0, 1])
    nw, nh = int(h * sin_a + w * cos_a), int(h * cos_a + w * sin_a)
    M[0, 2] += nw / 2 - w / 2
    M[1, 2] += nh / 2 - h / 2
    rotada = cv2.warpAffine(
        image, M, (nw, nh),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )
    return rotada, M, (nw - w) // 2, (nh - h) // 2


def enderezar_imagen(image: np.ndarray, nominal_size: Tuple[int, int] = (1836, 2376)) -> Tuple[np.ndarray, Dict]:
    """Analiza las marcas de esquina de la hoja y prepara la lectura OMR.

    1) Estima la transformacion SIMILITUD (escala+rotacion+traslacion) que
       mapea las 4 marcas detectadas a sus posiciones nominales en la hoja
       (marco de referencia a SCALE_3X: nominal_size). Esa matriz `A` permite
       leer burbujas muestreando directamente la imagen original con
       _inv_A() -> determinista e independiente del angulo.
    2) Adicionalmente produce una imagen VISUALMENTE nivelada (rotacion pura
       sobre lienzo expandido + recorte de banda de marcas) para salidas
       anotadas/deteccion generica.

    Retorna (imagen_nivelada, {"A": matriz 2x3 o None, "enderezada": bool,
    "angulo": float})"""
    import math

    h, w = image.shape[:2]
    corners = detect_corner_marks(image)

    A = None
    angulo = 0.0
    if corners and len(corners) == 4:
        orden = ("top_left", "top_right", "bottom_right", "bottom_left")
        nw, nh = nominal_size
        m = MARCA_MARGEN_PX
        s = MARCA_LADO_PX
        nominales = np.float32([
            [m + s / 2, m + s / 2],
            [nw - m - s / 2, m + s / 2],
            [nw - m - s / 2, nh - m - s / 2],
            [m + s / 2, nh - m - s / 2],
        ])
        detectadas = np.float32([
            [corners[k][0], corners[k][1]] for k in orden
        ])
        try:
            A, _ = cv2.estimateAffinePartial2D(detectadas, nominales)
        except cv2.error:
            A = None
        # Validar por reproyeccion: si los candidatos no forman el rectangulo
        # nominal (p.ej. falsos positivos del QR), descartar la transformacion
        if A is not None:
            reproj = (A[:, :2] @ detectadas.T).T + A[:, 2]
            if float(np.mean(np.linalg.norm(reproj - nominales, axis=1))) > 8.0:
                A = None

        tl, tr = corners["top_left"], corners["top_right"]
        bl, br = corners["bottom_left"], corners["bottom_right"]

        def _angulo(p1, p2):
            return math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))

        angulo = (_angulo(tl, tr) + _angulo(bl, br)) / 2.0

    # Imagen nivelada para salida visual / deteccion generica
    if A is not None:
        rotada, _, gx, gy = _rotar_expandiendo(image, angulo)
        base = max(gx, gy) + 70
        nivelada, _banda = _recortar_banda_marcas(rotada, base=base)
    else:
        nivelada, _banda = _recortar_banda_marcas(image)

    return nivelada, {"A": A, "enderezada": A is not None, "angulo": angulo}


def _inv_A(A: np.ndarray, cx: float, cy: float) -> Tuple[int, int]:
    """Inversa de la similitud 2x3: coords de hoja -> px de la imagen original."""
    a, b, tx = A[0, 0], A[0, 1], A[0, 2]
    c, d, ty = A[1, 0], A[1, 1], A[1, 2]
    det = a * d - b * c
    if abs(det) < 1e-9:
        return int(round(cx)), int(round(cy))
    # [a b; c d]^-1
    ia, ib = d / det, -b / det
    ic, idd = -c / det, a / det
    x = cx - tx
    y = cy - ty
    return int(round(ia * x + ib * y)), int(round(ic * x + idd * y))


def _muestreo_osculo(gray: np.ndarray, cx: int, cy: int, r: int) -> Optional[float]:
    """Intensidad media dentro del círculo; None si queda fuera del frame."""
    h, w = gray.shape[:2]
    x1, x2 = max(0, cx - r), min(w, cx + r)
    y1, y2 = max(0, cy - r), min(h, cy + r)
    if x2 <= x1 or y2 <= y1:
        return None
    roi = gray[y1:y2, x1:x2]
    if roi.size == 0:
        return None
    return float(np.mean(roi))


UMBRAL_LAPIZ = 185  # gris medio por debajo => marcada (lapiz HB ~60-160, papel >215)


def leer_respuestas_con_coords(image: np.ndarray, coords: dict,
                               transform: Dict, num_preguntas: int) -> List[List[int]]:
    """Lee las respuestas muestreando las coordenadas conocidas
    (coordenadas.json). Si transform['A'] existe se mapea hoja->imagen con su
    inversa (funciona con cualquier inclinacion); si no, se asume imagen
    alineada 1:1 con el marco de referencia.
    Retorna {indice_pregunta: [indices_opcion]} (soporta múltiples marcas)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    A = transform.get("A")
    if A is not None:
        def _pos(b):
            return _inv_A(A, b["cx_img"], b["cy_img"])
    else:
        # Sin marcas: asumir hoja alineada y escalar por ancho de imagen
        escala = gray.shape[1] / 1836.0

        def _pos(b):
            return int(round(b["cx_img"] * escala)), int(round(b["cy_img"] * escala))

    respuestas: Dict[int, List[int]] = {}
    for b in coords.get("answer_bubbles", []):
        idx = b["question_index"]
        if idx >= num_preguntas:
            continue
        cx, cy = _pos(b)
        g = _muestreo_osculo(gray, cx, cy, max(4, int(b["r_img"] * 0.75)))
        if g is not None and g < UMBRAL_LAPIZ:
            respuestas.setdefault(idx, []).append(b["option_index"])
    return [respuestas.get(i, []) for i in range(num_preguntas)]


def leer_id_con_coords(image: np.ndarray, coords: dict, transform: Dict,
                       num_digitos: int = 8) -> str:
    """Lee el ID PERSONA muestreando el grid conocido de coordenadas.json."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if len(image.shape) == 3 else image
    A = transform.get("A")
    if A is not None:
        def _pos(b):
            return _inv_A(A, b["cx_img"], b["cy_img"])
    else:
        escala = gray.shape[1] / 1836.0

        def _pos(b):
            return int(round(b["cx_img"] * escala)), int(round(b["cy_img"] * escala))

    grid: Dict[int, List[dict]] = {}
    for b in coords.get("id_grid", []):
        if b["row"] < num_digitos:
            grid.setdefault(b["row"], []).append(b)

    identifier = ""
    for row_idx in sorted(grid.keys())[:num_digitos]:
        celdas = sorted(grid[row_idx], key=lambda b: b["col"])
        mejor_col, mejor_g = -1, UMBRAL_LAPIZ
        for celda in celdas:
            cx, cy = _pos(celda)
            g = _muestreo_osculo(gray, cx, cy, max(3, int(celda["r_img"] * 0.8)))
            if g is not None and g < mejor_g:
                mejor_g, mejor_col = g, celda["col"]
        # Solo aceptar si hay tinta real (evita fantasmas con papel levemente gris)
        if 0 <= mejor_col <= 9 and mejor_g < UMBRAL_LAPIZ - 25:
            identifier += str(mejor_col)
        elif mejor_col >= 0:
            identifier += "?"
    return identifier
