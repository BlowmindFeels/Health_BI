"""Acceso a la base de datos (SQLite por defecto; PostgreSQL opcional)."""
import os
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

DDL = [
    """CREATE TABLE IF NOT EXISTS hecho_morbilidad (
        anio SMALLINT NOT NULL, sexo TEXT, quinquenio TEXT,
        edad_min SMALLINT, edad_max SMALLINT,
        localidad_cod TEXT, localidad TEXT, localidad_norm TEXT,
        tipo_afiliacion TEXT, tipo_atencion TEXT, administradora TEXT,
        dx_grupo1 TEXT, dx_grupo2 TEXT,
        enfermedad TEXT NOT NULL, atenciones INTEGER NOT NULL,
        archivo_origen TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_hecho_anio_enf ON hecho_morbilidad (anio, enfermedad)",
    "CREATE INDEX IF NOT EXISTS ix_hecho_archivo ON hecho_morbilidad (archivo_origen)",
    """CREATE TABLE IF NOT EXISTS poblacion_localidad (
        anio SMALLINT NOT NULL, localidad TEXT, localidad_norm TEXT, sexo TEXT,
        edad_min SMALLINT, edad_max SMALLINT, habitantes DOUBLE PRECISION,
        archivo_origen TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS ix_pob_archivo ON poblacion_localidad (archivo_origen)",
    """CREATE TABLE IF NOT EXISTS etl_log (
        fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, tipo TEXT, archivo TEXT,
        leidas INTEGER, cargadas INTEGER, rechazadas INTEGER)""",
]


def get_engine():
    """Orden: variable DATABASE_URL -> archivo ~/HealthBI/conexion.txt -> SQLite en ~/HealthBI/healthbi.db"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        carpeta = Path.home() / "HealthBI"
        carpeta.mkdir(exist_ok=True)
        conf = carpeta / "conexion.txt"
        if conf.exists() and conf.read_text(encoding="utf-8").strip():
            url = conf.read_text(encoding="utf-8").strip()
        else:
            url = f"sqlite:///{(carpeta / 'healthbi.db').as_posix()}"
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}   # la carga corre en un hilo aparte
    return create_engine(url, **kwargs)


def crear_tablas(engine):
    with engine.begin() as con:
        for sentencia in DDL:
            con.execute(text(sentencia))


def borrar_archivo(engine, tabla: str, archivo: str):
    """Recargar el MISMO archivo reemplaza sus filas (no duplica); otros archivos no se tocan."""
    with engine.begin() as con:
        con.execute(text(f"DELETE FROM {tabla} WHERE archivo_origen = :a"), {"a": archivo})


def cargar_df(engine, df: pd.DataFrame, tabla: str):
    if df.empty:
        return
    df = df.astype(object).where(df.notna(), None)            # pd.NA -> NULL
    chunk = max(1, 30000 // len(df.columns))                  # limite de parametros de SQLite (32766)
    df.to_sql(tabla, engine, if_exists="append", index=False, method="multi", chunksize=chunk)


def registrar_log(engine, tipo, archivo, leidas, cargadas, rechazadas):
    with engine.begin() as con:
        con.execute(text("INSERT INTO etl_log (tipo, archivo, leidas, cargadas, rechazadas) "
                         "VALUES (:t, :a, :l, :c, :r)"),
                    {"t": tipo, "a": archivo, "l": int(leidas), "c": int(cargadas), "r": int(rechazadas)})


def contar(engine, tabla: str) -> int:
    with engine.connect() as con:
        return int(con.execute(text(f"SELECT COUNT(*) FROM {tabla}")).scalar())


def cargar_agregado(engine) -> pd.DataFrame:
    sql = """SELECT anio, enfermedad, localidad, localidad_norm, quinquenio, edad_min, edad_max,
                    sexo, SUM(atenciones) AS atenciones
             FROM hecho_morbilidad
             GROUP BY anio, enfermedad, localidad, localidad_norm, quinquenio, edad_min, edad_max, sexo"""
    return pd.read_sql(text(sql), engine)


def cargar_poblacion(engine, edad_min=None, edad_max=None) -> pd.DataFrame:
    sql = "SELECT anio, localidad_norm, SUM(habitantes) AS habitantes FROM poblacion_localidad"
    params = {}
    if edad_min is not None:
        sql += " WHERE edad_min >= :emin AND edad_max <= :emax"
        params = {"emin": int(edad_min), "emax": int(edad_max)}
    sql += " GROUP BY anio, localidad_norm"
    return pd.read_sql(text(sql), engine, params=params)


def ver_log(engine) -> pd.DataFrame:
    return pd.read_sql(text("SELECT * FROM etl_log ORDER BY fecha DESC LIMIT 50"), engine)