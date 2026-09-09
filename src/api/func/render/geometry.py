# render/geometry.py — la geometria de la ESCENA: zonas poligonales.
#
# Este modulo es EL SEGUNDO CAJON de StreamSession, y esa division es la decision de
# fondo del trabajo de zonas (spec 2026-08-28 §3). Lo que ya vivia en la sesion —
# tracker, suavizador, trazas— se olvida cuando cambia el modelo, porque todo eso
# depende de tracker_id y unas identidades nacidas con otro pipeline no significan
# nada. La geometria es al reves: describe LA ESCENA, no el modelo. Comparar dos
# modelos sobre la misma zona es exactamente el caso de uso del banco de pruebas, asi
# que borrar el poligono al cambiar de modelo seria destruir el experimento.
#
# De ahi la regla: sync() vacia el cajon del tracker y NO toca este.
#
# Lo que si mata la geometria es el cierre de la conexion, y eso es a proposito: la
# zona vale para la escena que se esta mirando, y cambiar de fuente cierra el WS. NO
# se persiste en ningun lado (decidido con el usuario el 2026-08-28): persistirla
# obligaria a identificar la fuente con una clave, y el nombre de archivo es fragil
# —"Prueba.mp4" y "Prueba_4x3.mp4" son escenas distintas, y renombrar rompe todo—.
#
# Las coordenadas se guardan NORMALIZADAS en [0,1], fracciones del ancho y del alto.
# El motivo no es portabilidad entre resoluciones (sin persistencia no hay nada que
# portar): es que asi el CLIENTE no necesita saber la resolucion del frame. Su editor
# dibuja sobre un <svg viewBox="0 0 1 1"> y convierte el click con
# (clientX - rect.left) / rect.width — sin canvas.width en ninguna parte. El backend,
# que ya calcula h, w para el auto_scale, multiplica una vez al construir la zona.
#
# stateful=false NO apaga esto. El camino one-shot de imagenes declara que no hay
# memoria TEMPORAL que construir; una zona sobre una foto suelta es perfectamente
# legitima (contar vehiculos en una region de una imagen). Por eso este cajon no se
# gatea por _stateful, y el del tracker si.

import logging
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np
import supervision as sv

from .annotators import annotators_for
from .draw_config import DrawConfig, anclaje_de_zona

logger = logging.getLogger(__name__)

# Topes defensivos. No son limites de producto: son la frontera entre "el usuario
# dibujo algo" y "algo esta mandando basura por el canal de control".
MAX_ZONAS = 8
MIN_VERTICES = 3
MAX_VERTICES = 64
# Area minima en coordenadas normalizadas. 1e-6 es una millonesima del frame: por
# debajo de eso el poligono es una raya (vertices colineales) y sv.PolygonZone
# construiria una mascara que nunca dispara.
AREA_MINIMA = 1e-6
# Cuanto puede diferir el aspecto declarado por el cliente del aspecto real del frame
# antes de que la zona se considere dibujada sobre otra escena (ver _aspecto_coincide).
TOLERANCIA_ASPECTO = 0.01


class GeometriaInvalida(ValueError):
    """
    Geometria que no se puede aceptar, con un motivo legible para el ack.

    Se levanta en el PARSEO, nunca en el hot path: el frame prefiere dibujar algo
    antes que caerse, mismo criterio que un box_style desconocido.
    """


@dataclass(frozen=True)
class Zona:
    """Un poligono de la escena, en coordenadas normalizadas [0,1]."""
    id: str
    puntos: np.ndarray          # (N,2) float32, x e y como fracciones del frame


@dataclass(frozen=True)
class ZonaViva:
    """Una zona ya materializada en pixeles para una resolucion concreta."""
    zona: Zona
    poligono: sv.PolygonZone
    annotator: sv.PolygonZoneAnnotator


def _punto_valido(p) -> Tuple[float, float]:
    """Un par [x, y] finito, recortado a [0,1]."""
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        raise GeometriaInvalida("cada punto tiene que ser un par [x, y]")
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError):
        raise GeometriaInvalida("las coordenadas tienen que ser numeros")
    if not (np.isfinite(x) and np.isfinite(y)):
        raise GeometriaInvalida("las coordenadas tienen que ser finitas")
    # Se RECORTA en vez de rechazar: arrastrar un vertice mas alla del borde del
    # canvas es un gesto legitimo ("que la zona llegue hasta el borde"), y el cliente
    # no tiene por que impedirlo con aritmetica propia.
    return (min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0))


def _area_normalizada(puntos: np.ndarray) -> float:
    """Area del poligono por la formula del cordon de zapato (shoelace), sin signo."""
    x, y = puntos[:, 0], puntos[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def parsear_geometria(payload: dict):
    """
    Valida el mensaje de control del cliente y devuelve (frame_wh, [Zona, ...]).

    El mensaje es DECLARATIVO Y COMPLETO, no incremental: trae toda la geometria
    vigente, no un delta. Un delta obliga a las dos puntas a coincidir sobre un
    historial, y ese es justo el acoplamiento que este proyecto viene evitando.

    Por eso mismo el rechazo es del MENSAJE ENTERO y no por zona: aceptar la mitad
    dejaria al backend con un estado que el cliente no cree tener, que es peor que
    rechazar. Ante error se conserva la geometria anterior y el ack lo explica.
    """
    if not isinstance(payload, dict):
        raise GeometriaInvalida("el mensaje de control tiene que ser un objeto")

    frame = payload.get("frame")
    if not isinstance(frame, dict):
        raise GeometriaInvalida("falta 'frame' con el tamano sobre el que se dibujo")
    try:
        fw, fh = int(frame["w"]), int(frame["h"])
    except (KeyError, TypeError, ValueError):
        raise GeometriaInvalida("'frame' tiene que traer 'w' y 'h' enteros")
    if fw <= 0 or fh <= 0:
        raise GeometriaInvalida("el tamano del frame tiene que ser positivo")

    # Las lineas de conteo todavia no existen (van en la tanda siguiente, reusando
    # este mismo canal con dos vertices). Se rechaza explicito en vez de ignorarlas
    # en silencio: un cliente que las mande y no vea nada no tendria como enterarse.
    lineas = payload.get("lines")
    if lineas:
        raise GeometriaInvalida("las lineas de conteo todavia no estan implementadas")

    crudas = payload.get("zones")
    if crudas is None:
        crudas = []
    if not isinstance(crudas, list):
        raise GeometriaInvalida("'zones' tiene que ser una lista")
    if len(crudas) > MAX_ZONAS:
        raise GeometriaInvalida("como mucho %d zonas a la vez" % MAX_ZONAS)

    zonas = []
    vistos = set()
    for cruda in crudas:
        if not isinstance(cruda, dict):
            raise GeometriaInvalida("cada zona tiene que ser un objeto")
        zid = cruda.get("id")
        if not isinstance(zid, str) or not zid or len(zid) > 32:
            raise GeometriaInvalida("cada zona necesita un 'id' de 1 a 32 caracteres")
        if zid in vistos:
            raise GeometriaInvalida("id de zona repetido: %s" % zid)
        vistos.add(zid)

        puntos = cruda.get("points")
        if not isinstance(puntos, list):
            raise GeometriaInvalida("la zona %s no trae 'points'" % zid)
        if not (MIN_VERTICES <= len(puntos) <= MAX_VERTICES):
            raise GeometriaInvalida(
                "la zona %s tiene %d vertices: van de %d a %d"
                % (zid, len(puntos), MIN_VERTICES, MAX_VERTICES))

        arr = np.array([_punto_valido(p) for p in puntos], dtype=np.float32)
        if _area_normalizada(arr) < AREA_MINIMA:
            # Vertices colineales o encimados: el poligono no encierra nada y su
            # mascara nunca dispararia. Decirlo es mas util que dibujar una raya.
            raise GeometriaInvalida("la zona %s no encierra area" % zid)
        zonas.append(Zona(id=zid, puntos=arr))

    return (fw, fh), zonas


def _aspecto_coincide(frame_wh, resolution_wh) -> bool:
    """
    Si la zona se dibujo sobre un frame de la misma forma que el que esta llegando.

    Es una ASERCION, no una funcion: sin persistencia esto no puede dispararse en uso
    normal —la fuente tiene la resolucion que tiene y la zona muere con ella—, asi que
    si se dispara es un bug. Por eso loguea y no dibuja, y no tiene interfaz: hacerle
    UI seria inflar el panel por un caso que no ocurre.

    Se guardan w y h y no el cociente porque son estrictamente mas informacion: el
    mensaje puede decir "se dibujo sobre 1440x1080 y estan llegando frames de
    1920x1080" en vez de "1.333 != 1.778".

    Tolerancia 1%: los aspectos reales estan lejisimos entre si (4:3 = 1,333 ·
    3:2 = 1,500 · 16:10 = 1,600 · 16:9 = 1,778; el par mas cercano se lleva 4%) y el
    ruido de redondeo es despreciable (854x480 da 1,779 contra 1,778, o sea 0,08%).
    """
    aw, ah = frame_wh
    bw, bh = resolution_wh
    if ah <= 0 or bh <= 0:
        return False
    a, b = aw / ah, bw / bh
    return abs(a - b) <= TOLERANCIA_ASPECTO * max(a, b)


class SceneGeometry:
    """
    El cajon de la ESCENA de una conexion: las zonas y el tamano sobre el que se
    dibujaron. Sobrevive al cambio de modelo; muere con la conexion.

    No es thread-safe y no necesita serlo: el protocolo del stream es de un frame en
    vuelo y el canal de control comparte ese mismo bucle, asi que nunca hay dos
    llamadas simultaneas.
    """

    def __init__(self):
        self._zonas: Tuple[Zona, ...] = ()
        self._frame_wh: Optional[Tuple[int, int]] = None
        # Version de la geometria. Es parte de la clave del cache de zonas vivas, con
        # el mismo criterio (y por la misma razon) que la version de DrawConfig: que
        # el hot path no reconstruya nada mientras nadie toque nada.
        self._version = 0
        self._cache_clave = None
        self._cache_zonas: Tuple[ZonaViva, ...] = ()
        # Para no repetir el aviso de aspecto una vez por frame.
        self._aviso_aspecto = None

    def __len__(self) -> int:
        return len(self._zonas)

    @property
    def vacia(self) -> bool:
        return not self._zonas

    @property
    def zonas(self) -> Tuple[Zona, ...]:
        return self._zonas

    @property
    def frame_wh(self) -> Optional[Tuple[int, int]]:
        """Sobre que tamano de frame se dibujo la geometria vigente."""
        return self._frame_wh

    def set(self, frame_wh, zonas: Sequence[Zona]) -> None:
        """Reemplaza TODA la geometria (el mensaje de control es completo, no un delta)."""
        self._zonas = tuple(zonas)
        self._frame_wh = (int(frame_wh[0]), int(frame_wh[1]))
        self._version += 1
        self._cache_clave = None
        self._cache_zonas = ()
        self._aviso_aspecto = None

    def limpiar(self) -> None:
        """Borra la geometria. NO la llama sync(): solo el cierre de la conexion."""
        self.set((1, 1), ())
        self._frame_wh = None

    def zonas_vivas(self, cfg: DrawConfig, resolution_wh) -> Tuple[ZonaViva, ...]:
        """
        Las zonas materializadas en pixeles para esta resolucion y esta config.

        Se cachean contra (version de la geometria, version de la config, resolucion):
        construir un PolygonZone rasteriza una mascara del tamano del poligono, que no
        es algo para hacer por frame. La version de la config entra porque el color, el
        grosor y el ANCLAJE salen de ahi, y el anclaje cambia que se cuenta.
        """
        if not self._zonas:
            return ()

        if self._frame_wh is not None and not _aspecto_coincide(self._frame_wh, resolution_wh):
            if self._aviso_aspecto != resolution_wh:
                logger.warning(
                    "Zonas ignoradas: se dibujaron sobre %dx%d y estan llegando frames "
                    "de %dx%d. La geometria describe otra escena.",
                    self._frame_wh[0], self._frame_wh[1],
                    resolution_wh[0], resolution_wh[1])
                self._aviso_aspecto = resolution_wh
            return ()

        clave = (self._version, cfg.version, tuple(resolution_wh))
        if self._cache_clave == clave:
            return self._cache_zonas

        w, h = resolution_wh
        ann = annotators_for(cfg, (w, h))
        color = sv.Color.from_hex(cfg.zone_color)
        anclaje = anclaje_de_zona(cfg.zone_anchor)
        vivas = []
        for zona in self._zonas:
            # np.round y no int(): truncar corre el poligono medio pixel hacia el
            # origen, y sobre un frame chico eso se ve.
            pts = np.round(zona.puntos * np.array([w, h], dtype=np.float32)).astype(np.int64)
            poligono = sv.PolygonZone(polygon=pts, triggering_anchors=(anclaje,))
            vivas.append(ZonaViva(
                zona=zona,
                poligono=poligono,
                annotator=sv.PolygonZoneAnnotator(
                    zone=poligono,
                    color=color,
                    thickness=ann.thickness,
                    text_color=sv.Color.from_hex(cfg.label_color),
                    text_scale=ann.text_scale,
                    text_thickness=max(1, ann.thickness - 1),
                ),
            ))

        self._cache_clave = clave
        self._cache_zonas = tuple(vivas)
        return self._cache_zonas
