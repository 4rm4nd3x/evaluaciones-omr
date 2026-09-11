#!/usr/bin/env python3
"""
Demo completo: crea evaluaciones, agrega preguntas desde CSV, genera hojas, evalua.

Uso:
    python demo.py
"""
import os
import sys
import csv
import json
import base64
import time

# Ensure we're in the project root
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.database import engine, Base, SessionLocal
from app.models import Evaluacion, Pregunta, Opcion, HojaRespuesta, Resultado, RespuestaDetalle
from app.services.sheet_generator import (
    generar_hoja_respuestas, generar_hoja_preguntas,
    generar_pdf_revisado
)

DEMO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo")
OUTPUT_DIR = os.path.join(DEMO_DIR, "output")


def setup_db():
    """Crea las tablas."""
    print("=" * 60)
    print("  DEMO EVALUACIONES OMR")
    print("=" * 60)
    print()
    print("[1/5] Regenerando base de datos...")
    print("      - Dropping tablas existentes...")
    Base.metadata.drop_all(bind=engine)
    print("      - Creando tablas...")
    Base.metadata.create_all(bind=engine)
    print("      ✓ Tablas regeneradas")


def load_csv(filepath):
    """Lee un CSV de preguntas."""
    preguntas = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            preguntas.append(row)
    return preguntas


def crear_evaluacion(db, nombre, descripcion):
    """Crea una evaluación y retorna el objeto."""
    import secrets
    short_id = secrets.token_hex(4)
    ev = Evaluacion(nombre=nombre, descripcion=descripcion, short_id=short_id)
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


def agregar_preguntas(db, evaluacion_id, preguntas_csv):
    """Agrega preguntas desde CSV a una evaluación."""
    for row in preguntas_csv:
        tipo = row.get("tipo", "single").strip()
        nombre = row["nombre"].strip()
        enunciado = row.get("enunciado", "").strip()
        orden = int(row.get("orden", 1))
        puntos = float(row.get("puntos", 1.0))
        seccion = row.get("seccion", "").strip()

        pregunta = Pregunta(
            evaluacion_id=evaluacion_id,
            nombre=nombre,
            tipo=tipo,
            enunciado=enunciado,
            orden=orden,
            puntos=puntos,
            requerida=True,
            seccion=seccion
        )
        db.add(pregunta)
        db.flush()

        # Opciones A-E
        option_labels = ["a", "b", "c", "d", "e"]
        correctas = row.get("correcta", "").strip().upper()

        for label in option_labels:
            texto = row.get(f"opcion_{label}", "").strip()
            if not texto:
                continue
            es_correcta = label.upper() in correctas
            opcion = Opcion(
                pregunta_id=pregunta.id,
                key=label.upper(),
                label=texto,
                es_correcta=es_correcta,
                orden=option_labels.index(label) + 1
            )
            db.add(opcion)

    db.commit()


def crear_evaluaciones(db):
    """Crea las 2 evaluaciones con sus preguntas."""
    print()
    print("[2/5] Creando evaluaciones...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    evaluaciones = []

    # Evaluación 1: Matemáticas
    csv_files = [
        ("evaluacion_1_preguntas.csv", "Evaluacion Matematicas 2026", "Examen final de matematicas - 50 preguntas"),
        ("evaluacion_2_preguntas.csv", "Evaluacion Cultura General 2026", "Examen final de cultura general - 50 preguntas"),
    ]

    for csv_file, nombre, desc in csv_files:
        filepath = os.path.join(DEMO_DIR, csv_file)
        preguntas = load_csv(filepath)

        ev = crear_evaluacion(db, nombre, desc)
        agregar_preguntas(db, ev.id, preguntas)
        evaluaciones.append(ev)

        print(f"      ✓ {nombre}: {len(preguntas)} preguntas creadas (ID: {ev.id})")

    return evaluaciones


def generar_hojas(db, evaluaciones):
    """Genera hojas de respuestas para cada evaluación."""
    print()
    print("[3/5] Generando hojas de respuestas...")

    hojas_info = []

    for ev in evaluaciones:
        preguntas_db = db.query(Pregunta).filter(
            Pregunta.evaluacion_id == ev.id
        ).order_by(Pregunta.orden).all()

        preguntas_list = []
        for p in preguntas_db:
            preguntas_list.append({
                "nombre": p.nombre,
                "tipo": p.tipo.value if hasattr(p.tipo, 'value') else p.tipo,
                "enunciado": p.enunciado,
                "opciones": [{"key": o.key, "label": o.label, "es_correcta": o.es_correcta} for o in p.opciones],
                "orden": p.orden,
                "puntos": p.puntos
            })

        identificador = f"DEMO-{ev.nombre[:3].upper()}-{str(ev.id)[:8].upper()}"

        # Generate PDF + coordinates
        from app.services.sheet_generator import exportar_coordenadas_json
        pdf_bytes, coords = generar_hoja_respuestas(
            evaluacion_nombre=ev.nombre,
            identificador=identificador,
            preguntas=preguntas_list,
            descripcion=ev.descripcion or "",
            short_id=ev.short_id
        )
        coords_json = exportar_coordenadas_json(coords)

        # Save PDF to disk
        pdf_filename = f"hoja_{ev.nombre.replace(' ', '_').lower()}.pdf"
        pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # Save base64 for demo
        b64_filename = f"hoja_{ev.nombre.replace(' ', '_').lower()}.b64"
        b64_path = os.path.join(OUTPUT_DIR, b64_filename)
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        with open(b64_path, "w") as f:
            f.write(pdf_b64)

        # Save coordinates JSON
        coords_filename = f"coordenadas_{ev.nombre.replace(' ', '_').lower()}.json"
        coords_path = os.path.join(OUTPUT_DIR, coords_filename)
        with open(coords_path, "w") as f:
            f.write(coords_json)

        # Create QR data (short_id)
        qr_data = ev.short_id

        # Save to DB
        hoja = HojaRespuesta(
            evaluacion_id=ev.id,
            identificador=identificador,
            qr_data=qr_data
        )
        db.add(hoja)

        hojas_info.append({
            "evaluacion": ev,
            "identificador": identificador,
            "qr_data": qr_data,
            "pdf_path": pdf_path,
            "preguntas_count": len(preguntas_list)
        })

        print(f"      ✓ {ev.nombre}: {len(pdf_bytes)} bytes guardado en {pdf_path}")

        # Generate questions sheet
        preguntas_preguntas = []
        for p in preguntas_db:
            opciones = [{"key": o.key, "label": o.label, "es_correcta": o.es_correcta} for o in p.opciones]
            preguntas_preguntas.append({
                "nombre": p.nombre,
                "enunciado": p.enunciado,
                "tipo": p.tipo.value if hasattr(p.tipo, 'value') else p.tipo,
                "opciones": opciones,
                "orden": p.orden,
                "puntos": p.puntos,
                "seccion": p.seccion or ""
            })

        preguntas_pdf, preguntas_coords = generar_hoja_preguntas(
            evaluacion_nombre=ev.nombre,
            identificador=identificador,
            preguntas=preguntas_preguntas,
            descripcion=ev.descripcion or ""
        )
        preguntas_filename = f"preguntas_{ev.nombre.replace(' ', '_').lower()}.pdf"
        preguntas_path = os.path.join(OUTPUT_DIR, preguntas_filename)
        with open(preguntas_path, "wb") as f:
            f.write(preguntas_pdf)
        print(f"      ✓ {ev.nombre} (preguntas): {len(preguntas_pdf)} bytes en {preguntas_path}")

    db.commit()
    return hojas_info


def simular_evaluacion(db, hojas_info):
    """Simula evaluar respuestas (marcas burbujas rellenas)."""
    print()
    print("[4/5] Simulando evaluaciones...")

    option_labels = ["A", "B", "C", "D", "E"]
    resultados_info = []

    for info in hojas_info:
        ev = info["evaluacion"]
        identificador = info["identificador"]

        # Get correct answers
        preguntas_db = db.query(Pregunta).filter(
            Pregunta.evaluacion_id == ev.id
        ).order_by(Pregunta.orden).all()

        correct_answers = {}
        for idx, p in enumerate(preguntas_db):
            correct_indices = []
            for opt in p.opciones:
                if opt.es_correcta:
                    key = opt.key.upper().strip()
                    if key in option_labels:
                        correct_indices.append(option_labels.index(key))
            correct_answers[idx] = correct_indices

        # Simulate: mark correct answers (as if student got 70-80% right)
        respuestas_detalle = []
        total_puntos = 0.0
        respuestas_correctas = 0

        import random
        random.seed(42)  # Reproducible

        for idx, p in enumerate(preguntas_db):
            correct = correct_answers.get(idx, [])

            # 75% chance to get it right
            if random.random() < 0.75 and correct:
                detected_idx = random.choice(correct)
                es_correcta = True
            else:
                # Mark a random wrong answer
                wrong = [i for i in range(len(option_labels)) if i not in correct]
                detected_idx = random.choice(wrong) if wrong else 0
                es_correcta = False

            puntos = p.puntos if es_correcta else 0.0
            total_puntos += puntos
            if es_correcta:
                respuestas_correctas += 1

            respuesta_texto = option_labels[detected_idx]

            respuestas_detalle.append({
                "pregunta_id": p.id,
                "nombre": p.nombre,
                "respuesta": respuesta_texto,
                "es_correcta": es_correcta,
                "puntos_obtenidos": puntos
            })

        # Create resultado in DB
        resultado = Resultado(
            evaluacion_id=ev.id,
            identificador=identificador,
            identificador_persona="1234567890",
            puntuacion_total=total_puntos,
            total_preguntas=len(preguntas_db),
            respuestas_correctas=respuestas_correctas
        )
        db.add(resultado)
        db.flush()

        for rd in respuestas_detalle:
            rd_obj = RespuestaDetalle(
                resultado_id=resultado.id,
                pregunta_id=rd["pregunta_id"],
                nombre=rd["nombre"],
                respuesta=rd["respuesta"],
                es_correcta=rd["es_correcta"],
                puntos_obtenidos=rd["puntos_obtenidos"]
            )
            db.add(rd_obj)

        # Generate reviewed PDF
        pdf_bytes = generar_pdf_revisado(respuestas_detalle, ev.nombre, identificador)
        pdf_filename = f"resultado_{ev.nombre.replace(' ', '_').lower()}.pdf"
        pdf_path = os.path.join(OUTPUT_DIR, pdf_filename)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        resultados_info.append({
            "resultado_id": str(resultado.id),
            "identificador": identificador,
            "puntuacion_total": total_puntos,
            "total_preguntas": len(preguntas_db),
            "respuestas_correctas": respuestas_correctas,
            "respuestas": respuestas_detalle,
            "pdf_revisado_path": pdf_path,
            "evaluacion_nombre": ev.nombre
        })

        porcentaje = (respuestas_correctas / len(preguntas_db)) * 100 if preguntas_db else 0
        print(f"      ✓ {ev.nombre}: {respuestas_correctas}/{len(preguntas_db)} correctas ({porcentaje:.0f}%) - {total_puntos:.1f} puntos")

    db.commit()
    return resultados_info


def mostrar_resultados(resultados_info):
    """Muestra el resumen de resultados."""
    print()
    print("[5/5] Resumen de resultados")
    print()
    print("-" * 60)

    for res in resultados_info:
        print(f"  Evaluacion: {res['evaluacion_nombre']}")
        print(f"  Identificador: {res['identificador']}")
        print(f"  Puntuacion: {res['puntuacion_total']:.1f} puntos")
        print(f"  Correctas: {res['respuestas_correctas']}/{res['total_preguntas']}")
        print(f"  PDF revisado: {res['pdf_revisado_path']}")
        print(f"  Resultado ID: {res['resultado_id']}")

        # Show first 5 responses
        print(f"  Primeras 5 respuestas:")
        for i, r in enumerate(res["respuestas"][:5]):
            marca = "✓" if r["es_correcta"] else "✗"
            print(f"    {i+1}. {r['nombre']}: {r['respuesta']} {marca} ({r['puntos_obtenidos']:.1f} pts)")
        if len(res["respuestas"]) > 5:
            print(f"    ... y {len(res['respuestas']) - 5} mas")
        print("-" * 60)

    # JSON output
    output_json = os.path.join(OUTPUT_DIR, "resultados.json")
    with open(output_json, "w") as f:
        json.dump(resultados_info, f, indent=2, default=str)
    print()
    print(f"  Resultados JSON guardados en: {output_json}")


def main():
    start = time.time()

    setup_db()
    db = SessionLocal()

    try:
        evaluaciones = crear_evaluaciones(db)
        hojas_info = generar_hojas(db, evaluaciones)
        resultados_info = simular_evaluacion(db, hojas_info)
        mostrar_resultados(resultados_info)
    finally:
        db.close()

    elapsed = time.time() - start
    print()
    print(f"  ✓ Demo completado en {elapsed:.2f} segundos")
    print(f"  ✓ Archivos generados en: {OUTPUT_DIR}")
    print()


if __name__ == "__main__":
    main()
