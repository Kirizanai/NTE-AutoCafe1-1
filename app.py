"""
NTE Auto Cafe - interfaz grafica (Dear PyGui) para el bot de la etapa 1-1.
NTE Auto Cafe - Dear PyGui front end for the stage 1-1 bot.
"""

import json
import os
import queue
import threading
import time
from dataclasses import asdict
from datetime import datetime

import cv2
import numpy as np

import nucleo
from i18n import IDIOMAS, set_idioma, t
from nucleo import Ajustes, Bot, CARPETA_DATOS, CARPETA_RECURSOS, VK_F8, VK_F9

ESCALA = nucleo.activar_dpi()          # antes de crear la ventana
import dearpygui.dearpygui as dpg      # noqa: E402

NOMBRE = "NTE Auto Cafe"
VERSION = "1.0.0"
ARCHIVO_AJUSTES = CARPETA_DATOS / "ajustes.json"
VISTA_W, VISTA_H = 640, 360
ESPERA_BOTON = 5                       # s para cambiar al juego tras pulsar Iniciar
MAX_LINEAS_LOG = 300

# Paleta (cafe oscuro + dorado del aro de pedidos)
C_FONDO = (22, 19, 17)
C_PANEL = (33, 28, 25)
C_CAMPO = (47, 40, 36)
C_CAMPO_H = (60, 51, 46)
C_BORDE = (66, 57, 50)
C_TEXTO = (238, 230, 220)
C_TENUE = (150, 138, 126)
C_DORADO = (245, 184, 40)
C_DORADO_H = (255, 203, 82)
C_ROJO = (176, 64, 52)
C_ROJO_H = (198, 82, 68)
C_VERDE = (120, 214, 140)
C_AZUL = (110, 180, 255)
C_AVISO = (240, 170, 90)
C_ERROR = (240, 120, 100)

COLOR_ESTADO = {"MENU": C_AZUL, "JUEGO": C_VERDE, "CLAIM": C_DORADO, "ESPERANDO": C_TENUE}
COLOR_LOG = {"info": C_TEXTO, "ok": C_VERDE, "meta": C_DORADO, "aviso": C_AVISO, "error": C_ERROR}

CAMPOS_BLOQUEABLES = ("in_stamina", "in_costo", "in_monitor", "in_martillo", "in_tmax",
                      "chk_observar", "cmb_ventana", "btn_buscar", "chk_humano",
                      "in_reac_min", "in_reac_max", "cmb_idioma")


def S(v):
    return int(round(v * ESCALA))


def mmss(seg):
    seg = int(seg)
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class App:
    def __init__(self):
        self.cola_log = queue.Queue()
        self.lock = threading.Lock()
        self.estado = {}
        self.vista_nueva = None
        self.hilo = None
        self.parar = threading.Event()
        self.cuenta_hasta = None          # cuenta atras antes de arrancar
        self.teclas_prev = {VK_F8: False, VK_F9: False}
        self.scroll_log = 0
        self.historial = []               # (linea, tipo) para redibujar al cambiar de idioma
        self.idioma_pendiente = None
        self.ajustes = self._cargar_ajustes()
        set_idioma(self.ajustes.get("idioma", "en"))

    # ------------------------------------------------------------------ datos
    def _cargar_ajustes(self):
        datos = {"stamina": 472, "idioma": "en", **asdict(Ajustes())}
        try:
            datos.update(json.loads(ARCHIVO_AJUSTES.read_text(encoding="utf-8")))
        except Exception:
            pass
        return datos

    def _guardar_ajustes(self):
        try:
            ARCHIVO_AJUSTES.write_text(json.dumps(self.ajustes, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _leer_campos(self):
        a = self.ajustes
        a["stamina"] = max(0, dpg.get_value("in_stamina"))
        a["costo"] = max(1, dpg.get_value("in_costo"))
        a["monitor"] = max(1, dpg.get_value("in_monitor"))
        a["intervalo_martillo"] = max(0.1, round(dpg.get_value("in_martillo"), 2))
        a["tiempo_max_juego"] = max(10.0, float(dpg.get_value("in_tmax")))
        a["observar"] = dpg.get_value("chk_observar")
        a["humanizar"] = dpg.get_value("chk_humano")
        r1 = max(0.0, round(dpg.get_value("in_reac_min"), 2))
        r2 = max(0.0, round(dpg.get_value("in_reac_max"), 2))
        a["reaccion_min"], a["reaccion_max"] = min(r1, r2), max(r1, r2)
        v = dpg.get_value("cmb_ventana")
        a["ventana"] = "" if v == t("pantalla_completa") else v
        return a

    def _refrescar_ventanas(self, *_):
        completa = t("pantalla_completa")
        titulos = []
        for _, titulo in nucleo.listar_ventanas(excluir=NOMBRE):
            if titulo not in titulos:
                titulos.append(titulo)
        juego = nucleo.adivinar_ventana_juego(titulos)
        if juego:
            titulos.remove(juego)
            titulos.insert(0, juego)
        actual = dpg.get_value("cmb_ventana")
        deseada = actual or self.ajustes.get("ventana") or completa
        if deseada != completa and deseada not in titulos:
            titulos.insert(0, deseada)      # se mantiene aunque el juego aun no este abierto
        dpg.configure_item("cmb_ventana", items=[completa] + titulos)
        dpg.set_value("cmb_ventana", deseada)
        pista = bool(juego) and deseada == completa
        dpg.set_value("txt_pista", t("pista_juego") if pista else "")
        dpg.configure_item("txt_pista", show=pista)

    # --------------------------------------------------------------- callbacks del bot
    def log(self, msg, tipo="info"):
        linea = f"{datetime.now():%H:%M:%S}  {msg}"
        self.cola_log.put((linea, tipo))
        try:
            with open(nucleo.ARCHIVO_LOG, "a", encoding="utf-8") as f:
                f.write(linea + "\n")
        except Exception:
            pass

    def on_estado(self, d):
        with self.lock:
            self.estado = d

    def on_vista(self, img_bgr):
        with self.lock:
            self.vista_nueva = img_bgr

    # ------------------------------------------------------------------ control
    def corriendo(self):
        return (self.hilo is not None and self.hilo.is_alive()) or self.cuenta_hasta is not None

    def pedir_inicio(self, inmediato=False):
        if self.corriendo():
            return
        a = self._leer_campos()
        self._guardar_ajustes()
        rondas = a["stamina"] // a["costo"]
        if not a["observar"] and rondas <= 0:
            self.log(t("log_sin_stamina", st=a["stamina"], c=a["costo"]), "aviso")
            return
        if inmediato or a["ventana"]:
            self._arrancar()          # en modo ventana el bot trae el juego al frente
        else:
            self.cuenta_hasta = time.time() + ESPERA_BOTON
            self.log(t("log_empiezo_en", n=ESPERA_BOTON))
        self._bloquear_campos(True)

    def _arrancar(self):
        self.cuenta_hasta = None
        a = self.ajustes
        ajustes = Ajustes(**{k: a[k] for k in asdict(Ajustes()).keys()})
        rondas = a["stamina"] // a["costo"]
        self.parar.clear()
        self.estado = {}
        bot = Bot(ajustes, rondas, self.parar, self.log, self.on_estado, self.on_vista)
        self.hilo = threading.Thread(target=bot.correr, daemon=True)
        self.hilo.start()

    def detener(self):
        if self.cuenta_hasta is not None:
            self.cuenta_hasta = None
            self.log(t("log_cancelado"))
        self.parar.set()

    def _bloquear_campos(self, bloquear):
        for tag in CAMPOS_BLOQUEABLES:
            dpg.configure_item(tag, enabled=not bloquear)
        dpg.configure_item("btn_iniciar", enabled=not bloquear)
        dpg.configure_item("btn_detener", enabled=bloquear)

    def _elegir_idioma(self, _sender, valor):
        codigo = next((k for k, v in IDIOMAS.items() if v == valor), "en")
        if codigo != self.ajustes.get("idioma"):
            self.idioma_pendiente = codigo      # se aplica en el bucle principal

    def _aplicar_idioma(self):
        codigo, self.idioma_pendiente = self.idioma_pendiente, None
        if self.corriendo():
            return
        self._leer_campos()
        self.ajustes["idioma"] = codigo
        self._guardar_ajustes()
        set_idioma(codigo)
        dpg.delete_item("principal")
        self._ventana()
        dpg.set_primary_window("principal", True)
        for linea, tipo in self.historial:
            dpg.add_text(linea, parent="caja_log", color=COLOR_LOG.get(tipo, C_TEXTO), wrap=0)
        self.scroll_log = 2
        self._recalcular()
        self._refrescar_ventanas()

    # ------------------------------------------------------------------ estilo
    def _fuentes(self):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidatos = [
            (os.path.join(windir, "Fonts", "segoeui.ttf"), os.path.join(windir, "Fonts", "segoeuib.ttf")),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ]
        self.f_normal = self.f_titulo = self.f_chico = self.f_grande = None
        for normal, negrita in candidatos:
            if os.path.exists(normal):
                negrita = negrita if os.path.exists(negrita) else normal
                with dpg.font_registry():
                    self.f_normal = dpg.add_font(normal, S(16))
                    self.f_chico = dpg.add_font(negrita, S(12))
                    self.f_titulo = dpg.add_font(negrita, S(22))
                    self.f_grande = dpg.add_font(negrita, S(30))
                dpg.bind_font(self.f_normal)
                return
        dpg.set_global_font_scale(ESCALA)

    def _tema(self):
        with dpg.theme() as tema:
            with dpg.theme_component(dpg.mvAll):
                col = dpg.add_theme_color
                col(dpg.mvThemeCol_WindowBg, C_FONDO)
                col(dpg.mvThemeCol_ChildBg, C_PANEL)
                col(dpg.mvThemeCol_PopupBg, C_PANEL)
                col(dpg.mvThemeCol_FrameBg, C_CAMPO)
                col(dpg.mvThemeCol_FrameBgHovered, C_CAMPO_H)
                col(dpg.mvThemeCol_FrameBgActive, C_CAMPO_H)
                col(dpg.mvThemeCol_Text, C_TEXTO)
                col(dpg.mvThemeCol_TextDisabled, C_TENUE)
                col(dpg.mvThemeCol_Border, C_BORDE)
                col(dpg.mvThemeCol_Button, C_CAMPO)
                col(dpg.mvThemeCol_ButtonHovered, C_CAMPO_H)
                col(dpg.mvThemeCol_ButtonActive, C_BORDE)
                col(dpg.mvThemeCol_Header, C_CAMPO)
                col(dpg.mvThemeCol_HeaderHovered, C_CAMPO_H)
                col(dpg.mvThemeCol_HeaderActive, C_CAMPO_H)
                col(dpg.mvThemeCol_CheckMark, C_DORADO)
                col(dpg.mvThemeCol_PlotHistogram, C_DORADO)
                col(dpg.mvThemeCol_ScrollbarBg, C_PANEL)
                col(dpg.mvThemeCol_ScrollbarGrab, C_BORDE)
                col(dpg.mvThemeCol_Separator, C_BORDE)
                sty = dpg.add_theme_style
                sty(dpg.mvStyleVar_WindowPadding, S(16), S(14))
                sty(dpg.mvStyleVar_FramePadding, S(9), S(6))
                sty(dpg.mvStyleVar_ItemSpacing, S(8), S(8))
                sty(dpg.mvStyleVar_FrameRounding, S(6))
                sty(dpg.mvStyleVar_ChildRounding, S(10))
                sty(dpg.mvStyleVar_GrabRounding, S(6))
                sty(dpg.mvStyleVar_PopupRounding, S(6))
                sty(dpg.mvStyleVar_ScrollbarSize, S(10))
                sty(dpg.mvStyleVar_WindowBorderSize, 0)
                sty(dpg.mvStyleVar_ChildBorderSize, 0)
            with dpg.theme_component(dpg.mvButton, enabled_state=False):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (40, 35, 32))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (40, 35, 32))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (105, 96, 88))
            for componente in (dpg.mvInputInt, dpg.mvInputFloat, dpg.mvCombo, dpg.mvCheckbox):
                with dpg.theme_component(componente, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (130, 120, 110))
        dpg.bind_theme(tema)

        def boton(fondo, hover, texto):
            with dpg.theme() as tb:
                with dpg.theme_component(dpg.mvButton):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, fondo)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, hover)
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, hover)
                    dpg.add_theme_color(dpg.mvThemeCol_Text, texto)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, S(8))
                with dpg.theme_component(dpg.mvButton, enabled_state=False):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (40, 35, 32))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (40, 35, 32))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (40, 35, 32))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (105, 96, 88))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, S(8))
            return tb

        self.t_iniciar = boton(C_DORADO, C_DORADO_H, (30, 22, 10))
        self.t_detener = boton(C_ROJO, C_ROJO_H, (255, 240, 235))
        with dpg.theme() as self.t_log:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (26, 22, 20))
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, S(4), S(2))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, S(10), S(8))
        with dpg.theme() as self.t_compacto:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, S(8), S(2))
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, S(4), S(1))

    def _etiqueta(self, texto):
        item = dpg.add_text(texto, color=C_TENUE)
        if self.f_chico:
            dpg.bind_item_font(item, self.f_chico)
        return item

    @staticmethod
    def _tabla_form():
        return dpg.table(header_row=False, policy=dpg.mvTable_SizingFixedFit, pad_outerX=False,
                         borders_innerH=False, borders_outerH=False,
                         borders_innerV=False, borders_outerV=False)

    # ------------------------------------------------------------------ ventana
    def construir(self):
        dpg.create_context()
        self._fuentes()
        self._tema()
        negro = np.zeros((VISTA_H, VISTA_W, 4), np.float32)
        negro[..., 3] = 1.0
        with dpg.texture_registry():
            dpg.add_raw_texture(VISTA_W, VISTA_H, negro.ravel(), format=dpg.mvFormat_Float_rgba,
                                tag="tex_vista")
        self._ventana()
        ico = str(CARPETA_RECURSOS / "icono.ico")
        dpg.create_viewport(title=NOMBRE, width=S(1010), height=S(870), min_width=S(960),
                            min_height=S(760), small_icon=ico, large_icon=ico,
                            clear_color=C_FONDO + (255,))
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("principal", True)
        self._recalcular()
        self._refrescar_ventanas()
        self.log(t("log_listo", app=NOMBRE))

    def _ventana(self):
        a = self.ajustes
        izq_w = S(380)
        vista_w = S(552)
        vista_h = int(vista_w * VISTA_H / VISTA_W)
        campo_w = S(150)
        lbl_w = S(118)

        with dpg.window(tag="principal", no_scrollbar=True):
            # Encabezado
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp):
                dpg.add_table_column()
                dpg.add_table_column(width_fixed=True)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        item = dpg.add_text(NOMBRE, color=C_DORADO)
                        if self.f_titulo:
                            dpg.bind_item_font(item, self.f_titulo)
                        with dpg.group():
                            dpg.add_spacer(height=S(5))
                            dpg.add_text(t("subtitulo", v=VERSION), color=C_TENUE)
                    with dpg.group():
                        dpg.add_spacer(height=S(5))
                        with dpg.group(horizontal=True):
                            with dpg.group():
                                dpg.add_spacer(height=S(1))
                                with dpg.drawlist(width=S(12), height=S(12)):
                                    dpg.draw_circle((S(6), S(6)), S(5), tag="punto",
                                                    fill=C_TENUE, color=C_TENUE)
                            dpg.add_text(t("detenido"), tag="txt_estado_bot")
            dpg.add_spacer(height=S(2))

            with dpg.group(horizontal=True):
                # ---------------- columna izquierda
                with dpg.child_window(width=izq_w, height=-1):
                    self._etiqueta(t("sec_config"))
                    with self._tabla_form():
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=lbl_w)
                        dpg.add_table_column(width_stretch=True)
                        with dpg.table_row():
                            dpg.add_text(t("idioma"), color=C_TENUE)
                            dpg.add_combo(tag="cmb_idioma", items=list(IDIOMAS.values()),
                                          default_value=IDIOMAS.get(a.get("idioma", "en"), "English"),
                                          width=campo_w, callback=self._elegir_idioma)
                        with dpg.table_row():
                            dpg.add_text(t("stamina"), color=C_TENUE)
                            dpg.add_input_int(tag="in_stamina", default_value=a["stamina"], width=campo_w,
                                              step=0, min_value=0, min_clamped=True,
                                              callback=self._recalcular)
                        with dpg.table_row():
                            dpg.add_text(t("costo"), color=C_TENUE)
                            dpg.add_input_int(tag="in_costo", default_value=a["costo"], width=campo_w,
                                              step=0, min_value=1, min_clamped=True,
                                              callback=self._recalcular)
                        with dpg.table_row():
                            dpg.add_text(t("juego_en"), color=C_TENUE)
                            with dpg.group(horizontal=True):
                                dpg.add_combo(tag="cmb_ventana", items=[], width=-S(74))
                                dpg.add_button(label=t("buscar"), tag="btn_buscar", width=S(66),
                                               callback=self._refrescar_ventanas)
                    item = dpg.add_text("", tag="txt_pista", color=C_DORADO, wrap=izq_w - S(36),
                                        show=False)
                    if self.f_chico:
                        dpg.bind_item_font(item, self.f_chico)
                    with dpg.group(horizontal=True):
                        item = dpg.add_text("0", tag="txt_rondas_calc", color=C_DORADO)
                        if self.f_titulo:
                            dpg.bind_item_font(item, self.f_titulo)
                        with dpg.group():
                            dpg.add_spacer(height=S(4))
                            dpg.add_text(t("rondas_lbl"), color=C_TENUE)
                    dpg.add_checkbox(label=t("humanizar"), tag="chk_humano", default_value=a["humanizar"])
                    dpg.add_checkbox(label=t("observar"), tag="chk_observar",
                                     default_value=a["observar"], callback=self._recalcular)
                    with dpg.collapsing_header(label=t("avanzado"), default_open=False):
                        with self._tabla_form():
                            dpg.add_table_column(width_fixed=True, init_width_or_weight=S(150))
                            dpg.add_table_column(width_stretch=True)
                            with dpg.table_row():
                                dpg.add_text(t("monitor"), color=C_TENUE)
                                dpg.add_input_int(tag="in_monitor", default_value=a["monitor"],
                                                  width=S(120), step=1, min_value=1, min_clamped=True)
                            with dpg.table_row():
                                dpg.add_text(t("reaccion"), color=C_TENUE)
                                with dpg.group(horizontal=True):
                                    dpg.add_input_float(tag="in_reac_min", default_value=a["reaccion_min"],
                                                        width=S(62), step=0, format="%.2f")
                                    dpg.add_text(t("a"), color=C_TENUE)
                                    dpg.add_input_float(tag="in_reac_max", default_value=a["reaccion_max"],
                                                        width=S(62), step=0, format="%.2f")
                            with dpg.table_row():
                                dpg.add_text(t("pausa_martillo"), color=C_TENUE)
                                dpg.add_input_float(tag="in_martillo", default_value=a["intervalo_martillo"],
                                                    width=S(120), step=0.05, format="%.2f",
                                                    min_value=0.1, min_clamped=True)
                            with dpg.table_row():
                                dpg.add_text(t("max_partida"), color=C_TENUE)
                                dpg.add_input_float(tag="in_tmax", default_value=a["tiempo_max_juego"],
                                                    width=S(120), step=5, format="%.0f",
                                                    min_value=10, min_clamped=True)

                    dpg.add_spacer(height=S(2))
                    bw = (izq_w - S(16) * 2 - S(8)) // 2
                    with dpg.group(horizontal=True):
                        dpg.add_button(label=t("iniciar"), tag="btn_iniciar", width=bw, height=S(44),
                                       callback=lambda: self.pedir_inicio())
                        dpg.add_button(label=t("detener"), tag="btn_detener", width=bw, height=S(44),
                                       callback=lambda: self.detener(), enabled=False)
                    dpg.bind_item_theme("btn_iniciar", self.t_iniciar)
                    dpg.bind_item_theme("btn_detener", self.t_detener)

                    dpg.add_spacer(height=S(4))
                    dpg.add_separator()
                    dpg.add_spacer(height=S(2))
                    self._etiqueta(t("sec_progreso"))
                    with dpg.group(horizontal=True):
                        item = dpg.add_text("0", tag="txt_hechas")
                        if self.f_grande:
                            dpg.bind_item_font(item, self.f_grande)
                        with dpg.group():
                            dpg.add_spacer(height=S(10))
                            dpg.add_text(t("de_rondas", n=0), tag="txt_total", color=C_TENUE)
                    dpg.add_progress_bar(tag="barra", default_value=0.0, width=-1, height=S(8))
                    with self._tabla_form() as tb:
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=S(130))
                        dpg.add_table_column(width_stretch=True)
                        for clave, tag in (("f_pantalla", "txt_pantalla"), ("f_revenue", "txt_revenue"),
                                           ("f_martillo", "txt_martillo"), ("f_tiempo", "txt_tiempo"),
                                           ("f_restante", "txt_eta")):
                            with dpg.table_row():
                                dpg.add_text(t(clave), color=C_TENUE)
                                dpg.add_text("-", tag=tag)
                    dpg.bind_item_theme(tb, self.t_compacto)
                    dpg.add_spacer(height=S(4))
                    item = dpg.add_text(t("freno"), color=C_TENUE, wrap=izq_w - S(36))
                    if self.f_chico:
                        dpg.bind_item_font(item, self.f_chico)

                # ---------------- columna derecha
                with dpg.child_window(width=-1, height=-1, no_scrollbar=True):
                    with dpg.group(horizontal=True):
                        self._etiqueta(t("sec_vista"))
                        dpg.add_spacer(width=S(6))
                        item = dpg.add_text(t("vista_sub"), color=(110, 100, 92))
                        if self.f_chico:
                            dpg.bind_item_font(item, self.f_chico)
                    dpg.add_image("tex_vista", width=vista_w, height=vista_h)
                    dpg.add_spacer(height=S(2))
                    self._etiqueta(t("sec_registro"))
                    with dpg.child_window(tag="caja_log", width=-1, height=-1):
                        pass
                    dpg.bind_item_theme("caja_log", self.t_log)

    def _recalcular(self, *_):
        st = max(0, dpg.get_value("in_stamina"))
        co = max(1, dpg.get_value("in_costo"))
        if dpg.get_value("chk_observar"):
            dpg.set_value("txt_rondas_calc", "--")
        else:
            dpg.set_value("txt_rondas_calc", str(st // co))
            if not self.corriendo():
                dpg.set_value("txt_total", t("de_rondas", n=st // co))

    # ------------------------------------------------------------------ bucle de la ventana
    def _teclas(self):
        for vk in (VK_F8, VK_F9):
            ahora = nucleo.tecla_presionada(vk)
            if ahora and not self.teclas_prev[vk]:
                if vk == VK_F9:
                    self.pedir_inicio(inmediato=True)
                else:
                    self.detener()
            self.teclas_prev[vk] = ahora

    def _punto(self, color):
        dpg.configure_item("punto", fill=color, color=color)

    def _actualizar(self):
        self._teclas()
        if self.idioma_pendiente:
            self._aplicar_idioma()

        if self.cuenta_hasta is not None:
            falta = self.cuenta_hasta - time.time()
            if falta <= 0:
                self._arrancar()
            else:
                dpg.set_value("txt_estado_bot", t("empieza_en", n=int(falta) + 1))
                self._punto(C_DORADO)

        vivo = self.hilo is not None and self.hilo.is_alive()
        if self.cuenta_hasta is None:
            if vivo:
                obs = self.ajustes.get("observar")
                dpg.set_value("txt_estado_bot", t("observando") if obs else t("en_marcha"))
                self._punto(C_AZUL if obs else C_VERDE)
            else:
                dpg.set_value("txt_estado_bot", t("detenido"))
                self._punto(C_TENUE)
                if self.hilo is not None:
                    self.hilo = None
                    self._bloquear_campos(False)

        # registro
        nuevas = 0
        while not self.cola_log.empty():
            linea, tipo = self.cola_log.get_nowait()
            dpg.add_text(linea, parent="caja_log", color=COLOR_LOG.get(tipo, C_TEXTO), wrap=0)
            self.historial.append((linea, tipo))
            nuevas += 1
        if nuevas:
            del self.historial[:-MAX_LINEAS_LOG]
            hijos = dpg.get_item_children("caja_log", 1) or []
            for h in hijos[:-MAX_LINEAS_LOG]:
                dpg.delete_item(h)
            self.scroll_log = 2
        if self.scroll_log:
            self.scroll_log -= 1
            dpg.set_y_scroll("caja_log", dpg.get_y_scroll_max("caja_log"))

        # vista
        with self.lock:
            img, self.vista_nueva = self.vista_nueva, None
            est = dict(self.estado)
        if img is not None:
            rgba = cv2.cvtColor(cv2.resize(img, (VISTA_W, VISTA_H)), cv2.COLOR_BGR2RGBA)
            dpg.set_value("tex_vista", (rgba.astype(np.float32) / 255.0).ravel())

        # progreso
        if est:
            e = est.get("estado", "ESPERANDO")
            dpg.set_value("txt_pantalla", t(f"est_{e}"))
            dpg.configure_item("txt_pantalla", color=COLOR_ESTADO.get(e, C_TEXTO))
            hechas, total = est.get("rondas_hechas", 0), est.get("rondas", 0)
            dpg.set_value("txt_hechas", str(hechas))
            dpg.set_value("txt_total", t("de_rondas", n=total))
            dpg.set_value("barra", hechas / total if total else 0.0)
            dig = est.get("digitos")
            if not dig:
                dpg.set_value("txt_revenue", "-")
            else:
                txt = t("dig_1") if dig == 1 else t("dig_n", n=dig)
                if dig >= nucleo.DIGITOS_META:
                    txt += " " + t("meta")
                if est.get("cliente"):
                    txt += "  |  " + t("cliente_vista")
                dpg.set_value("txt_revenue", txt)
            dpg.set_value("txt_martillo", str(est.get("martillazos", 0)))
            prom = est.get("promedio")
            tiempo = mmss(est.get("transcurrido", 0))
            if prom:
                tiempo += "  ·  " + t("por_ronda", s=f"{prom:.0f}")
                dpg.set_value("txt_eta", mmss(prom * max(total - hechas, 0)))
            dpg.set_value("txt_tiempo", tiempo)

    def correr(self):
        self.construir()
        while dpg.is_dearpygui_running():
            self._actualizar()
            dpg.render_dearpygui_frame()
        self.parar.set()
        dpg.destroy_context()


def main():
    App().correr()


if __name__ == "__main__":
    main()
