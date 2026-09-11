#!/usr/bin/env python3
"""
Demo: hojas con ID persona prellenado + respuestas → evaluación completa.
Verifica detección de IDs con diferentes formatos.
"""
import os
import sys
import json
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.sheet_generator import (
    generar_hoja_respuestas, exportar_coordenadas_json
)
from app.routes.evaluar import (
    _detect_id_from_coords, _detect_multiple_answers_from_coords, _compute_scale
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "demo", "output")


def pdf_to_image(pdf_path):
    import fitz
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
    img_bytes = pix.tobytes("png")
    nparr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


def mark_id(image, coords_data, id_str, ratio):
    """Marca las burbujas del ID persona en la imagen."""
    for row_idx, digit_char in enumerate(id_str):
        if digit_char == " ":
            continue  # skip spaces
        col_idx = int(digit_char)
        for b in coords_data["id_grid"]:
            if b["row"] == row_idx and b["col"] == col_idx:
                cx = int(b["cx_img"] * ratio)
                cy = int(b["cy_img"] * ratio)
                r = max(3, int(b["r_img"] * ratio))
                cv2.circle(image, (cx, cy), r, (0, 0, 0), -1)
                break


def mark_answers(image, coords_data, answers, ratio):
    """Marca las respuestas en la imagen. answers = list of (qi, [option_indices])."""
    option_labels = ["A", "B", "C", "D", "E"]
    for qi, option_indices in answers:
        for opt_idx in option_indices:
            for b in coords_data["answer_bubbles"]:
                if b["question_index"] == qi and b["option_index"] == opt_idx:
                    cx = int(b["cx_img"] * ratio)
                    cy = int(b["cy_img"] * ratio)
                    r = max(3, int(b["r_img"] * ratio))
                    cv2.circle(image, (cx, cy), r, (0, 0, 0), -1)
                    break


def main():
    print("=" * 65)
    print("  DEMO: IDs PRELLENADOS + EVALUACIÓN")
    print("=" * 65)

    # Preguntas de prueba (10 single + 3 multiple)
    preguntas = []
    for i in range(13):
        if i < 10:
            preguntas.append({
                "nombre": f"q{i+1}", "tipo": "single",
                "enunciado": f"P{i+1}",
                "opciones": [{"key": k, "label": k, "es_correcta": k == ["A","B","C","D","E"][i%5]}
                             for k in ["A","B","C","D","E"]],
                "orden": i+1, "puntos": 1.0
            })
        else:
            preguntas.append({
                "nombre": f"q{i+1}", "tipo": "multiple",
                "enunciado": f"P{i+1} [M]",
                "opciones": [{"key": k, "label": k, "es_correcta": k in ["A","C"]}
                             for k in ["A","B","C","D","E"]],
                "orden": i+1, "puntos": 2.0
            })

    # Respuestas correctas esperadas
    correct_answers = {}
    for i in range(10):
        correct_answers[i] = [i % 5]
    for i in range(10, 13):
        correct_answers[i] = [0, 2]  # A, C

    # =====================================================
    # Casos de prueba: (id_str, respuestas_marcadas)
    # =====================================================
    casos = [
        {
            "nombre": "Caso 1: ID completo 7 dígitos",
            "id": "6185178",
            "respuestas": [(i, correct_answers[i]) for i in range(13)],
            "esperado_id": "6185178",
            "esperado_correctas": 13,
        },
        {
            "nombre": "Caso 2: ID de 10 dígitos",
            "id": "1234567890",
            "respuestas": [(i, correct_answers[i]) for i in range(13)],
            "esperado_id": "1234567890",
            "esperado_correctas": 13,
        },
        {
            "nombre": "Caso 3: ID de 3 dígitos (solo primeras filas)",
            "id": "456",
            "respuestas": [(i, correct_answers[i]) for i in range(13)],
            "esperado_id": "456",
            "esperado_correctas": 13,
        },
        {
            "nombre": "Caso 4: ID + respuestas parcialmente incorrectas",
            "id": "9999999",
            "respuestas": [
                (0, [1]),   # B en vez de A → incorrecta
                (1, [1]),   # B en vez de B → correcta
                (2, [2]),   # C en vez de C → correcta
                (3, [0]),   # A en vez de D → incorrecta
                (10, [0,2]), # A,C → correcta
                (11, [0,1]), # A,B en vez de A,C → incorrecta
            ],
            "esperado_id": "9999999",
            "esperado_correctas": 3,
        },
        {
            "nombre": "Caso 5: ID largo + todas mal",
            "id": "0000000000",
            "respuestas": [
                (0, [1]), (1, [2]), (2, [3]), (3, [4]), (4, [0]),
                (5, [1]), (6, [2]), (7, [3]), (8, [4]), (9, [0]),
                (10, [1,3]), (11, [1,3]), (12, [1,3]),
            ],
            "esperado_id": "0000000000",
            "esperado_correctas": 0,
        },
    ]

    option_labels = ["A", "B", "C", "D", "E"]
    total_pass = 0

    for caso_idx, caso in enumerate(casos):
        print()
        print(f"--- {caso['nombre']} ---")

        # Generar hoja
        pdf_bytes, coords = generar_hoja_respuestas(
            evaluacion_nombre="Test",
            identificador=f"ID-{caso['id']}",
            preguntas=preguntas,
            descripcion="",
            short_id="abc12345"
        )

        # PDF → imagen
        pdf_path = os.path.join(OUTPUT_DIR, f"test_id_{caso_idx+1}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        image = pdf_to_image(pdf_path)

        # Preparar coordenadas
        coords_data = json.loads(exportar_coordenadas_json(coords))
        scale = _compute_scale(coords_data, image.shape)
        ratio = scale / coords_data.get("scale_factor", 3)

        # Marcar ID
        mark_id(image, coords_data, caso["id"], ratio)

        # Marcar respuestas
        mark_answers(image, coords_data, caso["respuestas"], ratio)

        # Guardar imagen escaneada
        scan_path = os.path.join(OUTPUT_DIR, f"test_id_{caso_idx+1}_scan.png")
        cv2.imwrite(scan_path, image)

        # Evaluar
        detected_id = _detect_id_from_coords(image, coords_data)
        detected_multi = _detect_multiple_answers_from_coords(image, coords_data, 13)

        # Calificar
        correctas = 0
        for qi in range(13):
            expected = set(correct_answers.get(qi, []))
            detected = set(detected_multi[qi]) if qi < len(detected_multi) else set()
            if detected == expected:
                correctas += 1

        # Verificar ID
        id_ok = detected_id == caso["esperado_id"]
        eval_ok = correctas == caso["esperado_correctas"]
        pass_ok = id_ok and eval_ok

        if pass_ok:
            total_pass += 1

        status = "✓ PASS" if pass_ok else "✗ FAIL"
        print(f"  ID detectado:    '{detected_id}' (esperado: '{caso['esperado_id']}') {'✓' if id_ok else '✗'}")
        print(f"  Respuestas:      {correctas}/{13} correctas (esperado: {caso['esperado_correctas']}) {'✓' if eval_ok else '✗'}")
        print(f"  Resultado:       {status}")

        # Detalle de respuestas
        if not eval_ok:
            print("  Detalle:")
            for qi in range(13):
                expected = set(correct_answers.get(qi, []))
                detected = set(detected_multi[qi]) if qi < len(detected_multi) else set()
                if detected != expected:
                    e = ",".join(option_labels[i] for i in sorted(expected))
                    d = ",".join(option_labels[i] for i in sorted(detected))
                    print(f"    Q{qi+1}: detectado={d or '-':5s} esperado={e}")

    print()
    print("=" * 65)
    print(f"  RESUMEN: {total_pass}/{len(casos)} casos pasaron")
    if total_pass == len(casos):
        print("  🎉 ¡TODOS LOS CASOS PASARON!")
    else:
        print("  ⚠️  Algunos casos fallaron")
    print("=" * 65)


if __name__ == "__main__":
    main()
