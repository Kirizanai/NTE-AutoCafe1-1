"""
Nucleo del bot: vision con OpenCV, captura de pantalla, clics y la logica del ciclo.

Ciclo: Menu -> 1-1 -> Open Shop -> (cuenta atras) -> martillo 100T con cada cliente
       hasta que Revenue tenga 3 digitos (>=100) -> Salir -> Claim -> Menu
"""

import math
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from i18n import t

# ---------------------------------------------------------------------------
# Rutas (funciona igual como script y como .exe de PyInstaller)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    CARPETA_RECURSOS = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    CARPETA_DATOS = Path(sys.executable).resolve().parent
else:
    CARPETA_RECURSOS = Path(__file__).resolve().parent
    CARPETA_DATOS = CARPETA_RECURSOS

CARPETA_PLANTILLAS = CARPETA_RECURSOS / "plantillas"
CARPETA_CAPTURAS = CARPETA_DATOS / "capturas"
ARCHIVO_LOG = CARPETA_DATOS / "registro.txt"

# ---------------------------------------------------------------------------
# Constantes de deteccion (coordenadas en la resolucion de referencia 3840x2160)
# ---------------------------------------------------------------------------
REF_W, REF_H = 3840, 2160
ESCALA_TRABAJO = 0.5            # template matching a 1920x1080
UMBRAL_PLANTILLA = 0.80
DIGITOS_META = 3                # 39 -> 83 -> 132: tres digitos = meta cumplida

P_ETAPA_1_1 = (343, 781)
P_OPEN_SHOP = (3437, 2009)
P_MARTILLO = (180, 895)
P_SALIR = (92, 92)

R_OPEN_SHOP = (3000, 1850, 3840, 2160)
R_TITULO = (700, 200, 2400, 480)
R_CLAIM = (1900, 1500, 2800, 1850)
R_BARRA = (1440, 75, 2400, 100)
R_INGRESOS = (3300, 205, 3740, 270)
R_ESTRELLA = (3637, 353, 3677, 393)
R_CLIENTES = (384, 216, 2995, 1037)

HSV_BARRA = ((33, 90, 170), (50, 255, 255))
HSV_ANILLO = ((17, 190, 180), (29, 255, 255))
HSV_NUMERO = ((12, 110, 140), (32, 255, 255))
HSV_DORADO = ((12, 90, 150), (35, 255, 255))

VK_F8, VK_F9 = 0x77, 0x78


@dataclass
class Ajustes:
    costo: int = 18                     # stamina por Claim
    monitor: int = 1                    # 1 = principal
    intervalo_martillo: float = 0.40    # s minimos entre martillazos
    tiempo_max_juego: float = 45.0      # si no detecta la meta, sale igual
    tiempo_sin_cliente: float = 6.0     # sin ver clientes: martillo a intervalos fijos
    intervalo_ciego: float = 1.5
    tiempo_max_desconocido: float = 60.0
    observar: bool = False              # True = no hace clics
    ventana: str = ""                   # titulo de la ventana del juego; "" = pantalla completa
    humanizar: bool = True              # tiempos, recorrido y punto de clic al azar
    reaccion_min: float = 1.0           # s desde que aparece el cliente hasta el martillazo
    reaccion_max: float = 3.0


class Terminado(Exception):
    def __init__(self, msg, tipo="info"):
        super().__init__(msg)
        self.tipo = tipo


class EsperarVentana(Exception):
    """La ventana del juego no se puede capturar ahora (minimizada)."""


def guardar_captura(frame, motivo):
    CARPETA_CAPTURAS.mkdir(exist_ok=True)
    ruta = CARPETA_CAPTURAS / f"{datetime.now():%Y%m%d_%H%M%S}_{motivo}.png"
    cv2.imwrite(str(ruta), frame)
    return ruta


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------
@dataclass
class Deteccion:
    estado: str
    p_open_shop: float
    p_titulo: float
    p_claim: float
    pos_open_shop: tuple
    pos_claim: tuple
    barra: float
    cliente: bool
    pos_cliente: tuple
    digitos: int
    estrella: bool
    ancho_numero: float = 0.0
    chico: np.ndarray = field(default=None, repr=False)
    escala: float = ESCALA_TRABAJO


def _rec(frame, region):
    h, w = frame.shape[:2]
    sx, sy = w / REF_W, h / REF_H
    x1, y1, x2, y2 = region
    return frame[int(y1 * sy):int(y2 * sy), int(x1 * sx):int(x2 * sx)]


def _mascara(bgr, rango):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array(rango[0]), np.array(rango[1]))


class Vision:
    def __init__(self):
        self.originales = {}
        self._cache = {}
        for nombre in ("open_shop", "titulo_1_1", "claim"):
            ruta = CARPETA_PLANTILLAS / f"{nombre}.png"
            img = cv2.imdecode(np.fromfile(str(ruta), np.uint8), cv2.IMREAD_GRAYSCALE) \
                if ruta.exists() else None
            if img is None:
                raise RuntimeError(t("err_plantilla", r=ruta))
            self.originales[nombre] = img

    def _plantilla(self, nombre, e):
        clave = (nombre, round(e, 4))
        tpl = self._cache.get(clave)
        if tpl is None:
            tpl = cv2.resize(self.originales[nombre], None, fx=e, fy=e, interpolation=cv2.INTER_AREA)
            self._cache[clave] = tpl
        return tpl

    def _buscar(self, gris, nombre, region, e):
        x1, y1, x2, y2 = [int(v * e) for v in region]
        zona = gris[y1:y2, x1:x2]
        tpl = self._plantilla(nombre, e)
        if zona.shape[0] < tpl.shape[0] or zona.shape[1] < tpl.shape[1]:
            return 0.0, None
        res = cv2.matchTemplate(zona, tpl, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, (mx, my) = cv2.minMaxLoc(res)
        return float(maxv), ((x1 + mx + tpl.shape[1] / 2) / e, (y1 + my + tpl.shape[0] / 2) / e)

    def analizar(self, frame):
        h, w = frame.shape[:2]
        # Escala de trabajo: 1920x1080 como maximo; por debajo se usa la imagen tal cual
        # (las plantillas se achican a esa escala en vez de agrandar la captura).
        e = min(ESCALA_TRABAJO, w / REF_W)
        tw, th = int(round(REF_W * e)), int(round(REF_H * e))
        chico = frame if (w, h) == (tw, th) else cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        gris = cv2.cvtColor(chico, cv2.COLOR_BGR2GRAY)

        p_os, pos_os = self._buscar(gris, "open_shop", R_OPEN_SHOP, e)
        p_ti, _ = self._buscar(gris, "titulo_1_1", R_TITULO, e)
        p_cl, pos_cl = self._buscar(gris, "claim", R_CLAIM, e)

        m = _mascara(_rec(frame, R_BARRA), HSV_BARRA)
        barra = float(np.count_nonzero(m)) / max(m.size, 1)

        # Burbuja de pedido: aro dorado de ~200 px (en 4K)
        cliente, pos_cliente = False, None
        x1, y1, x2, y2 = [int(v * e) for v in R_CLIENTES]
        m = _mascara(chico[y1:y2, x1:x2], HSV_ANILLO)
        n, _, stats, cents = cv2.connectedComponentsWithStats(m)
        for i in range(1, n):
            cw, ch, area = stats[i, 2] / e, stats[i, 3] / e, stats[i, 4] / (e * e)
            if 90 <= max(cw, ch) <= 340 and area >= 1500:
                cliente = True
                pos_cliente = ((x1 + cents[i][0]) / e, (y1 + cents[i][1]) / e)
                break

        # Digitos naranjas del numero de Revenue (columnas separadas)
        sx, sy = w / REF_W, h / REF_H
        m = _mascara(_rec(frame, R_INGRESOS), HSV_NUMERO)
        cols = (m > 0).sum(axis=0) >= max(1, round(2 * sy))
        corridas, inicio = [], None
        for i, v in enumerate(list(cols) + [False]):
            if v and inicio is None:
                inicio = i
            elif not v and inicio is not None:
                if i - inicio >= max(2, round(4 * sx)):
                    corridas.append((inicio, i))
                inicio = None
        # A baja resolucion los digitos se pegan, asi que tambien se estima por el ancho
        # total (en unidades de 4K: 1 digito ~24, "83" ~54, "132" ~70).
        ancho_ref = (corridas[-1][1] - corridas[0][0]) / sx if corridas else 0.0
        por_ancho = 3 if ancho_ref >= 62 else 2 if ancho_ref >= 34 else 1 if ancho_ref > 0 else 0
        digitos = max(len(corridas), por_ancho)

        m = _mascara(_rec(frame, R_ESTRELLA), HSV_DORADO)
        estrella = float(np.count_nonzero(m)) / max(m.size, 1) > 0.25

        if p_cl >= UMBRAL_PLANTILLA:
            estado = "CLAIM"
        elif p_os >= UMBRAL_PLANTILLA:
            estado = "MENU"
        elif barra >= 0.25:
            estado = "JUEGO"
        else:
            estado = "ESPERANDO"

        return Deteccion(estado, p_os, p_ti, p_cl, pos_os, pos_cl, barra,
                         cliente, pos_cliente, digitos, estrella, ancho_ref, chico, e)


COLORES_ESTADO = {            # BGR
    "MENU": (255, 180, 80),
    "JUEGO": (90, 220, 120),
    "CLAIM": (60, 200, 255),
    "ESPERANDO": (170, 170, 170),
}


def dibujar_vista(d, ancho=640):
    """Miniatura de lo que ve el bot, con las zonas que esta mirando."""
    img = d.chico
    k = ancho / img.shape[1]
    vista = cv2.resize(img, (ancho, int(img.shape[0] * k)), interpolation=cv2.INTER_AREA)
    s = k * d.escala           # de coordenadas de referencia a la miniatura

    def rect(r, color, grosor=2):
        cv2.rectangle(vista, (int(r[0] * s), int(r[1] * s)), (int(r[2] * s), int(r[3] * s)), color, grosor)

    def punto(p, color, radio=6):
        cv2.circle(vista, (int(p[0] * s), int(p[1] * s)), radio, (0, 0, 0), -1)
        cv2.circle(vista, (int(p[0] * s), int(p[1] * s)), radio - 2, color, -1)

    verde, amarillo, gris = (90, 220, 120), (60, 200, 255), (150, 150, 150)
    if d.estado == "JUEGO":
        rect(R_CLIENTES, gris, 1)
        meta = d.digitos >= DIGITOS_META or d.estrella
        rect(R_INGRESOS, verde if meta else amarillo)
        punto(P_MARTILLO, amarillo if d.cliente else gris, 8)
        punto(P_SALIR, verde if meta else gris)
        if d.pos_cliente:
            cv2.circle(vista, (int(d.pos_cliente[0] * s), int(d.pos_cliente[1] * s)),
                       int(110 * s), verde, 2)
    elif d.estado == "MENU":
        rect(R_TITULO, verde if d.p_titulo >= UMBRAL_PLANTILLA else amarillo)
        punto(P_ETAPA_1_1, amarillo)
        if d.pos_open_shop:
            punto(d.pos_open_shop, verde, 8)
    elif d.estado == "CLAIM" and d.pos_claim:
        punto(d.pos_claim, verde, 8)

    color = COLORES_ESTADO.get(d.estado, gris)
    etiqueta = t(f"corto_{d.estado}")
    cv2.rectangle(vista, (0, 0), (len(etiqueta) * 13 + 20, 28), (20, 20, 20), -1)
    cv2.putText(vista, etiqueta, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
    return vista


# ---------------------------------------------------------------------------
# Humanizacion
# ---------------------------------------------------------------------------
def azar(a, b):
    """Valor aleatorio entre a y b, cargado hacia el tercio inferior (como un tiempo de reaccion)."""
    if b <= a:
        return a
    return a + (b - a) * random.betavariate(2.0, 3.0)


def desvio(punto, radio):
    """Punto aleatorio dentro de un boton: gaussiano alrededor del centro, sin salirse del radio."""
    rx, ry = radio
    dx = max(-rx, min(rx, random.gauss(0, rx / 2.2)))
    dy = max(-ry, min(ry, random.gauss(0, ry / 2.2)))
    return punto[0] + dx, punto[1] + dy


def trayectoria(x0, y0, x1, y1, diag):
    """
    Camino del mouse de (x0, y0) a (x1, y1): curva de Bezier con puntos de control al azar
    y perfil de velocidad de minimo tiron (arranca rapido, frena al llegar).
    Devuelve [(x, y, espera_en_s), ...] terminando exactamente en el destino.
    """
    dist = math.hypot(x1 - x0, y1 - y0)
    if dist < 3:
        return [(x1, y1, 0.0)]
    dur = 0.07 + 0.32 * min(dist / max(diag, 1), 1.0) ** 0.6 + random.uniform(0.0, 0.07)
    pasos = max(6, int(dur / 0.008))
    # vector perpendicular para curvar el camino
    px, py = -(y1 - y0) / dist, (x1 - x0) / dist
    c1 = random.uniform(-0.18, 0.18) * dist
    c2 = random.uniform(-0.12, 0.12) * dist
    cx1, cy1 = x0 + (x1 - x0) * 0.3 + px * c1, y0 + (y1 - y0) * 0.3 + py * c1
    cx2, cy2 = x0 + (x1 - x0) * 0.7 + px * c2, y0 + (y1 - y0) * 0.7 + py * c2
    puntos = []
    for i in range(1, pasos + 1):
        s = i / pasos
        t = 10 * s ** 3 - 15 * s ** 4 + 6 * s ** 5
        u = 1 - t
        x = u ** 3 * x0 + 3 * u * u * t * cx1 + 3 * u * t * t * cx2 + t ** 3 * x1
        y = u ** 3 * y0 + 3 * u * u * t * cy1 + 3 * u * t * t * cy2 + t ** 3 * y1
        puntos.append((x, y, dur / pasos))
    puntos[-1] = (x1, y1, 0.0)
    return puntos


# Radio aleatorio de cada boton (unidades de 4K): el clic cae en cualquier parte de esa zona
RADIO_ETAPA = (110, 30)
RADIO_OPEN_SHOP = (110, 24)
RADIO_MARTILLO = (40, 40)
RADIO_SALIR = (18, 18)
RADIO_CLAIM = (120, 24)


# ---------------------------------------------------------------------------
# Windows: DPI, pantalla, mouse y teclas
# ---------------------------------------------------------------------------
def activar_dpi():
    """Pixeles reales (sin escalado de Windows). Devuelve el factor de escala del sistema."""
    if sys.platform != "win32":
        return 1.0
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def tecla_presionada(vk):
    if sys.platform != "win32":
        return False
    import ctypes
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)


def _user32():
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.IsWindow.argtypes = [wt.HWND]
    u.IsIconic.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    u.WindowFromPoint.argtypes = [wt.POINT]
    u.WindowFromPoint.restype = wt.HWND
    u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    u.GetAncestor.restype = wt.HWND
    return u


def listar_ventanas(excluir=""):
    """Ventanas visibles con titulo y tamano razonable: lista de (hwnd, titulo)."""
    if sys.platform != "win32":
        return []
    import ctypes
    import ctypes.wintypes as wt
    u = _user32()
    res = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        titulo = buf.value.strip()
        r = wt.RECT()
        u.GetClientRect(hwnd, ctypes.byref(r))
        if titulo and titulo != excluir and (r.right >= 320 and r.bottom >= 180 or u.IsIconic(hwnd)):
            res.append((hwnd, titulo))
        return True

    u.EnumWindows(cb, 0)
    return res


def adivinar_ventana_juego(titulos):
    for titulo in titulos:
        if "neverness" in titulo.lower() or "異環" in titulo or "异环" in titulo:
            return titulo
    for titulo in titulos:
        if titulo.strip().upper() == "NTE":
            return titulo
    return ""


class Pantalla:
    """Captura el monitor completo, o solo el area de una ventana si se da su titulo."""

    def __init__(self, monitor=1, ventana=""):
        if sys.platform != "win32":
            raise RuntimeError(t("err_windows"))
        import ctypes
        import ctypes.wintypes
        import mss
        self.ctypes, self.wintypes = ctypes, ctypes.wintypes
        self.user32 = _user32()
        self.sct = mss.mss()
        self.hwnd = None
        self.titulo = ""
        self._ultima_region = 0.0
        if ventana:
            hallada = [v for v in listar_ventanas() if v[1] == ventana] or \
                      [v for v in listar_ventanas() if ventana.lower() in v[1].lower()]
            if not hallada:
                raise RuntimeError(t("err_ventana", v=ventana))
            self.hwnd, self.titulo = hallada[0]
            self._actualizar_region(forzar=True)
        else:
            if monitor >= len(self.sct.monitors):
                monitor = 1
            self.mon = dict(self.sct.monitors[monitor])
            self.ancho, self.alto = self.mon["width"], self.mon["height"]

        c = ctypes

        class MOUSEINPUT(c.Structure):
            _fields_ = [("dx", c.c_long), ("dy", c.c_long), ("mouseData", c.c_ulong),
                        ("dwFlags", c.c_ulong), ("time", c.c_ulong), ("dwExtraInfo", c.c_size_t)]

        class _U(c.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(c.Structure):
            _fields_ = [("type", c.c_ulong), ("u", _U)]

        self._MI, self._U, self._INPUT = MOUSEINPUT, _U, INPUT
        g = self.user32.GetSystemMetrics
        self.vx, self.vy, self.vw, self.vh = g(76), g(77), g(78), g(79)

    def _actualizar_region(self, forzar=False):
        """En modo ventana, sigue el area cliente aunque la ventana se mueva."""
        ahora = time.time()
        if not forzar and ahora - self._ultima_region < 0.5:
            return
        self._ultima_region = ahora
        u, wt, c = self.user32, self.wintypes, self.ctypes
        if not u.IsWindow(self.hwnd):
            raise Terminado(t("err_cerrada"))
        if u.IsIconic(self.hwnd):
            raise EsperarVentana(t("log_minimizada"))
        r = wt.RECT()
        u.GetClientRect(self.hwnd, c.byref(r))
        p = wt.POINT(0, 0)
        u.ClientToScreen(self.hwnd, c.byref(p))
        w, h = max(r.right - r.left, 1), max(r.bottom - r.top, 1)
        self.mon = {"left": p.x, "top": p.y, "width": w, "height": h}
        self.ancho, self.alto = w, h

    def capturar(self):
        if self.hwnd:
            self._actualizar_region()
        img = np.asarray(self.sct.grab(self.mon))
        return np.ascontiguousarray(img[:, :, :3])

    def _enviar(self, flags, dx=0, dy=0):
        inp = self._INPUT(0, self._U(self._MI(dx, dy, 0, flags, 0, 0)))
        self.user32.SendInput(1, self.ctypes.byref(inp), self.ctypes.sizeof(inp))

    def _mover(self, x, y):
        x = max(self.vx + 4, min(self.vx + self.vw - 1, int(round(x))))   # nunca a la esquina
        y = max(self.vy + 4, min(self.vy + self.vh - 1, int(round(y))))
        nx = int((x - self.vx) * 65535 / max(self.vw - 1, 1))
        ny = int((y - self.vy) * 65535 / max(self.vh - 1, 1))
        self._enviar(0x0001 | 0x8000 | 0x4000, nx, ny)   # mover, absoluto, escritorio virtual

    def clic(self, x_ref, y_ref, humano=False):
        """Clic en coordenadas de referencia. Devuelve False si otra ventana tapa el punto."""
        x = self.mon["left"] + x_ref * self.ancho / REF_W
        y = self.mon["top"] + y_ref * self.alto / REF_H
        libre = True
        if self.hwnd:
            debajo = self.user32.WindowFromPoint(self.wintypes.POINT(int(x), int(y)))
            raiz = self.user32.GetAncestor(debajo, 2) if debajo else None   # GA_ROOT
            libre = raiz == self.hwnd
        if humano:
            pt = self.wintypes.POINT()
            self.user32.GetCursorPos(self.ctypes.byref(pt))
            diag = (self.ancho ** 2 + self.alto ** 2) ** 0.5
            for px, py, espera in trayectoria(pt.x, pt.y, x, y, diag):
                self._mover(px, py)
                time.sleep(espera)
            time.sleep(azar(0.03, 0.09))                   # se asienta antes de presionar
            self._enviar(0x0002)
            time.sleep(azar(0.06, 0.14))                   # duracion de la presion
            self._enviar(0x0004)
        else:
            self._mover(x, y)
            time.sleep(0.03)
            self._enviar(0x0002)
            time.sleep(0.05)
            self._enviar(0x0004)
        return libre

    def traer_al_frente(self):
        if self.hwnd:
            try:
                self.user32.ShowWindow(self.hwnd, 9)          # SW_RESTORE
                self.user32.SetForegroundWindow(self.hwnd)
            except Exception:
                pass

    def mouse_en_esquina(self):
        pt = self.wintypes.POINT()
        self.user32.GetCursorPos(self.ctypes.byref(pt))
        return pt.x <= self.vx + 1 and pt.y <= self.vy + 1

    def cerrar(self):
        try:
            self.sct.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class Bot:
    """
    Corre en un hilo aparte. Se comunica con la interfaz por tres funciones:
      on_log(texto), on_estado(dict), on_vista(imagen_bgr)
    y se detiene cuando se activa el Event `parar`.
    """

    def __init__(self, ajustes, rondas, parar, on_log, on_estado, on_vista):
        self.a = ajustes
        self.rondas = rondas
        self.parar = parar
        self.on_log, self.on_estado, self.on_vista = on_log, on_estado, on_vista
        self.v = None
        self.p = None
        self.rondas_hechas = 0
        self.estado_prev = None
        self.ultimo_conocido = time.time()
        self.ultimo_open_shop = 0.0
        self.fallos_titulo = 0
        self.claim_click = 0.0
        self.claim_pendiente = False
        self.ultima_vista = 0.0
        self.inicio_total = time.time()
        self.duraciones = []
        self.inicio_ronda = None
        self.aviso_tapada = 0.0
        self.aviso_minimizada = False
        self.menu_desde, self.espera_menu = time.time(), 0.0
        self.claim_desde, self.espera_claim = time.time(), 0.0
        self._reiniciar_juego()

    def log(self, msg, tipo="info"):
        self.on_log(msg, tipo)

    def _reiniciar_juego(self):
        self.inicio_juego = time.time()
        self.proximo_martillo = 0.0
        self.cliente_desde = None
        self.cliente_visto = 0.0
        self.reaccion = 0.0
        self.martillazos = 0
        self.vio_cliente = False
        self.aviso_ciego = False
        self.frames_meta = 0
        self.salida_click = 0.0
        self.digitos_prev = None

    # -- utilidades ---------------------------------------------------------
    def _chequear_parada(self):
        if self.parar.is_set():
            raise Terminado(t("log_detenido"))
        if self.p and self.p.mouse_en_esquina():
            raise Terminado(t("log_esquina"))

    def dormir(self, seg):
        fin = time.time() + seg
        while time.time() < fin:
            self._chequear_parada()
            time.sleep(0.03)

    def clic(self, punto, radio=(0, 0)):
        if self.a.observar:
            return
        if self.a.humanizar:
            punto = desvio(punto, radio)
        libre = self.p.clic(punto[0], punto[1], humano=self.a.humanizar)
        if libre is False and time.time() - self.aviso_tapada > 30:
            self.aviso_tapada = time.time()
            self.log(t("log_tapada"), "aviso")

    def pausa(self, a, b, fija):
        """Espera al azar entre a y b si se humaniza; si no, el tiempo fijo."""
        self.dormir(azar(a, b) if self.a.humanizar else fija)

    def _espera_menu(self):
        if not self.a.humanizar:
            return 0.0
        if self.rondas_hechas == 0:
            return azar(0.3, 0.9)
        if random.random() < 0.06:
            seg = azar(4.0, 10.0)
            self.log(t("log_pausa", s=f"{seg:.0f}"))
            return seg
        return azar(0.6, 2.2)

    def leer(self):
        frame = self.p.capturar()
        d = self.v.analizar(frame)
        ahora = time.time()
        if ahora - self.ultima_vista > 0.2:
            self.ultima_vista = ahora
            self.on_vista(dibujar_vista(d))
        return frame, d

    def _publicar(self, d):
        prom = sum(self.duraciones) / len(self.duraciones) if self.duraciones else None
        self.on_estado({
            "estado": d.estado,
            "rondas_hechas": self.rondas_hechas,
            "rondas": self.rondas,
            "martillazos": self.martillazos,
            "digitos": d.digitos if d.estado == "JUEGO" else None,
            "cliente": d.cliente if d.estado == "JUEGO" else None,
            "promedio": prom,
            "transcurrido": time.time() - self.inicio_total,
        })

    # -- ciclo principal ----------------------------------------------------
    def correr(self):
        try:
            self.v = Vision()
            self.p = Pantalla(self.a.monitor, self.a.ventana)
            donde = t("donde_ventana", t=self.p.titulo) if self.p.hwnd else t("donde_completa")
            if abs(self.p.ancho / self.p.alto - 16 / 9) > 0.02:
                self.log(t("log_aviso_aspecto", w=self.p.ancho, h=self.p.alto), "aviso")
            modo = t("modo_observar") if self.a.observar else t("modo_rondas", n=self.rondas)
            self.log(t("log_inicio", donde=donde, w=self.p.ancho, h=self.p.alto, modo=modo), "ok")
            if self.p.hwnd and not self.a.observar:
                self.p.traer_al_frente()
            self._bucle()
        except Terminado as e:
            self.log(str(e), getattr(e, "tipo", "info"))
        except Exception as e:  # cualquier error se muestra en la ventana
            self.log(t("log_error", tipo=type(e).__name__, e=e), "error")
        finally:
            if self.p:
                self.p.cerrar()
            if not self.a.observar:
                self.log(t("log_completadas", d=self.rondas_hechas, n=self.rondas))

    def _bucle(self):
        while True:
            self._chequear_parada()
            try:
                frame, d = self.leer()
            except EsperarVentana as e:
                if not self.aviso_minimizada:
                    self.log(str(e), "aviso")
                    self.aviso_minimizada = True
                self.ultimo_conocido = time.time()
                self.dormir(1.0)
                continue
            self.aviso_minimizada = False
            ahora = time.time()

            if d.estado != self.estado_prev:
                if self.estado_prev == "CLAIM" and self.claim_pendiente:
                    self.rondas_hechas += 1
                    self.claim_pendiente = False
                    if self.inicio_ronda:
                        self.duraciones.append(ahora - self.inicio_ronda)
                    self.inicio_ronda = ahora
                    self.log(t("log_ronda", d=self.rondas_hechas, n=self.rondas), "ok")
                if d.estado == "JUEGO":
                    self._reiniciar_juego()
                    self.log(t("log_partida"))
                elif d.estado != "ESPERANDO":
                    self.log(t("log_pantalla", e=t(f"corto_{d.estado}")))
                if d.estado == "MENU" and not self.a.observar:
                    self.menu_desde, self.espera_menu = ahora, self._espera_menu()
                elif d.estado == "CLAIM":
                    self.claim_desde = ahora
                    self.espera_claim = azar(0.5, 1.4) if self.a.humanizar else 0.0
                self.estado_prev = d.estado

            if d.estado != "ESPERANDO":
                self.ultimo_conocido = ahora
            self._publicar(d)

            if self.a.observar:
                time.sleep(0.15)
                continue

            if d.estado == "MENU":
                self._menu()
            elif d.estado == "JUEGO":
                self._juego(d, frame, ahora)
            elif d.estado == "CLAIM":
                self._claim(d, ahora)
            elif ahora - self.ultimo_conocido > self.a.tiempo_max_desconocido:
                ruta = guardar_captura(frame, "desconocida")
                raise Terminado(t("log_desconocida", s=f"{self.a.tiempo_max_desconocido:.0f}",
                                  f=ruta.name), tipo="error")
            time.sleep(0.10)

    def _menu(self):
        if self.rondas_hechas >= self.rondas:
            raise Terminado(t("log_listo_rondas", n=self.rondas_hechas), tipo="ok")
        if time.time() - self.ultimo_open_shop < 4:
            return
        if time.time() - self.menu_desde < self.espera_menu:
            return
        if self.inicio_ronda is None:
            self.inicio_ronda = time.time()
        self.clic(P_ETAPA_1_1, RADIO_ETAPA)
        self.pausa(0.6, 1.3, 1.0)
        frame, d2 = self.leer()
        if d2.estado != "MENU":
            return
        if d2.p_titulo < UMBRAL_PLANTILLA:
            self.fallos_titulo += 1
            self.log(t("log_sin_1_1", p=f"{d2.p_titulo:.2f}"), "aviso")
            if self.fallos_titulo >= 3:
                ruta = guardar_captura(frame, "sin_1_1")
                raise Terminado(t("log_no_1_1", f=ruta.name), tipo="error")
            return
        self.fallos_titulo = 0
        self.log(t("log_1_1_ok"))
        self.clic(d2.pos_open_shop or P_OPEN_SHOP, RADIO_OPEN_SHOP)
        self.ultimo_open_shop = time.time()

    def _juego(self, d, frame, ahora):
        seg = ahora - self.inicio_juego
        if d.digitos != self.digitos_prev:
            if self.digitos_prev is not None:
                self.log(t("log_digitos", n=d.digitos))
            self.digitos_prev = d.digitos
        if ahora - self.salida_click < 3:
            return

        meta = d.digitos >= DIGITOS_META or d.estrella
        self.frames_meta = self.frames_meta + 1 if meta else 0
        if self.frames_meta >= 2:
            self.log(t("log_meta", t=f"{seg:.1f}", n=self.martillazos), "meta")
            self.pausa(0.35, 1.1, 0.4)
            self.clic(P_SALIR, RADIO_SALIR)
            self.salida_click = time.time()
            return

        if seg > self.a.tiempo_max_juego:
            ruta = guardar_captura(frame, "meta_no_detectada")
            self.log(t("log_meta_no", s=f"{self.a.tiempo_max_juego:.0f}", f=ruta.name), "aviso")
            self.clic(P_SALIR, RADIO_SALIR)
            self.salida_click = time.time()
            return

        h = self.a.humanizar
        if d.cliente:
            self.vio_cliente = True
            self.cliente_visto = ahora
            if self.cliente_desde is None:
                # tiempo de reaccion antes del primer martillazo a este cliente
                self.cliente_desde = ahora
                self.reaccion = azar(self.a.reaccion_min, self.a.reaccion_max) if h else 0.0
            if ahora - self.cliente_desde >= self.reaccion and ahora >= self.proximo_martillo:
                self.clic(P_MARTILLO, RADIO_MARTILLO)
                self.martillazos += 1
                base = self.a.intervalo_martillo
                self.proximo_martillo = time.time() + (base + azar(0.0, base) if h else base)
        else:
            if ahora - self.cliente_visto > 0.3:      # ignora parpadeos de un cuadro
                self.cliente_desde = None
            if (not self.vio_cliente and seg > self.a.tiempo_sin_cliente
                    and ahora >= self.proximo_martillo):
                if not self.aviso_ciego:
                    self.log(t("log_ciego"), "aviso")
                    self.aviso_ciego = True
                self.clic(P_MARTILLO, RADIO_MARTILLO)
                self.martillazos += 1
                base = self.a.intervalo_ciego
                self.proximo_martillo = time.time() + (azar(base * 0.8, base * 1.5) if h else base)

    def _claim(self, d, ahora):
        if ahora - self.claim_desde < self.espera_claim:
            return
        if ahora - self.claim_click < 3:
            return
        self.log(t("log_claim"))
        self.clic(d.pos_claim, RADIO_CLAIM)
        self.claim_click = ahora
        self.claim_pendiente = True
