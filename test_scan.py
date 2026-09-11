#!/usr/bin/env python3
"""
Test real: simula escanear una hoja de respuestas y evaluarla con OMR.
"""
import os
import sys
import json
import base64
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import engine, Base, SessionLocal
from app.models import Evaluacion, Pregunta, Opcion
from app.services.sheet_generator import generar_hoja_respuestas
from app.services.omr_processor import (
    detect_qr_codes, preprocess_image, is_bubble_filled,
    detect_identifier_section, detect_answer_bubbles
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "demo", "output")


def get_evaluaciones(db):
    """Obtiene las evaluaciones de la BD."""
    return db.query(Evaluacion).all()


def get_preguntas(db, evaluacion_id):
    """Obtiene las preguntas de una evaluación."""
    return db.query(Pregunta).filter(
        Pregunta.evaluacion_id == evaluacion_id
    ).order_by(Pregunta.orden).all()


def pdf_to_image(pdf_path):
    """Convierte un PDF a imagen usando PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x resolution
        img_bytes = pix.tobytes("png")
        nparr = np.frombuffer(img_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return img
    except ImportError:
        print("  PyMuPDF no instalado, intentando con poppler...")
        # Fallback: use pdf2image if available
        try:
            from pdf2image import convert_from_path
            images = convert_from_path(pdf_path, dpi=200)
            if images:
                # Convert PIL to OpenCV
                img_array = np.array(images[0])
                return cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
        except ImportError:
            pass
        return None


def mark_bubble(image, cx, cy, radius=10):
    """Marca una burbuja como llenada (círculo negro sólido)."""
    cv2.circle(image, (cx, cy), radius, (0, 0, 0), -1)


def simulate_student_marks(image, preguntas, correct_answers):
    """Simula un estudiante marcando respuestas (75% correctas)."""
    import random
    random.seed(42)

    processed = preprocess_image(image)
    contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    bubbles = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < 50 or area > 3000:
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
                bubbles.append((cx, cy, area))

    bubbles.sort(key=lambda b: (b[1], b[0]))

    # Group into rows
    rows = []
    current_row = [bubbles[0]] if bubbles else []
    for b in bubbles[1:]:
        if current_row and abs(b[1] - current_row[-1][1]) < 25:
            current_row.append(b)
        else:
            if current_row:
                rows.append(sorted(current_row, key=lambda x: x[0]))
            current_row = [b]
    if current_row:
        rows.append(sorted(current_row, key=lambda x: x[0]))

    # Mark answers (skip ID section rows, only mark answer bubbles)
    option_labels = ["A", "B", "C", "D", "E"]
    marked = []

    for row_idx, row in enumerate(rows):
        if row_idx >= len(preguntas):
            break

        pregunta = preguntas[row_idx]
        correct_idx = correct_answers.get(row_idx, [])

        # 75% chance to get it right
        if random.random() < 0.75 and correct_idx:
            mark_idx = random.choice(correct_idx)
        else:
            # Mark wrong answer
            wrong = [i for i in range(5) if i not in correct_idx]
            mark_idx = random.choice(wrong) if wrong else 0

        # Mark the bubble
        if mark_idx < len(row):
            cx, cy, area = row[mark_idx]
            mark_bubble(image, cx, cy, radius=8)
            marked.append({
                "pregunta": pregunta.nombre,
                "marcada": option_labels[mark_idx],
                "correcta_idx": correct_idx,
                "es_correcta": mark_idx in correct_idx
            })

    return image, marked


def main():
    print("=" * 60)
    print("  TEST REAL: ESCANEO Y EVALUACIÓN OMR")
    print("=" * 60)
    print()

    db = SessionLocal()

    try:
        # Get evaluations
        evaluaciones = get_evaluaciones(db)
        if not evaluaciones:
            print("  No hay evaluaciones en la BD. Ejecuta python demo.py primero.")
            return

        for ev in evaluaciones:
            print(f"  Evaluación: {ev.nombre}")
            print(f"  ID: {ev.id}")
            print()

            # Get questions
            preguntas = get_preguntas(db, ev.id)
            print(f"  Preguntas: {len(preguntas)}")

            # Build correct answers
            option_labels = ["A", "B", "C", "D", "E"]
            correct_answers = {}
            for idx, p in enumerate(preguntas):
                correct_indices = []
                for opt in p.opciones:
                    if opt.es_correcta:
                        key = opt.key.upper().strip()
                        if key in option_labels:
                            correct_indices.append(option_labels.index(key))
                correct_answers[idx] = correct_indices

            # Generate answer sheet PDF
            preguntas_list = []
            for p in preguntas:
                preguntas_list.append({
                    "nombre": p.nombre,
                    "tipo": p.tipo.value if hasattr(p.tipo, 'value') else p.tipo,
                    "enunciado": p.enunciado,
                    "opciones": [{"texto": o.label, "es_correcta": o.es_correcta} for o in p.opciones],
                    "orden": p.orden,
                    "puntos": p.puntos
                })

            pdf_bytes, _coords = generar_hoja_respuestas(
                evaluacion_nombre=ev.nombre,
                identificador=f"TEST-{str(ev.id)[:8].upper()}",
                preguntas=preguntas_list,
                descripcion=ev.descripcion or ""
            )

            # Save PDF
            pdf_path = os.path.join(OUTPUT_DIR, f"test_scan_{ev.nombre.replace(' ', '_').lower()}.pdf")
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)
            print(f"  PDF generado: {pdf_path}")

            # Convert PDF to image
            print()
            print("  Convirtiendo PDF a imagen...")
            image = pdf_to_image(pdf_path)

            if image is None:
                print("  ✗ No se pudo convertir el PDF a imagen.")
                print("  Instala PyMuPDF: pip install PyMuPDF")
                continue

            print(f"  ✓ Imagen: {image.shape[1]}x{image.shape[0]} px")

            # Save clean image
            clean_path = os.path.join(OUTPUT_DIR, f"test_scan_clean_{ev.nombre.replace(' ', '_').lower()}.png")
            cv2.imwrite(clean_path, image)
            print(f"  ✓ Imagen limpia guardada: {clean_path}")

            # Simulate student marks
            print()
            print("  Simulando marcas de estudiante (75% correctas)...")
            marked_image, marked_answers = simulate_student_marks(image, preguntas, correct_answers)

            # Save marked image (simulated scan)
            scan_path = os.path.join(OUTPUT_DIR, f"test_scan_marked_{ev.nombre.replace(' ', '_').lower()}.png")
            cv2.imwrite(scan_path, marked_image)
            print(f"  ✓ Imagen escaneada guardada: {scan_path}")

            # Now run OMR detection on the marked image
            print()
            print("  Procesando con OMR...")

            # Detect QR
            qr_data_list = detect_qr_codes(marked_image)
            print(f"  QR detectados: {len(qr_data_list)}")

            # Detect identifier
            identificador = detect_identifier_section(marked_image)
            print(f"  ID Persona detectado: {identificador}")

            # Detect answers
            num_options = max(len(p.opciones) for p in preguntas) if preguntas else 5
            detected_answers = detect_answer_bubbles(marked_image, len(preguntas), num_options)

            # Grade
            correctas = 0
            for idx, detected in enumerate(detected_answers):
                correct = correct_answers.get(idx, [])
                if detected is not None and detected in correct:
                    correctas += 1

            total = len(preguntas)
            print()
            print(f"  Resultados OMR:")
            print(f"    Total preguntas: {total}")
            print(f"    Correctas detectadas: {correctas}/{total}")
            print(f"    Porcentaje: {correctas/total*100:.0f}%")

            # Generate annotated image
            annotated = marked_image.copy()
            h, w = annotated.shape[:2]

            # Banner
            overlay = annotated.copy()
            cv2.rectangle(overlay, (0, 0), (w, 50), (40, 40, 40), -1)
            cv2.addWeighted(overlay, 0.7, annotated, 0.3, 0, annotated)
            cv2.putText(annotated, f"ID: {identificador}", (15, 25),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 180, 0), 2)
            cv2.putText(annotated, f"Correctas: {correctas}/{total}",
                        (w - 250, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 180, 0), 2)

            # Find bubbles and mark them
            processed = preprocess_image(marked_image)
            contours, _ = cv2.findContours(processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            bubbles = []
            for cnt in contours:
                area = cv2.contourArea(cnt)
                if area < 50 or area > 3000:
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
                        bubbles.append((cx, cy, area))

            bubbles.sort(key=lambda b: (b[1], b[0]))

            rows = []
            current_row = [bubbles[0]] if bubbles else []
            for b in bubbles[1:]:
                if current_row and abs(b[1] - current_row[-1][1]) < 25:
                    current_row.append(b)
                else:
                    if current_row:
                        rows.append(sorted(current_row, key=lambda x: x[0]))
                    current_row = [b]
            if current_row:
                rows.append(sorted(current_row, key=lambda x: x[0]))

            # Mark each answer
            for row_idx, row in enumerate(rows[:total]):
                detected = detected_answers[row_idx] if row_idx < len(detected_answers) else None
                correct = correct_answers.get(row_idx, [])

                for i, (cx, cy, area) in enumerate(row[:5]):
                    is_correct_answer = i in correct
                    is_detected = detected == i

                    if is_detected and is_correct_answer:
                        cv2.circle(annotated, (cx, cy), 18, (0, 180, 0), 3)
                        cv2.putText(annotated, "V", (cx - 8, cy + 6),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 180, 0), 2)
                    elif is_detected and not is_correct_answer:
                        cv2.circle(annotated, (cx, cy), 18, (0, 0, 220), 3)
                        cv2.putText(annotated, "X", (cx - 7, cy + 6),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 220), 2)

            # Save annotated
            annotated_path = os.path.join(OUTPUT_DIR, f"test_scan_annotated_{ev.nombre.replace(' ', '_').lower()}.png")
            cv2.imwrite(annotated_path, annotated)
            print(f"  ✓ Imagen anotada guardada: {annotated_path}")

            # Show marked answers detail
            print()
            print("  Detalle de respuestas marcadas:")
            for i, m in enumerate(marked_answers[:10]):
                marca = "✓" if m["es_correcta"] else "✗"
                print(f"    {i+1}. {m['pregunta']}: {m['marcada']} {marca}")
            if len(marked_answers) > 10:
                print(f"    ... y {len(marked_answers) - 10} más")

            print()
            print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()
