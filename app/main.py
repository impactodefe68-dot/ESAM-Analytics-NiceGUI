# ============================================================
# ESAM Analytics 360° — main.py (versión completa, NiceGUI 4.x)
# ============================================================
import os
import hashlib
import hmac
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
from urllib.parse import urlparse, parse_qs, urlunparse

import asyncpg
import pandas as pd
import plotly.graph_objects as go
from dotenv import load_dotenv
from nicegui import app, ui

load_dotenv()

# ---------- CONFIG ----------
DATABASE_URL = os.getenv("DATABASE_URL", "")
STORAGE_SECRET = os.getenv("STORAGE_SECRET", "esam-secret-change-me")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
UTC_OFFSET = int(os.getenv("UTC_OFFSET", "-4"))
PBKDF2_ITER = 240_000

TASAS_DEFECTO = {
    "tasa_tramo_1": 6.00, "tasa_tramo_2": 6.50, "tasa_tramo_3": 7.00,
    "tasa_tramo_4": 7.50, "tasa_tramo_5": 8.00,
    "tasa_en_vivo_normal": 12.00, "tasa_en_vivo_nba": 18.50,
    "precio_beisbol": 0.024,
}


def ahora_local():
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=UTC_OFFSET)


def hoy_local():
    return ahora_local().date()


def parsear_tiempo(v):
    if v is None or pd.isna(v):
        return None
    t = str(v).strip()
    if not t or t.lower() in ("none", "nan", "", "null"):
        return None
    try:
        nums = [int(float(p)) for p in t.split(":")]
    except ValueError:
        return None
    if len(nums) == 3:
        h, m, s = nums
    elif len(nums) == 2:
        h, m, s = 0, nums[0], nums[1]
    elif len(nums) == 1:
        h, m, s = 0, 0, nums[0]
    else:
        return None
    return h * 3600 + m * 60 + s


def formatear_tiempo(seg):
    if seg is None:
        return ""
    t = int(round(seg))
    return f"{t // 3600:02d}:{(t % 3600) // 60:02d}:{t % 60:02d}"


def rango_quincena(f, num=None):
    if num == 1:
        return date(f.year, f.month, 1), date(f.year, f.month, 15)
    if num == 2:
        fin = date(f.year, 12, 31) if f.month == 12 else date(f.year, f.month + 1, 1) - timedelta(days=1)
        return date(f.year, f.month, 16), fin
    if f.day <= 15:
        return rango_quincena(f, 1)
    return rango_quincena(f, 2)


def hash_password(p):
    salt = os.urandom(16)
    d = hashlib.pbkdf2_hmac("sha256", p.encode(), salt, PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt.hex()}${d.hex()}"


def verify_password(p, stored):
    if not stored:
        return False
    if stored.startswith("pbkdf2_sha256$"):
        try:
            _, it, salt_hex, dig_hex = stored.split("$")
            d = hashlib.pbkdf2_hmac("sha256", p.encode(), bytes.fromhex(salt_hex), int(it))
            return hmac.compare_digest(d.hex(), dig_hex)
        except Exception:
            return False
    return hmac.compare_digest(hashlib.sha256(p.encode()).hexdigest(), stored)


def _dsn_and_ssl():
    if not DATABASE_URL:
        return "", None
    try:
        parsed = urlparse(DATABASE_URL)
        qs = parse_qs(parsed.query)
        qs.pop("sslmode", None)
        qs.pop("ssl", None)
        qs.pop("channel_binding", None)
        new_query = "&".join(f"{k}={v[0]}" for k, v in qs.items())
        new_parsed = parsed._replace(query=new_query)
        return urlunparse(new_parsed), "require"
    except Exception:
        return DATABASE_URL, None


class DB:
    pool = None

    @classmethod
    async def connect(cls):
        dsn, ssl_mode = _dsn_and_ssl()
        if not dsn:
            print("❌ DATABASE_URL no configurada")
            return
        try:
            cls.pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10, ssl=ssl_mode)
            await cls._init_schema()
            print("✅ Base de datos conectada")
        except Exception as e:
            print(f"❌ Error conectando a BD: {e}")

    @classmethod
    async def close(cls):
        if cls.pool:
            await cls.pool.close()

    @classmethod
    async def _init_schema(cls):
        async with cls.pool.acquire() as c:
            await c.execute("""
                CREATE TABLE IF NOT EXISTS usuarios (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(50) UNIQUE NOT NULL,
                    nombre VARCHAR(100) NOT NULL DEFAULT 'Operador',
                    password_hash VARCHAR(200) NOT NULL DEFAULT '',
                    rol VARCHAR(30) DEFAULT 'operador',
                    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS partidos (
                    id SERIAL PRIMARY KEY,
                    usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                    codigo_partido VARCHAR(50) NOT NULL,
                    fecha DATE NOT NULL,
                    tiempo_ejecucion VARCHAR(30),
                    tarifa NUMERIC(10,2) DEFAULT 0.00,
                    tipo_partido VARCHAR(30) DEFAULT 'quincenal',
                    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS picheos (
                    id SERIAL PRIMARY KEY,
                    usuario_id INTEGER REFERENCES usuarios(id) ON DELETE CASCADE,
                    fecha DATE NOT NULL,
                    id_control VARCHAR(50) NOT NULL,
                    cantidad INTEGER NOT NULL,
                    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await c.execute("""
                CREATE TABLE IF NOT EXISTS configuracion_global (
                    clave VARCHAR(50) PRIMARY KEY,
                    valor NUMERIC(10,4) NOT NULL
                );
            """)
            await c.execute("CREATE INDEX IF NOT EXISTS idx_partidos_uf ON partidos (usuario_id, fecha);")
            await c.execute("CREATE INDEX IF NOT EXISTS idx_picheos_uf ON picheos (usuario_id, fecha);")
            for k, v in TASAS_DEFECTO.items():
                await c.execute(
                    "INSERT INTO configuracion_global (clave, valor) VALUES ($1, $2) ON CONFLICT (clave) DO NOTHING",
                    k, v,
                )
            await c.execute(
                "INSERT INTO usuarios (username, nombre, password_hash, rol) VALUES ('admin','Administrador', $1, 'admin') ON CONFLICT (username) DO NOTHING",
                hash_password(ADMIN_PASSWORD),
            )

    @classmethod
    async def authenticate(cls, username, password):
        async with cls.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT id, username, nombre, rol, password_hash FROM usuarios WHERE username = $1",
                username.lower().strip(),
            )
            if not row or not verify_password(password.strip(), row["password_hash"]):
                return None
            if not row["password_hash"].startswith("pbkdf2_sha256$"):
                await c.execute("UPDATE usuarios SET password_hash = $1 WHERE id = $2",
                                hash_password(password.strip()), row["id"])
            return {"id": row["id"], "username": row["username"], "nombre": row["nombre"], "rol": row["rol"]}

    @classmethod
    async def register(cls, username, nombre, password):
        try:
            async with cls.pool.acquire() as c:
                await c.execute(
                    "INSERT INTO usuarios (username, nombre, password_hash) VALUES ($1, $2, $3)",
                    username.lower().strip(), nombre.strip(), hash_password(password),
                )
            return True, "✅ Usuario registrado"
        except asyncpg.UniqueViolationError:
            return False, "⚠️ El usuario ya existe"
        except Exception as e:
            return False, f"❌ Error: {e}"

    @classmethod
    async def change_password(cls, user_id, new_pass):
        try:
            async with cls.pool.acquire() as c:
                await c.execute("UPDATE usuarios SET password_hash = $1 WHERE id = $2",
                                hash_password(new_pass), user_id)
            return True, "✅ Contraseña actualizada"
        except Exception as e:
            return False, f"❌ Error: {e}"

    @classmethod
    async def list_users(cls):
        async with cls.pool.acquire() as c:
            return await c.fetch("SELECT id, username, nombre, rol FROM usuarios ORDER BY id")

    @classmethod
    async def get_tasas(cls):
        tasas = dict(TASAS_DEFECTO)
        async with cls.pool.acquire() as c:
            rows = await c.fetch("SELECT clave, valor FROM configuracion_global")
            m = {"tasa_tramo_1": "tramo_1", "tasa_tramo_2": "tramo_2", "tasa_tramo_3": "tramo_3",
                 "tasa_tramo_4": "tramo_4", "tasa_tramo_5": "tramo_5",
                 "tasa_en_vivo_normal": "en_vivo_normal", "tasa_en_vivo_nba": "en_vivo_nba",
                 "precio_beisbol": "precio_beisbol"}
            for r in rows:
                if r["clave"] in m:
                    tasas[m[r["clave"]]] = float(r["valor"])
        return tasas

    @classmethod
    async def set_tasa(cls, clave, valor):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE configuracion_global SET valor = $1 WHERE clave = $2",
                            float(valor), clave)

    @classmethod
    async def get_partidos(cls, uid, f_ini, f_fin):
        async with cls.pool.acquire() as c:
            if uid == 0:
                return await c.fetch("""
                    SELECT p.id, p.usuario_id, u.nombre AS operador, p.codigo_partido,
                           p.fecha, p.tiempo_ejecucion, p.tarifa, p.tipo_partido
                    FROM partidos p JOIN usuarios u ON u.id = p.usuario_id
                    WHERE p.fecha BETWEEN $1 AND $2 ORDER BY p.fecha DESC, p.id DESC
                """, f_ini, f_fin)
            return await c.fetch("""
                SELECT id, usuario_id, codigo_partido, fecha, tiempo_ejecucion,
                       tarifa, tipo_partido FROM partidos
                WHERE usuario_id = $1 AND fecha BETWEEN $2 AND $3
                ORDER BY fecha DESC, id DESC
            """, uid, f_ini, f_fin)

    @classmethod
    async def get_partido(cls, pid):
        async with cls.pool.acquire() as c:
            return await c.fetchrow(
                "SELECT id, usuario_id, codigo_partido, fecha, tiempo_ejecucion, tarifa, tipo_partido FROM partidos WHERE id = $1",
                pid)

    @classmethod
    async def add_partido(cls, uid, codigo, fecha, tiempo, tarifa, tipo):
        async with cls.pool.acquire() as c:
            await c.execute("""
                INSERT INTO partidos (usuario_id, codigo_partido, fecha, tiempo_ejecucion, tarifa, tipo_partido)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, uid, codigo, fecha, tiempo, tarifa, tipo)

    @classmethod
    async def update_partido(cls, pid, fecha, codigo, tiempo, tarifa):
        async with cls.pool.acquire() as c:
            await c.execute("""
                UPDATE partidos SET fecha = $1, codigo_partido = $2,
                                    tiempo_ejecucion = $3, tarifa = $4 WHERE id = $5
            """, fecha, codigo, tiempo, tarifa, pid)

    @classmethod
    async def delete_partido(cls, pid):
        async with cls.pool.acquire() as c:
            await c.execute("DELETE FROM partidos WHERE id = $1", pid)

    @classmethod
    async def contar_quincenales(cls, uid, f_ini, f_fin):
        async with cls.pool.acquire() as c:
            row = await c.fetchval("""
                SELECT COUNT(*) FROM partidos
                WHERE usuario_id = $1 AND fecha BETWEEN $2 AND $3
                  AND (tipo_partido IS NULL OR tipo_partido = 'quincenal')
            """, uid, f_ini, f_fin)
            return int(row or 0)

    @classmethod
    async def update_tarifa_quincenal(cls, uid, f_ini, f_fin, tarifa):
        async with cls.pool.acquire() as c:
            await c.execute("""
                UPDATE partidos SET tarifa = $1
                WHERE usuario_id = $2 AND fecha BETWEEN $3 AND $4
                  AND (tipo_partido IS NULL OR tipo_partido = 'quincenal')
            """, tarifa, uid, f_ini, f_fin)

    @classmethod
    async def get_picheos(cls, uid, f_ini, f_fin):
        async with cls.pool.acquire() as c:
            if uid == 0:
                return await c.fetch("""
                    SELECT pi.id, pi.usuario_id, u.nombre AS operador, pi.fecha,
                           pi.id_control, pi.cantidad
                    FROM picheos pi JOIN usuarios u ON u.id = pi.usuario_id
                    WHERE pi.fecha BETWEEN $1 AND $2 ORDER BY pi.fecha DESC, pi.id DESC
                """, f_ini, f_fin)
            return await c.fetch("""
                SELECT id, usuario_id, fecha, id_control, cantidad FROM picheos
                WHERE usuario_id = $1 AND fecha BETWEEN $2 AND $3
                ORDER BY fecha DESC, id DESC
            """, uid, f_ini, f_fin)

    @classmethod
    async def add_picheos(cls, uid, fecha, id_ctrl, cant):
        async with cls.pool.acquire() as c:
            await c.execute("INSERT INTO picheos (usuario_id, fecha, id_control, cantidad) VALUES ($1,$2,$3,$4)",
                            uid, fecha, id_ctrl, cant)

    @classmethod
    async def update_picheos(cls, pid, fecha, id_ctrl, cant):
        async with cls.pool.acquire() as c:
            await c.execute("UPDATE picheos SET fecha = $1, id_control = $2, cantidad = $3 WHERE id = $4",
                            fecha, id_ctrl, cant, pid)

    @classmethod
    async def delete_picheos(cls, pid):
        async with cls.pool.acquire() as c:
            await c.execute("DELETE FROM picheos WHERE id = $1", pid)


async def recalcular_quincena(uid, fecha_ref):
    iq_i, iq_f = rango_quincena(fecha_ref)
    tasas = await DB.get_tasas()

    async def aplicar(u):
        total = await DB.contar_quincenales(u, iq_i, iq_f)
        if total <= 11:
            t = tasas["tramo_1"]
        elif total <= 14:
            t = tasas["tramo_2"]
        elif total <= 26:
            t = tasas["tramo_3"]
        elif total <= 29:
            t = tasas["tramo_4"]
        else:
            t = tasas["tramo_5"]
        await DB.update_tarifa_quincenal(u, iq_i, iq_f, t)

    if uid == 0:
        for u in await DB.list_users():
            await aplicar(u["id"])
    else:
        await aplicar(uid)


# ---------- MIDDLEWARE AUTH ----------
class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in {"/login", "/favicon.ico"} or path.startswith("/_nicegui"):
            await self.app(scope, receive, send)
            return
        if not app.storage.user.get("authenticated"):
            from starlette.responses import RedirectResponse
            resp = RedirectResponse(f"/login?redirect_to={path}")
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def logout():
    app.storage.user.clear()
    ui.navigate.to("/login")


@contextmanager
def page_layout(title, active):
    with ui.left_drawer(value=True).classes("bg-slate-800"):
        ui.label("🎯 ESAM Analytics").classes("text-white text-lg font-bold p-4")
        ui.label("360° · Control Operativo").classes("text-slate-400 text-xs px-4 pb-4")
        items = [("📊 Panel General", "/"), ("🏀 Básquetbol", "/basket"), ("⚾ Béisbol", "/beisbol")]
        if app.storage.user.get("rol") == "admin":
            items.append(("⚙️ Administración", "/admin"))
        items.append(("🔐 Seguridad", "/security"))
        for label, route in items:
            cls = "w-full justify-start " + ("bg-blue-600" if route == active else "text-slate-300")
            ui.button(label, on_click=lambda r=route: ui.navigate.to(r)) \
                .props("flat color=white align=left no-caps").classes(cls)
        ui.space()
        ui.label(f"📍 {ahora_local().strftime('%d/%m/%Y · %H:%M')}").classes("text-xs text-slate-400 p-4")

    with ui.header().classes("bg-slate-900 text-white"):
        with ui.row().classes("w-full items-center justify-between px-4"):
            with ui.row().classes("items-center gap-2"):
                ui.button(icon="menu", on_click=lambda: None).props("flat color=white")
                ui.label(f"ESAM 360° · {title}").classes("text-lg font-bold")
            with ui.row().classes("items-center gap-3"):
                ui.label(f"👤 {app.storage.user.get('nombre','')}").classes("text-sm text-slate-300")
                ui.button("Salir", icon="logout", on_click=logout).props("flat color=white no-caps")

    with ui.column().classes("w-full p-6 max-w-7xl mx-auto gap-4") as content:
        yield content


def get_uid():
    if app.storage.user.get("rol") != "admin":
        return app.storage.user["user_id"]
    return app.storage.user.get("admin_target", app.storage.user["user_id"])


async def user_selector():
    if app.storage.user.get("rol") != "admin":
        return
    users = await DB.list_users()
    opts = {0: "🌐 GLOBAL (todos)"}
    for u in users:
        opts[u["id"]] = f"👤 {u['nombre']} (@{u['username']})"
    sel = ui.select(opts, label="Ver datos de", value=app.storage.user.get("admin_target", 0)) \
        .classes("w-80").props("outlined dense")

    def on_change(e):
        app.storage.user.update(admin_target=e.value)
        ui.navigate.to(ui.context.client.page.path)

    sel.on_value_change(on_change)


# ============================================================
# LOGIN
# ============================================================
@ui.page("/login")
def login_page(redirect_to: str = "/"):
    if app.storage.user.get("authenticated"):
        ui.navigate.to("/")
        return
    ui.add_head_html("<style>body{background:#0f172a;}</style>")
    with ui.card().classes("absolute-center w-96 p-8 shadow-2xl bg-slate-800 gap-3"):
        ui.label("🎯 ESAM Analytics 360°").classes("text-2xl font-bold text-white text-center")
        ui.label("CONTROL OPERATIVO").classes("text-xs text-slate-400 text-center mb-4 tracking-widest")
        tabs = ui.tabs().classes("w-full").props("dark")
        with tabs:
            ui.tab("Ingresar")
            ui.tab("Crear cuenta")
        with ui.tab_panels(tabs, value="Ingresar").classes("w-full bg-transparent"):
            with ui.tab_panel("Ingresar"):
                u = ui.input("Usuario").props("outlined dark dense").classes("w-full")
                p = ui.input("Contraseña", password=True, password_toggle_button=True).props("outlined dark dense").classes("w-full")

                async def do_login():
                    if not (u.value and p.value):
                        ui.notify("Completa todos los campos", type="warning")
                        return
                    user = await DB.authenticate(u.value, p.value)
                    if not user:
                        ui.notify("Credenciales incorrectas", type="negative")
                        return
                    app.storage.user.update(
                        authenticated=True, user_id=user["id"], username=user["username"],
                        nombre=user["nombre"], rol=user["rol"], admin_target=user["id"],
                    )
                    ui.navigate.to(redirect_to)

                ui.button("Ingresar", on_click=do_login).classes("w-full mt-2").props("color=primary no-caps")

            with ui.tab_panel("Crear cuenta"):
                rn = ui.input("Nombre completo").props("outlined dark dense").classes("w-full")
                ru = ui.input("Usuario").props("outlined dark dense").classes("w-full")
                rp1 = ui.input("Contraseña", password=True).props("outlined dark dense").classes("w-full")
                rp2 = ui.input("Confirmar contraseña", password=True).props("outlined dark dense").classes("w-full")

                async def do_register():
                    if not all([rn.value, ru.value, rp1.value, rp2.value]):
                        ui.notify("Completa todos los campos", type="warning")
                        return
                    if rp1.value != rp2.value:
                        ui.notify("Las contraseñas no coinciden", type="negative")
                        return
                    if len(rp1.value) < 6:
                        ui.notify("Mínimo 6 caracteres", type="warning")
                        return
                    ok, msg = await DB.register(ru.value, rn.value, rp1.value)
                    ui.notify(msg, type="positive" if ok else "negative")

                ui.button("Registrarse", on_click=do_register).classes("w-full mt-2").props("color=primary no-caps")


# ============================================================
# DASHBOARD
# ============================================================
@ui.page("/")
async def dashboard():
    with page_layout("Dashboard", "/"):
        await user_selector()
        uid = get_uid()
        period = ui.toggle(["Quincena Actual", "Quincena Anterior", "Este Mes"], value="Quincena Actual").props("dense")
        metrics_row = ui.row().classes("w-full gap-4 flex-wrap")
        chart_area = ui.column().classes("w-full")

        async def refresh():
            hoy = hoy_local()
            if period.value == "Quincena Actual":
                fi, ff = rango_quincena(hoy)
            elif period.value == "Quincena Anterior":
                if hoy.day <= 15:
                    primero = date(hoy.year, hoy.month, 1)
                    fi, ff = rango_quincena(primero - timedelta(days=1), 2)
                else:
                    fi, ff = rango_quincena(hoy, 1)
            else:
                fin = (date(hoy.year, hoy.month + 1, 1) if hoy.month < 12
                       else date(hoy.year, 12, 31)) - timedelta(days=1)
                fi, ff = date(hoy.year, hoy.month, 1), fin

            partidos = await DB.get_partidos(uid, fi, ff)
            picheos = await DB.get_picheos(uid, fi, ff)
            tasas = await DB.get_tasas()
            total_b = sum(float(p["tarifa"]) for p in partidos)
            total_p = sum(int(p["cantidad"]) for p in picheos)
            total_beis = total_p * tasas["precio_beisbol"]
            total = total_b + total_beis
            tiempos = [t for t in (parsear_tiempo(p["tiempo_ejecucion"]) for p in partidos) if t is not None]
            prom = sum(tiempos) / len(tiempos) if tiempos else None

            metrics_row.clear()
            with metrics_row:
                for label, val, sub in [
                    ("💰 Total Acumulado", f"${total:,.2f}", f"📅 {fi.strftime('%d/%m')}–{ff.strftime('%d/%m')}"),
                    ("🏀 Básquetbol", f"${total_b:,.2f}", f"🔺 {len(partidos)} partidos"),
                    ("⚾ Béisbol", f"${total_beis:,.2f}", f"🔺 {total_p:,} picheos"),
                    ("⏱️ Tiempo Prom.", formatear_tiempo(prom) or "—", f"⏱️ {len(tiempos)} con tiempo"),
                ]:
                    with ui.card().classes("flex-1 p-4 shadow-sm min-w-[180px]"):
                        ui.label(label).classes("text-xs text-gray-500 uppercase font-semibold")
                        ui.label(val).classes("text-2xl font-bold text-slate-900 mt-1")
                        ui.label(sub).classes("text-xs text-gray-400 mt-1")

            chart_area.clear()
            with chart_area:
                by_date = {}
                for p in partidos:
                    by_date.setdefault(p["fecha"], {"b": 0.0, "p": 0.0})
                    by_date[p["fecha"]]["b"] += float(p["tarifa"])
                for p in picheos:
                    by_date.setdefault(p["fecha"], {"b": 0.0, "p": 0.0})
                    by_date[p["fecha"]]["p"] += int(p["cantidad"]) * tasas["precio_beisbol"]
                if by_date:
                    fechas = sorted(by_date.keys())
                    fig = go.Figure()
                    fig.add_trace(go.Bar(name="🏀 Básquetbol", x=fechas,
                                         y=[by_date[d]["b"] for d in fechas], marker_color="#2563EB"))
                    fig.add_trace(go.Bar(name="⚾ Béisbol", x=fechas,
                                         y=[by_date[d]["p"] for d in fechas], marker_color="#0EA5E9"))
                    fig.update_layout(barmode="stack", height=380,
                                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                      margin=dict(l=20, r=20, t=20, b=20),
                                      legend=dict(orientation="h", y=1.1), yaxis_title="USD")
                    ui.plotly(fig).classes("w-full")
                else:
                    ui.label("Sin movimientos en el periodo").classes("text-gray-500 p-8 text-center")

        period.on_value_change(lambda _: refresh())
        await refresh()


# ============================================================
# BASKET
# ============================================================
@ui.page("/basket")
async def basket_page():
    with page_layout("Básquetbol", "/basket"):
        await user_selector()
        uid = get_uid()
        hoy = hoy_local()
        period = ui.toggle(["Quincena Actual", "Quincena Anterior"], value="Quincena Actual").props("dense")

        with ui.row().classes("w-full justify-between items-center"):
            title = ui.label("").classes("text-lg font-semibold")
            add_btn = ui.button("➕ Nuevo Partido").props("color=primary no-caps")

        totals = ui.label("").classes("text-sm text-gray-600")
        table_area = ui.column().classes("w-full")

        async def refresh():
            if period.value == "Quincena Actual":
                fi, ff = rango_quincena(hoy)
            else:
                if hoy.day <= 15:
                    primero = date(hoy.year, hoy.month, 1)
                    fi, ff = rango_quincena(primero - timedelta(days=1), 2)
                else:
                    fi, ff = rango_quincena(hoy, 1)
            title.text = f"Partidos · {fi.strftime('%d/%m/%Y')} al {ff.strftime('%d/%m/%Y')}"
            await recalcular_quincena(uid, fi)
            partidos = await DB.get_partidos(uid, fi, ff)
            total = sum(float(p["tarifa"]) for p in partidos)
            totals.text = f"📊 {len(partidos)} partidos · 💰 ${total:,.2f}"

            table_area.clear()
            with table_area:
                if not partidos:
                    ui.label("Sin partidos en este periodo").classes("text-gray-500 p-8 text-center w-full")
                    return
                cols = [
                    {"name": "id", "label": "ID", "field": "id", "align": "left"},
                    {"name": "codigo", "label": "Código", "field": "codigo", "align": "left"},
                    {"name": "fecha", "label": "Fecha", "field": "fecha", "align": "left"},
                    {"name": "tiempo", "label": "Tiempo", "field": "tiempo", "align": "left"},
                    {"name": "tipo", "label": "Modalidad", "field": "tipo", "align": "left"},
                    {"name": "tarifa", "label": "Tarifa $", "field": "tarifa", "align": "right"},
                    {"name": "acc", "label": "", "field": "acc", "align": "right"},
                ]
                if "operador" in partidos[0].keys():
                    cols.insert(1, {"name": "operador", "label": "Operador", "field": "operador", "align": "left"})
                rows = []
                for p in partidos:
                    r = {"id": p["id"], "codigo": p["codigo_partido"],
                         "fecha": p["fecha"].strftime("%d/%m/%Y"),
                         "tiempo": p["tiempo_ejecucion"] or "—",
                         "tipo": p["tipo_partido"] or "quincenal",
                         "tarifa": f"${float(p['tarifa']):.2f}"}
                    if "operador" in p.keys():
                        r["operador"] = p["operador"]
                    rows.append(r)
                tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full").props("flat bordered dense")
                tbl.add_slot("body-cell-acc", """
                    <q-td :props="props">
                        <q-btn icon="edit" flat dense color="primary" @click="$parent.$emit('edit_row', props.row.id)"/>
                        <q-btn icon="delete" flat dense color="negative" @click="$parent.$emit('del_row', props.row.id)"/>
                    </q-td>
                """)
                tbl.on("edit_row", lambda e: open_edit(e.args))
                tbl.on("del_row", lambda e: ask_delete(e.args))

        async def ask_delete(pid):
            with ui.dialog() as d, ui.card():
                ui.label(f"¿Eliminar partido #{pid}?").classes("text-lg")
                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")

                    async def ok():
                        await DB.delete_partido(pid)
                        d.close()
                        await refresh()
                        ui.notify("Partido eliminado", type="positive")

                    ui.button("Eliminar", on_click=ok).props("color=negative no-caps")
            d.open()

        async def open_edit(pid):
            p = await DB.get_partido(pid)
            if not p:
                return
            with ui.dialog() as d, ui.card().classes("w-96"):
                ui.label(f"Editar #{pid}").classes("text-lg font-semibold")
                f_ = ui.input("Fecha", value=p["fecha"].isoformat()).props("type=date outlined dense").classes("w-full")
                c_ = ui.input("Código", value=p["codigo_partido"]).props("outlined dense").classes("w-full")
                t_ = ui.input("Tiempo (HH:MM:SS)", value=p["tiempo_ejecucion"] or "").props("outlined dense").classes("w-full")
                ta_ = ui.number("Tarifa $", value=float(p["tarifa"]), step=0.5).props("outlined dense").classes("w-full")

                async def save():
                    try:
                        nf = date.fromisoformat(f_.value)
                    except Exception:
                        ui.notify("Fecha inválida", type="negative"); return
                    seg = parsear_tiempo(t_.value) if t_.value else None
                    if t_.value and seg is None:
                        ui.notify("Tiempo inválido", type="negative"); return
                    await DB.update_partido(pid, nf, c_.value, formatear_tiempo(seg), float(ta_.value))
                    d.close()
                    await refresh()
                    ui.notify("Actualizado", type="positive")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")
                    ui.button("Guardar", on_click=save).props("color=primary no-caps")
            d.open()

        async def open_add():
            tasas = await DB.get_tasas()
            with ui.dialog() as d, ui.card().classes("w-96"):
                ui.label("Nuevo Partido").classes("text-lg font-semibold")
                f_ = ui.input("Fecha", value=hoy.isoformat()).props("type=date outlined dense").classes("w-full")
                c_ = ui.input("Código").props("outlined dense").classes("w-full")
                t_ = ui.input("Tiempo (HH:MM:SS)").props("outlined dense").classes("w-full")
                tipo = ui.select(
                    {"quincenal": "🏀 Quincenal", "en_vivo_normal": "⚡ En Vivo Normal", "en_vivo_nba": "🔥 En Vivo NBA"},
                    label="Modalidad", value="quincenal",
                ).props("outlined dense").classes("w-full")

                async def save():
                    if not c_.value:
                        ui.notify("Falta código", type="warning"); return
                    try:
                        nf = date.fromisoformat(f_.value)
                    except Exception:
                        ui.notify("Fecha inválida", type="negative"); return
                    seg = parsear_tiempo(t_.value) if t_.value else None
                    if t_.value and seg is None:
                        ui.notify("Tiempo inválido", type="negative"); return
                    tarifa_map = {"quincenal": tasas["tramo_1"], "en_vivo_normal": tasas["en_vivo_normal"], "en_vivo_nba": tasas["en_vivo_nba"]}
                    await DB.add_partido(app.storage.user["user_id"], c_.value.strip(), nf,
                                         formatear_tiempo(seg), tarifa_map[tipo.value], tipo.value)
                    d.close()
                    await refresh()
                    ui.notify("Guardado", type="positive")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")
                    ui.button("Guardar", on_click=save).props("color=primary no-caps")
            d.open()

        add_btn.on_click(open_add)
        period.on_value_change(lambda _: refresh())
        await refresh()


# ============================================================
# BEISBOL
# ============================================================
@ui.page("/beisbol")
async def beisbol_page():
    with page_layout("Béisbol", "/beisbol"):
        await user_selector()
        uid = get_uid()
        hoy = hoy_local()
        period = ui.toggle(["Quincena Actual", "Quincena Anterior"], value="Quincena Actual").props("dense")

        with ui.row().classes("w-full justify-between items-center"):
            title = ui.label("").classes("text-lg font-semibold")
            add_btn = ui.button("➕ Nuevo Lote").props("color=primary no-caps")

        totals = ui.label("").classes("text-sm text-gray-600")
        table_area = ui.column().classes("w-full")

        async def refresh():
            if period.value == "Quincena Actual":
                fi, ff = rango_quincena(hoy)
            else:
                if hoy.day <= 15:
                    primero = date(hoy.year, hoy.month, 1)
                    fi, ff = rango_quincena(primero - timedelta(days=1), 2)
                else:
                    fi, ff = rango_quincena(hoy, 1)
            title.text = f"Picheos · {fi.strftime('%d/%m/%Y')} al {ff.strftime('%d/%m/%Y')}"
            picheos = await DB.get_picheos(uid, fi, ff)
            tasas = await DB.get_tasas()
            total_p = sum(int(p["cantidad"]) for p in picheos)
            total_usd = total_p * tasas["precio_beisbol"]
            totals.text = f"⚾ {total_p:,} picheos · 💵 ${total_usd:,.2f} (@ ${tasas['precio_beisbol']}/u)"

            table_area.clear()
            with table_area:
                if not picheos:
                    ui.label("Sin registros").classes("text-gray-500 p-8 text-center w-full")
                    return
                cols = [
                    {"name": "id", "label": "ID", "field": "id", "align": "left"},
                    {"name": "fecha", "label": "Fecha", "field": "fecha", "align": "left"},
                    {"name": "control", "label": "ID Control", "field": "control", "align": "left"},
                    {"name": "cantidad", "label": "Cantidad", "field": "cantidad", "align": "right"},
                    {"name": "acc", "label": "", "field": "acc", "align": "right"},
                ]
                if "operador" in picheos[0].keys():
                    cols.insert(1, {"name": "operador", "label": "Operador", "field": "operador", "align": "left"})
                rows = []
                for p in picheos:
                    r = {"id": p["id"], "fecha": p["fecha"].strftime("%d/%m/%Y"),
                         "control": p["id_control"], "cantidad": int(p["cantidad"])}
                    if "operador" in p.keys():
                        r["operador"] = p["operador"]
                    rows.append(r)
                tbl = ui.table(columns=cols, rows=rows, row_key="id").classes("w-full").props("flat bordered dense")
                tbl.add_slot("body-cell-acc", """
                    <q-td :props="props">
                        <q-btn icon="edit" flat dense color="primary" @click="$parent.$emit('edit_row', props.row.id)"/>
                        <q-btn icon="delete" flat dense color="negative" @click="$parent.$emit('del_row', props.row.id)"/>
                    </q-td>
                """)
                tbl.on("edit_row", lambda e: open_edit(e.args))
                tbl.on("del_row", lambda e: ask_delete(e.args))

        async def ask_delete(pid):
            with ui.dialog() as d, ui.card():
                ui.label(f"¿Eliminar lote #{pid}?").classes("text-lg")
                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")

                    async def ok():
                        await DB.delete_picheos(pid)
                        d.close()
                        await refresh()
                        ui.notify("Eliminado", type="positive")

                    ui.button("Eliminar", on_click=ok).props("color=negative no-caps")
            d.open()

        async def open_edit(pid):
            all_p = await DB.get_picheos(uid, date(2020, 1, 1), date(2035, 12, 31))
            p = next((x for x in all_p if x["id"] == pid), None)
            if not p:
                return
            with ui.dialog() as d, ui.card().classes("w-96"):
                ui.label(f"Editar #{pid}").classes("text-lg font-semibold")
                f_ = ui.input("Fecha", value=p["fecha"].isoformat()).props("type=date outlined dense").classes("w-full")
                c_ = ui.input("ID Control", value=p["id_control"]).props("outlined dense").classes("w-full")
                n_ = ui.number("Cantidad", value=int(p["cantidad"]), step=1).props("outlined dense").classes("w-full")

                async def save():
                    try:
                        nf = date.fromisoformat(f_.value)
                    except Exception:
                        ui.notify("Fecha inválida", type="negative"); return
                    await DB.update_picheos(pid, nf, c_.value, int(n_.value))
                    d.close()
                    await refresh()
                    ui.notify("Actualizado", type="positive")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")
                    ui.button("Guardar", on_click=save).props("color=primary no-caps")
            d.open()

        async def open_add():
            with ui.dialog() as d, ui.card().classes("w-96"):
                ui.label("Nuevo Lote").classes("text-lg font-semibold")
                f_ = ui.input("Fecha", value=hoy.isoformat()).props("type=date outlined dense").classes("w-full")
                c_ = ui.input("ID Control").props("outlined dense").classes("w-full")
                n_ = ui.number("Cantidad", value=100, step=1).props("outlined dense").classes("w-full")

                async def save():
                    if not c_.value:
                        ui.notify("Falta ID", type="warning"); return
                    try:
                        nf = date.fromisoformat(f_.value)
                    except Exception:
                        ui.notify("Fecha inválida", type="negative"); return
                    await DB.add_picheos(app.storage.user["user_id"], nf, c_.value.strip(), int(n_.value))
                    d.close()
                    await refresh()
                    ui.notify("Guardado", type="positive")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("Cancelar", on_click=d.close).props("flat no-caps")
                    ui.button("Guardar", on_click=save).props("color=primary no-caps")
            d.open()

        add_btn.on_click(open_add)
        period.on_value_change(lambda _: refresh())
        await refresh()


# ============================================================
# ADMIN
# ============================================================
@ui.page("/admin")
async def admin_page():
    if app.storage.user.get("rol") != "admin":
        ui.navigate.to("/")
        return
    with page_layout("Administración", "/admin"):
        ui.label("⚙️ Configuración de Tarifas").classes("text-xl font-bold")
        ui.label("Los cambios se aplican retroactivamente al recalcular quincenas.").classes("text-sm text-gray-500")
        tasas = await DB.get_tasas()

        with ui.card().classes("w-full"):
            ui.label("🏀 Básquetbol — Tramos").classes("font-semibold mb-2")
            with ui.row().classes("w-full gap-4 flex-wrap"):
                t1 = ui.number("Tramo 1 (1–11)", value=tasas["tramo_1"], step=0.25).classes("flex-1 min-w-[140px]").props("outlined dense")
                t2 = ui.number("Tramo 2 (12–14)", value=tasas["tramo_2"], step=0.25).classes("flex-1 min-w-[140px]").props("outlined dense")
                t3 = ui.number("Tramo 3 (15–26)", value=tasas["tramo_3"], step=0.25).classes("flex-1 min-w-[140px]").props("outlined dense")
                t4 = ui.number("Tramo 4 (27–29)", value=tasas["tramo_4"], step=0.25).classes("flex-1 min-w-[140px]").props("outlined dense")
                t5 = ui.number("Tramo 5 (30+)", value=tasas["tramo_5"], step=0.25).classes("flex-1 min-w-[140px]").props("outlined dense")
            ui.label("⚡ En Vivo").classes("font-semibold mt-4 mb-2")
            with ui.row().classes("w-full gap-4 flex-wrap"):
                vn = ui.number("Normal ($)", value=tasas["en_vivo_normal"], step=0.5).classes("flex-1 min-w-[140px]").props("outlined dense")
                vna = ui.number("NBA ($)", value=tasas["en_vivo_nba"], step=0.5).classes("flex-1 min-w-[140px]").props("outlined dense")
            ui.label("⚾ Béisbol").classes("font-semibold mt-4 mb-2")
            pb = ui.number("Precio por picheo ($)", value=tasas["precio_beisbol"], format="%.4f", step=0.001).classes("w-64").props("outlined dense")

            async def save():
                for k, v in [("tasa_tramo_1", t1.value), ("tasa_tramo_2", t2.value), ("tasa_tramo_3", t3.value),
                             ("tasa_tramo_4", t4.value), ("tasa_tramo_5", t5.value),
                             ("tasa_en_vivo_normal", vn.value), ("tasa_en_vivo_nba", vna.value),
                             ("precio_beisbol", pb.value)]:
                    await DB.set_tasa(k, v)
                ui.notify("✅ Tarifas actualizadas", type="positive")

            ui.button("💾 Guardar Cambios", on_click=save).classes("mt-4").props("color=primary no-caps")


# ============================================================
# SEGURIDAD
# ============================================================
@ui.page("/security")
async def security_page():
    with page_layout("Seguridad", "/security"):
        ui.label("🔐 Seguridad").classes("text-xl font-bold")
        with ui.card().classes("w-full max-w-md"):
            user_sel = None
            if app.storage.user.get("rol") == "admin":
                ui.label("Cambiar contraseña de usuario").classes("font-semibold mb-2")
                users = await DB.list_users()
                opts = {u["id"]: f"{u['nombre']} (@{u['username']})" for u in users}
                user_sel = ui.select(opts, label="Usuario", value=app.storage.user["user_id"]).props("outlined dense").classes("w-full")
            else:
                ui.label("Cambiar mi contraseña").classes("font-semibold mb-2")
            p1 = ui.input("Nueva contraseña", password=True).props("outlined dense").classes("w-full")
            p2 = ui.input("Confirmar", password=True).props("outlined dense").classes("w-full")

            async def do_change():
                if not (p1.value and p2.value):
                    ui.notify("Completa ambos", type="warning"); return
                if p1.value != p2.value:
                    ui.notify("No coinciden", type="negative"); return
                if len(p1.value) < 6:
                    ui.notify("Mínimo 6 caracteres", type="warning"); return
                target = user_sel.value if user_sel else app.storage.user["user_id"]
                ok, msg = await DB.change_password(target, p1.value)
                ui.notify(msg, type="positive" if ok else "negative")
                if ok:
                    p1.value = ""
                    p2.value = ""

            ui.button("Actualizar", on_click=do_change).classes("mt-2").props("color=primary no-caps")


# ============================================================
# ARRANQUE
# ============================================================
app.add_middleware(AuthMiddleware)


@app.on_startup
async def _start():
    await DB.connect()


@app.on_shutdown
async def _stop():
    await DB.close()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="ESAM Analytics 360°",
        port=int(os.getenv("PORT", 8080)),
        host="0.0.0.0",
        storage_secret=STORAGE_SECRET,
        reload=False,
        favicon="🎯",
    )
