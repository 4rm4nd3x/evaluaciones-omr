"""
Migración de datos legacy para el nuevo modelo de secciones.

- Crea las tablas nuevas (secciones, hojas_genericas) si no existen.
- Añade columnas idempotentes (seccion_id, config_seleccion).
- Backfill: convierte el string 'seccion' de cada pregunta en una fila de
  la tabla Seccion (una por evaluación con el mismo nombre) y asocia el id.

Uso: python db_migrate.py
"""
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.database import engine, Base, SessionLocal
from app.models import Evaluacion, Seccion, Pregunta


def _backfill_secciones(db: Session):
    print("Backfill de secciones legacy (string -> tabla Seccion)...")
    preguntas = db.query(Pregunta).filter(
        Pregunta.seccion.isnot(None),
        Pregunta.seccion != "",
        Pregunta.seccion_id.is_(None),
    ).all()

    # Cache: (evaluacion_id, nombre) -> Seccion
    cache = {}
    creadas = 0
    for p in preguntas:
        key = (p.evaluacion_id, p.seccion.strip())
        if key not in cache:
            seccion = db.query(Seccion).filter(
                Seccion.evaluacion_id == p.evaluacion_id,
                Seccion.nombre == p.seccion.strip(),
            ).first()
            if not seccion:
                seccion = Seccion(evaluacion_id=p.evaluacion_id, nombre=p.seccion.strip())
                db.add(seccion)
                db.flush()
                creadas += 1
            cache[key] = seccion
        p.seccion_id = cache[key].id
    db.commit()
    print(f"      ✓ {len(preguntas)} preguntas asociadas, {creadas} secciones creadas")


def main():
    print("Aplicando ALTER TABLE idempotentes...")
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE hojas_respuesta ADD COLUMN IF NOT EXISTS preguntas_orden JSONB"))
        conn.execute(text("ALTER TABLE hojas_respuesta ADD COLUMN IF NOT EXISTS config_seleccion JSONB"))
        conn.execute(text("ALTER TABLE preguntas ADD COLUMN IF NOT EXISTS seccion_id UUID"))
        conn.execute(text("ALTER TABLE preguntas ADD COLUMN IF NOT EXISTS activa BOOLEAN NOT NULL DEFAULT TRUE"))
        conn.execute(text("ALTER TABLE resultados ADD COLUMN IF NOT EXISTS errores JSONB"))
        conn.execute(text("ALTER TABLE respuestas_detalle ADD COLUMN IF NOT EXISTS ambigua BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.commit()
    print("      ✓ Columnas verificadas")

    print("Creando tablas si no existen...")
    Base.metadata.create_all(bind=engine)
    print("      ✓ Tablas creadas")

    db = SessionLocal()
    try:
        _backfill_secciones(db)
    finally:
        db.close()
    print("Migración completada.")


if __name__ == "__main__":
    main()