"""
ESAM Analytics 360° — versión NiceGUI (main.py).

Misma base de datos (Neon PostgreSQL) y mismas reglas de negocio que la app
Streamlit: tramos de básquetbol con recálculo retroactivo, tarifas en vivo,
picheos de béisbol, quincenas, permisos por rol, auditoría consolidada y
descargas CSV. Interfaz oscura (fondo negro/azul noche).

Ejecutar en local:
    pip install -r requirements.txt
    python main.py

Despliegue en Render:
    Build:  pip install -r requirements.txt
    Start:  python main.py
    Variables: DATABASE_URL, ADMIN_PASSWORD, STORAGE_SECRET

Configuración (en este orden de prioridad):
    1. Variables de entorno: DATABASE_URL, ADMIN_PASSWORD, STORAGE_SECRET, PORT.
    2. Archivo .streamlit/secrets.toml ([postgres] url = "..." / admin_password = "...").

Nota: NiceGUI necesita un host con proceso persistente (Render, Railway, Fly,
VPS). No corre sobre Streamlit Cloud.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import tomllib
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse, urlunparse

import pandas as pd
from nicegui import app, ui


def _cargar_psycopg2():
    """Importa psycopg2; si el entorno de despliegue no lo trae, lo instala al arrancar."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2, RealDictCursor
    except ImportError:
        pass

    print("[ESAM] psycopg2 no está instalado; intentando instalar psycopg2-binary…")
    import subprocess
    import sys

    for paquete in ("psycopg2-binary==2.9.13", "psycopg2-binary"):
        cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir", paquete]
        resultado = subprocess.run(cmd, capture_output=True, text=True)
        if resultado.returncode == 0:
            print(f"[ESAM] Instalado {paquete} correctamente.")
            break
        print(f"[ESAM] pip falló al instalar {paquete} (código {resultado.returncode}).")
        detalle = ((resultado.stderr or "") + (resultado.stdout or "")).strip().splitlines()
        for linea in detalle[-15:]:
            print("[ESAM][pip]", linea)

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        return psycopg2, RealDictCursor
    except ImportError:
        print("[ESAM] Sin psycopg2. Agrega esta línea al requirements.txt que lee el despliegue:")
        print("[ESAM]     psycopg2-binary==2.9.13")
        raise RuntimeError("psycopg2 no disponible en el entorno")


psycopg2, RealDictCursor = _cargar_psycopg2()

# =========================================================
# 0. ZONA HORARIA LOCAL (EL SERVIDOR TRABAJA EN UTC)
# =========================================================
ZONA_HORARIA_UTC_OFFSET = -4  # Venezuela / Bolivia (UTC-4). Ajusta si cambias de país.


def ahora_local() -> datetime:
    """Fecha y hora local real del operador (UTC + desfase)."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=ZONA_HORARIA_UTC_OFFSET)


def hoy_local() -> date:
    """Fecha de HOY según la hora local del operador (nunca la del servidor)."""
    return ahora_local().date()


# =========================================================
# 0.1 CONFIGURACIÓN / SECRETOS
# =========================================================
def _cargar_secrets_toml() -> dict:
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit", "secrets.toml")
    try:
        with open(ruta, "rb") as archivo:
            return tomllib.load(archivo)
    except Exception:
        return {}


_SECRETS = _cargar_secrets_toml()


def _secreto(clave, defecto=None):
    if clave in os.environ:
        return os.environ[clave]
    return _SECRETS.get(clave, defecto)


def _limpiar_url(url):
    """Neon agrega channel_binding/sslmode; libpq viejos rechazan esos parámetros."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        for clave in ("sslmode", "ssl", "channel_binding"):
            qs.pop(clave, None)
        nueva_query = "&".join(f"{k}={v[0]}" for k, v in qs.items())
        return urlunparse(parsed._replace(query=nueva_query))
    except Exception:
        return url


def get_db_url():
    """URL de Neon: DATABASE_URL o [postgres] url de secrets.toml."""
    url = _secreto("DATABASE_URL")
    if not url:
        postgres = _SECRETS.get("postgres") or {}
        url = postgres.get("url")
    return _limpiar_url(url)


ADMIN_PASSWORD_INICIAL = _secreto("ADMIN_PASSWORD") or _secreto("admin_password") or "admin123"
STORAGE_SECRET = _secreto("STORAGE_SECRET") or "esam-analytics-360-cambia-esta-clave"
PUERTO_PREFERIDO = int(_secreto("PORT") or 8081)


def puerto_libre(host: str, puerto: int) -> bool:
    """True si el puerto se puede ocupar en este equipo."""
    prueba = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        prueba.bind((host, puerto))
        return True
    except OSError:
        return False
    finally:
        prueba.close()


def elegir_puerto(host: str, preferido: int, intentos: int = 25) -> int:
    """Usa el puerto preferido o el siguiente libre (evita choques con otros programas)."""
    for candidato in range(preferido, preferido + intentos):
        if puerto_libre(host, candidato):
            return candidato
    return preferido


# El puerto 8080 suele estar tomado o bloqueado en Windows: se elige uno libre.
PUERTO = elegir_puerto("0.0.0.0", PUERTO_PREFERIDO)

# Resultado de init_db(), para avisar en la pantalla de acceso si no hay base.
ESTADO_DB = {"ok": False}

PBKDF2_ITERACIONES = 240_000


# =========================================================
# 0.2 UTILIDADES DE TIEMPO DE PARTIDO (HH:MM:SS)
# =========================================================
def parsear_tiempo_a_segundos(valor):
    """Convierte 'HH:MM:SS', 'MM:SS' o 'SS' a segundos. None si no es válido."""
    if valor is None or pd.isna(valor):
        return None
    texto_val = str(valor).strip()
    if not texto_val or texto_val.lower() in ("none", "nan", "nat", "null"):
        return None
    partes = texto_val.split(":")
    try:
        numeros = [int(float(p)) for p in partes]
    except ValueError:
        return None
    if len(numeros) == 3:
        horas, minutos, segundos = numeros
    elif len(numeros) == 2:
        horas, minutos, segundos = 0, numeros[0], numeros[1]
    elif len(numeros) == 1:
        horas, minutos, segundos = 0, 0, numeros[0]
    else:
        return None
    return horas * 3600 + minutos * 60 + segundos


def formatear_segundos(segundos):
    """Convierte segundos a texto HH:MM:SS legible."""
    if segundos is None:
        return "—"
    total = int(round(segundos))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


# =========================================================
# 1. SEGURIDAD Y ENCRIPTACIÓN (PBKDF2-SHA256 CON SAL)
# =========================================================
def hash_password(password: str) -> str:
    """Hash con sal aleatoria: pbkdf2_sha256$iteraciones$sal$hash."""
    sal = os.urandom(16)
    digesto = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), sal, PBKDF2_ITERACIONES)
    return f"pbkdf2_sha256${PBKDF2_ITERACIONES}${sal.hex()}${digesto.hex()}"


def verificar_password(password: str, hash_guardado) -> bool:
    """Verifica la clave contra el hash nuevo (con sal) o el viejo SHA-256."""
    if not hash_guardado:
        return False
    guardado = str(hash_guardado)
    if guardado.startswith("pbkdf2_sha256$"):
        try:
            _, iteraciones, sal_hex, digesto_hex = guardado.split("$")
            digesto = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), bytes.fromhex(sal_hex), int(iteraciones)
            )
            return hmac.compare_digest(digesto.hex(), digesto_hex)
        except Exception:
            return False
    # Compatibilidad con el hash antiguo (SHA-256 sin sal), se migra al iniciar sesión
    return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), guardado)


def hash_es_legado(hash_guardado) -> bool:
    """True si el hash guardado es del esquema viejo (sin sal) o está vacío."""
    return not str(hash_guardado or "").startswith("pbkdf2_sha256$")


# =========================================================
# 2. CONEXIÓN A BASE DE DATOS (NEON POSTGRESQL)
# =========================================================
def get_db_connection():
    url = get_db_url()
    if not url:
        print("[ESAM] Falta DATABASE_URL o [postgres] url en .streamlit/secrets.toml")
        return None
    try:
        # sslmode=require: Neon exige TLS; así no depende del libpq instalado.
        return psycopg2.connect(url, connect_timeout=10, sslmode="require")
    except Exception as e:
        print(f"[ESAM] Error de conexión a PostgreSQL: {e}")
        return None


def cerrar_conexion(conn):
    """Cierra la conexión sin romper la app si ya estaba cerrada."""
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


# =========================================================
# 3. INICIALIZACIÓN Y MIGRACIÓN AUTOMÁTICA DE TABLAS
# =========================================================
def init_db():
    """Crea y migra las tablas con autocommit (un ALTER fallido no deshace el CREATE)."""
    conn = get_db_connection()
    if not conn:
        return False

    cur = None
    try:
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                nombre VARCHAR(100) NOT NULL DEFAULT 'Operador',
                password_hash VARCHAR(200) NOT NULL DEFAULT '',
                password VARCHAR(100),
                rol VARCHAR(30) DEFAULT 'operador',
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre VARCHAR(100) DEFAULT 'Operador';")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS password_hash VARCHAR(200) DEFAULT '';")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS password VARCHAR(100);")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS rol VARCHAR(30) DEFAULT 'operador';")
        cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("ALTER TABLE usuarios ALTER COLUMN password DROP NOT NULL;")
        cur.execute("ALTER TABLE usuarios ALTER COLUMN password_hash TYPE VARCHAR(200);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS partidos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                codigo_partido VARCHAR(50) NOT NULL,
                fecha DATE NOT NULL,
                tiempo_ejecucion VARCHAR(30),
                tarifa NUMERIC(10, 2) DEFAULT 0.00,
                tipo_partido VARCHAR(30) DEFAULT 'quincenal',
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS picheos (
                id SERIAL PRIMARY KEY,
                usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                fecha DATE NOT NULL,
                id_control VARCHAR(50) NOT NULL,
                cantidad INTEGER NOT NULL,
                creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS configuracion_global (
                clave VARCHAR(50) PRIMARY KEY,
                valor NUMERIC(10, 4) NOT NULL
            );
        """)

        # Conserva los datos históricos si ya hay duplicados; la aplicación
        # igualmente impide nuevas cargas duplicadas dentro de una transacción.
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_partidos_codigo_normalizado ON partidos (btrim(codigo_partido));")
        except psycopg2.errors.UniqueViolation:
            print("[ESAM] Hay códigos de partido duplicados históricos. Revísalos antes de crear el índice único; no se eliminó ningún registro.")

        cur.execute("CREATE INDEX IF NOT EXISTS idx_partidos_usuario_fecha ON partidos (usuario_id, fecha);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_partidos_fecha ON partidos (fecha);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_picheos_usuario_fecha ON picheos (usuario_id, fecha);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_picheos_fecha ON picheos (fecha);")

        defaults = [
            ("tasa_tramo_1", 6.00),
            ("tasa_tramo_2", 6.50),
            ("tasa_tramo_3", 7.00),
            ("tasa_tramo_4", 7.50),
            ("tasa_tramo_5", 8.00),
            ("tasa_en_vivo_normal", 12.00),
            ("tasa_en_vivo_nba", 18.50),
            ("precio_beisbol", 0.024),
        ]
        for clave, val in defaults:
            cur.execute("""
                INSERT INTO configuracion_global (clave, valor)
                VALUES (%s, %s)
                ON CONFLICT (clave) DO NOTHING;
            """, (clave, val))

        # Migración única al esquema de 5 tramos de básquetbol
        cur.execute("SELECT valor FROM configuracion_global WHERE clave = 'esquema_tramos_basket';")
        if cur.fetchone() is None:
            tramos_nuevos = [
                ("tasa_tramo_1", 6.00),
                ("tasa_tramo_2", 6.50),
                ("tasa_tramo_3", 7.00),
                ("tasa_tramo_4", 7.50),
                ("tasa_tramo_5", 8.00),
            ]
            for clave_t, val_t in tramos_nuevos:
                cur.execute("""
                    INSERT INTO configuracion_global (clave, valor)
                    VALUES (%s, %s)
                    ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor;
                """, (clave_t, val_t))
            cur.execute("""
                INSERT INTO configuracion_global (clave, valor)
                VALUES ('esquema_tramos_basket', 2)
                ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor;
            """)

        # El admin se crea una sola vez: no se resetea la clave en cada arranque
        cur.execute("""
            INSERT INTO usuarios (username, nombre, password_hash, rol)
            VALUES ('admin', 'Administrador Global', %s, 'admin')
            ON CONFLICT (username) DO NOTHING;
        """, (hash_password(ADMIN_PASSWORD_INICIAL),))

        return True
    except Exception as e:
        print(f"[ESAM] Error al inicializar tablas: {e}")
        return False
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def codigo_partido_ya_existe(cur, codigo, excluir_id=None):
    """Serializa escrituras desde esta app y busca el código en todos los usuarios."""
    cur.execute("SELECT pg_advisory_xact_lock(1163084109);")
    cur.execute(
        "SELECT 1 FROM partidos WHERE btrim(codigo_partido) = %s AND (%s::integer IS NULL OR id <> %s) LIMIT 1;",
        (codigo, excluir_id, excluir_id),
    )
    return cur.fetchone() is not None


# =========================================================
# 4. TARIFAS Y RECÁLCULO RETROACTIVO
# =========================================================
def obtener_tasas_globales():
    tasas = {
        "tramo_1": 6.00,
        "tramo_2": 6.50,
        "tramo_3": 7.00,
        "tramo_4": 7.50,
        "tramo_5": 8.00,
        "en_vivo_normal": 12.00,
        "en_vivo_nba": 18.50,
        "precio_beisbol": 0.024,
    }
    conn = get_db_connection()
    if conn:
        cur = None
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT clave, valor FROM configuracion_global;")
            for r in cur.fetchall():
                k = r["clave"]
                val = float(r["valor"])
                if k == "tasa_tramo_1":
                    tasas["tramo_1"] = val
                elif k == "tasa_tramo_2":
                    tasas["tramo_2"] = val
                elif k == "tasa_tramo_3":
                    tasas["tramo_3"] = val
                elif k == "tasa_tramo_4":
                    tasas["tramo_4"] = val
                elif k == "tasa_tramo_5":
                    tasas["tramo_5"] = val
                elif k == "tasa_en_vivo_normal":
                    tasas["en_vivo_normal"] = val
                elif k == "tasa_en_vivo_nba":
                    tasas["en_vivo_nba"] = val
                elif k == "precio_beisbol":
                    tasas["precio_beisbol"] = val
        except Exception as e:
            print(f"[ESAM] No se pudieron leer las tarifas ({e}). Se usan las de fábrica.")
        finally:
            try:
                if cur is not None:
                    cur.close()
            except Exception:
                pass
            cerrar_conexion(conn)
    return tasas


def actualizar_tasa_global(clave, nuevo_valor, usuario) -> bool:
    """Solo la cuenta administradora puede modificar las tarifas globales."""
    username = str((usuario or {}).get("username", "")).lower().strip()
    if username != "admin":
        return False
    conn = get_db_connection()
    if not conn:
        return False
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE configuracion_global SET valor = %s WHERE clave = %s;", (nuevo_valor, clave))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"[ESAM] Error al actualizar tarifa {clave}: {e}")
        return False
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def obtener_tarifa_basket(tipo_partido, cant_partidos=0):
    tasas = obtener_tasas_globales()
    if tipo_partido == "en_vivo_nba":
        return tasas["en_vivo_nba"]
    if tipo_partido == "en_vivo_normal":
        return tasas["en_vivo_normal"]
    if cant_partidos <= 11:
        return tasas["tramo_1"]
    if cant_partidos <= 14:
        return tasas["tramo_2"]
    if cant_partidos <= 26:
        return tasas["tramo_3"]
    if cant_partidos <= 29:
        return tasas["tramo_4"]
    return tasas["tramo_5"]


def _ids_usuarios():
    conn = get_db_connection()
    if not conn:
        return []
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM usuarios;")
        return [int(r[0]) for r in cur.fetchall()]
    except Exception as e:
        print(f"[ESAM] Error listando usuarios: {e}")
        return []
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def recalcular_tarifas_quincena_basket(u_id, fecha_ref):
    """
    1. Cuenta EXCLUSIVAMENTE los partidos de modalidad 'quincenal' en la quincena dada.
    2. Los partidos 'en_vivo_normal' y 'en_vivo_nba' NO CUENTAN para el volumen del tramo.
    3. Actualiza RETROACTIVAMENTE solo la tarifa de los partidos 'quincenal'.
    """
    if u_id == 0:
        for uid in _ids_usuarios():
            recalcular_tarifas_quincena_basket(uid, fecha_ref)
        return

    iq_i, iq_f = obtener_rango_quincena(fecha_ref)
    conn = get_db_connection()
    if not conn:
        return
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM partidos
            WHERE usuario_id = %s
              AND fecha >= %s
              AND fecha <= %s
              AND (tipo_partido IS NULL OR tipo_partido = 'quincenal');
        """, (u_id, iq_i, iq_f))
        partidos_quincenales = cur.fetchone()[0]

        nueva_tarifa = obtener_tarifa_basket("quincenal", partidos_quincenales)

        cur.execute("""
            UPDATE partidos
            SET tarifa = %s
            WHERE usuario_id = %s
              AND fecha >= %s
              AND fecha <= %s
              AND (tipo_partido IS NULL OR tipo_partido = 'quincenal');
        """, (nueva_tarifa, u_id, iq_i, iq_f))
        conn.commit()
    except Exception as e:
        print(f"[ESAM] Error al recalcular quincena: {e}")
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


# =========================================================
# 5. USUARIOS Y FECHAS DE QUINCENA
# =========================================================
def registrar_usuario(username, nombre, password, rol="operador"):
    conn = get_db_connection()
    if not conn:
        return False, "❌ Error de conexión a BD."
    cur = None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO usuarios (username, nombre, password_hash, rol)
            VALUES (%s, %s, %s, %s);
        """, (username.lower().strip(), nombre.strip(), hash_password(password), rol))
        conn.commit()
        return True, "✅ Usuario registrado exitosamente."
    except psycopg2.IntegrityError:
        conn.rollback()
        return False, "⚠️ El nombre de usuario ya existe."
    except Exception as e:
        conn.rollback()
        return False, f"❌ Error en registro: {e}"
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def autenticar_usuario(username, password):
    """Valida la clave en Python y migra el hash viejo en el mismo inicio de sesión."""
    user_clean = str(username or "").lower().strip()
    pass_clean = str(password or "").strip()
    if not user_clean or not pass_clean:
        return None

    conn = get_db_connection()
    if not conn:
        return None

    cur = None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            cur.execute("""
                SELECT id, username, nombre, rol, password_hash, password
                FROM usuarios WHERE username = %s;
            """, (user_clean,))
            fila = cur.fetchone()
        except Exception:
            conn.rollback()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT id, username, nombre, rol, password_hash
                FROM usuarios WHERE username = %s;
            """, (user_clean,))
            fila = cur.fetchone()

        if not fila:
            return None

        clave_plana = fila.get("password")
        coincide = verificar_password(pass_clean, fila.get("password_hash"))
        if not coincide and clave_plana and hmac.compare_digest(str(clave_plana), pass_clean):
            coincide = True
        if not coincide:
            return None

        # Migración silenciosa: pasa de SHA-256/plano a PBKDF2 con sal
        if hash_es_legado(fila.get("password_hash")) or clave_plana:
            try:
                cur.execute(
                    "UPDATE usuarios SET password_hash = %s, password = NULL WHERE id = %s;",
                    (hash_password(pass_clean), fila["id"]),
                )
                conn.commit()
            except Exception:
                conn.rollback()

        return {
            "id": int(fila["id"]),
            "username": fila["username"],
            "nombre": fila["nombre"],
            "rol": fila["rol"],
        }
    except Exception as e:
        print(f"[ESAM] Error al autenticar: {e}")
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def cambiar_password_usuario(user_id, nueva_password):
    conn = get_db_connection()
    if not conn:
        return False, "❌ Error de conexión a BD."
    cur = None
    try:
        cur = conn.cursor()
        nuevo_hash = hash_password(nueva_password)
        try:
            cur.execute(
                "UPDATE usuarios SET password_hash = %s, password = NULL WHERE id = %s;",
                (nuevo_hash, user_id),
            )
        except Exception:
            conn.rollback()
            cur = conn.cursor()
            cur.execute("UPDATE usuarios SET password_hash = %s WHERE id = %s;", (nuevo_hash, user_id))
        conn.commit()
        return True, "✅ Contraseña actualizada correctamente."
    except Exception as e:
        conn.rollback()
        return False, f"❌ Error al actualizar contraseña: {e}"
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        cerrar_conexion(conn)


def obtener_todos_los_usuarios():
    conn = get_db_connection()
    if not conn:
        return pd.DataFrame()
    try:
        query = """
            SELECT u.id, u.username, u.nombre, u.rol,
                   COUNT(DISTINCT p.id) AS total_partidos_basket,
                   COUNT(DISTINCT pi.id) AS total_picheos_beisbol
            FROM usuarios u
            LEFT JOIN partidos p ON u.id = p.usuario_id
            LEFT JOIN picheos pi ON u.id = pi.usuario_id
            GROUP BY u.id, u.username, u.nombre, u.rol
            ORDER BY u.id ASC;
        """
        return pd.read_sql_query(query, conn)
    except Exception as e:
        print(f"[ESAM] Error al cargar usuarios: {e}")
        return pd.DataFrame()
    finally:
        cerrar_conexion(conn)


def obtener_rango_quincena(fecha_ref, numero_quincena=None):
    anio = fecha_ref.year
    mes = fecha_ref.month

    if numero_quincena == 1:
        return date(anio, mes, 1), date(anio, mes, 15)
    if numero_quincena == 2:
        fin_mes = date(anio, 12, 31) if mes == 12 else date(anio, mes + 1, 1) - timedelta(days=1)
        return date(anio, mes, 16), fin_mes
    if fecha_ref.day <= 15:
        return date(anio, mes, 1), date(anio, mes, 15)
    fin_mes = date(anio, 12, 31) if mes == 12 else date(anio, mes + 1, 1) - timedelta(days=1)
    return date(anio, mes, 16), fin_mes


RANGOS_PERIODO = [
    "Todos los Registros",
    "1. 1.ª Quincena (Días 1 al 15)",
    "2. 2.ª Quincena (Días 16 al final de mes)",
    "Quincena Anterior",
    "Este Mes Completo",
    "Rango de Fechas Personalizado",
]


def resolver_periodo(estado):
    """Devuelve (fecha_inicio, fecha_fin) según la selección guardada en la sesión."""
    hoy = hoy_local()
    seleccion = estado.get("rango", RANGOS_PERIODO[0])

    if "1. 1.ª Quincena" in seleccion:
        return obtener_rango_quincena(hoy, numero_quincena=1)
    if "2. 2.ª Quincena" in seleccion:
        return obtener_rango_quincena(hoy, numero_quincena=2)
    if seleccion == "Quincena Anterior":
        if hoy.day <= 15:
            primer_dia_mes_actual = date(hoy.year, hoy.month, 1)
            ultimo_dia_mes_pasado = primer_dia_mes_actual - timedelta(days=1)
            return obtener_rango_quincena(ultimo_dia_mes_pasado, numero_quincena=2)
        return obtener_rango_quincena(hoy, numero_quincena=1)
    if seleccion == "Este Mes Completo":
        fin_mes = date(hoy.year, 12, 31) if hoy.month == 12 else date(hoy.year, hoy.month + 1, 1) - timedelta(days=1)
        return date(hoy.year, hoy.month, 1), fin_mes
    if seleccion == "Rango de Fechas Personalizado":
        desde = fecha_desde_iso(estado.get("desde")) or (hoy - timedelta(days=30))
        hasta = fecha_desde_iso(estado.get("hasta")) or hoy
        if desde > hasta:
            desde, hasta = hasta, desde
        return desde, hasta
    return date(2020, 1, 1), date(2035, 12, 31)


def fecha_desde_iso(valor):
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def iso_fecha(valor):
    """Convierte date/Timestamp/texto a 'YYYY-MM-DD' para los selectores de fecha."""
    try:
        return pd.to_datetime(valor).date().isoformat()
    except Exception:
        return hoy_local().isoformat()


# =========================================================
# 6. ESTILOS
# =========================================================
ESTILOS = """@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* ============================================================
   ESAM Analytics 360° — Paleta Render (panel de la foto)
   Fondo plano #090D14 · Superficie #10151E · Borde #2A3441
   Azul sólido #315CFF · Verde eléctrico #00D68F · Texto #FFFFFF
   Sin degradados, sin brillos, sin sombras de color.
   ============================================================ */

/* Negro desde el primer pintado: evita el destello blanco al cargar */
html, body { background: #090D14 !important; color-scheme: dark; }
body::before {
    content: ''; position: fixed; top: 0; left: 0; right: 0; height: 440px; z-index: 0; pointer-events: none;
    background: radial-gradient(1000px 430px at 50% -170px, rgba(49, 92, 255, .22), transparent 62%);
}
.nicegui-content { position: relative; z-index: 1; }

:root {
    --bg: #090D14;
    --bg-soft: #0D121B;
    --surface: #10151E;
    --surface-2: #171E2A;
    --surface-3: #1F2937;
    --border: #2A3441;
    --border-soft: #1E293B;
    --text: #FFFFFF;
    --text-soft: #CBD5E1;
    --muted: #94A3B8;
    --accent: #315CFF;
    --accent-hover: #4A6FFF;
    --accent-soft: rgba(49, 92, 255, .14);
    --success: #00D68F;
    --danger: #EF4444;
    --warning: #F59E0B;
    --radio: 8px;
    --radio-sm: 6px;
}

/* ---------- Base ---------- */
body, .q-page-container, .nicegui-content {
    background: var(--bg) !important;
    color: var(--text) !important;
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif !important;
    -webkit-font-smoothing: antialiased;
}

.nicegui-content { padding: 1.6rem 2rem 3.5rem !important; max-width: 1560px; margin: 0 auto; width: 100%; }

.q-page, .q-layout, .q-drawer, .q-header { background: transparent !important; color: var(--text) !important; }
.q-card, .q-dialog__inner > .q-card { background: var(--surface) !important; color: var(--text) !important; border: 1px solid var(--border) !important; }
.q-tab-panel { background: transparent !important; padding: .55rem 0 0 !important; }
::selection { background: var(--accent); color: #FFFFFF; }

/* ---------- Cabecera (como la barra superior de Render) ---------- */
.app-header {
    background: var(--bg-soft) !important;
    border-bottom: 1px solid var(--border) !important;
    padding: .5rem .85rem !important;
    align-items: center;
}

.brand-mark { display: inline-flex; filter: drop-shadow(0 6px 18px rgba(49, 92, 255, .35)); }
.app-brand {
    display: block; font-family: 'Inter', sans-serif;
    font-size: 1rem; font-weight: 800; letter-spacing: .045em; color: #FFFFFF;
    line-height: 1.1; white-space: nowrap;
}
.app-brand em { font-style: normal; color: #00D68F; }
.brand-tag {
    font-size: .56rem; letter-spacing: .22em; text-transform: uppercase;
    color: var(--muted); font-weight: 700; white-space: nowrap;
}
.drawer-toggle {
    color: var(--text-soft) !important; border: 1px solid var(--border) !important;
    border-radius: 9px !important; background: var(--surface) !important;
}
.drawer-toggle:hover { color: #FFFFFF !important; border-color: #3A475C !important; background: var(--surface-2) !important; }
.header-user {
    display: inline-flex; align-items: center; gap: .4rem; font-size: .8rem; color: var(--text-soft);
    background: var(--surface); border: 1px solid var(--border); padding: .32rem .7rem; border-radius: 999px;
    white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis;
}
.live-pill {
    display: inline-flex; align-items: center; gap: .4rem; font-size: .6rem; letter-spacing: .15em;
    font-weight: 800; color: #00D68F; background: rgba(0, 214, 143, .08);
    border: 1px solid rgba(0, 214, 143, .35); border-radius: 999px; padding: .3rem .65rem; white-space: nowrap;
}
.live-dot { width: .45rem; height: .45rem; border-radius: 50%; background: #00D68F; animation: pulso 1.6s ease-in-out infinite; }
@keyframes pulso { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }

/* ---------- Menú lateral (como el sidebar de Render) ---------- */
.app-drawer {
    background: var(--bg-soft) !important;
    border-right: 1px solid var(--border) !important;
}

.app-drawer .q-drawer__content { padding: 1rem .8rem 1.4rem; }

.menu-item {
    width: 100%; justify-content: flex-start; color: var(--text-soft) !important;
    border: 1px solid transparent; border-radius: var(--radio-sm);
    text-transform: none; font-weight: 500; font-size: .855rem;
    padding: .5rem .7rem; min-height: 40px;
    transition: background .15s ease, color .15s ease, border-color .15s ease;
}

.menu-item:hover {
    background: var(--surface-2) !important;
    color: #FFFFFF !important;
    border-color: var(--border);
}

.menu-item.activo {
    background: var(--accent) !important;
    color: #FFFFFF !important;
    border-color: var(--accent);
}

/* ---------- Tipografía ---------- */
.titulo-pagina {
    font-family: 'Inter', sans-serif;
    font-size: clamp(1.28rem, 2vw, 1.5rem); font-weight: 700;
    letter-spacing: -.02em; color: #FFFFFF; line-height: 1.28;
}

.subtitulo {
    display: flex; align-items: center; gap: .55rem;
    font-size: .95rem; font-weight: 600; color: #FFFFFF; letter-spacing: -.01em;
}

.subtitulo::before {
    content: ''; width: 3px; height: 15px; border-radius: 2px; flex: none;
    background: var(--accent);
}

.etiqueta { font-size: .655rem; letter-spacing: .11em; text-transform: uppercase; color: var(--muted); font-weight: 700; }
.aviso { font-size: .795rem; color: var(--muted); line-height: 1.55; }
.nota { font-size: .795rem; color: var(--text-soft); }

/* ---------- Tarjetas y métricas (planas, como las tarjetas de Render) ---------- */
.tarjeta {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radio);
    padding: 1.05rem 1.15rem; gap: .3rem; min-width: 0;
    transition: border-color .15s ease;
}

.tarjeta:hover { border-color: #3A475C; }

.tarjeta-tabla { padding: .45rem .55rem; }

.metrica { position: relative; overflow: hidden; padding-top: 1.15rem; }

.metrica::after {
    content: ''; position: absolute; inset: 0 0 auto 0; height: 3px;
    background: var(--accent);
}

/* Métricas planas: azul Render; la 3.ª (Béisbol) lleva el verde eléctrico #00D68F */
.grid > .metrica:nth-child(2)::after,
.grid > .metrica:nth-child(4)::after,
.grid > .metrica:nth-child(5)::after { background: var(--accent); }
.grid > .metrica:nth-child(3)::after { background: #00D68F; }

.metrica-label {
    font-size: .655rem; letter-spacing: .09em; text-transform: uppercase;
    color: var(--muted); font-weight: 700; line-height: 1.35; white-space: normal;
}

.metrica-valor {
    font-family: 'Inter', sans-serif;
    font-size: clamp(1.12rem, 1.8vw, 1.5rem); font-weight: 700;
    letter-spacing: -.02em; color: #FFFFFF; line-height: 1.16;
    white-space: normal; overflow-wrap: anywhere; font-variant-numeric: tabular-nums;
}

.metrica-pie { font-size: .715rem; color: var(--muted); line-height: 1.35; }

/* ---------- Campos (como el buscador de Render) ---------- */
.q-field--outlined .q-field__control {
    background: var(--bg-soft) !important; border-radius: var(--radio-sm) !important;
    min-height: 42px;
}

.q-field--outlined .q-field__control:before { border-color: var(--border) !important; }
.q-field--outlined.q-field--focused .q-field__control:after { border-color: var(--accent) !important; }
.q-field--outlined.q-field--focused .q-field__control { box-shadow: 0 0 0 3px rgba(49, 92, 255, .22); }

.q-field__native, .q-field__input, .q-field__prefix, .q-field__suffix { color: var(--text) !important; }
.q-field__native::placeholder, .q-field__input::placeholder { color: #64748B !important; opacity: 1; }
.q-field__label { color: var(--muted) !important; font-size: .82rem; }
.q-field--outlined .q-field__label { background: transparent !important; }
.q-field__bottom { color: var(--muted) !important; }

.q-menu, .q-dialog__inner .q-card, .q-date, .q-time {
    background: var(--surface) !important; color: var(--text) !important;
    border: 1px solid var(--border) !important; border-radius: var(--radio) !important;
}

.q-date__header { background: var(--accent) !important; }
.q-date__calendar-item button { color: var(--text-soft) !important; }
.q-date__navigation { color: var(--text-soft) !important; }

.q-item { color: var(--text-soft) !important; border-radius: 6px; }
.q-item:hover { background: var(--surface-2) !important; color: #FFFFFF !important; }
.q-checkbox__label, .q-radio__label { color: var(--text-soft) !important; font-size: .85rem; }

/* ---------- Botones (azul sólido como Free/banner; secundario como Connect) ---------- */
.q-btn { text-transform: none !important; font-weight: 600 !important; letter-spacing: 0 !important; }
.q-btn { border-radius: var(--radio-sm); transition: background .15s ease, border-color .15s ease, color .15s ease; }

.btn-primario {
    background: var(--accent) !important;
    color: #FFFFFF !important;
    border: 1px solid var(--accent) !important;
}

.btn-primario:hover { background: var(--accent-hover) !important; border-color: var(--accent-hover) !important; }
.btn-primario:hover .q-focus-helper { opacity: 0 !important; }

.btn-suave { color: var(--text-soft) !important; border: 1px solid var(--border) !important; background: var(--surface) !important; }
.btn-suave:hover { background: var(--surface-2) !important; color: #FFFFFF !important; border-color: #3A475C !important; }

.btn-peligro { color: #FCA5A5 !important; background: rgba(239, 68, 68, .10) !important; border: 1px solid rgba(239, 68, 68, .55) !important; }
.btn-peligro:hover { background: rgba(239, 68, 68, .20) !important; color: #FFFFFF !important; }

.btn-descarga { background: var(--surface) !important; border: 1px solid var(--border) !important; color: var(--text-soft) !important; }
.btn-descarga:hover { border-color: var(--accent) !important; color: #FFFFFF !important; background: var(--surface-2) !important; }

/* ---------- Tablas ---------- */
.q-table__container { background: transparent !important; border-radius: var(--radio) !important; overflow: hidden; }
.tarjeta-tabla .q-table__container { border: none !important; }
.q-table { background: transparent !important; color: var(--text-soft) !important; }
.q-table thead tr { background: var(--bg-soft) !important; }

.q-table th {
    color: var(--muted) !important; font-size: .665rem !important; text-transform: uppercase;
    letter-spacing: .085em; font-weight: 700; padding: .6rem .7rem !important;
}

.q-table tbody td {
    color: var(--text-soft) !important; font-size: .815rem !important; padding: .55rem .7rem !important;
    border-bottom: 1px solid var(--border-soft) !important; font-variant-numeric: tabular-nums;
}

.q-table tbody tr:hover { background: var(--surface-2) !important; }
.q-table tbody tr:last-child td { border-bottom: none !important; }
.q-table__bottom { color: var(--muted) !important; background: transparent !important; }

/* ---------- Pestañas ---------- */
.q-tabs { border-bottom: 1px solid var(--border); }
.q-tab { color: var(--muted) !important; text-transform: none !important; font-weight: 600; font-size: .85rem; min-height: 42px; padding: 0 .95rem; }
.q-tab--active { color: #FFFFFF !important; }
.q-tab__indicator { background: var(--accent) !important; height: 2px !important; }

/* ---------- Expansiones ---------- */
.q-expansion-item {
    background: var(--surface) !important;
    border: 1px solid var(--border) !important; border-radius: var(--radio) !important; overflow: hidden;
}

.q-expansion-item__container > .q-item { color: var(--text) !important; font-weight: 600; font-size: .88rem; padding: .6rem .9rem; min-height: auto; }
.q-expansion-item__content { padding: 0 .95rem .95rem; }
.q-item__label { color: inherit !important; }

/* ---------- Avisos / notificaciones ---------- */
.q-notification {
    font-size: .85rem; border-radius: var(--radio) !important;
    background: var(--surface-2) !important; color: #FFFFFF !important;
    border: 1px solid var(--border) !important;
}

.q-notification--positive { border-color: rgba(34, 197, 94, .6) !important; }
.q-notification--negative { border-color: rgba(239, 68, 68, .6) !important; }

.separador { height: 1px; width: 100%; margin: .45rem 0; background: var(--border-soft); }

/* ---------- Acceso ---------- */
.login-wrap { width: 100%; max-width: 470px; margin: 6vh auto 0; }

.login-crest {
    position: relative; width: 96px; height: 96px; margin: 0 auto 1rem;
    display: flex; align-items: center; justify-content: center;
}
.login-crest::before {
    content: ''; position: absolute; inset: -28px; border-radius: 50%;
    background: radial-gradient(circle, rgba(49, 92, 255, .38), transparent 70%);
}
.login-crest svg { position: relative; filter: drop-shadow(0 10px 26px rgba(49, 92, 255, .4)); }

.login-name {
    font-family: 'Inter', sans-serif;
    font-size: 1.85rem; font-weight: 800; letter-spacing: .03em;
    color: #FFFFFF; text-align: center; line-height: 1.12;
}

.login-name span { color: #00D68F; }
.login-sub { font-size: .6rem; letter-spacing: .24em; color: var(--muted); text-align: center; font-weight: 700; margin-top: .3rem; }

.login-line {
    position: relative; width: 128px; height: 3px; margin: .95rem auto 1.15rem; border-radius: 4px;
    background: var(--accent);
}
.login-line::after {
    content: ''; position: absolute; right: -9px; top: -3.5px; width: 10px; height: 10px;
    border-radius: 50%; background: #00D68F;
}

.panel-acceso {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px; padding: 1.3rem 1.45rem 1.5rem; width: 100%;
}

/* ---------- Barra de desplazamiento ---------- */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 8px; border: 2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background: var(--accent); }

/* ---------- Foco accesible ---------- */
.q-btn:focus-visible, .q-field--focused .q-field__control { outline: none; }

@media (max-width: 760px) {
    .brand-tag, .header-user, .live-pill { display: none; }
    .app-brand { font-size: .88rem; letter-spacing: .03em; }
    .app-header { padding: .45rem .55rem !important; }
    .nicegui-content { padding: .75rem .65rem calc(2.2rem + env(safe-area-inset-bottom, 0px)) !important; }
    .titulo-pagina { font-size: 1.12rem; letter-spacing: -.01em; }
    .metrica { padding: .75rem .8rem; }
    .metrica-valor { font-size: .98rem; }
    .tarjeta { padding: .8rem .85rem; border-radius: 12px; }
    .q-drawer { width: min(300px, 86vw) !important; }
    .login-wrap { margin-top: 1.5rem; max-width: 100%; padding: 0 .15rem; }
    .login-name { font-size: 1.32rem; }
    .login-sub { letter-spacing: .13em; font-size: .53rem; }
    .panel-acceso { padding: 1rem 1rem 1.15rem; border-radius: 14px; }
    .subtitulo { font-size: .86rem; }
    .q-table th { font-size: .58rem !important; padding: .5rem .45rem !important; }
    .q-table tbody td { font-size: .72rem !important; padding: .45rem .45rem !important; }
}
@media (max-width: 400px) {
    .app-brand { font-size: .8rem; }
    .login-name { font-size: 1.15rem; }
}
"""


# =========================================================
# 7. AYUDANTES DE INTERFAZ
# =========================================================
def ok(msg):
    ui.notify(msg, type="positive", position="top-right")


def error(msg):
    ui.notify(msg, type="negative", position="top-right")


def aviso(msg):
    ui.notify(msg, type="warning", position="top-right")


@contextmanager
def tarjeta(clases=""):
    with ui.column().classes(f"tarjeta {clases}"):
        yield


def rejilla(clases):
    """Contenedor CSS grid (evita el flex de ui.row para repartir columnas)."""
    return ui.element("div").classes(f"grid {clases} w-full")


def _logo_svg(tam=40):
    """Marca ESAM 360°: anillo 360° azul, punto verde electrico y barras de estadistica."""
    return (
        f'<svg width="{tam}" height="{tam}" viewBox="0 0 48 48" fill="none" '
        'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="ESAM 360">'
        '<rect x="1" y="1" width="46" height="46" rx="12.5" fill="#0D1424" stroke="#26324A" stroke-width="1.5"/>'
        '<path d="M24 7.6a16.4 16.4 0 1 1-12.1 5.1" stroke="#315CFF" stroke-width="4" stroke-linecap="round"/>'
        '<circle cx="12.1" cy="12.5" r="3.1" fill="#00D68F"/>'
        '<rect x="17.1" y="25.6" width="4.4" height="9" rx="1.3" fill="#8FA8FF"/>'
        '<rect x="23" y="19.6" width="4.4" height="15" rx="1.3" fill="#4A6FFF"/>'
        '<rect x="28.9" y="24.6" width="4.4" height="10" rx="1.3" fill="#315CFF"/>'
        '</svg>'
    )


def metrica(etiqueta, valor, pie=""):
    with ui.column().classes("tarjeta metrica"):
        ui.label(etiqueta).classes("metrica-label")
        ui.label(str(valor)).classes("metrica-valor")
        if pie:
            ui.label(pie).classes("metrica-pie")


def _valor_json(valor):
    if valor is None:
        return ""
    if isinstance(valor, (datetime, date, pd.Timestamp)):
        return str(valor)[:19] if isinstance(valor, datetime) else str(valor)[:10]
    try:
        if pd.isna(valor):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(valor, "item"):
        try:
            return valor.item()
        except Exception:
            return str(valor)
    return valor


def filas_json(df: pd.DataFrame):
    if df is None or df.empty:
        return []
    return [{k: _valor_json(v) for k, v in fila.items()} for fila in df.to_dict("records")]


def tabla(df: pd.DataFrame, por_pagina=12):
    """Tabla NiceGUI a partir de un DataFrame."""
    columnas = [
        {"name": str(c), "label": str(c), "field": str(c), "align": "left", "sortable": True}
        for c in df.columns
    ]
    # row_key='id' por defecto: solo válido si el DataFrame trae esa columna
    clave = "id" if "id" in df.columns else None
    ui.table(
        columns=columnas, rows=filas_json(df), pagination=por_pagina, row_key=clave
    ).classes("w-full")


def descargar_csv(df: pd.DataFrame, nombre_archivo):
    if df is None or df.empty:
        aviso("No hay registros para exportar.")
        return
    ui.download(df.to_csv(index=False).encode("utf-8-sig"), nombre_archivo)


def boton_descarga(texto, df: pd.DataFrame, nombre_archivo):
    ui.button(texto, on_click=lambda: descargar_csv(df, nombre_archivo)).props(
        "outline no-caps"
    ).classes("btn-descarga w-full")


def actualizar(estado, clave, valor, refrescar=None):
    estado[clave] = valor
    if refrescar:
        refrescar()


def es_admin_estricto(usuario) -> bool:
    return str((usuario or {}).get("username", "")).lower().strip() == "admin"


# =========================================================
# 8. PANTALLA DE ACCESO
# =========================================================
def _vista_acceso():
    with ui.column().classes("login-wrap items-center gap-2"):
        ui.html(f"""
            <div style="text-align:center;">
                <div class="login-crest">{_logo_svg(96)}</div>
                <div class="login-name">ESAM ANALYTICS <span>360°</span></div>
                <div class="login-sub">CENTRO DE CONTROL · ESTADÍSTICAS Y OPERACIONES</div>
                <div class="login-line"></div>
            </div>
        """)

        if not ESTADO_DB.get("ok"):
            with ui.column().classes("panel-acceso gap-1 w-full"):
                ui.label("⚠️ Sin conexión a la base de datos").classes("subtitulo")
                ui.label(
                    "No se encontró DATABASE_URL ni [postgres] url en .streamlit/secrets.toml, "
                    "o Neon no respondió. Sin base de datos el inicio de sesión no puede funcionar."
                ).classes("aviso")

                def reintentar_conexion():
                    ESTADO_DB["ok"] = init_db()
                    if ESTADO_DB["ok"]:
                        ok("Conexión establecida.")
                        ui.navigate.reload()
                    else:
                        error("Sigue sin conexión: revisa la URL de la base en la consola.")

                ui.button("🔄 Reintentar conexión", on_click=reintentar_conexion).props(
                    "no-caps"
                ).classes("btn-suave w-full").style("height:38px")

        with ui.column().classes("panel-acceso gap-2"):
            with ui.tabs().classes("w-full") as tabs:
                tab_login = ui.tab("🔐 Iniciar sesión")
                tab_registro = ui.tab("＋ Crear cuenta")

            with ui.tab_panels(tabs, value=tab_login).classes("w-full"):
                with ui.tab_panel(tab_login):
                    ui.label("Ingreso de Operadores").classes("subtitulo")
                    usuario_in = ui.input("Usuario").classes("w-full")
                    clave_in = ui.input("Contraseña", password=True, password_toggle_button=True).classes("w-full")

                    def entrar():
                        if not ESTADO_DB.get("ok"):
                            error("No hay conexión con la base de datos. Revisa DATABASE_URL / secrets.toml.")
                            return
                        if not (usuario_in.value and clave_in.value):
                            aviso("Escribe usuario y contraseña.")
                            return
                        datos = autenticar_usuario(usuario_in.value, clave_in.value)
                        if datos:
                            app.storage.user.clear()
                            app.storage.user["usuario"] = datos
                            ui.navigate.reload()
                        else:
                            error("Usuario o contraseña incorrectos.")

                    usuario_in.on("keydown.enter", lambda _: entrar())
                    clave_in.on("keydown.enter", lambda _: entrar())
                    ui.button("Entrar al sistema", on_click=entrar).props("no-caps").classes(
                        "btn-primario w-full"
                    ).style("height:42px")

                with ui.tab_panel(tab_registro):
                    ui.label("Crear Cuenta de Operador").classes("subtitulo")
                    nombre_r = ui.input("Nombre completo").classes("w-full")
                    usuario_r = ui.input("Nombre de usuario").classes("w-full")
                    clave_r1 = ui.input("Contraseña", password=True, password_toggle_button=True).classes("w-full")
                    clave_r2 = ui.input(
                        "Confirmar contraseña", password=True, password_toggle_button=True
                    ).classes("w-full")

                    def registrar():
                        if not (nombre_r.value and usuario_r.value and clave_r1.value and clave_r2.value):
                            aviso("Todos los campos son obligatorios.")
                            return
                        if clave_r1.value != clave_r2.value:
                            error("Las contraseñas no coinciden.")
                            return
                        if len(clave_r1.value) < 6:
                            error("La contraseña debe tener al menos 6 caracteres.")
                            return
                        exito, msg = registrar_usuario(usuario_r.value, nombre_r.value, clave_r1.value)
                        if exito:
                            ok(msg)
                            nombre_r.value = ""
                            usuario_r.value = ""
                            clave_r1.value = ""
                            clave_r2.value = ""
                        else:
                            error(msg)

                    ui.button("Registrar cuenta", on_click=registrar).props("no-caps").classes(
                        "btn-primario w-full"
                    ).style("height:42px")

        ui.label("Servidor en horario UTC · las fechas usan la hora local del operador").classes(
            "aviso text-center"
        )


# =========================================================
# 9. ESTRUCTURA PRINCIPAL (CABECERA, MENÚ Y CONTENIDO)
# =========================================================
MENUS = [
    ("panel", "📊  Panel General y Estadísticas"),
    ("basket", "🏀  Módulo Básquetbol"),
    ("beisbol", "⚾  Módulo Béisbol"),
    ("seguridad", "🔐  Seguridad y Contraseñas"),
]
MENU_ADMIN = ("precios", "⚙️  Administración de Precios")


def _cerrar_sesion():
    app.storage.user.clear()
    ui.navigate.reload()


def _objetivo(usuario, estado):
    """Devuelve (usuario_id objetivo, etiqueta) según el modo admin."""
    if es_admin_estricto(usuario) and estado.get("modo_admin", True):
        uid = int(estado.get("analista", 0) or 0)
        if uid == 0:
            return 0, "🌐 Consolidado global (todos los usuarios)"
        usuarios = obtener_todos_los_usuarios()
        if not usuarios.empty:
            fila = usuarios[usuarios["id"] == uid]
            if not fila.empty:
                r = fila.iloc[0]
                return uid, f"👤 {r['nombre']} (@{r['username']})"
        return 0, "🌐 Consolidado global (todos los usuarios)"
    return usuario["id"], f"mis datos ({usuario['nombre']})"


def _dibujar_drawer(usuario, estado, refrescar):
    with ui.column().classes("w-full gap-1"):
        with ui.row().classes("items-center gap-2"):
            ui.html(_logo_svg(36)).classes("brand-mark")
            ui.html('<div class="app-brand" style="font-size:.9rem">ESAM ANALYTICS <em>360°</em></div>'
                    '<div class="brand-tag">360° Stats Pro</div>')
        ui.html(f'<span class="header-user" style="margin-top:.45rem">👤 {usuario["nombre"]}</span>')
        ui.element("div").classes("separador")

        for clave, texto in MENUS:
            activo = estado.get("menu") == clave
            boton = ui.button(
                texto,
                on_click=lambda c=clave: actualizar(estado, "menu", c, refrescar),
            ).props("flat no-caps align=left")
            boton.classes("menu-item activo" if activo else "menu-item")

        if es_admin_estricto(usuario):
            activo = estado.get("menu") == MENU_ADMIN[0]
            boton = ui.button(
                MENU_ADMIN[1],
                on_click=lambda: actualizar(estado, "menu", MENU_ADMIN[0], refrescar),
            ).props("flat no-caps align=left")
            boton.classes("menu-item activo" if activo else "menu-item")

            ui.element("div").classes("separador")
            ui.checkbox(
                "👑 Modo Admin Global",
                value=bool(estado.get("modo_admin", True)),
                on_change=lambda e: actualizar(estado, "modo_admin", bool(e.value), refrescar),
            ).classes("aviso")

        # Selector de analista (solo admin en modo global)
        if es_admin_estricto(usuario) and estado.get("modo_admin", True):
            usuarios = obtener_todos_los_usuarios()
            opciones = {0: "🌐 Consolidado global"}
            if not usuarios.empty:
                for _, r in usuarios.iterrows():
                    opciones[int(r["id"])] = f"👤 {r['nombre']} (@{r['username']})"
            valor = int(estado.get("analista", 0) or 0)
            if valor not in opciones:
                valor = 0
            ui.label("Selector de analista").classes("etiqueta").style("margin-top:.5rem")
            ui.select(
                opciones,
                value=valor,
                on_change=lambda e: actualizar(estado, "analista", int(e.value), refrescar),
            ).classes("w-full").props("outlined dense")

        ui.element("div").classes("separador")
        ui.label("🔍 Filtro por quincena y periodo").classes("etiqueta")
        ui.select(
            RANGOS_PERIODO,
            value=estado.get("rango", RANGOS_PERIODO[0]),
            on_change=lambda e: actualizar(estado, "rango", e.value, refrescar),
        ).classes("w-full").props("outlined dense")

        if estado.get("rango") == "Rango de Fechas Personalizado":
            ui.date(
                value=estado.get("desde") or (hoy_local() - timedelta(days=30)).isoformat(),
                on_change=lambda e: actualizar(estado, "desde", e.value, refrescar),
            ).props("dense").classes("w-full")
            ui.date(
                value=estado.get("hasta") or hoy_local().isoformat(),
                on_change=lambda e: actualizar(estado, "hasta", e.value, refrescar),
            ).props("dense").classes("w-full")

        ui.element("div").classes("separador")
        ui.button("🚪 Cerrar sesión", on_click=_cerrar_sesion).props("flat no-caps").classes(
            "menu-item"
        )


def _dibujar_contenido(usuario, estado, refrescar):
    f_ini, f_fin = resolver_periodo(estado)
    target_id, etiqueta = _objetivo(usuario, estado)
    menu = estado.get("menu", "panel")

    if menu == "panel":
        _vista_panel_general(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta)
    elif menu == "basket":
        _vista_basket(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta)
    elif menu == "beisbol":
        _vista_beisbol(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta)
    elif menu == "precios" and es_admin_estricto(usuario):
        _vista_precios(usuario, estado, refrescar)
    else:
        _vista_seguridad(usuario, estado, refrescar)


def _vista_app(usuario):
    estado = app.storage.user
    estado.setdefault("menu", "panel")
    estado.setdefault("rango", RANGOS_PERIODO[0])
    estado.setdefault("desde", (hoy_local() - timedelta(days=30)).isoformat())
    estado.setdefault("hasta", hoy_local().isoformat())
    estado.setdefault("analista", 0)
    estado.setdefault("modo_admin", True)

    drawer = ui.left_drawer(value=True).classes("app-drawer").style("width:300px")

    with ui.header().classes("app-header"):
        with ui.element("div").classes("w-full flex items-center justify-between gap-2"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.button(icon="menu", on_click=drawer.toggle).props("flat round dense").classes("drawer-toggle")
                ui.html(_logo_svg(38)).classes("brand-mark")
                ui.html('<div class="app-brand">ESAM ANALYTICS <em>360°</em></div>'
                        '<div class="brand-tag">Centro de control</div>')
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.html('<span class="live-pill"><span class="live-dot"></span>EN&nbsp;VIVO</span>')
                ui.html(f'<span class="header-user" title="{usuario["nombre"]}">👤 {usuario["nombre"]}</span>')
                ui.button("Salir", icon="logout", on_click=_cerrar_sesion).props(
                    "flat no-caps dense"
                ).classes("btn-suave")
    contenido = ui.column().classes("w-full gap-3")

    def refrescar():
        drawer.clear()
        contenido.clear()
        with drawer:
            _dibujar_drawer(usuario, estado, refrescar)
        with contenido:
            _dibujar_contenido(usuario, estado, refrescar)

    refrescar()


# =========================================================
# 10. PANEL GENERAL
# =========================================================
def _tasa_beisbol(tasas):
    return float(tasas["precio_beisbol"])


def _grafico_pastel(total_basket, total_beisbol):
    ui.echart({
        "tooltip": {
            "trigger": "item",
            "formatter": "{b}: ${c} ({d}%)",
            "backgroundColor": "#10151E",
            "borderColor": "#2A3441",
            "textStyle": {"color": "#FFFFFF"},
        },
        "legend": {"bottom": 0, "textStyle": {"color": "#94A3B8"}},
        "series": [{
            "name": "Ingresos",
            "type": "pie",
            "radius": ["44%", "70%"],
            "itemStyle": {"borderColor": "#10151E", "borderWidth": 2},
            "label": {"color": "#CBD5E1", "formatter": "{b}\n${c}"},
            "data": [
                {"value": round(float(total_basket), 2), "name": "Básquetbol", "itemStyle": {"color": "#315CFF"}},
                {"value": round(float(total_beisbol), 2), "name": "Béisbol", "itemStyle": {"color": "#94A3B8"}},
            ],
        }],
    }).classes("w-full").style("height: 330px")


def _grafico_evolucion(df_serie):
    fechas = [pd.to_datetime(i).strftime("%d/%m") for i in df_serie.index]
    series = []
    for columna, color in (("🏀 Básquetbol ($)", "#315CFF"), ("⚾ Béisbol ($)", "#94A3B8")):
        if columna in df_serie.columns:
            series.append({
                "name": columna,
                "type": "bar",
                "stack": "total",
                "itemStyle": {"color": color},
                "data": [round(float(v), 2) for v in df_serie[columna]],
            })
    ui.echart({
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": "#10151E",
            "borderColor": "#2A3441",
            "textStyle": {"color": "#FFFFFF"},
        },
        "legend": {"top": 0, "textStyle": {"color": "#94A3B8"}},
        "grid": {"left": 54, "right": 18, "top": 44, "bottom": 34},
        "xAxis": {
            "type": "category",
            "data": fechas,
            "axisLabel": {"color": "#94A3B8"},
            "axisLine": {"lineStyle": {"color": "#2A3441"}},
        },
        "yAxis": {
            "type": "value",
            "axisLabel": {"color": "#94A3B8", "formatter": "${value}"},
            "splitLine": {"lineStyle": {"color": "#1E293B"}},
        },
        "series": series,
    }).classes("w-full").style("height: 330px")


def _vista_panel_general(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta):
    ui.label("📊 Panel General y Análisis Estadístico 360°").classes("titulo-pagina")
    ui.label(
        f"{etiqueta} · Periodo: {f_ini.strftime('%d/%m/%Y')} al {f_fin.strftime('%d/%m/%Y')}"
    ).classes("aviso")

    recalcular_tarifas_quincena_basket(target_id, f_ini)

    tasas = obtener_tasas_globales()
    precio_beisbol = _tasa_beisbol(tasas)

    df_b_all = pd.DataFrame()
    df_beis_all = pd.DataFrame()
    total_g_basket = 0.0
    total_g_beisbol = 0.0
    total_juegos_b = 0
    total_lotes_beisbol = 0
    total_pich_b = 0
    tiempo_promedio_seg = None
    juegos_con_tiempo = 0

    conn = get_db_connection()
    if conn:
        try:
            if target_id == 0:
                df_b_all = pd.read_sql_query(
                    "SELECT tarifa, tiempo_ejecucion, fecha FROM partidos WHERE fecha BETWEEN %s AND %s;",
                    conn, params=(f_ini, f_fin),
                )
                df_beis_all = pd.read_sql_query(
                    "SELECT cantidad, fecha FROM picheos WHERE fecha BETWEEN %s AND %s;",
                    conn, params=(f_ini, f_fin),
                )
            else:
                df_b_all = pd.read_sql_query(
                    "SELECT tarifa, tiempo_ejecucion, fecha FROM partidos WHERE usuario_id = %s AND fecha BETWEEN %s AND %s;",
                    conn, params=(target_id, f_ini, f_fin),
                )
                df_beis_all = pd.read_sql_query(
                    "SELECT cantidad, fecha FROM picheos WHERE usuario_id = %s AND fecha BETWEEN %s AND %s;",
                    conn, params=(target_id, f_ini, f_fin),
                )
        except Exception as e:
            error(f"Error al calcular totales: {e}")
        finally:
            cerrar_conexion(conn)

    if not df_b_all.empty:
        total_g_basket = float(df_b_all["tarifa"].sum())
        total_juegos_b = len(df_b_all)
        tiempos = [
            t for t in (parsear_tiempo_a_segundos(v) for v in df_b_all["tiempo_ejecucion"])
            if t is not None
        ]
        if tiempos:
            tiempo_promedio_seg = sum(tiempos) / len(tiempos)
            juegos_con_tiempo = len(tiempos)

    if not df_beis_all.empty:
        total_pich_b = int(df_beis_all["cantidad"].sum())
        total_lotes_beisbol = len(df_beis_all)
        total_g_beisbol = total_pich_b * precio_beisbol

    total_general = total_g_basket + total_g_beisbol
    total_registros = total_juegos_b + total_lotes_beisbol
    promedio_basket = (total_g_basket / total_juegos_b) if total_juegos_b else 0.0

    with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3 w-full"):
        metrica("💰 Total acumulado", f"${total_general:,.2f}", "📈 Rendimiento del periodo")
        metrica("🏀 Básquetbol", f"${total_g_basket:,.2f}", f"🔺 {total_juegos_b} partidos")
        metrica("⚾ Béisbol", f"${total_g_beisbol:,.2f}", f"🔺 {total_pich_b:,} picheos")
        metrica("📊 Cargas totales", f"{total_registros}", "🟢 Sistema óptimo")
        metrica(
            "⏱️ Tiempo promedio",
            formatear_segundos(tiempo_promedio_seg),
            f"⏱️ {juegos_con_tiempo} partidos con tiempo",
        )

    with ui.element("div").classes("grid grid-cols-1 lg:grid-cols-2 gap-3 w-full"):
        with tarjeta("tarjeta-tabla"):
            ui.label("📊 Distribución de ingresos por deporte").classes("subtitulo")
            if total_general > 0:
                _grafico_pastel(total_g_basket, total_g_beisbol)
            else:
                ui.label(
                    "Registra datos en Básquetbol o Béisbol para el periodo seleccionado para generar gráficos."
                ).classes("aviso")

        with tarjeta("tarjeta-tabla"):
            ui.label("📈 Análisis y desglose operativo").classes("subtitulo")
            df_desglose = pd.DataFrame({
                "Disciplina": ["🏀 Básquetbol", "⚾ Béisbol"],
                "Unidades registradas": [f"{total_juegos_b} partidos", f"{total_pich_b:,} picheos"],
                "Promedio por carga": [
                    f"${promedio_basket:.2f} / partido",
                    f"${precio_beisbol:.3f} / picheo",
                ],
                "Total USD": [f"${total_g_basket:,.2f}", f"${total_g_beisbol:,.2f}"],
            })
            tabla(df_desglose, por_pagina=5)

    # Evolución diaria
    with tarjeta("tarjeta-tabla"):
        ui.label("📈 Evolución diaria del periodo").classes("subtitulo")
        series_diarias = []
        if not df_b_all.empty:
            tmp_b = df_b_all.copy()
            tmp_b["fecha"] = pd.to_datetime(tmp_b["fecha"])
            series_diarias.append(
                tmp_b.groupby("fecha").agg({"tarifa": "sum"}).rename(columns={"tarifa": "🏀 Básquetbol ($)"})
            )
        if not df_beis_all.empty:
            tmp_p = df_beis_all.copy()
            tmp_p["fecha"] = pd.to_datetime(tmp_p["fecha"])
            tmp_p["usd"] = tmp_p["cantidad"].astype(float) * precio_beisbol
            series_diarias.append(
                tmp_p.groupby("fecha").agg({"usd": "sum"}).rename(columns={"usd": "⚾ Béisbol ($)"})
            )
        if series_diarias:
            df_serie = pd.concat(series_diarias, axis=1).fillna(0.0).sort_index()
            df_serie["Total ($)"] = df_serie.sum(axis=1)
            _grafico_evolucion(df_serie)
            ui.label(
                f"Mejor día: ${df_serie['Total ($)'].max():,.2f} · "
                f"Días con actividad: {len(df_serie)} · "
                f"Promedio diario: ${df_serie['Total ($)'].mean():,.2f}"
            ).classes("aviso")
        else:
            ui.label("Sin movimientos en el periodo seleccionado para graficar la evolución.").classes("aviso")

    # Descarga de cargas por rango
    with ui.expansion("📦 Descargar cargas completas (cualquier periodo)", icon="archive").classes("w-full"):
        ui.label(
            "Descarga los registros crudos de los dos deportes entre dos fechas, aunque no coincidan "
            "con el periodo elegido arriba. Sirve para guardar un respaldo o revisar un periodo viejo."
        ).classes("aviso")
        estado.setdefault("dl_desde", f_ini.isoformat())
        estado.setdefault("dl_hasta", f_fin.isoformat())

        with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 gap-3 w-full"):
            ui.date(
                value=estado.get("dl_desde") or f_ini.isoformat(),
                on_change=lambda e: actualizar(estado, "dl_desde", e.value, refrescar),
            ).props("dense").classes("w-full")
            ui.date(
                value=estado.get("dl_hasta") or f_fin.isoformat(),
                on_change=lambda e: actualizar(estado, "dl_hasta", e.value, refrescar),
            ).props("dense").classes("w-full")

        dl_desde = fecha_desde_iso(estado.get("dl_desde")) or f_ini
        dl_hasta = fecha_desde_iso(estado.get("dl_hasta")) or f_fin
        if dl_desde > dl_hasta:
            aviso("'Desde' es posterior a 'Hasta'. Se invirtieron las fechas.")
            dl_desde, dl_hasta = dl_hasta, dl_desde

        df_dl_b = pd.DataFrame()
        df_dl_p = pd.DataFrame()
        conn = get_db_connection()
        if conn:
            try:
                if target_id == 0:
                    df_dl_b = pd.read_sql_query("""
                        SELECT p.id AS "ID", u.username AS "Usuario", u.nombre AS "Operador",
                               p.codigo_partido AS "Código Partido", p.fecha AS "Fecha",
                               p.tiempo_ejecucion AS "Tiempo Ejecución",
                               p.tarifa AS "Tarifa USD", p.tipo_partido AS "Modalidad",
                               p.creado_en AS "Registrado"
                        FROM partidos p
                        JOIN usuarios u ON u.id = p.usuario_id
                        WHERE p.fecha BETWEEN %s AND %s
                        ORDER BY p.fecha ASC, p.id ASC;
                    """, conn, params=(dl_desde, dl_hasta))
                    df_dl_p = pd.read_sql_query("""
                        SELECT pi.id AS "ID", u.username AS "Usuario", u.nombre AS "Operador",
                               pi.fecha AS "Fecha", pi.id_control AS "ID Control",
                               pi.cantidad AS "Cantidad", pi.creado_en AS "Registrado"
                        FROM picheos pi
                        JOIN usuarios u ON u.id = pi.usuario_id
                        WHERE pi.fecha BETWEEN %s AND %s
                        ORDER BY pi.fecha ASC, pi.id ASC;
                    """, conn, params=(dl_desde, dl_hasta))
                else:
                    df_dl_b = pd.read_sql_query("""
                        SELECT p.id AS "ID", u.username AS "Usuario", u.nombre AS "Operador",
                               p.codigo_partido AS "Código Partido", p.fecha AS "Fecha",
                               p.tiempo_ejecucion AS "Tiempo Ejecución",
                               p.tarifa AS "Tarifa USD", p.tipo_partido AS "Modalidad",
                               p.creado_en AS "Registrado"
                        FROM partidos p
                        JOIN usuarios u ON u.id = p.usuario_id
                        WHERE p.usuario_id = %s AND p.fecha BETWEEN %s AND %s
                        ORDER BY p.fecha ASC, p.id ASC;
                    """, conn, params=(target_id, dl_desde, dl_hasta))
                    df_dl_p = pd.read_sql_query("""
                        SELECT pi.id AS "ID", u.username AS "Usuario", u.nombre AS "Operador",
                               pi.fecha AS "Fecha", pi.id_control AS "ID Control",
                               pi.cantidad AS "Cantidad", pi.creado_en AS "Registrado"
                        FROM picheos pi
                        JOIN usuarios u ON u.id = pi.usuario_id
                        WHERE pi.usuario_id = %s AND pi.fecha BETWEEN %s AND %s
                        ORDER BY pi.fecha ASC, pi.id ASC;
                    """, conn, params=(target_id, dl_desde, dl_hasta))
            except Exception as e:
                error(f"No se pudieron leer las cargas: {e}")
            finally:
                cerrar_conexion(conn)

        if not df_dl_p.empty:
            df_dl_p["USD (precio actual)"] = (
                df_dl_p["Cantidad"].astype(float) * precio_beisbol
            ).round(2)

        ui.label(
            f"Básquetbol: {len(df_dl_b)} partidos · Béisbol: {len(df_dl_p)} lotes de picheos"
        ).classes("aviso")

        with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 gap-3 w-full"):
            boton_descarga(
                "📥 Partidos de básquetbol (CSV)",
                df_dl_b,
                f"esam_partidos_{dl_desde}_al_{dl_hasta}.csv",
            )
            boton_descarga(
                "📥 Picheos de béisbol (CSV)",
                df_dl_p,
                f"esam_picheos_{dl_desde}_al_{dl_hasta}.csv",
            )

    # Auditoría consolidada (solo admin)
    if es_admin_estricto(usuario) and estado.get("modo_admin", True) and target_id == 0:
        with tarjeta("tarjeta-tabla"):
            ui.label("👥 Auditoría general de operadores (filtrada por periodo)").classes("subtitulo")
            conn = get_db_connection()
            if not conn:
                return
            try:
                df_det = pd.read_sql_query("""
                    WITH basket_por_usuario AS (
                        SELECT usuario_id,
                               COUNT(*) AS partidos_basket,
                               COALESCE(SUM(tarifa), 0.00) AS usd_basket
                        FROM partidos
                        WHERE fecha BETWEEN %s AND %s
                        GROUP BY usuario_id
                    ),
                    beisbol_por_usuario AS (
                        SELECT usuario_id,
                               COALESCE(SUM(cantidad), 0) AS total_picheos
                        FROM picheos
                        WHERE fecha BETWEEN %s AND %s
                        GROUP BY usuario_id
                    )
                    SELECT u.id AS "ID",
                           u.username AS "Usuario",
                           u.nombre AS "Nombre Completo",
                           COALESCE(b.partidos_basket, 0) AS "Partidos Basket",
                           COALESCE(b.usd_basket, 0.00) AS "USD Basket",
                           COALESCE(pi.total_picheos, 0) AS "Total Picheos"
                    FROM usuarios u
                    LEFT JOIN basket_por_usuario b ON b.usuario_id = u.id
                    LEFT JOIN beisbol_por_usuario pi ON pi.usuario_id = u.id
                    ORDER BY u.id ASC;
                """, conn, params=(f_ini, f_fin, f_ini, f_fin))
                df_tiempos = pd.read_sql_query("""
                    SELECT u.id AS uid, p.tiempo_ejecucion
                    FROM usuarios u
                    JOIN partidos p ON u.id = p.usuario_id AND p.fecha BETWEEN %s AND %s;
                """, conn, params=(f_ini, f_fin))

                if not df_det.empty:
                    df_det["USD Béisbol"] = (df_det["Total Picheos"].astype(float) * precio_beisbol).round(2)
                    df_det["Ganancia Total ($)"] = (df_det["USD Basket"] + df_det["USD Béisbol"]).round(2)

                    promedios = {}
                    if not df_tiempos.empty:
                        for uid, grupo in df_tiempos.groupby("uid"):
                            segs = [
                                t for t in (parsear_tiempo_a_segundos(v) for v in grupo["tiempo_ejecucion"])
                                if t is not None
                            ]
                            promedios[uid] = formatear_segundos(sum(segs) / len(segs)) if segs else "—"
                    df_det["Tiempo Promedio"] = df_det["ID"].map(promedios).fillna("—")

                    tabla(df_det)
                    boton_descarga(
                        "📥 Exportar auditoría consolidada (CSV)",
                        df_det,
                        f"esam_auditoria_{f_ini}_al_{f_fin}.csv",
                    )
            except Exception as e:
                error(f"Error cargando detalle de usuarios: {e}")
            finally:
                cerrar_conexion(conn)


# =========================================================
# 11. MÓDULO BÁSQUETBOL
# =========================================================
def _vista_basket(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta):
    ui.label("🏀 Módulo Básquetbol").classes("titulo-pagina")
    ui.label(f"{etiqueta} · Control financiero y tiempos de partidos por quincena").classes("aviso")

    recalcular_tarifas_quincena_basket(target_id, f_ini)

    ui.label(
        f"📅 Periodo evaluado: {f_ini.strftime('%d/%m/%Y')} al {f_fin.strftime('%d/%m/%Y')}"
    ).classes("nota")

    # ---------------- Registro ----------------
    with ui.expansion("➕ Registrar nuevo partido de básquetbol", icon="add").classes("w-full"):
        modalidad = ui.radio(
            ["🏀 Quincenal Estándar", "⚡ En Vivo Normal", "🔥 En Vivo NBA"],
            value=estado.get("b_modalidad", "🏀 Quincenal Estándar"),
        ).props("inline")

        with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-3 gap-3 w-full"):
            f_p = ui.date(value=hoy_local().isoformat()).props("dense").classes("w-full")
            c_p = ui.input("Código de partido", placeholder="Ej: 1042").props("outlined dense").classes("w-full")
            t_p = ui.input("Tiempo de ejecución", placeholder="HH:MM:SS").props("outlined dense").classes("w-full")

        ui.label(
            "El tiempo es opcional. Si lo escribes, usa HH:MM:SS (ejemplo: 01:42:15)."
        ).classes("aviso")

        def guardar_partido():
            tasas = obtener_tasas_globales()
            seleccion = modalidad.value or ""
            if "NBA" in seleccion:
                tipo, tarifa = "en_vivo_nba", tasas["en_vivo_nba"]
            elif "Normal" in seleccion:
                tipo, tarifa = "en_vivo_normal", tasas["en_vivo_normal"]
            else:
                tipo, tarifa = "quincenal", tasas["tramo_1"]

            codigo = (c_p.value or "").strip()
            tiempo_val = (t_p.value or "").strip()
            segundos = parsear_tiempo_a_segundos(tiempo_val) if tiempo_val else None
            fecha_p = fecha_desde_iso(f_p.value)

            if not codigo:
                aviso("Ingrese el código del partido.")
                return
            if not fecha_p:
                aviso("Seleccione una fecha válida.")
                return
            if tiempo_val and segundos is None:
                error("Tiempo inválido. Usa el formato HH:MM:SS (ejemplo: 01:42:15).")
                return

            tiempo_guardar = formatear_segundos(segundos) if segundos is not None else ""
            conn = get_db_connection()
            if not conn:
                error("Sin conexión a la base de datos.")
                return
            cur = None
            try:
                cur = conn.cursor()
                if codigo_partido_ya_existe(cur, codigo):
                    conn.rollback()
                    aviso("Este código de partido ya está registrado por un usuario.")
                    return
                cur.execute("""
                    INSERT INTO partidos (usuario_id, codigo_partido, fecha, tiempo_ejecucion, tarifa, tipo_partido)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """, (usuario["id"], codigo, fecha_p, tiempo_guardar, tarifa, tipo))
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                aviso("Este código de partido ya está registrado por un usuario.")
                return
            except Exception as e:
                conn.rollback()
                error(f"No se pudo guardar el partido: {e}")
                return
            finally:
                try:
                    if cur is not None:
                        cur.close()
                except Exception:
                    pass
                cerrar_conexion(conn)

            if tipo == "quincenal":
                recalcular_tarifas_quincena_basket(usuario["id"], fecha_p)
            estado["b_modalidad"] = seleccion
            ok("✅ Partido guardado exitosamente.")
            refrescar()

        ui.button("💾 Guardar partido", on_click=guardar_partido).props("no-caps").classes(
            "btn-primario w-full"
        ).style("height:40px")

    # ---------------- Consulta ----------------
    df_basket = pd.DataFrame()
    conn = get_db_connection()
    if conn:
        try:
            if target_id == 0:
                df_basket = pd.read_sql_query("""
                    SELECT p.id, p.usuario_id, u.nombre AS operador, p.codigo_partido, p.fecha,
                           p.tiempo_ejecucion, p.tarifa, p.tipo_partido
                    FROM partidos p
                    JOIN usuarios u ON p.usuario_id = u.id
                    WHERE p.fecha BETWEEN %s AND %s
                    ORDER BY p.fecha DESC, p.id DESC;
                """, conn, params=(f_ini, f_fin))
            else:
                df_basket = pd.read_sql_query("""
                    SELECT id, usuario_id, codigo_partido, fecha, tiempo_ejecucion, tarifa, tipo_partido
                    FROM partidos
                    WHERE usuario_id = %s AND fecha BETWEEN %s AND %s
                    ORDER BY fecha DESC, id DESC;
                """, conn, params=(target_id, f_ini, f_fin))
        except Exception as e:
            error(f"Error al consultar partidos: {e}")
        finally:
            cerrar_conexion(conn)

    ui.label(
        f"📋 Registros de básquetbol ({f_ini.strftime('%d/%m/%Y')} al {f_fin.strftime('%d/%m/%Y')})"
    ).classes("subtitulo")

    if df_basket.empty:
        ui.label("No hay registros de básquetbol para la quincena o periodo seleccionado.").classes("aviso")
        return

    total_ganancias = float(df_basket["tarifa"].astype(float).sum())
    with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-3 gap-3 w-full"):
        metrica("🏀 Partidos del periodo", len(df_basket), "📈 Actividad registrada")
        metrica("💵 Total ganancias", f"${total_ganancias:.2f}", "🔺 Monto acumulado")
        metrica(
            "📅 Rango evaluado",
            f"{f_ini.strftime('%d/%m')} - {f_fin.strftime('%d/%m/%Y')}",
            "🗓️ Quincena seleccionada",
        )

    with tarjeta("tarjeta-tabla"):
        columnas_visibles = [c for c in df_basket.columns if c != "usuario_id"]
        tabla(df_basket[columnas_visibles])
        boton_descarga(
            "📥 Exportar reporte quincenal de básquetbol (CSV)",
            df_basket[columnas_visibles],
            f"esam_basket_quincena_{f_ini}_al_{f_fin}.csv",
        )

    with ui.expansion("🛠️ Modificar, corregir o eliminar cargas", icon="build").classes("w-full"):
        with ui.element("div").classes("grid grid-cols-1 lg:grid-cols-2 gap-4 w-full"):
            # ---- Editar ----
            with ui.column().classes("gap-2 w-full"):
                ui.label("✏️ Modificar / corregir partido").classes("subtitulo")
                ids = [int(v) for v in df_basket["id"].tolist()]
                sel = ui.select(ids, label="ID del partido", value=ids[0]).props("outlined dense").classes("w-full")
                fila0 = df_basket[df_basket["id"] == sel.value].iloc[0]

                f_e = ui.date(value=iso_fecha(fila0["fecha"])).props("dense").classes("w-full")
                c_e = ui.input("Código de partido", value=str(fila0["codigo_partido"])).props(
                    "outlined dense"
                ).classes("w-full")
                t_e = ui.input("Tiempo de ejecución", value=str(fila0["tiempo_ejecucion"] or "")).props(
                    "outlined dense"
                ).classes("w-full")

                if es_admin_estricto(usuario):
                    tar_e = ui.number(
                        "Tarifa ($)", value=float(fila0["tarifa"]), step=0.5, format="%.2f"
                    ).props("outlined dense").classes("w-full")
                else:
                    tar_e = ui.number(
                        "Tarifa ($) — solo administrador",
                        value=float(fila0["tarifa"]),
                        format="%.2f",
                    ).props("outlined dense readonly").classes("w-full")

                def al_cambiar_id(e):
                    fila = df_basket[df_basket["id"] == e.value]
                    if fila.empty:
                        return
                    fila = fila.iloc[0]
                    f_e.value = iso_fecha(fila["fecha"])
                    c_e.value = str(fila["codigo_partido"])
                    t_e.value = str(fila["tiempo_ejecucion"] or "")
                    tar_e.value = float(fila["tarifa"])

                sel.on_value_change(al_cambiar_id)

                def guardar_edicion():
                    fila = df_basket[df_basket["id"] == sel.value]
                    if fila.empty:
                        aviso("Seleccione un partido válido.")
                        return
                    fila = fila.iloc[0]
                    propietario = int(fila["usuario_id"])
                    fecha_edit = fecha_desde_iso(f_e.value)
                    codigo_edit = (c_e.value or "").strip()
                    tiempo_edit_val = (t_e.value or "").strip()
                    segundos_edit = parsear_tiempo_a_segundos(tiempo_edit_val) if tiempo_edit_val else None

                    if not codigo_edit:
                        aviso("Ingrese el código del partido.")
                        return
                    if not fecha_edit:
                        aviso("Seleccione una fecha válida.")
                        return
                    if tiempo_edit_val and segundos_edit is None:
                        error("Tiempo inválido. Usa el formato HH:MM:SS (ejemplo: 01:42:15).")
                        return

                    tiempo_edit = formatear_segundos(segundos_edit) if segundos_edit is not None else ""
                    conn = get_db_connection()
                    if not conn:
                        error("Sin conexión a la base de datos.")
                        return
                    cur = None
                    try:
                        cur = conn.cursor()
                        if codigo_partido_ya_existe(cur, codigo_edit, sel.value):
                            conn.rollback()
                            aviso("Este código de partido ya está registrado por un usuario.")
                            return
                        if es_admin_estricto(usuario):
                            cur.execute("""
                                UPDATE partidos
                                SET fecha = %s, codigo_partido = %s, tiempo_ejecucion = %s, tarifa = %s
                                WHERE id = %s AND usuario_id = %s;
                            """, (fecha_edit, codigo_edit, tiempo_edit,
                                  float(tar_e.value or 0), sel.value, propietario))
                        else:
                            cur.execute("""
                                UPDATE partidos
                                SET fecha = %s, codigo_partido = %s, tiempo_ejecucion = %s
                                WHERE id = %s AND usuario_id = %s;
                            """, (fecha_edit, codigo_edit, tiempo_edit,
                                  sel.value, propietario))
                        if cur.rowcount != 1:
                            raise PermissionError("No tienes permiso para modificar este registro.")
                        conn.commit()
                    except psycopg2.errors.UniqueViolation:
                        conn.rollback()
                        aviso("Este código de partido ya está registrado por un usuario.")
                        return
                    except Exception as e:
                        conn.rollback()
                        error(f"No se pudo modificar el partido: {e}")
                        return
                    finally:
                        try:
                            if cur is not None:
                                cur.close()
                        except Exception:
                            pass
                        cerrar_conexion(conn)

                    recalcular_tarifas_quincena_basket(propietario, fecha_edit)
                    ok("✅ Partido modificado y quincena recalculada.")
                    refrescar()

                ui.button("💾 Guardar correcciones", on_click=guardar_edicion).props("no-caps").classes(
                    "btn-primario w-full"
                ).style("height:40px")

            # ---- Eliminar ----
            with ui.column().classes("gap-2 w-full"):
                ui.label("🗑️ Eliminar partido").classes("subtitulo")
                ids_del = [int(v) for v in df_basket["id"].tolist()]
                sel_del = ui.select(ids_del, label="ID del partido a eliminar", value=ids_del[0]).props(
                    "outlined dense"
                ).classes("w-full")
                confirmo = ui.checkbox("Confirmo que deseo eliminar este partido")

                def eliminar_partido():
                    if not confirmo.value:
                        aviso("Marca la confirmación para eliminar.")
                        return
                    fila = df_basket[df_basket["id"] == sel_del.value]
                    if fila.empty:
                        aviso("Seleccione un partido válido.")
                        return
                    propietario = int(fila.iloc[0]["usuario_id"])
                    fecha_borrada = fecha_desde_iso(iso_fecha(fila.iloc[0]["fecha"])) or hoy_local()

                    conn = get_db_connection()
                    if not conn:
                        error("Sin conexión a la base de datos.")
                        return
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "DELETE FROM partidos WHERE id = %s AND usuario_id = %s;",
                            (sel_del.value, propietario),
                        )
                        if cur.rowcount != 1:
                            raise PermissionError("No tienes permiso para eliminar este registro.")
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        error(f"No se pudo eliminar el partido: {e}")
                        return
                    finally:
                        try:
                            if cur is not None:
                                cur.close()
                        except Exception:
                            pass
                        cerrar_conexion(conn)

                    recalcular_tarifas_quincena_basket(propietario, fecha_borrada)
                    ok("🗑️ Partido eliminado correctamente.")
                    refrescar()

                ui.button("🚨 Confirmar y eliminar", on_click=eliminar_partido).props("no-caps").classes(
                    "btn-peligro w-full"
                ).style("height:40px")
                ui.label("La eliminación recalcula la tarifa de toda la quincena.").classes("aviso")


# =========================================================
# 12. MÓDULO BÉISBOL
# =========================================================
def _vista_beisbol(usuario, estado, refrescar, f_ini, f_fin, target_id, etiqueta):
    ui.label("⚾ Módulo Béisbol").classes("titulo-pagina")
    ui.label(f"{etiqueta} · Control de picheos y registros por quincena").classes("aviso")
    ui.label(
        f"📅 Periodo evaluado: {f_ini.strftime('%d/%m/%Y')} al {f_fin.strftime('%d/%m/%Y')}"
    ).classes("nota")

    with ui.expansion("➕ Registrar lote de picheos", icon="add").classes("w-full"):
        with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-3 gap-3 w-full"):
            f_pich = ui.date(value=hoy_local().isoformat()).props("dense").classes("w-full")
            id_ctrl = ui.input("ID control / partido", placeholder="Ej: BC-2026").props(
                "outlined dense"
            ).classes("w-full")
            cant_pich = ui.number("Cantidad de picheos", value=100, min=1, step=1).props(
                "outlined dense"
            ).classes("w-full")

        def guardar_picheos():
            control = (id_ctrl.value or "").strip()
            fecha_pich = fecha_desde_iso(f_pich.value)
            cantidad = int(cant_pich.value or 0)

            if not control:
                aviso("Ingrese el ID de control.")
                return
            if not fecha_pich:
                aviso("Seleccione una fecha válida.")
                return
            if cantidad < 1:
                aviso("La cantidad debe ser mayor que cero.")
                return

            conn = get_db_connection()
            if not conn:
                error("Sin conexión a la base de datos.")
                return
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO picheos (usuario_id, fecha, id_control, cantidad)
                    VALUES (%s, %s, %s, %s);
                """, (usuario["id"], fecha_pich, control, cantidad))
                conn.commit()
            except Exception as e:
                conn.rollback()
                error(f"No se pudo guardar el lote: {e}")
                return
            finally:
                try:
                    if cur is not None:
                        cur.close()
                except Exception:
                    pass
                cerrar_conexion(conn)

            ok("✅ Picheos guardados correctamente.")
            refrescar()

        ui.button("💾 Guardar picheos", on_click=guardar_picheos).props("no-caps").classes(
            "btn-primario w-full"
        ).style("height:40px")

    df_picheos = pd.DataFrame()
    conn = get_db_connection()
    if conn:
        try:
            if target_id == 0:
                df_picheos = pd.read_sql_query("""
                    SELECT pi.id, pi.usuario_id, u.nombre AS operador, pi.fecha, pi.id_control, pi.cantidad
                    FROM picheos pi
                    JOIN usuarios u ON pi.usuario_id = u.id
                    WHERE pi.fecha BETWEEN %s AND %s
                    ORDER BY pi.fecha DESC, pi.id DESC;
                """, conn, params=(f_ini, f_fin))
            else:
                df_picheos = pd.read_sql_query("""
                    SELECT id, usuario_id, fecha, id_control, cantidad
                    FROM picheos
                    WHERE usuario_id = %s AND fecha BETWEEN %s AND %s
                    ORDER BY fecha DESC, id DESC;
                """, conn, params=(target_id, f_ini, f_fin))
        except Exception as e:
            error(f"Error al consultar picheos: {e}")
        finally:
            cerrar_conexion(conn)

    ui.label(
        f"📋 Listado de picheos ({f_ini.strftime('%d/%m/%Y')} al {f_fin.strftime('%d/%m/%Y')})"
    ).classes("subtitulo")

    if df_picheos.empty:
        ui.label("No hay registros de picheos para la quincena o periodo seleccionado.").classes("aviso")
        return

    precio_u = float(obtener_tasas_globales()["precio_beisbol"])
    total_p = int(df_picheos["cantidad"].sum())
    total_dolares = total_p * precio_u

    with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-3 gap-3 w-full"):
        metrica("⚾ Total picheos", f"{total_p:,}", "📈 Volumen registrado")
        metrica("💵 USD estimado", f"${total_dolares:,.2f}", f"@ ${precio_u} por picheo")
        metrica("📅 Lotes registrados", len(df_picheos), "🟢 Estable")

    with tarjeta("tarjeta-tabla"):
        columnas_visibles = [c for c in df_picheos.columns if c != "usuario_id"]
        tabla(df_picheos[columnas_visibles])
        boton_descarga(
            "📥 Exportar reporte quincenal de béisbol (CSV)",
            df_picheos[columnas_visibles],
            f"esam_beisbol_quincena_{f_ini}_al_{f_fin}.csv",
        )

    with ui.expansion("🛠️ Modificar, corregir o eliminar lotes", icon="build").classes("w-full"):
        with ui.element("div").classes("grid grid-cols-1 lg:grid-cols-2 gap-4 w-full"):
            with ui.column().classes("gap-2 w-full"):
                ui.label("✏️ Modificar / corregir lote").classes("subtitulo")
                ids = [int(v) for v in df_picheos["id"].tolist()]
                sel = ui.select(ids, label="ID del lote", value=ids[0]).props("outlined dense").classes("w-full")
                fila0 = df_picheos[df_picheos["id"] == sel.value].iloc[0]

                f_e = ui.date(value=iso_fecha(fila0["fecha"])).props("dense").classes("w-full")
                c_e = ui.input("ID control / partido", value=str(fila0["id_control"])).props(
                    "outlined dense"
                ).classes("w-full")
                cant_e = ui.number("Cantidad de picheos", value=int(fila0["cantidad"]), min=1, step=1).props(
                    "outlined dense"
                ).classes("w-full")

                def al_cambiar_id(e):
                    fila = df_picheos[df_picheos["id"] == e.value]
                    if fila.empty:
                        return
                    fila = fila.iloc[0]
                    f_e.value = iso_fecha(fila["fecha"])
                    c_e.value = str(fila["id_control"])
                    cant_e.value = int(fila["cantidad"])

                sel.on_value_change(al_cambiar_id)

                def guardar_edicion():
                    fila = df_picheos[df_picheos["id"] == sel.value]
                    if fila.empty:
                        aviso("Seleccione un lote válido.")
                        return
                    propietario = int(fila.iloc[0]["usuario_id"])
                    fecha_edit = fecha_desde_iso(f_e.value)
                    control = (c_e.value or "").strip()
                    cantidad = int(cant_e.value or 0)

                    if not fecha_edit or not control or cantidad < 1:
                        aviso("Complete fecha, ID de control y una cantidad mayor que cero.")
                        return

                    conn = get_db_connection()
                    if not conn:
                        error("Sin conexión a la base de datos.")
                        return
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute("""
                            UPDATE picheos
                            SET fecha = %s, id_control = %s, cantidad = %s
                            WHERE id = %s AND usuario_id = %s;
                        """, (fecha_edit, control, cantidad, sel.value, propietario))
                        if cur.rowcount != 1:
                            raise PermissionError("No tienes permiso para modificar este lote.")
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        error(f"No se pudo modificar el lote: {e}")
                        return
                    finally:
                        try:
                            if cur is not None:
                                cur.close()
                        except Exception:
                            pass
                        cerrar_conexion(conn)

                    ok("✅ Lote de picheos modificado correctamente.")
                    refrescar()

                ui.button("💾 Guardar correcciones", on_click=guardar_edicion).props("no-caps").classes(
                    "btn-primario w-full"
                ).style("height:40px")

            with ui.column().classes("gap-2 w-full"):
                ui.label("🗑️ Eliminar lote").classes("subtitulo")
                ids_del = [int(v) for v in df_picheos["id"].tolist()]
                sel_del = ui.select(ids_del, label="ID del lote a eliminar", value=ids_del[0]).props(
                    "outlined dense"
                ).classes("w-full")
                confirmo = ui.checkbox("Confirmo que deseo eliminar este lote")

                def eliminar_lote():
                    if not confirmo.value:
                        aviso("Marca la confirmación para eliminar.")
                        return
                    fila = df_picheos[df_picheos["id"] == sel_del.value]
                    if fila.empty:
                        aviso("Seleccione un lote válido.")
                        return
                    propietario = int(fila.iloc[0]["usuario_id"])

                    conn = get_db_connection()
                    if not conn:
                        error("Sin conexión a la base de datos.")
                        return
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "DELETE FROM picheos WHERE id = %s AND usuario_id = %s;",
                            (sel_del.value, propietario),
                        )
                        if cur.rowcount != 1:
                            raise PermissionError("No tienes permiso para eliminar este lote.")
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        error(f"No se pudo eliminar el lote: {e}")
                        return
                    finally:
                        try:
                            if cur is not None:
                                cur.close()
                        except Exception:
                            pass
                        cerrar_conexion(conn)

                    ok("🗑️ Lote de picheos eliminado correctamente.")
                    refrescar()

                ui.button("🚨 Confirmar y eliminar", on_click=eliminar_lote).props("no-caps").classes(
                    "btn-peligro w-full"
                ).style("height:40px")


# =========================================================
# 13. ADMINISTRACIÓN DE PRECIOS (EXCLUSIVO ADMIN)
# =========================================================
def _vista_precios(usuario, estado, refrescar):
    ui.label("⚙️ Panel de control y configuración de tarifas").classes("titulo-pagina")
    ui.label("Ajusta en tiempo real los valores en USD asignados a cada disciplina deportiva").classes(
        "aviso"
    )

    tasas = obtener_tasas_globales()

    with ui.tabs().classes("w-full") as tabs:
        tab_b = ui.tab("🏀 Tarifas de básquetbol")
        tab_p = ui.tab("⚾ Precios de béisbol")

    with ui.tab_panels(tabs, value=tab_b).classes("w-full"):
        with ui.tab_panel(tab_b):
            with tarjeta():
                ui.label("🏀 Tramos quincenales (USD por partido)").classes("subtitulo")
                with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3 w-full"):
                    n1 = ui.number("Tramo 1 (1-11)", value=tasas["tramo_1"], step=0.25, format="%.2f").props("outlined dense").classes("w-full")
                    n2 = ui.number("Tramo 2 (12-14)", value=tasas["tramo_2"], step=0.25, format="%.2f").props("outlined dense").classes("w-full")
                    n3 = ui.number("Tramo 3 (15-26)", value=tasas["tramo_3"], step=0.25, format="%.2f").props("outlined dense").classes("w-full")
                    n4 = ui.number("Tramo 4 (27-29)", value=tasas["tramo_4"], step=0.25, format="%.2f").props("outlined dense").classes("w-full")
                    n5 = ui.number("Tramo 5 (30+)", value=tasas["tramo_5"], step=0.25, format="%.2f").props("outlined dense").classes("w-full")

                ui.label("⚡ Modalidad especial en vivo").classes("etiqueta").style("margin-top:.4rem")
                with ui.element("div").classes("grid grid-cols-1 sm:grid-cols-2 gap-3 w-full"):
                    n_norm = ui.number("En vivo normal ($)", value=tasas["en_vivo_normal"], step=0.5, format="%.2f").props("outlined dense").classes("w-full")
                    n_nba = ui.number("En vivo NBA ($)", value=tasas["en_vivo_nba"], step=0.5, format="%.2f").props("outlined dense").classes("w-full")

                def guardar_tarifas_basket():
                    cambios = [
                        ("tasa_tramo_1", n1.value),
                        ("tasa_tramo_2", n2.value),
                        ("tasa_tramo_3", n3.value),
                        ("tasa_tramo_4", n4.value),
                        ("tasa_tramo_5", n5.value),
                        ("tasa_en_vivo_normal", n_norm.value),
                        ("tasa_en_vivo_nba", n_nba.value),
                    ]
                    for clave, valor in cambios:
                        actualizar_tasa_global(clave, float(valor or 0), usuario)
                    ok("✅ Tarifas de básquetbol actualizadas.")
                    refrescar()

                ui.button(
                    "💾 Actualizar tarifas de básquetbol", on_click=guardar_tarifas_basket
                ).props("no-caps").classes("btn-primario w-full").style("height:40px")

        with ui.tab_panel(tab_p):
            with tarjeta():
                ui.label("⚾ Tarifa unitaria de béisbol").classes("subtitulo")
                n_precio = ui.number(
                    "Precio por picheo registrado (USD)",
                    value=tasas["precio_beisbol"],
                    step=0.001,
                    format="%.4f",
                ).props("outlined dense").classes("w-full")

                def guardar_precio_beisbol():
                    actualizar_tasa_global("precio_beisbol", float(n_precio.value or 0), usuario)
                    ok("✅ Precio por picheo actualizado.")
                    refrescar()

                ui.button(
                    "💾 Actualizar precio de béisbol", on_click=guardar_precio_beisbol
                ).props("no-caps").classes("btn-primario w-full").style("height:40px")


# =========================================================
# 14. SEGURIDAD Y CONTRASEÑAS
# =========================================================
def _vista_seguridad(usuario, estado, refrescar):
    ui.label("🔐 Gestión de seguridad y contraseñas").classes("titulo-pagina")

    if es_admin_estricto(usuario) and estado.get("modo_admin", True):
        ui.label(
            "👑 Modo administrador: puedes cambiar la contraseña de cualquier operador "
            "sin conocer la clave anterior."
        ).classes("aviso")

        usuarios = obtener_todos_los_usuarios()
        if usuarios.empty:
            ui.label("No hay usuarios registrados.").classes("aviso")
            return

        opciones = {int(r["id"]): f"👤 {r['nombre']} (@{r['username']})" for _, r in usuarios.iterrows()}
        with tarjeta():
            sel = ui.select(opciones, label="Usuario", value=list(opciones.keys())[0]).props(
                "outlined dense"
            ).classes("w-full")
            clave1 = ui.input("Nueva contraseña", password=True, password_toggle_button=True).props(
                "outlined dense"
            ).classes("w-full")
            clave2 = ui.input(
                "Confirmar nueva contraseña", password=True, password_toggle_button=True
            ).props("outlined dense").classes("w-full")

            def forzar_cambio():
                if not (clave1.value and clave2.value):
                    aviso("Complete todos los campos.")
                    return
                if clave1.value != clave2.value:
                    error("Las nuevas contraseñas no coinciden.")
                    return
                if len(clave1.value) < 6:
                    error("La nueva contraseña debe tener al menos 6 caracteres.")
                    return
                exito, msg = cambiar_password_usuario(sel.value, clave1.value)
                if exito:
                    ok(msg)
                    clave1.value = ""
                    clave2.value = ""
                else:
                    error(msg)

            ui.button("🔒 Forzar cambio de contraseña", on_click=forzar_cambio).props(
                "no-caps"
            ).classes("btn-primario w-full").style("height:40px")
        return

    ui.label(f"Actualiza la contraseña de tu cuenta — {usuario['nombre']}").classes("aviso")
    with tarjeta():
        clave_actual = ui.input("Contraseña actual", password=True, password_toggle_button=True).props(
            "outlined dense"
        ).classes("w-full")
        clave1 = ui.input("Nueva contraseña", password=True, password_toggle_button=True).props(
            "outlined dense"
        ).classes("w-full")
        clave2 = ui.input(
            "Confirmar nueva contraseña", password=True, password_toggle_button=True
        ).props("outlined dense").classes("w-full")

        def actualizar_clave():
            if not (clave_actual.value and clave1.value and clave2.value):
                aviso("Complete todos los campos.")
                return
            if clave1.value != clave2.value:
                error("Las nuevas contraseñas no coinciden.")
                return
            if len(clave1.value) < 6:
                error("La nueva contraseña debe tener al menos 6 caracteres.")
                return
            verificado = autenticar_usuario(usuario["username"], clave_actual.value)
            if not verificado:
                error("La contraseña actual es incorrecta.")
                return
            exito, msg = cambiar_password_usuario(usuario["id"], clave1.value)
            if exito:
                ok(msg)
                clave_actual.value = ""
                clave1.value = ""
                clave2.value = ""
            else:
                error(msg)

        ui.button("🔒 Actualizar mi contraseña", on_click=actualizar_clave).props("no-caps").classes(
            "btn-primario w-full"
        ).style("height:40px")


# =========================================================
# 15. PÁGINA Y ARRANQUE
# =========================================================
@ui.page("/", title="ESAM Analytics 360°")
def pagina_principal():
    ui.dark_mode(True)  # la interfaz es oscura por diseño, no depende del tema del sistema
    ui.add_head_html('<meta name="theme-color" content="#090D14">')
    ui.add_head_html(f"<style>{ESTILOS}</style>")
    usuario = app.storage.user.get("usuario")
    if not usuario:
        _vista_acceso()
    else:
        _vista_app(usuario)


@app.on_startup
async def _arranque():
    ESTADO_DB["ok"] = init_db()
    if ESTADO_DB["ok"]:
        print("[ESAM] Base de datos conectada y tablas listas.")
    else:
        print("[ESAM] SIN base de datos: el inicio de sesión no funcionará hasta configurarla.")


if __name__ in {"__main__", "__mp_main__"}:
    print(f"\n[ESAM] Abre en el navegador: http://localhost:{PUERTO}\n")
    ui.run(
        title="ESAM Analytics 360°",
        host="0.0.0.0",
        port=PUERTO,
        storage_secret=STORAGE_SECRET,
        dark=True,
        reload=False,
        show=False,
        favicon="💻",
    )
