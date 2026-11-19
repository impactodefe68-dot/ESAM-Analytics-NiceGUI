import os
from dotenv import load_dotenv
import psycopg2

# Cargar variables privadas desde .env si existe
load_dotenv()


def get_connection():
    """Abre una conexión con la base de datos Neon."""

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "Falta configurar la variable DATABASE_URL."
        )

    return psycopg2.connect(
        database_url,
        connect_timeout=10
    )


def probar_conexion():
    """Comprueba la conexión sin modificar ningún dato."""

    conexion = None

    try:
        conexion = get_connection()

        with conexion.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_user;"
            )
            resultado = cursor.fetchone()

        return {
            "ok": True,
            "mensaje": "Conexión correcta con Neon.",
            "base_datos": resultado[0],
            "usuario": resultado[1],
        }

    except Exception:
        return {
            "ok": False,
            "mensaje": (
                "No se pudo conectar. "
                "Revisa la configuración de DATABASE_URL."
            ),
        }

    finally:
        if conexion is not None:
            conexion.close()
