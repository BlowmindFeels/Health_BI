"""HealthBI: cargar datos, hacer ETL y analizar enfermedades cronicas (EPOC, asma, diabetes)."""
import os
from collections import Counter
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import db
import etl

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

st.set_page_config(page_title="HealthBI - Enfermedades crónicas", layout="wide")
st.title("HealthBI · Enfermedades crónicas (EPOC, asma, diabetes)")


@st.cache_resource
def motor():
    e = db.get_engine()
    db.crear_tablas(e)
    return e


try:
    engine = motor()
except Exception as e:
    st.error(f"No se pudo conectar a la base de datos: {e}")
    st.stop()


@st.cache_data(show_spinner=False)
def _hojas(data, nombre):
    return etl.listar_hojas(data, nombre)


@st.cache_data(show_spinner=False)
def _cruda(data, nombre, hoja):
    return etl.vista_cruda(data, nombre, hoja)


@st.cache_data(show_spinner=False)
def _tabla(data, nombre, hoja, header):
    return etl.leer_tabla(data, nombre, hoja, header)


def elegir_archivo(clave, tipos):
    """Devuelve (nombre, funcion_que_lee_los_bytes) o None."""
    origen = st.radio("Origen del archivo", ["Carpeta datos/", "Subir archivo"],
                      horizontal=True, key=f"origen_{clave}")
    if origen == "Subir archivo":
        up = st.file_uploader("Archivo", type=tipos, key=f"up_{clave}")
        return (up.name, up.getvalue) if up else None
    nombres = []
    if DATA_DIR.exists():
        nombres = sorted(p.name for p in DATA_DIR.iterdir()
                         if p.is_file() and p.suffix.lower().lstrip(".") in tipos)
    if not nombres:
        st.info(f"No hay archivos {tipos} en la carpeta datos/. Copie allí sus archivos y recargue la página.")
        return None
    sel = st.selectbox("Archivo en datos/", nombres, key=f"sel_{clave}")
    return sel, (DATA_DIR / sel).read_bytes


def adivinar(cols, claves, con_ninguna=False):
    for i, c in enumerate(cols):
        if any(k in c.lower() for k in claves):
            return i + (1 if con_ninguna else 0)
    return 0


tab1, tab2, tab3, tab4 = st.tabs(["1 · Cargar RIPS", "2 · Cargar población", "3 · Análisis", "Estado de la base"])

# =========================================================== 1. RIPS
with tab1:
    st.subheader("ETL de morbilidad (RIPS)")
    c1, c2, c3, c4 = st.columns(4)
    anio_min = c1.number_input("Año mínimo", 1990, 2100, etl.ANIO_MIN)
    anio_max = c2.number_input("Año máximo", 1990, 2100, etl.ANIO_MAX)
    edad_min = c3.number_input("Edad mínima", 0, 120, etl.EDAD_MIN)
    edad_max = c4.number_input("Edad máxima", 0, 120, etl.EDAD_MAX)
    st.caption("El RIPS abierto trae edades por quinquenio: con 18-59 el grupo 15-19 se descarta (rango efectivo 20-59).")

    f = elegir_archivo("rips", ["csv", "xlsx", "ods"])
    if f and st.button("Ejecutar ETL y cargar a la base", type="primary"):
        nombre, leer = f
        try:
            data = leer()
            db.borrar_archivo(engine, "hecho_morbilidad", nombre)
            total, rechazos, primer_bloque = Counter(), [], None
            avance = st.empty()
            for i, bloque in enumerate(etl.leer_rips_bloques(data, nombre), 1):
                if primer_bloque is None:
                    primer_bloque = bloque.head(50_000)
                limpio, rechazadas, s = etl.procesar_bloque(
                    bloque, int(anio_min), int(anio_max), int(edad_min), int(edad_max))
                db.cargar_df(engine, limpio.assign(archivo_origen=nombre), "hecho_morbilidad")
                total.update(s)
                if sum(len(r) for r in rechazos) < 5000:
                    rechazos.append(rechazadas.head(1000))
                avance.write(f"Bloque {i} procesado · {total['leidas']:,} filas leídas")
            db.registrar_log(engine, "RIPS", nombre, total["leidas"], total["cargadas"], total["rechazadas"])
            st.success(f"ETL terminado: {nombre}")
            m = st.columns(5)
            m[0].metric("Filas leídas", f"{total['leidas']:,}")
            m[1].metric("Rechazadas (calidad)", f"{total['rechazadas']:,}")
            m[2].metric("Fuera del rango de edad", f"{total['fuera_edad']:,}")
            m[3].metric("Otras enfermedades", f"{total['no_cronicas']:,}")
            m[4].metric("Cargadas a la base", f"{total['cargadas']:,}")
            if total["cargadas"] == 0 and primer_bloque is not None:
                st.warning("No se cargó ninguna fila. Probablemente los patrones de diagnóstico de "
                           "ENFERMEDADES (app/etl.py) no coinciden con su archivo. Estos son los diagnósticos "
                           "más frecuentes que trae:")
                dx = etl.normalizar_rips(primer_bloque)
                st.dataframe(dx["dx_grupo2"].value_counts().head(40).rename("filas"))
                st.dataframe(dx["dx_grupo1"].value_counts().head(40).rename("filas"))
            if rechazos and sum(len(r) for r in rechazos):
                rech = pd.concat(rechazos).head(5000)
                with st.expander(f"Filas rechazadas (muestra de {len(rech):,})"):
                    st.dataframe(rech)
                    st.download_button("Descargar rechazadas (CSV)", rech.to_csv(index=False).encode("utf-8"),
                                       "rechazadas.csv", "text/csv")
        except Exception as e:
            db.borrar_archivo(engine, "hecho_morbilidad", nombre)   # no dejar cargas a medias
            st.error(f"{type(e).__name__}: {e}")

# =========================================================== 2. Poblacion
with tab2:
    st.subheader("ETL de población (denominador para tasas)")
    f = elegir_archivo("pob", ["ods", "xlsx", "csv"])
    if f:
        nombre, leer = f
        try:
            data = leer()
            hoja = None
            if etl.es_excel(nombre):
                hoja = st.selectbox("Hoja", _hojas(data, nombre))
            with st.expander("Vista cruda (para ver en qué fila empieza la tabla)", expanded=True):
                st.dataframe(_cruda(data, nombre, hoja))
            header = st.number_input("Fila del encabezado (0 = primera fila)", 0, 100, 0)
            df = _tabla(data, nombre, hoja, int(header))
            st.write("Tabla interpretada (primeras filas):")
            st.dataframe(df.head(15).astype(str))

            if st.checkbox("Los años están en columnas (formato ancho)"):
                cols_anio = st.multiselect("Columnas de años", list(df.columns),
                                           default=etl.anios_en_columnas(df))
                if cols_anio:
                    df = etl.a_formato_largo(df, cols_anio)
                    st.write("Convertida a formato largo:")
                    st.dataframe(df.head(10).astype(str))

            cols, ninguna = list(df.columns), "(ninguna)"
            a, b, c = st.columns(3)
            col_anio = a.selectbox("Columna de AÑO", cols, index=adivinar(cols, ["año", "ano", "anio", "year", "vigencia"]))
            col_loc = b.selectbox("Columna de LOCALIDAD", cols, index=adivinar(cols, ["localidad"]))
            col_hab = c.selectbox("Columna de HABITANTES", cols, index=adivinar(cols, ["poblaci", "habitantes", "total"]))
            d, e_ = st.columns(2)
            col_sexo = d.selectbox("Columna de SEXO (opcional)", [ninguna] + cols,
                                   index=adivinar(cols, ["sexo", "genero", "género"], True))
            col_edad = e_.selectbox("Columna de EDAD (opcional pero necesaria para tasas por edad)",
                                    [ninguna] + cols, index=adivinar(cols, ["edad", "grupo"], True))

            if st.button("Procesar y cargar población", type="primary"):
                mapa = {"anio": col_anio, "localidad": col_loc, "habitantes": col_hab,
                        "sexo": None if col_sexo == ninguna else col_sexo,
                        "edad": None if col_edad == ninguna else col_edad}
                limpio, s = etl.procesar_poblacion(df, mapa)
                db.borrar_archivo(engine, "poblacion_localidad", nombre)
                db.cargar_df(engine, limpio.assign(archivo_origen=nombre), "poblacion_localidad")
                db.registrar_log(engine, "POBLACION", nombre, s["leidas"], s["cargadas"], s["descartadas"])
                st.success(f"Cargadas {s['cargadas']:,} filas ({s['descartadas']:,} descartadas por datos incompletos o totales).")
                st.dataframe(limpio.head(20))
        except Exception as e:
            st.error(f"{type(e).__name__}: {e}")

# =========================================================== 3. Analisis
with tab3:
    base = db.cargar_agregado(engine)
    if base.empty:
        st.info("Aún no hay datos. Cargue un archivo RIPS en la pestaña 1.")
    else:
        c1, c2, c3 = st.columns(3)
        enf = c1.multiselect("Enfermedad", sorted(base["enfermedad"].unique()), default=sorted(base["enfermedad"].unique()))
        anios_disp = sorted(base["anio"].unique())
        anios = c2.multiselect("Años", anios_disp, default=anios_disp)
        locs = c3.multiselect("Localidad (vacío = todas)", sorted(base["localidad"].dropna().unique()))
        f = base[base["enfermedad"].isin(enf) & base["anio"].isin(anios)]
        if locs:
            f = f[f["localidad"].isin(locs)]

        k = st.columns(3)
        k[0].metric("Atenciones", f"{int(f['atenciones'].sum()):,}")
        k[1].metric("Años", f["anio"].nunique())
        k[2].metric("Localidades", f["localidad"].nunique())

        por_anio = f.groupby(["anio", "enfermedad"], as_index=False)["atenciones"].sum()
        st.plotly_chart(px.line(por_anio, x="anio", y="atenciones", color="enfermedad", markers=True,
                                title="Atenciones por año (2020-2021 afectados por COVID-19)"),
                        use_container_width=True)

        l, r = st.columns(2)
        por_edad = (f.groupby(["quinquenio", "edad_min", "enfermedad"], as_index=False)["atenciones"].sum()
                    .sort_values("edad_min"))
        l.plotly_chart(px.bar(por_edad, x="quinquenio", y="atenciones", color="enfermedad", barmode="group",
                              title="Atenciones por grupo de edad"), use_container_width=True)
        por_loc = f.groupby(["localidad", "enfermedad"], as_index=False)["atenciones"].sum()
        r.plotly_chart(px.bar(por_loc, x="localidad", y="atenciones", color="enfermedad",
                              title="Atenciones por localidad del prestador"), use_container_width=True)

        st.markdown("### Tasa por 10.000 habitantes")
        emin, emax = int(base["edad_min"].min()), int(base["edad_max"].max())
        pob = db.cargar_poblacion(engine, emin, emax)
        cruda = False
        if pob.empty:
            pob = db.cargar_poblacion(engine)
            cruda = not pob.empty
        if pob.empty:
            st.info("Cargue la población en la pestaña 2 para calcular tasas.")
        else:
            if cruda:
                st.warning("La población cargada no tiene edades que encajen con el rango del RIPS: "
                           "la tasa usa la población total (tasa cruda, subestimada).")
            casos = f.groupby(["anio", "localidad_norm", "enfermedad"], as_index=False)["atenciones"].sum()
            tasas = casos.merge(pob, on=["anio", "localidad_norm"], how="inner")
            tasas["tasa_10k"] = (tasas["atenciones"] / tasas["habitantes"] * 10_000).round(2)
            sin_match = sorted(set(casos["localidad_norm"].dropna()) - set(pob["localidad_norm"].dropna()))
            if sin_match:
                st.warning(f"Localidades del RIPS sin población asociada (revise los nombres): {sin_match}")
            if tasas.empty:
                st.info("No hubo coincidencias entre localidades/años del RIPS y de la población.")
            else:
                anio_t = st.selectbox("Año", sorted(tasas["anio"].unique()), index=len(sorted(tasas["anio"].unique())) - 1)
                t = tasas[tasas["anio"] == anio_t]
                st.plotly_chart(px.bar(t, x="localidad_norm", y="tasa_10k", color="enfermedad", barmode="group",
                                       title=f"Atenciones por 10.000 habitantes ({anio_t})"), use_container_width=True)
                st.dataframe(tasas.sort_values(["anio", "enfermedad", "tasa_10k"], ascending=[True, True, False]))
        st.caption("Son atenciones (no personas ni prevalencia). La localidad es la del prestador, no la de residencia; "
                   "el análisis por estrato solo es posible a nivel de localidad.")

# =========================================================== Estado
with tab4:
    a, b = st.columns(2)
    a.metric("Filas en hecho_morbilidad", f"{db.contar(engine, 'hecho_morbilidad'):,}")
    b.metric("Filas en poblacion_localidad", f"{db.contar(engine, 'poblacion_localidad'):,}")
    st.write("Historial de ETL:")
    st.dataframe(db.ver_log(engine))