"""ETL de RIPS (morbilidad) y poblacion. No depende de Streamlit ni de la base de datos."""
import io
import re
import unicodedata

import pandas as pd

# ---- Parametros de la poblacion objetivo (se pueden cambiar desde la interfaz) ----
ANIO_MIN, ANIO_MAX = 2020, 2025
EDAD_MIN, EDAD_MAX = 18, 59

# Patrones para detectar cada enfermedad en el texto del diagnostico.
# Son un punto de partida: revisenlos contra los diagnosticos reales de su archivo.
ENFERMEDADES = {
    "EPOC": r"pulmonar obstructiva|epoc|bronquitis cr[oó]nica|enfisema",
    "ASMA": r"asma",
    "DIABETES": r"diabetes",
}

RENAME = {
    "ano": "anio", "año": "anio",
    "sexo_gen": "sexo",
    "edad_quiquenio": "quinquenio", "edad_quinquenio": "quinquenio",
    "tipo_usuario_afiliacion": "tipo_afiliacion",
    "prestador_localidad_codigo": "localidad_cod",
    "prestador_localidad_nombre": "localidad",
    "tipo_atencion_nombre": "tipo_atencion",
    "dxprincipal_agrupacion1_nombre": "dx_grupo1",
    "dxprincipal_agrupacion2_nombre": "dx_grupo2",
    "administradora_consolidada": "administradora",
    "sum_atenciones": "atenciones",
}
REQUERIDAS = {"anio", "quinquenio", "atenciones", "dx_grupo1", "dx_grupo2", "localidad"}
OPCIONALES = ["sexo", "localidad_cod", "tipo_afiliacion", "tipo_atencion", "administradora"]


# ------------------------------------------------------------------ utilidades
def norm_txt(s):
    """Minusculas, sin tildes, sin codigo numerico inicial: '01 - Usaquén' -> 'usaquen'."""
    if s is None or pd.isna(s):
        return None
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"^\s*\d+\s*[-.:)]?\s*", "", s)
    return re.sub(r"\s+", " ", s).strip().lower() or None


def parse_edad(x):
    """'20-24' -> (20, 24) | '80 y mas' -> (80, None) | '35' -> (35, 35) | 'Total' -> (None, None)."""
    s = str(x)
    nums = re.findall(r"\d+", s)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    if len(nums) == 1:
        n = int(nums[0])
        if re.search(r"m[aá]s|\+", s, re.I):
            return n, None
        if re.search(r"menor|<", s, re.I):
            return 0, max(n - 1, 0)
        return n, n
    return None, None


def es_excel(nombre: str) -> bool:
    return nombre.lower().endswith((".xlsx", ".ods"))


def _engine(nombre: str):
    return "odf" if nombre.lower().endswith(".ods") else None


def _detectar(data: bytes):
    """Detecta codificacion y delimitador de un CSV."""
    head = data[:1_000_000]
    if head.lstrip()[:1] == b"<":
        raise ValueError("El archivo parece una pagina HTML, no un CSV (descarga fallida).")
    try:
        head[:-4].decode("utf-8-sig")
        enc = "utf-8-sig"
    except UnicodeDecodeError:
        enc = "latin-1"
    primera = head.split(b"\n")[0].decode(enc, errors="ignore")
    sep = max([";", ",", "\t", "|"], key=primera.count)
    return enc, sep


# ------------------------------------------------------------------ RIPS
def leer_rips_bloques(data: bytes, nombre: str, bloque: int = 200_000):
    """Entrega el archivo por bloques para no llenar la memoria."""
    if es_excel(nombre):
        yield pd.read_excel(io.BytesIO(data), dtype=str, engine=_engine(nombre))
        return
    enc, sep = _detectar(data)
    yield from pd.read_csv(io.BytesIO(data), dtype=str, sep=sep, encoding=enc,
                           chunksize=bloque, low_memory=False)


def normalizar_rips(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns=RENAME)
    faltan = REQUERIDAS - set(df.columns)
    if faltan:
        raise ValueError(
            f"Faltan columnas de RIPS: {sorted(faltan)}. Columnas del archivo: {list(df.columns)}"
        )
    for c in OPCIONALES:
        if c not in df.columns:
            df[c] = None
    for c in df.columns:
        df[c] = df[c].astype("string").str.strip()
    df["atenciones"] = pd.to_numeric(df["atenciones"], errors="coerce")
    df["anio"] = pd.to_numeric(df["anio"], errors="coerce")
    return df


def procesar_bloque(raw, anio_min=ANIO_MIN, anio_max=ANIO_MAX, edad_min=EDAD_MIN, edad_max=EDAD_MAX):
    """Devuelve (filas_limpias, filas_rechazadas, estadisticas)."""
    df = normalizar_rips(raw)

    # 1) Calidad: filas invalidas se apartan con su motivo
    motivo = pd.Series("", index=df.index, dtype="object")
    motivo[df["atenciones"].isna()] = "atenciones no numerico"
    motivo[df["atenciones"] < 0] = "atenciones negativo"
    motivo[df["anio"].isna()] = "anio invalido"
    motivo[(df["anio"] < anio_min) | (df["anio"] > anio_max)] = "anio fuera de rango"
    malo = motivo != ""
    rechazadas = df[malo].assign(motivo_rechazo=motivo[malo])
    ok = df[~malo].copy()

    # 2) Edad: se conservan quinquenios COMPLETAMENTE dentro del rango
    lims = {q: parse_edad(q) for q in ok["quinquenio"].dropna().unique()}
    ok["edad_min"] = pd.to_numeric(ok["quinquenio"].map({k: v[0] for k, v in lims.items()}), errors="coerce")
    ok["edad_max"] = pd.to_numeric(ok["quinquenio"].map({k: v[1] for k, v in lims.items()}), errors="coerce")
    n_antes = len(ok)
    ok = ok[(ok["edad_min"] >= edad_min) & (ok["edad_max"] <= edad_max)].copy()
    fuera_edad = n_antes - len(ok)

    # 3) Enfermedad cronica por patron de texto en el diagnostico
    texto = ok["dx_grupo1"].fillna("") + " " + ok["dx_grupo2"].fillna("")
    ok["enfermedad"] = None
    for nombre, patron in ENFERMEDADES.items():
        m = texto.str.contains(patron, case=False, regex=True, na=False)
        ok.loc[m & ok["enfermedad"].isna(), "enfermedad"] = nombre
    n_antes = len(ok)
    ok = ok[ok["enfermedad"].notna()].copy()
    no_cronicas = n_antes - len(ok)

    # 4) Tipos finales
    loc_norm = {x: norm_txt(x) for x in ok["localidad"].dropna().unique()}
    ok["localidad_norm"] = ok["localidad"].map(loc_norm)
    ok["anio"] = ok["anio"].astype(int)
    ok["atenciones"] = ok["atenciones"].round().astype(int)
    ok["edad_min"] = ok["edad_min"].astype(int)
    ok["edad_max"] = ok["edad_max"].astype(int)

    cols = ["anio", "sexo", "quinquenio", "edad_min", "edad_max", "localidad_cod", "localidad",
            "localidad_norm", "tipo_afiliacion", "tipo_atencion", "administradora",
            "dx_grupo1", "dx_grupo2", "enfermedad", "atenciones"]
    stats = {"leidas": len(df), "rechazadas": int(malo.sum()), "fuera_edad": fuera_edad,
             "no_cronicas": no_cronicas, "cargadas": len(ok)}
    return ok[cols], rechazadas, stats


# ------------------------------------------------------------------ Poblacion
def limpiar_col(c) -> str:
    s = str(c).strip()
    m = re.fullmatch(r"(\d{4})\.0", s)
    return m.group(1) if m else s


def listar_hojas(data: bytes, nombre: str):
    return pd.ExcelFile(io.BytesIO(data), engine=_engine(nombre)).sheet_names


def vista_cruda(data: bytes, nombre: str, hoja=None, n: int = 15) -> pd.DataFrame:
    """Primeras filas SIN interpretar encabezado (para ver donde empieza la tabla)."""
    if es_excel(nombre):
        df = pd.read_excel(io.BytesIO(data), sheet_name=hoja or 0, header=None, nrows=n,
                           engine=_engine(nombre))
    else:
        enc, sep = _detectar(data)
        df = pd.read_csv(io.BytesIO(data), sep=sep, encoding=enc, header=None, nrows=n)
    return df.astype(str)


def leer_tabla(data: bytes, nombre: str, hoja=None, header: int = 0) -> pd.DataFrame:
    if es_excel(nombre):
        df = pd.read_excel(io.BytesIO(data), sheet_name=hoja or 0, header=header, engine=_engine(nombre))
    else:
        enc, sep = _detectar(data)
        df = pd.read_csv(io.BytesIO(data), sep=sep, encoding=enc, header=header)
    df.columns = [limpiar_col(c) for c in df.columns]
    return df


def anios_en_columnas(df: pd.DataFrame):
    return [c for c in df.columns if re.fullmatch(r"(19|20)\d{2}", c)]


def a_formato_largo(df: pd.DataFrame, columnas_anio) -> pd.DataFrame:
    ids = [c for c in df.columns if c not in columnas_anio]
    return df.melt(id_vars=ids, value_vars=list(columnas_anio), var_name="anio", value_name="habitantes")


def procesar_poblacion(df: pd.DataFrame, m: dict):
    """m = {'anio','localidad','habitantes'} obligatorias; 'sexo','edad' opcionales (o None)."""
    out = pd.DataFrame({
        "anio": pd.to_numeric(df[m["anio"]], errors="coerce"),
        "localidad": df[m["localidad"]].astype("string").str.strip(),
        "habitantes": pd.to_numeric(df[m["habitantes"]], errors="coerce"),
    })
    out["sexo"] = df[m["sexo"]].astype("string").str.strip() if m.get("sexo") else "Total"
    if m.get("edad"):
        lims = {v: parse_edad(v) for v in df[m["edad"]].dropna().unique()}
        out["edad_min"] = pd.to_numeric(df[m["edad"]].map({k: v[0] for k, v in lims.items()}), errors="coerce")
        out["edad_max"] = pd.to_numeric(df[m["edad"]].map({k: v[1] for k, v in lims.items()}), errors="coerce")
    else:
        out["edad_min"], out["edad_max"] = 0, 120   # sin edades: solo sirve para tasa cruda

    descartar = (out[["anio", "habitantes", "localidad", "edad_min", "edad_max"]].isna().any(axis=1))
    if m.get("sexo"):                                # evitar doble conteo con filas 'Total'
        es_total = out["sexo"].str.lower().isin(["total", "ambos", "ambos sexos"]).fillna(False)
        if (~es_total).any():
            descartar = descartar | es_total
    stats = {"leidas": len(out), "descartadas": int(descartar.sum())}
    out = out[~descartar].copy()
    out["localidad_norm"] = out["localidad"].map({x: norm_txt(x) for x in out["localidad"].unique()})
    for c in ("anio", "edad_min", "edad_max"):
        out[c] = out[c].astype(int)
    out["habitantes"] = out["habitantes"].astype(float)
    stats["cargadas"] = len(out)
    return out[["anio", "localidad", "localidad_norm", "sexo", "edad_min", "edad_max", "habitantes"]], stats