#!/usr/bin/env python3
"""
Test completo: genera hoja con coordenadas → escanea → evalúa usando coordenadas.
Verifica que el ID y las respuestas se detectan correctamente.
"""
import os
import sys
import json
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.sheet_generator import (
    generar_hoja_respuestas, exportar_coordenadas_json, cargar_coordenadas
)
from app.routes.evaluar import (
    _detect_id_from_coords, _detect_answers_from_coords,
    _detect_multiple_answers_from_coords, _compute_scale
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "demo", "output")


def pdf_to_image(pdf_path):
    """Convierte un PDF a imagen usando PyMuPDF."""
    try:
        import fitz
        doc = fitz.open(pdf_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        img_bytes = pix.tobytes("png")
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img
    except Exception as e:
        print(f"  Error convirtiendo PDF: {e}")
        return None


def main():
    print("=" * 60)
    print("  TEST: EVALUACIÓN CON COORDENADAS CONOCIDAS")
    print("=" * 60)
    print()

    # =========================================================
    # 1. Crear preguntas de prueba
    # =========================================================
    print("[1/5] Creando preguntas de prueba...")

    # 13 preguntas: 10 single + 3 multiple
    preguntas = []
    correct_answers = {}  # question_index -> list of correct option indices

    for i in range(13):
        if i < 10:
            # Single answer: correct is the letter matching index (A=0, B=1, etc.)
            correct_idx = i % 5
            correct_answers[i] = [correct_idx]
            preguntas.append({
                "nombre": f"pregunta_{i+1}",
                "tipo": "single",
                "enunciado": f"Pregunta {i+1}",
                "opciones": [
                    {"key": k, "label": f"Opción {k}", "es_correcta": idx == correct_idx}
                    for idx, k in enumerate(["A", "B", "C", "D", "E"])
                ],
                "orden": i + 1,
                "puntos": 1.0,
            })
        else:
            # Multiple answer: correct is A+C (indices 0,2)
            correct_answers[i] = [0, 2]
            preguntas.append({
                "nombre": f"pregunta_{i+1}",
                "tipo": "multiple",
                "enunciado": f"Pregunta {i+1} [MÚLTIPLE]",
                "opciones": [
                    {"key": k, "label": f"Opción {k}", "es_correcta": idx in [0, 2]}
                    for idx, k in enumerate(["A", "B", "C", "D", "E"])
                ],
                "orden": i + 1,
                "puntos": 2.0,
            })

    print(f"  ✓ {len(preguntas)} preguntas (10 single + 3 multiple)")

    # =========================================================
    # 2. Generar hoja de respuestas + coordenadas
    # =========================================================
    print()
    print("[2/5] Generando hoja de respuestas con coordenadas...")

    id_persona = "6185178"
    pdf_bytes, coords = generar_hoja_respuestas(
        evaluacion_nombre="Evaluación de Prueba",
        identificador=f"TEST-{id_persona}",
        preguntas=preguntas,
        descripcion="Test de evaluación con coordenadas",
        short_id="abc12345"
    )

    # Save PDF
    pdf_path = os.path.join(OUTPUT_DIR, "test_coordenadas.pdf")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)

    # Save coordinates JSON
    coords_json = exportar_coordenadas_json(coords)
    coords_path = os.path.join(OUTPUT_DIR, "test_coordenadas.json")
    with open(coords_path, "w") as f:
        f.write(coords_json)

    print(f"  ✓ PDF: {pdf_path} ({len(pdf_bytes)} bytes)")
    print(f"  ✓ Coordenadas: {coords_path}")
    print(f"    - Grid ID: {len(coords['id_grid'])} burbujas (10 filas × 10 cols)")
    print(f"    - Respuestas: {len(coords['answer_bubbles'])} burbujas")

    # =========================================================
    # 3. Convertir PDF a imagen y marcar burbujas simuladas
    # =========================================================
    print()
    print("[3/5] Convirtiendo PDF a imagen y simulando marcas...")

    image = pdf_to_image(pdf_path)
    if image is None:
        print("  ✗ No se pudo convertir el PDF a imagen")
        return

    h_img, w_img = image.shape[:2]
    print(f"  ✓ Imagen: {w_img}×{h_img} px")

    # Reload coordinates for this image scale
    coords_data = json.loads(coords_json)
    scale = _compute_scale(coords_data, image.shape)
    scale_factor = coords_data.get("scale_factor", 3)
    ratio = scale / scale_factor

    print(f"  Escala: {scale:.2f}x (ratio={ratio:.2f})")

    # Mark ID persona: 6185178
    # Digits: 6,1,8,5,1,7,8 → rows 0-6 have marks in columns 6,1,8,5,1,7,8
    id_marks = [6, 1, 8, 5, 1, 7, 8]  # column index for each row
    print(f"  Marcando ID persona: {''.join(str(d) for d in id_marks)}")

    for row_idx, col_idx in enumerate(id_marks):
        # Find the bubble in coords
        for b in coords_data["id_grid"]:
            if b["row"] == row_idx and b["col"] == col_idx:
                cx = int(b["cx_img"] * ratio)
                cy = int(b["cy_img"] * ratio)
                r = max(3, int(b["r_img"] * ratio))
                # Fill bubble (draw filled black circle)
                cv2.circle(image, (cx, cy), r, (0, 0, 0), -1)
                break

    # Mark answers:
    # Single questions (0-9): mark the correct answer
    # Multiple questions (10-12): mark A and C (indices 0 and 2)
    print("  Marcando respuestas:")
    for qi in range(13):
        correct = correct_answers[qi]
        marked_letters = []
        for option_idx in correct:
            for b in coords_data["answer_bubbles"]:
                if b["question_index"] == qi and b["option_index"] == option_idx:
                    cx = int(b["cx_img"] * ratio)
                    cy = int(b["cy_img"] * ratio)
                    r = max(3, int(b["r_img"] * ratio))
                    cv2.circle(image, (cx, cy), r, (0, 0, 0), -1)
                    marked_letters.append(b["option"])
                    break
        tipo = "M" if qi >= 10 else "S"
        print(f"    Q{qi+1} [{tipo}]: {','.join(marked_letters)}")

    # Save the "scanned" image
    scan_path = os.path.join(OUTPUT_DIR, "test_coordenadas_scanned.png")
    cv2.imwrite(scan_path, image)
    print(f"  ✓ Imagen escaneada guardada: {scan_path}")

    # =========================================================
    # 4. Evaluar usando coordenadas conocidas
    # =========================================================
    print()
    print("[4/5] Evaluando con coordenadas conocidas...")

    # Detect ID
    detected_id = _detect_id_from_coords(image, coords_data)
    print(f"  ID detectado: {detected_id}")
    print(f"  ID esperado:  {id_persona}")

    # Compare only the digits we marked (first 7), the rest are "?" (unmarked)
    id_match = detected_id[:len(id_persona)] == id_persona and all(
        c == "?" for c in detected_id[len(id_persona):]
    )
    print(f"  ID {'✓ CORRECTO' if id_match else '✗ INCORRECTO'}")

    # Detect answers (single)
    detected_answers = _detect_answers_from_coords(image, coords_data, 13)

    # Detect answers (multiple)
    detected_multi = _detect_multiple_answers_from_coords(image, coords_data, 13)

    # Grade
    option_labels = ["A", "B", "C", "D", "E"]
    correct_count = 0
    total_questions = 13

    print()
    print("  Resultados por pregunta:")
    for qi in range(13):
        correct = set(correct_answers[qi])
        detected_set = set(detected_multi[qi])
        detected_letters = ",".join(option_labels[i] for i in sorted(detected_set))
        correct_letters = ",".join(option_labels[i] for i in sorted(correct))

        # Grade: for single, detected must equal correct; for multiple, all must match
        is_correct = detected_set == correct
        if is_correct:
            correct_count += 1

        status = "✓" if is_correct else "✗"
        tipo = "M" if qi >= 10 else "S"
        print(f"    Q{qi+1} [{tipo}]: detectado={detected_letters or '-':5s} correcto={correct_letters:5s} {status}")

    print()
    print(f"  Total correctas: {correct_count}/{total_questions}")
    print(f"  Porcentaje: {correct_count/total_questions*100:.0f}%")

    # =========================================================
    # 5. Verificar resultados
    # =========================================================
    print()
    print("[5/5] Verificación final:")
    all_pass = True

    if id_match:
        print(f"  ✓ ID persona detectado correctamente: {detected_id}")
    else:
        print(f"  ✗ ID persona incorrecto: detectado={detected_id}, esperado={id_persona}")
        all_pass = False

    if correct_count == total_questions:
        print(f"  ✓ Todas las respuestas detectadas correctamente ({correct_count}/{total_questions})")
    else:
        print(f"  ✗ Algunas respuestas incorrectas: {correct_count}/{total_questions}")
        all_pass = False

    print()
    if all_pass:
        print("  🎉 ¡TEST PASADO! Todas las detecciones son correctas.")
    else:
        print("  ⚠️  Algunas detecciones fallaron. Revisar la detección.")

    print()
    print("=" * 60)
    print(f"  Archivos generados:")
    print(f"    - {pdf_path}")
    print(f"    - {coords_path}")
    print(f"    - {scan_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
