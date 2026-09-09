# render/export.py — volcado de las detecciones a disco.
#
# POR QUE EXISTE: esto DEVUELVE algo que el paso 3 se llevo. Hasta el 2026-08-26 el
# cliente recibia las filas [x1,y1,x2,y2,conf,cls] por el WebSocket y podia hacer lo que
# quisiera con ellas; desde que el backend compone el frame, el cliente recibe PIXELES y
# el dato numerico dejo de estar al alcance del usuario. El spec de aquel paso lo anoto
# como riesgo 3. La solucion no es volver a mandar JSON al cliente —eso desharia el paso
# entero— sino exportarlo desde donde el dato ahora vive.
#
# Para alguien que use la app para trabajar, ese dato ES el producto: quiere las cajas
# para contarlas, graficarlas o comparar dos modelos.
#
# QUE ES UNA FILA. Una deteccion en un frame, no una trayectoria. Los "movimientos" se
# derivan agrupando por tracker_id — y por eso el export hereda la dependencia del
# Tier B: SIN SEGUIMIENTO PRENDIDO no hay tracker_id, y sin tracker_id no hay nada que
# una la fila del frame 12 con la del 13. El archivo sigue siendo util (las cajas de
# cada instante), pero deja de poder contar el recorrido de nada.
#
# SE ESCRIBE INCREMENTALMENTE, y no es un detalle: sv.JSONSink acumula TODAS las filas
# en memoria y las vuelca al cerrar. Con 'best' sobre material aereo son ~70 detecciones
# por frame; a 30 fps, dos minutos son ~250.000 filas. Se usa el parseador de supervision
# (para que el esquema de la fila sea el suyo y no uno inventado) pero el arreglo JSON se
# arma a mano sobre el archivo abierto, asi la memoria no depende de cuanto dure la
# grabacion.

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import supervision as sv

logger = logging.getLogger(__name__)

# Donde van los archivos. Misma convencion que logs/: una carpeta al lado del codigo,
# creada al vuelo. El backend y el cliente corren en la misma maquina (Electron levanta
# uvicorn), asi que "escribir a disco" y "el usuario lo tiene" son lo mismo.
EXPORTS_DIR = Path(__file__).resolve().parents[3].parent / "exports"

# Nombre de archivo seguro para servirlo despues por HTTP. Mismo criterio que el resto
# de la API (_SAFE_CONFIG_NAME): sin separadores, sin "..".
_NOMBRE_SEGURO = re.compile(r"^[A-Za-z0-9_-]+\.json$")


def nombre_seguro(nombre: str) -> bool:
    """Si el nombre puede servirse sin riesgo de salir de EXPORTS_DIR."""
    return bool(_NOMBRE_SEGURO.match(nombre))


class DetectionExport:
    """
    Un archivo de detecciones en curso, atado a UNA conexion del stream.

    No es thread-safe y no necesita serlo: se escribe desde el mismo bucle que atiende
    los frames, de a uno por vez.
    """

    def __init__(self, modelo: str = None, carpeta: Path = None):
        carpeta = Path(carpeta) if carpeta is not None else EXPORTS_DIR
        carpeta.mkdir(parents=True, exist_ok=True)
        # El nombre lleva la marca de tiempo y el modelo: el usuario sabe cual es cual
        # sin abrirlos.
        #
        # Y lleva un sufijo si hace falta, que NO es paranoia: la marca tiene resolucion
        # de SEGUNDOS, asi que arrancar y parar dos veces seguidas —cosa que se hace sin
        # pensar cuando uno esta probando— produce el mismo nombre, y como el archivo se
        # abre en modo "w" el segundo TRUNCABA al primero. Lo destapo un test.
        marca = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = re.sub(r"[^A-Za-z0-9_-]", "_", modelo or "sin-modelo")
        raiz = re.sub(r"[^A-Za-z0-9_-]", "_", "detecciones-%s-%s" % (marca, base))
        self.nombre = "%s.json" % raiz
        n = 2
        while (carpeta / self.nombre).exists():
            self.nombre = "%s-%d.json" % (raiz, n)
            n += 1
        self.ruta = carpeta / self.nombre
        self.filas = 0
        self.frames = 0
        self._primera = True
        self._inicio = None
        self._archivo = open(self.ruta, "w", encoding="utf-8")
        self._archivo.write("[\n")

    def append(self, detections, modelo: str = None, ahora_ms: float = 0.0) -> None:
        """
        Agrega las detecciones de un frame.

        Se cuenta el frame AUNQUE venga vacio: "en el frame 40 no habia nada" es un dato,
        y si los frames vacios no aparecieran, quien lea el archivo no podria distinguir
        un hueco de una pausa. La fila no se escribe (no hay ninguna), pero el contador
        de frames avanza y el numero de frame de las filas siguientes queda correcto.
        """
        if self._inicio is None:
            self._inicio = ahora_ms
        idx = self.frames
        self.frames += 1
        if detections is None or len(detections) == 0:
            return

        # El esquema de la fila lo define supervision, no nosotros: asi el archivo es
        # el mismo que produciria sv.JSONSink y cualquier cosa que ya lo lea sirve.
        # x_min/y_min/x_max/y_max, class_id, confidence, tracker_id, class_name.
        extra = {"frame": idx, "ms": round(ahora_ms - self._inicio, 1)}
        if modelo:
            extra["model"] = modelo
        for fila in sv.JSONSink.parse_detection_data(detections, extra):
            # UNICA divergencia deliberada del esquema de supervision: sin tracking,
            # su parseador pone tracker_id = "" (string vacio). En CSV eso es correcto
            # —una celda vacia—, pero en JSON un campo numerico con "" es una mentira de
            # tipo: cualquier lector que lo cargue en una tabla se come una columna de
            # tipos mezclados. null dice lo mismo y es cierto.
            if fila.get("tracker_id") == "":
                fila["tracker_id"] = None
            if not self._primera:
                self._archivo.write(",\n")
            self._primera = False
            self._archivo.write(json.dumps(fila, ensure_ascii=False))
            self.filas += 1

    def close(self) -> None:
        """Cierra el arreglo JSON y el archivo. Idempotente."""
        if self._archivo is None:
            return
        try:
            self._archivo.write("\n]\n")
            self._archivo.close()
        except Exception:
            logger.exception("No se pudo cerrar el archivo de exportacion %s", self.ruta)
        finally:
            self._archivo = None

    @property
    def abierto(self) -> bool:
        return self._archivo is not None

    def estado(self) -> dict:
        """Lo que el ack le devuelve al cliente."""
        return {"file": self.nombre, "rows": self.filas, "frames": self.frames}
