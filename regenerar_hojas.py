#!/usr/bin/env python3
"""
Regenera las hojas de respuestas existentes para que incluyan:
  - QR con {"short_id", "identificador"} (las viejas solo tenian short_id)
  - Marcas de esquina correctamente ancladas a las coordenadas nominales

Uso:
    python regenerar_hojas.py            # aplica los cambios
    python regenerar_hojas.py --dry-run  # solo lista que haria
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app.config import STORAGE_PATH
from app.database import SessionLocal
from app.models import Evaluacion, HojaRespuesta

DRY_RUN = "--dry-run" in sys.argv


def main():
    db = SessionLocal()
    hojas = db.query(HojaRespuesta).filter(HojaRespuesta.pdf_path.isnot(None)).all()
    pares = []
    vistos = set()
    for h in hojas:
        clave = (str(h.evaluacion_id), h.identificador)
        if clave in vistos:
            continue
        vistos.add(clave)
        ev = db.query(Evaluacion).filter(Evaluacion.id == h.evaluacion_id).first()
        pares.append((h, ev))
    db.close()

    print(f"Hojas a regenerar: {len(pares)}")
    if DRY_RUN:
        for h, ev in pares:
            estado = "OK" if ev else "SIN EVALUACION (se omite)"
            print(f"  - {h.identificador}  ({estado})")
        return

    ok = fallos = omitidas = 0
    with TestClient(__import__("app.main", fromlist=["app"]).app) as client:
        for h, ev in pares:
            if not ev:
                print(f"[OMITIDA] {h.identificador}: evaluación no existe")
                omitidas += 1
                continue

            dir_abs = os.path.dirname(os.path.join(STORAGE_PATH, h.pdf_path))
            # Borrar PDFs para forzar regeneración (/generar reutiliza el archivo si existe)
            for nombre in ("hoja_respuestas.pdf", "hojas_resultado.pdf", "hoja_preguntas.pdf"):
                p = os.path.join(dir_abs, nombre)
                if os.path.exists(p):
                    os.remove(p)

            r = client.post(
                f"/api/v1/generar/{h.evaluacion_id}",
                json={"identificador": h.identificador},
            )
            if r.status_code != 200:
                print(f"[FALLO] {h.identificador}: HTTP {r.status_code} {r.text[:120]}")
                fallos += 1
                continue

            coords_path = os.path.join(dir_abs, "coordenadas.json")
            qr_ok = False
            marks_ok = False
            if os.path.exists(coords_path):
                c = json.load(open(coords_path))
                marks_ok = len(c.get("corner_marks_img") or []) == 4
            try:
                data = r.json()
                qr = data.get("qr_data")
                if isinstance(qr, str):
                    qr = json.loads(qr)
                qr_ok = bool(qr and qr.get("identificador") == h.identificador)
            except Exception:
                pass
            estado = "OK" if (qr_ok and marks_ok) else "OK (verificar QR/marcas)"
            print(f"[{estado}] {h.identificador} -> qr_id={qr_ok}, marcas={marks_ok}")
            ok += 1

    print(f"\nRegeneradas: {ok} | Fallos: {fallos} | Omitidas: {omitidas}")


if __name__ == "__main__":
    main()
