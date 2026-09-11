"""
Script para crear las tablas de la base de datos.
Requiere que .env esté configurado con DATABASE_URL.
"""
from app.database import engine, Base
from app.models import (
    Evaluacion, Pregunta, Opcion,
    HojaRespuesta, Resultado, RespuestaDetalle
)


def main():
    print("Dropping tablas existentes...")
    Base.metadata.drop_all(bind=engine)
    print("      ✓ Tablas eliminadas")

    print("Creando tablas...")
    Base.metadata.create_all(bind=engine)
    print("Tablas creadas exitosamente.")


if __name__ == "__main__":
    main()
