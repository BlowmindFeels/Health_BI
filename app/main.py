"""HealthBI - aplicacion de escritorio: ETL y analisis de EPOC, asma y diabetes (Bogota)."""
import queue
import threading
import tkinter as tk
import traceback
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import matplotlib

matplotlib.use("TkAgg")
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

import db
import etl

CARPETA = Path.home() / "HealthBI"
NINGUNA = "(ninguna)"
GRAFICOS = ["Atenciones por año", "Atenciones por grupo de edad", "Atenciones por localidad",
            "Tasa por 10.000 hab. (último año)"]


def arbol(parent, alto):
    """Tabla (Treeview) con barras de desplazamiento."""
    marco = ttk.Frame(parent)
    tree = ttk.Treeview(marco, show="headings", height=alto)
    sy = ttk.Scrollbar(marco, orient="vertical", command=tree.yview)
    sx = ttk.Scrollbar(marco, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    sy.pack(side="right", fill="y")
    sx.pack(side="bottom", fill="x")
    tree.pack(side="left", fill="both", expand=True)
    return marco, tree


def llenar_arbol(tree, df, max_filas=300):
    tree.delete(*tree.get_children())
    cols = [str(c) for c in df.columns]
    tree["columns"] = cols
    for c in cols:
        tree.heading(c, text=c)
        tree.column(c, width=120, minwidth=60, stretch=False)
    for fila in df.head(max_filas).itertuples(index=False):
        tree.insert("", "end", values=["" if pd.isna(v) else str(v) for v in fila])


def adivinar(cols, claves, opcional=False):
    for c in cols:
        if any(k in c.lower() for k in claves):
            return c
    return NINGUNA if opcional else (cols[0] if cols else "")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("HealthBI · Enfermedades crónicas (EPOC, asma, diabetes)")
        self.geometry("1180x780")
        self.report_callback_exception = self._error_ui
        CARPETA.mkdir(exist_ok=True)
        try:
            self.engine = db.get_engine()
            db.crear_tablas(self.engine)
        except Exception as e:
            messagebox.showerror("Base de datos", f"No se pudo abrir la base de datos:\n{e}")
            raise SystemExit(1)

        self.q = queue.Queue()
        self.rips_files, self.base, self.tabla_actual = [], None, None
        self.pob_data = self.pob_name = self.pob_df = None

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        tabs = []
        for titulo in ("1 · Cargar RIPS", "2 · Cargar población", "3 · Análisis", "Estado"):
            t = ttk.Frame(self.nb)
            self.nb.add(t, text=titulo)
            tabs.append(t)
        self._tab_rips(tabs[0])
        self._tab_pob(tabs[1])
        self._tab_analisis(tabs[2])
        self._tab_estado(tabs[3])
        self.nb.bind("<<NotebookTabChanged>>", self._al_cambiar_tab)
        self.after(100, self._poll)

    def _error_ui(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("Error", f"{exc.__name__}: {val}")

    # ======================================================== 1. RIPS
    def _tab_rips(self, tab):
        p = ttk.LabelFrame(tab, text="Población objetivo")
        p.pack(fill="x", padx=10, pady=8)
        self.v_amin, self.v_amax = tk.IntVar(value=etl.ANIO_MIN), tk.IntVar(value=etl.ANIO_MAX)
        self.v_emin, self.v_emax = tk.IntVar(value=etl.EDAD_MIN), tk.IntVar(value=etl.EDAD_MAX)
        campos = [("Año mínimo", self.v_amin, 1990, 2100), ("Año máximo", self.v_amax, 1990, 2100),
                  ("Edad mínima", self.v_emin, 0, 120), ("Edad máxima", self.v_emax, 0, 120)]
        for i, (txt, var, lo, hi) in enumerate(campos):
            ttk.Label(p, text=txt).grid(row=0, column=2 * i, padx=(10, 2), pady=6)
            ttk.Spinbox(p, from_=lo, to=hi, textvariable=var, width=7).grid(row=0, column=2 * i + 1, padx=(0, 10))
        ttk.Label(p, foreground="#555", text="El RIPS trae edades por quinquenio: con 18-59 el grupo 15-19 "
                  "se descarta (rango efectivo 20-59).").grid(row=1, column=0, columnspan=8, sticky="w", padx=10, pady=(0, 6))

        f = ttk.Frame(tab)
        f.pack(fill="x", padx=10)
        ttk.Button(f, text="Elegir archivo(s) RIPS…", command=self._elegir_rips).pack(side="left")
        self.lbl_rips = ttk.Label(f, text="Ningún archivo seleccionado")
        self.lbl_rips.pack(side="left", padx=10)
        self.btn_rips = ttk.Button(f, text="Ejecutar ETL y cargar a la base", command=self._ejecutar_rips)
        self.btn_rips.pack(side="right")
        self.prog = ttk.Progressbar(tab, mode="indeterminate")
        self.prog.pack(fill="x", padx=10, pady=6)
        self.log_rips = ScrolledText(tab, height=20, state="disabled", font=("Consolas", 10))
        self.log_rips.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _log(self, msg):
        self.log_rips.config(state="normal")
        self.log_rips.insert("end", msg + "\n")
        self.log_rips.see("end")
        self.log_rips.config(state="disabled")

    def _elegir_rips(self):
        rutas = filedialog.askopenfilenames(title="Archivos RIPS de morbilidad",
                                            filetypes=[("Datos", "*.csv *.xlsx *.ods"), ("Todos", "*.*")])
        if rutas:
            self.rips_files = list(rutas)
            self.lbl_rips.config(text=f"{len(rutas)} archivo(s): " + ", ".join(Path(r).name for r in rutas)[:90])

    def _ejecutar_rips(self):
        if not self.rips_files:
            messagebox.showinfo("RIPS", "Primero elija uno o más archivos RIPS.")
            return
        try:
            params = (self.v_amin.get(), self.v_amax.get(), self.v_emin.get(), self.v_emax.get())
        except tk.TclError:
            messagebox.showerror("Parámetros", "Año y edad deben ser números enteros.")
            return
        archivos, engine = list(self.rips_files), self.engine
        self.btn_rips.config(state="disabled")
        self.prog.start(12)

        def trabajo():
            log = lambda m: self.q.put(("log", m))
            for ruta in archivos:
                nombre = Path(ruta).name
                log(f"\n=== {nombre}")
                total, rechazos, primer = Counter(), [], None
                try:
                    data = Path(ruta).read_bytes()
                    db.borrar_archivo(engine, "hecho_morbilidad", nombre)
                    for i, bloque in enumerate(etl.leer_rips_bloques(data, nombre), 1):
                        if primer is None:
                            primer = bloque.head(50_000)
                        limpio, rech, s = etl.procesar_bloque(bloque, *params)
                        db.cargar_df(engine, limpio.assign(archivo_origen=nombre), "hecho_morbilidad")
                        total.update(s)
                        if sum(len(r) for r in rechazos) < 5000:
                            rechazos.append(rech.head(1000))
                        log(f"  bloque {i}: {total['leidas']:,} filas leídas")
                    db.registrar_log(engine, "RIPS", nombre, total["leidas"], total["cargadas"], total["rechazadas"])
                    log(f"  leídas: {total['leidas']:,} | rechazadas: {total['rechazadas']:,} | "
                        f"fuera de edad: {total['fuera_edad']:,} | otras enfermedades: {total['no_cronicas']:,}")
                    log(f"  CARGADAS A LA BASE: {total['cargadas']:,}")
                    if total["cargadas"] == 0 and primer is not None:
                        log("  ⚠ No se cargó nada: los patrones de ENFERMEDADES (etl.py) no coinciden. "
                            "Diagnósticos más frecuentes del archivo:")
                        dx = etl.normalizar_rips(primer)["dx_grupo2"].value_counts().head(15)
                        for k, v in dx.items():
                            log(f"     {v:>7,}  {k}")
                    if rechazos and sum(len(r) for r in rechazos):
                        ruta_rech = CARPETA / f"rechazadas_{Path(nombre).stem}.csv"
                        pd.concat(rechazos).head(5000).to_csv(ruta_rech, index=False, encoding="utf-8-sig")
                        log(f"  Filas rechazadas (muestra) guardadas en: {ruta_rech}")
                except Exception as e:
                    db.borrar_archivo(engine, "hecho_morbilidad", nombre)   # no dejar cargas a medias
                    log(f"  ERROR en {nombre}: {type(e).__name__}: {e}")
            self.q.put(("fin", None))

        threading.Thread(target=trabajo, daemon=True).start()

    def _poll(self):
        try:
            while True:
                tipo, msg = self.q.get_nowait()
                if tipo == "log":
                    self._log(msg)
                else:
                    self._log("\n✔ Proceso terminado.")
                    self.prog.stop()
                    self.btn_rips.config(state="normal")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ======================================================== 2. Poblacion
    def _tab_pob(self, tab):
        f = ttk.Frame(tab)
        f.pack(fill="x", padx=10, pady=8)
        ttk.Button(f, text="Elegir archivo de población…", command=self._elegir_pob).pack(side="left")
        self.lbl_pob = ttk.Label(f, text="Ningún archivo seleccionado")
        self.lbl_pob.pack(side="left", padx=10)

        g = ttk.Frame(tab)
        g.pack(fill="x", padx=10)
        ttk.Label(g, text="Hoja:").pack(side="left")
        self.cb_hoja = ttk.Combobox(g, state="readonly", width=24)
        self.cb_hoja.pack(side="left", padx=(4, 14))
        self.cb_hoja.bind("<<ComboboxSelected>>", lambda e: self._vista_cruda())
        ttk.Label(g, text="Fila del encabezado (0 = primera):").pack(side="left")
        self.v_header = tk.IntVar(value=0)
        ttk.Spinbox(g, from_=0, to=100, textvariable=self.v_header, width=5).pack(side="left", padx=(4, 14))
        self.v_ancho = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="Años en columnas (formato ancho)", variable=self.v_ancho).pack(side="left", padx=(0, 14))
        ttk.Button(g, text="Interpretar tabla", command=self._interpretar_pob).pack(side="left")

        sub = ttk.Notebook(tab)
        sub.pack(fill="both", expand=True, padx=10, pady=8)
        m1, self.tree_cruda = arbol(sub, 8)
        m2, self.tree_pob = arbol(sub, 8)
        sub.add(m1, text="Vista cruda (ubique la fila del encabezado)")
        sub.add(m2, text="Tabla interpretada")

        mp = ttk.LabelFrame(tab, text="Asignar columnas")
        mp.pack(fill="x", padx=10, pady=(0, 8))
        self.cb_map = {}
        for i, (clave, txt) in enumerate([("anio", "AÑO"), ("localidad", "LOCALIDAD"), ("habitantes", "HABITANTES"),
                                          ("sexo", "SEXO (opcional)"), ("edad", "EDAD (opcional)")]):
            ttk.Label(mp, text=txt).grid(row=0, column=i, padx=8, pady=(6, 0), sticky="w")
            cb = ttk.Combobox(mp, state="readonly", width=22)
            cb.grid(row=1, column=i, padx=8, pady=(0, 8))
            self.cb_map[clave] = cb
        ttk.Button(tab, text="Procesar y cargar población", command=self._cargar_pob).pack(pady=(0, 10))

    def _elegir_pob(self):
        ruta = filedialog.askopenfilename(title="Archivo de población",
                                          filetypes=[("Datos", "*.ods *.xlsx *.csv"), ("Todos", "*.*")])
        if not ruta:
            return
        self.pob_name, self.pob_data = Path(ruta).name, Path(ruta).read_bytes()
        self.lbl_pob.config(text=self.pob_name)
        if etl.es_excel(self.pob_name):
            hojas = etl.listar_hojas(self.pob_data, self.pob_name)
            self.cb_hoja.config(values=hojas, state="readonly")
            self.cb_hoja.set(hojas[0])
        else:
            self.cb_hoja.config(values=["(csv)"], state="disabled")
            self.cb_hoja.set("(csv)")
        self._vista_cruda()

    def _hoja(self):
        return self.cb_hoja.get() if etl.es_excel(self.pob_name or "") else None

    def _vista_cruda(self):
        if self.pob_data is not None:
            llenar_arbol(self.tree_cruda, etl.vista_cruda(self.pob_data, self.pob_name, self._hoja(), 25))

    def _interpretar_pob(self):
        if self.pob_data is None:
            messagebox.showinfo("Población", "Primero elija un archivo.")
            return
        df = etl.leer_tabla(self.pob_data, self.pob_name, self._hoja(), int(self.v_header.get()))
        if self.v_ancho.get():
            cols = etl.anios_en_columnas(df)
            if not cols:
                messagebox.showwarning("Formato ancho", "No encontré columnas con años (ej. 2020, 2021…) en el encabezado.")
                return
            df = etl.a_formato_largo(df, cols)
        self.pob_df = df
        llenar_arbol(self.tree_pob, df)
        cols = list(df.columns)
        guess = {"anio": adivinar(cols, ["año", "ano", "anio", "year", "vigencia"]),
                 "localidad": adivinar(cols, ["localidad"]),
                 "habitantes": adivinar(cols, ["poblaci", "habitantes", "total"]),
                 "sexo": adivinar(cols, ["sexo", "genero", "género"], True),
                 "edad": adivinar(cols, ["edad", "grupo"], True)}
        for k, cb in self.cb_map.items():
            cb.config(values=cols if k in ("anio", "localidad", "habitantes") else [NINGUNA] + cols)
            cb.set(guess[k])

    def _cargar_pob(self):
        if self.pob_df is None:
            messagebox.showinfo("Población", "Primero pulse 'Interpretar tabla'.")
            return
        mapa = {k: cb.get() for k, cb in self.cb_map.items()}
        mapa = {k: (None if v == NINGUNA else v) for k, v in mapa.items()}
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            limpio, s = etl.procesar_poblacion(self.pob_df, mapa)
            db.borrar_archivo(self.engine, "poblacion_localidad", self.pob_name)
            db.cargar_df(self.engine, limpio.assign(archivo_origen=self.pob_name), "poblacion_localidad")
            db.registrar_log(self.engine, "POBLACION", self.pob_name, s["leidas"], s["cargadas"], s["descartadas"])
        finally:
            self.config(cursor="")
        messagebox.showinfo("Población", f"Cargadas {s['cargadas']:,} filas.\n"
                            f"Descartadas por datos incompletos o filas 'Total': {s['descartadas']:,}.")

    # ======================================================== 3. Analisis
    def _tab_analisis(self, tab):
        top = ttk.Frame(tab)
        top.pack(fill="x", padx=10, pady=8)
        self.lb_enf = self._lista(top, "Enfermedad", 8, 4)
        self.lb_anio = self._lista(top, "Años", 8, 4)
        self.lb_loc = self._lista(top, "Localidad (ninguna = todas)", 28, 4)
        ctl = ttk.Frame(top)
        ctl.pack(side="left", padx=14, anchor="n")
        ttk.Label(ctl, text="Gráfico").pack(anchor="w")
        self.cb_graf = ttk.Combobox(ctl, values=GRAFICOS, state="readonly", width=32)
        self.cb_graf.set(GRAFICOS[0])
        self.cb_graf.pack(pady=(0, 6))
        ttk.Button(ctl, text="Dibujar", command=self._dibujar).pack(fill="x")
        ttk.Button(ctl, text="Recargar datos de la base", command=self._refrescar_analisis).pack(fill="x", pady=4)
        ttk.Button(ctl, text="Exportar tabla (Excel/CSV)…", command=self._exportar).pack(fill="x")
        self.lbl_aviso = ttk.Label(tab, foreground="#a05a00", wraplength=1100, justify="left")
        self.lbl_aviso.pack(fill="x", padx=10)
        self.fig = Figure(figsize=(9, 4.6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, master=tab)
        barra = NavigationToolbar2Tk(self.canvas, tab, pack_toolbar=False)
        barra.pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=10)

    def _lista(self, parent, titulo, ancho, alto):
        marco = ttk.Frame(parent)
        marco.pack(side="left", padx=(0, 10))
        ttk.Label(marco, text=titulo).pack(anchor="w")
        lb = tk.Listbox(marco, selectmode="multiple", exportselection=False, width=ancho, height=7)
        sc = ttk.Scrollbar(marco, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sc.set)
        lb.pack(side="left")
        sc.pack(side="left", fill="y")
        return lb

    @staticmethod
    def _poblar(lb, valores, seleccionar_todo=True):
        valores = [str(v) for v in valores]
        if list(lb.get(0, "end")) == valores:
            return
        lb.delete(0, "end")
        for v in valores:
            lb.insert("end", v)
        if seleccionar_todo:
            lb.selection_set(0, "end")

    @staticmethod
    def _sel(lb):
        return [lb.get(i) for i in lb.curselection()]

    def _refrescar_analisis(self):
        self.base = db.cargar_agregado(self.engine)
        b = self.base
        self._poblar(self.lb_enf, sorted(b["enfermedad"].unique()))
        self._poblar(self.lb_anio, sorted(b["anio"].unique()))
        self._poblar(self.lb_loc, sorted(b["localidad"].dropna().unique()), seleccionar_todo=False)
        self._dibujar()

    def _filtrado(self):
        b = self.base
        f = b[b["enfermedad"].isin(self._sel(self.lb_enf)) & b["anio"].isin([int(a) for a in self._sel(self.lb_anio)])]
        locs = self._sel(self.lb_loc)
        return f[f["localidad"].isin(locs)] if locs else f

    def _dibujar(self):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        self.tabla_actual = None
        aviso = ""
        if self.base is None or self.base.empty:
            ax.text(0.5, 0.5, "Aún no hay datos.\nCargue un archivo RIPS en la pestaña 1.", ha="center", va="center")
            ax.axis("off")
        else:
            f, tipo = self._filtrado(), self.cb_graf.get()
            if f.empty:
                ax.text(0.5, 0.5, "Sin datos para los filtros elegidos", ha="center", va="center")
                ax.axis("off")
            elif tipo == GRAFICOS[0]:
                t = f.pivot_table(index="anio", columns="enfermedad", values="atenciones", aggfunc="sum")
                t.plot(ax=ax, marker="o")
                ax.set_xticks(list(t.index))
                ax.set_title("Atenciones por año (2020-2021 afectados por COVID-19)")
                ax.set_ylabel("Atenciones")
                self.tabla_actual = t
            elif tipo == GRAFICOS[1]:
                t = f.pivot_table(index=["edad_min", "quinquenio"], columns="enfermedad",
                                  values="atenciones", aggfunc="sum")
                t.index = t.index.get_level_values("quinquenio")
                t.plot(kind="bar", ax=ax, rot=0)
                ax.set_title("Atenciones por grupo de edad")
                ax.set_ylabel("Atenciones")
                self.tabla_actual = t
            elif tipo == GRAFICOS[2]:
                t = f.pivot_table(index="localidad", columns="enfermedad", values="atenciones", aggfunc="sum")
                t.plot(kind="bar", ax=ax, rot=45)
                for lab in ax.get_xticklabels():
                    lab.set_ha("right")
                ax.set_title("Atenciones por localidad del prestador")
                ax.set_ylabel("Atenciones")
                self.tabla_actual = t
            else:
                t, aviso = self._tasas(f)
                if t is None:
                    ax.text(0.5, 0.5, aviso, ha="center", va="center", wrap=True)
                    ax.axis("off")
                    aviso = ""
                else:
                    anio = int(t["anio"].max())
                    p = t[t["anio"] == anio].pivot_table(index="localidad_norm", columns="enfermedad", values="tasa_10k")
                    p.plot(kind="bar", ax=ax, rot=45)
                    for lab in ax.get_xticklabels():
                        lab.set_ha("right")
                    ax.set_title(f"Atenciones por 10.000 habitantes ({anio})")
                    ax.set_ylabel("Tasa por 10.000 hab.")
                    self.tabla_actual = t.sort_values(["anio", "enfermedad", "tasa_10k"], ascending=[True, True, False])
        self.lbl_aviso.config(text=aviso)
        self.fig.tight_layout()
        self.canvas.draw()

    def _tasas(self, f):
        """Devuelve (tabla_de_tasas | None, aviso)."""
        emin, emax = int(self.base["edad_min"].min()), int(self.base["edad_max"].max())
        pob = db.cargar_poblacion(self.engine, emin, emax)
        aviso, cruda = "", False
        if pob.empty:
            pob = db.cargar_poblacion(self.engine)
            cruda = not pob.empty
        if pob.empty:
            return None, "Cargue la población en la pestaña 2 para calcular tasas."
        casos = f.groupby(["anio", "localidad_norm", "enfermedad"], as_index=False)["atenciones"].sum()
        t = casos.merge(pob, on=["anio", "localidad_norm"], how="inner")
        if t.empty:
            return None, "No hubo coincidencias entre localidades/años del RIPS y de la población."
        t["tasa_10k"] = (t["atenciones"] / t["habitantes"] * 10_000).round(2)
        if cruda:
            aviso += "La población cargada no tiene edades que encajen con el RIPS: tasa cruda (subestimada). "
        sin = sorted(set(casos["localidad_norm"].dropna()) - set(pob["localidad_norm"].dropna()))
        if sin:
            aviso += f"Localidades del RIPS sin población asociada (revise los nombres): {sin}"
        return t, aviso

    def _exportar(self):
        if self.tabla_actual is None:
            messagebox.showinfo("Exportar", "No hay una tabla para exportar. Dibuje un gráfico primero.")
            return
        ruta = filedialog.asksaveasfilename(defaultextension=".xlsx", initialdir=str(CARPETA),
                                            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not ruta:
            return
        if ruta.lower().endswith(".csv"):
            self.tabla_actual.to_csv(ruta, encoding="utf-8-sig")
        else:
            self.tabla_actual.to_excel(ruta)
        messagebox.showinfo("Exportar", f"Tabla guardada en:\n{ruta}")

    # ======================================================== Estado
    def _tab_estado(self, tab):
        self.lbl_estado = ttk.Label(tab, justify="left", font=("Segoe UI", 10))
        self.lbl_estado.pack(anchor="w", padx=10, pady=10)
        ttk.Button(tab, text="Actualizar", command=self._refrescar_estado).pack(anchor="w", padx=10)
        marco, self.tree_log = arbol(tab, 15)
        marco.pack(fill="both", expand=True, padx=10, pady=10)

    def _refrescar_estado(self):
        destino = self.engine.url.render_as_string(hide_password=True)
        self.lbl_estado.config(text=f"Base de datos: {destino}\n"
                               f"Filas en hecho_morbilidad: {db.contar(self.engine, 'hecho_morbilidad'):,}\n"
                               f"Filas en poblacion_localidad: {db.contar(self.engine, 'poblacion_localidad'):,}")
        llenar_arbol(self.tree_log, db.ver_log(self.engine))

    def _al_cambiar_tab(self, _):
        idx = self.nb.index(self.nb.select())
        if idx == 2:
            self._refrescar_analisis()
        elif idx == 3:
            self._refrescar_estado()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()