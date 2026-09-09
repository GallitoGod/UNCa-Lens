# render/session.py — la memoria POR CONEXION del stream.
#
# Por que existe (Tier B del catalogo de supervision, ver docs/supervision-catalogo.md):
# el tracking, el suavizado y las trazas son las primeras piezas del sistema que
# RECUERDAN el frame anterior. Todo el resto del hot path es sin estado por diseno
# (reforma 8: los pasos del pipeline son closures puros y lo que varia por frame
# viaja en el dict 'meta'). Esa memoria nueva necesita un dueno explicito.
#
# El dueno NO puede ser el ModelController: es un singleton de proceso, asi que dos
# clientes conectados a la vez compartirian el tracker y se mezclarian las
# identidades. Tampoco puede ser el pipeline que arma build_pipeline(): son closures
# stateless a proposito, y meterles estado por frame rompe la reforma 8.
#
# El dueno correcto es la CONEXION del WebSocket, y elegirlo resuelve tres de los
# cuatro casos de reseteo de forma ESTRUCTURAL, sin que nadie tenga que acordarse
# de llamar a nada:
#   - cambio de fuente        -> el cliente cierra el WS y abre otro  -> sesion nueva
#   - reconexion tras caida   -> WS nuevo                             -> sesion nueva
#   - imagen fija (one-shot)  -> WS efimero de un solo frame          -> muere sola
#
# El cuarto NO se resuelve solo, y es el importante: al CAMBIAR DE MODELO el cliente
# mantiene abierto el mismo WebSocket (el effect de useVisionSession depende de la
# fuente, no del modelo). Sin nada mas, el tracker seguiria arrastrando tracks
# nacidos con otro pipeline, donde los class_id significaban otra cosa. Para eso
# esta sync(): el controller lleva un contador de generacion que avanza en cada
# carga y descarga, la sesion recuerda bajo cual nacio, y cuando no coinciden se
# olvida de todo. Es el mismo truco que la 'version' de DrawConfig con el cache de
# annotators, y por la misma razon: se auto-repara, en vez de exigir que el endpoint
# REST le avise a mano a cada conexion viva.
#
# Por que este archivo vive en render/: todo lo que esta memoria guarda existe
# unicamente para cambiar el frame compuesto. El usuario lo prende y lo apaga desde
# el mismo panel Render que los colores, y sus ajustes viajan por el mismo
# POST /config/draw.
#
# Por que el paquete 'trackers' y no sv.ByteTrack: el de supervision esta deprecado
# desde la 0.28 y SE ELIMINA en la 0.31. El reemplazo oficial de Roboflow no arrastra
# ninguna dependencia nueva (verificado: todo lo que pide ya lo trajo supervision) y
# cambia el metodo de update_with_detections() a update().

import logging

import supervision as sv
from trackers import ByteTrackTracker

from .annotators import annotators_for
from .export import DetectionExport
from .geometry import SceneGeometry, parsear_geometria

logger = logging.getLogger(__name__)


class StreamSession:
    """
    Estado que sobrevive de un frame al siguiente DENTRO de una conexion del stream.

    Se crea una por WebSocket aceptado y se descarta al cerrarse. No es thread-safe
    y no necesita serlo: el protocolo del stream es de UN frame en vuelo, asi que
    nunca hay dos llamadas a process() de la misma sesion al mismo tiempo.
    """

    def __init__(self, stateful: bool = True):
        """
        stateful=False para el camino one-shot de imagenes (una foto suelta no es una
        secuencia: no hay nada que rastrear entre frames que no existen). Se declara
        explicito desde el cliente en vez de deducirlo, porque una conexion que
        todavia no recibio su segundo frame es indistinguible de una que nunca lo va
        a recibir.
        """
        self._stateful = bool(stateful)
        # Generacion del pipeline bajo la que se construyo la memoria vigente.
        # None = todavia no se sincronizo con ninguna (sesion recien nacida).
        self._generation = None
        # Tracker vigente, o None si el tracking esta apagado o esta conexion no
        # recuerda nada. Se construye perezosamente: mientras nadie prenda el toggle,
        # esta clase no cuesta nada.
        self._tracker = None
        # Umbral de confianza con el que se construyo el tracker. Sus umbrales se
        # fijan al construirlo, asi que si el usuario mueve el slider hay que rehacerlo.
        self._tracker_conf = None
        # Suavizador vigente y la ventana con la que se armo (cambiarla lo rehace).
        self._smoother = None
        self._smoother_length = None
        # Annotator de trazas. NO puede vivir en el cache global de render/annotators.py
        # aunque se le parezca: tiene estado propio (su atributo 'trace' guarda el
        # recorrido de cada objeto), y ese cache es compartido por todas las conexiones.
        # Se rehace cuando cambia la config o la resolucion, igual que sus hermanos.
        self._trace = None
        self._trace_key = None
        # Identidades ya vistas dentro de cada zona: {id de zona -> set de tracker_id}.
        # VIVE EN ESTE CAJON, con el tracker, aunque la GEOMETRIA de la zona viva en el
        # otro. Los dos hechos son distintos: el poligono describe la escena y sobrevive
        # al cambio de modelo; lo que se conto ahi adentro pertenece a las identidades
        # de ESE modelo, y si se reinician y el contador no, cuenta doble.
        self._zona_vistos = {}

        # ── EL SEGUNDO CAJON: la escena ───────────────────────────────────────
        # Todo lo de arriba depende de tracker_id y se olvida en sync() (cambio de
        # modelo). Esto NO: la geometria describe la escena que se esta mirando, y
        # comparar dos modelos sobre la misma zona es el caso de uso del banco de
        # pruebas. Los dos cajones viven en la misma conexion y mueren juntos con
        # ella, pero se resetean por motivos distintos — por eso son dos.
        #
        # Tampoco se gatea por _stateful: una zona sobre una foto suelta es legitima
        # (contar vehiculos en una region de una imagen). Lo que una foto no tiene es
        # memoria TEMPORAL, que es lo otro.
        self._escena = SceneGeometry()

        # ── Ni un cajon ni el otro: el volcado de detecciones a disco ─────────
        # No es memoria entre frames (no cambia lo que se dibuja) ni geometria de la
        # escena: es un archivo abierto. Vive aca porque nace y muere con la conexion,
        # como todo lo demas, pero NO lo toca sync(): cambiar de modelo a mitad de una
        # grabacion no tiene por que cortarla, y cada fila lleva escrito con que modelo
        # se produjo, asi que el archivo se explica solo.
        self._export = None

    @property
    def stateful(self) -> bool:
        """False si esta conexion es una foto suelta y no debe recordar nada."""
        return self._stateful

    @property
    def generation(self):
        """Generacion del pipeline con la que esta sincronizada, o None si ninguna."""
        return self._generation

    def sync(self, generation: int) -> bool:
        """
        Alinea la sesion con la generacion actual del pipeline.

        Devuelve True si hubo que olvidar la memoria acumulada (cambio de modelo),
        False si venia alineada. Se llama UNA vez por frame, antes de process():
        es una comparacion de enteros, mas barata que cualquier alternativa que
        exija avisarle a la conexion desde afuera.

        La primera sincronizacion de una sesion nueva NO cuenta como reseteo: no
        habia nada que olvidar.
        """
        if self._generation == generation:
            return False
        primera_vez = self._generation is None
        self._generation = generation
        if primera_vez:
            return False
        self.reset()
        return True

    @property
    def tracking_activo(self) -> bool:
        """True si esta conexion esta rastreando de verdad (para tests y logs)."""
        return self._tracker is not None

    @property
    def suavizado_activo(self) -> bool:
        """True si esta conexion esta promediando posiciones (para tests y logs)."""
        return self._smoother is not None

    @property
    def trazas_activas(self) -> bool:
        """True si esta conexion viene acumulando recorridos (para tests y logs)."""
        return self._trace is not None

    def reset(self) -> None:
        """
        Olvida todo lo recordado de frames anteriores, dejando la sesion como recien
        creada (salvo 'stateful', que es una propiedad de la conexion, no del estado).

        OJO: esto vacia SOLO el cajon del tracker. La geometria de la escena
        (self._escena) NO se toca, y no es un olvido: reset() lo llama sync(), o sea
        el cambio de modelo, y una zona no debe morir porque cambio el modelo. Lo
        unico que borra la geometria es el cierre de la conexion, que se lleva la
        sesion entera. Cuando llegue el contador de la linea de conteo va ACA, no en
        el cajon de la escena: depende de tracker_id, y si las identidades se
        reinician y el contador no, cuenta doble.
        """
        # Se descarta el objeto entero en vez de llamar a su reset(): asi el proximo
        # frame lo reconstruye con el umbral que este vigente en ese momento, que es
        # justamente lo que puede haber cambiado.
        self._tracker = None
        self._tracker_conf = None
        self._smoother = None
        self._smoother_length = None
        self._trace = None
        self._trace_key = None
        self._olvidar_conteo()

    def _olvidar_conteo(self, zona_id: str = None) -> None:
        """
        Borra el acumulado de una zona, o de todas.

        LA REGLA QUE NO SE PUEDE ROMPER: esto tiene que correr EN TODOS LOS LUGARES
        donde el tracker se suelta o se reconstruye. Verificado contra la libreria: un
        ByteTrackTracker recien construido vuelve a numerar DESDE 0. Si el conjunto de
        ids ya vistos sobreviviera a esa reconstruccion, el primer objeto nuevo llegaria
        con un id que el conjunto ya tiene y NO SE CONTARIA — y el sintoma no es un
        error, es un numero que se queda corto sin explicacion. Es la misma familia de
        trampa que los tracker_id en -1.
        """
        if zona_id is None:
            self._zona_vistos = {}
        else:
            self._zona_vistos.pop(zona_id, None)

    def conteo_de(self, zona_id: str) -> int:
        """Cuantos objetos distintos pasaron por esa zona (para el cartel y los tests)."""
        return len(self._zona_vistos.get(zona_id, ()))

    def _tracker_para(self, conf_threshold: float):
        """
        El tracker vigente, construyendolo si hace falta.

        Los dos umbrales salen del umbral de confianza del USUARIO, y esto no es un
        detalle: los defaults del paquete (track_activation_threshold=0.7,
        high_conf_det_threshold=0.6) estan pensados para un pipeline que le entrega al
        tracker TODAS las detecciones, incluidas las de baja confianza, para que
        ByteTrack haga su asociacion en dos pasadas (primero las confiables, despues
        las dudosas para recuperar objetos tapados).

        Aca eso no pasa: nuestro postprocesador YA filtro por el umbral del usuario
        antes de que el tracker vea nada, asi que la banda de baja confianza que
        ByteTrack querria explotar llega vacia por construccion. Dejar los defaults
        tiene una consecuencia concreta y medida: con los tres modelos del repo
        (umbrales 0.5 / 0.3 / 0.25), una deteccion de 0.5 NUNCA recibe un tracker_id
        y el toggle queda prendido sin hacer absolutamente nada — exactamente el
        sintoma que el catalogo prohibe. El que bloquea es high_conf_det_threshold:
        bajar solo track_activation_threshold no alcanza (verificado).
        """
        if self._tracker is None or self._tracker_conf != conf_threshold:
            self._tracker = ByteTrackTracker(
                track_activation_threshold=conf_threshold,
                high_conf_det_threshold=conf_threshold,
            )
            self._tracker_conf = conf_threshold
            # El tracker nuevo vuelve a numerar desde 0: el acumulado de las zonas se
            # va con el o dejaria de contar objetos nuevos en silencio (ver
            # _olvidar_conteo). Mover el umbral resetea la cuenta, y esta bien que se
            # note: es otra corrida.
            self._olvidar_conteo()
        return self._tracker

    @staticmethod
    def _con_identidad(detections):
        """
        Solo las detecciones con un tracker_id confirmado (>= 0).

        Es la regla que comparten el suavizado y las trazas, y existe porque los
        tracks sin confirmar comparten TODOS el valor -1: cualquier cosa que agrupe
        por tracker_id los toma por un mismo objeto. En el suavizado eso funde varias
        cajas en una; en las trazas dibuja una estela que salta de un objeto a otro,
        que ademas es justo el sintoma que el usuario deberia leer como "el tracker
        esta confundiendo identidades". Un artefacto que imita al bug que la
        herramienta sirve para detectar es peor que no tener la herramienta.
        """
        if detections.tracker_id is None:
            return detections[[False] * len(detections)]
        return detections[detections.tracker_id >= 0]

    # ── El cajon de la escena: zonas poligonales ────────────────────────────────

    @property
    def geometria(self) -> SceneGeometry:
        """El cajon de la escena de esta conexion (para tests y para el ack)."""
        return self._escena

    @property
    def tiene_geometria(self) -> bool:
        """True si hay algo que dibujar aunque no haya ni una deteccion."""
        return not self._escena.vacia

    def set_geometry(self, payload: dict) -> dict:
        """
        Aplica un mensaje de control 'geometry' y devuelve el ACK.

        El ack lleva el ESTADO EFECTIVO, no el pedido — misma regla que
        POST /config/draw. Ante geometria invalida se conserva la anterior y el ack
        explica por que: el cliente puede mostrar el motivo en vez de quedarse
        creyendo que su zona entro.

        Que esto viva en la sesion y no en un singleton es la decision estructural
        del spec: una zona esta atada a UNA conexion, y un endpoint HTTP no sabe a
        cual le esta hablando. Por el propio WebSocket el mensaje se direcciona solo
        y ademas no hay carrera con los frames en vuelo: por el mismo canal, el orden
        ES el orden.
        """
        error = None
        try:
            frame_wh, zonas = parsear_geometria(payload)
            self._escena.set(frame_wh, zonas)
            # Se sueltan los acumulados de las zonas que ya no existen. No es higiene:
            # los ids de zona se REUSAN (el cliente numera z1, z2, ... y rellena los
            # huecos), asi que sin esto una zona nueva heredaria la cuenta de la que
            # acaba de borrarse. Mover un vertice, en cambio, NO resetea: el usuario
            # esta ajustando la region, y perder la cuenta en cada arrastre haria el
            # numero inutil justo mientras se lo acomoda. Para eso esta el boton.
            vivas = {z.id for z in zonas}
            for zid in [k for k in self._zona_vistos if k not in vivas]:
                self._olvidar_conteo(zid)
        except ValueError as e:
            error = str(e)
        return {
            "type": "geometry_ack",
            "zones": len(self._escena),
            "lines": 0,          # todavia no implementadas (tanda siguiente)
            "error": error,
        }

    def reset_zone_count(self, zona_id: str = None) -> dict:
        """
        Pone en cero el acumulado de una zona (o de todas) y devuelve el ACK.

        Existe porque los reseteos automaticos —cambio de modelo, de fuente, de umbral—
        no cubren el caso mas comun: un video en loop, donde el numero crece para
        siempre y en algun momento deja de significar algo.
        """
        self._olvidar_conteo(zona_id)
        return {"type": "zone_reset_ack", "zone": zona_id, "error": None}

    # ── El volcado de detecciones a disco ───────────────────────────────────────

    @property
    def exportando(self) -> bool:
        return self._export is not None and self._export.abierto

    def start_export(self, modelo: str = None) -> dict:
        """
        Abre un archivo de detecciones para esta conexion y devuelve el ACK.

        Arrancar dos veces cierra el anterior en vez de perderlo: un archivo a medio
        escribir pero cerrado sigue siendo un JSON valido y con datos, mientras que
        dejarlo colgado lo pierde entero.
        """
        if self._export is not None:
            self._export.close()
        try:
            self._export = DetectionExport(modelo=modelo)
        except OSError as e:
            self._export = None
            return {"type": "export_ack", "recording": False, "error": str(e)}
        estado = self._export.estado()
        estado.update({"type": "export_ack", "recording": True, "error": None})
        return estado

    def stop_export(self) -> dict:
        """Cierra el archivo y devuelve el ACK con el nombre y cuantas filas quedaron."""
        if self._export is None:
            return {"type": "export_ack", "recording": False, "file": None,
                    "rows": 0, "frames": 0, "error": None}
        self._export.close()
        estado = self._export.estado()
        self._export = None
        estado.update({"type": "export_ack", "recording": False, "error": None})
        return estado

    def export_frame(self, detections, modelo: str = None, ahora_ms: float = 0.0) -> None:
        """
        Vuelca las detecciones de este frame, si hay una exportacion abierta.

        Se llama DESPUES de process(), no antes, y eso es lo que hace util al archivo:
        asi las filas llevan el tracker_id, que es lo unico que permite reconstruir el
        recorrido de un objeto. Sin seguimiento prendido el archivo sigue siendo valido
        —son las cajas de cada instante— pero no hay nada que una un frame con el
        siguiente.
        """
        if self._export is None or not self._export.abierto:
            return
        try:
            self._export.append(detections, modelo=modelo, ahora_ms=ahora_ms)
        except Exception:
            # Un fallo de I/O no puede tumbar el stream: se corta la exportacion y el
            # frame sigue su camino. El archivo queda con lo que alcanzo a escribirse.
            logger.exception("Fallo al exportar un frame; se cierra la exportacion.")
            self._export.close()
            self._export = None

    def close(self) -> None:
        """
        Suelta lo que la conexion tenga abierto. La llama el handler del WS al terminar,
        pase lo que pase: sin esto un archivo de exportacion quedaria sin su cierre y no
        seria un JSON valido.
        """
        if self._export is not None:
            self._export.close()
            self._export = None

    def anotar_zonas(self, scene, detections, cfg, resolution_wh):
        """
        Dibuja las zonas con su contador de ocupacion y devuelve la escena.

        Se llama SIEMPRE que haya geometria, incluso con cero detecciones: una zona
        que dice "0" es informacion, y no dibujarla dejaria al usuario sin saber si
        la zona sigue ahi.

        NO filtra ni descarta detecciones: es una capa de LECTURA, no un filtro. El
        contador de cada zona sale de PolygonZone.trigger(), que es un test de punto
        en poligono y no toca el sv.Detections que recibe.
        """
        if cfg is None:
            return scene
        acumular = bool(getattr(cfg, "zone_total", False)) and self._stateful
        tids = detections.tracker_id if acumular else None

        vivas = self._escena.zonas_vivas(cfg, resolution_wh)
        for viva in vivas:
            # trigger() ademas de devolver la mascara ACTUALIZA current_count, que es
            # lo que el annotator estampa. Con cero detecciones lo pone en 0 solo
            # (verificado en supervision 0.30.1), asi que no hay contador viejo
            # colgado de un frame anterior.
            adentro = viva.poligono.trigger(detections)

            etiqueta = None                  # None -> el annotator estampa current_count
            if acumular:
                if tids is not None and len(adentro):
                    # Se guardan los IDS, no un contador: asi un objeto que sale y
                    # vuelve a entrar cuenta UNA vez. Contar transiciones seria mas
                    # barato y estaria mal — un objeto quieto sobre el borde titila
                    # adentro/afuera e inflaria el numero solo, que es exactamente el
                    # artefacto que hace desconfiar de un contador.
                    #
                    # El filtro >= 0 no es opcional: los tracks sin confirmar comparten
                    # TODOS el -1, asi que sin el, todos los objetos dudosos de la
                    # escena serian "el mismo objeto" y el acumulado se quedaria en 1.
                    ids = tids[adentro]
                    self._zona_vistos.setdefault(viva.zona.id, set()).update(
                        int(i) for i in ids[ids >= 0])
                # "ahora / total". Separador ASCII a proposito: supervision estampa el
                # texto con cv2.putText, que usa fuentes Hershey y NO tiene glifos fuera
                # de ASCII — un caracter lindo saldria como un signo de pregunta.
                etiqueta = "%d / %d" % (viva.poligono.current_count,
                                        self.conteo_de(viva.zona.id))

            scene = viva.annotator.annotate(scene=scene, label=etiqueta)
        return scene

    def anotar_trazas(self, scene, detections, cfg, resolution_wh):
        """
        Dibuja la estela de cada objeto rastreado sobre 'scene' y la devuelve.

        La llama render_detection como una capa mas. Vive aca y no en render/ porque
        el annotator es ESTADO de la conexion, no configuracion compartida: dos
        clientes mirando fuentes distintas no pueden compartir un buffer de recorridos.

        Devuelve la escena intacta si las trazas estan apagadas o si esta conexion no
        recuerda nada (foto suelta).
        """
        if not self._stateful or cfg is None or not getattr(cfg, "traces", False):
            if self._trace is not None:
                self._trace = None
                self._trace_key = None
            return scene

        largo = int(getattr(cfg, "traces_length", 30))
        clave = (cfg.version, resolution_wh, largo)
        if self._trace is None or self._trace_key != clave:
            # Rehacer pierde los recorridos acumulados. Es aceptable: se vuelven a
            # llenar en 'largo' frames (~1 s a 30 fps) y solo pasa cuando el usuario
            # toca un ajuste, no en el hot path.
            ann = annotators_for(cfg, resolution_wh)
            self._trace = sv.TraceAnnotator(
                color=sv.Color.from_hex(cfg.bbox_color),
                thickness=ann.thickness,
                trace_length=largo,
                # INDEX y no el default CLASS: con ColorLookup.CLASS supervision
                # ignora el color elegido y pinta una estela por clase. Misma trampa
                # que documenta render/annotators.py.
                color_lookup=sv.ColorLookup.INDEX,
            )
            self._trace_key = clave

        rastreadas = self._con_identidad(detections)
        if len(rastreadas) == 0:
            # Con cero detecciones el annotator no dibuja nada (verificado), asi que
            # llamarlo seria trabajo puro. Ademas evita tocar una escena que en el
            # camino "sin detecciones" todavia no fue copiada.
            return scene
        return self._trace.annotate(scene=scene, detections=rastreadas)

    def _suavizar(self, detections, length: int):
        """
        Promedia la posicion de cada objeto sobre los ultimos 'length' frames.

        Las detecciones se PARTEN en dos antes de suavizar, y eso no es una
        optimizacion: es correccion. sv.DetectionsSmoother agrupa por tracker_id, y
        todos los tracks sin confirmar comparten el valor -1, asi que para el
        suavizador son EL MISMO objeto. Verificado: dos detecciones separadas ambas
        con tracker_id=-1 entran, y sale UNA SOLA caja promediada entre las dos, en un
        punto de la imagen donde no hay nada. Entran 2, sale 1.

        Asi que por el suavizador pasan solo las que tienen identidad; las demas se
        devuelven intactas y se reunen despues. El orden cambia (primero las
        suavizadas), lo cual es inofensivo: el color es unico por eleccion del usuario
        y las etiquetas se calculan sobre el mismo objeto.
        """
        if detections.tracker_id is None:
            # Sin identidades no hay nada que agrupar. No deberia pasar (el singleton
            # garantiza que suavizado implica tracking), pero no vale la pena romper
            # un frame por eso.
            return detections

        if self._smoother is None or self._smoother_length != length:
            self._smoother = sv.DetectionsSmoother(length=length)
            self._smoother_length = length

        con_identidad = self._con_identidad(detections)
        sin_identidad = detections[detections.tracker_id < 0]

        suavizadas = self._smoother.update_with_detections(con_identidad)
        if len(sin_identidad) == 0:
            return suavizadas
        return sv.Detections.merge([suavizadas, sin_identidad])

    def process(self, detections, cfg, conf_threshold: float = 0.0):
        """
        Punto de insercion de todo lo que necesita memoria entre frames: recibe el
        sv.Detections que produjo el pipeline y devuelve el que se va a dibujar.

        Corre ENTRE controller.inference() y controller.render_result(), no adentro
        del pipeline (ver el encabezado del modulo).

        El tracker NUNCA descarta detecciones (verificado): entran N y salen N. Las
        que todavia no tienen identidad confirmada salen con tracker_id = -1, que es
        el valor que el resto del sistema tiene que saber leer. Eso importa porque
        significa que prender el tracking no puede hacer desaparecer una caja.
        """
        # Apagado, o conexion que no recuerda (foto suelta): se suelta lo que hubiera
        # quedado construido y se devuelve el resultado crudo, sin tocar.
        if not self._stateful or cfg is None or not getattr(cfg, "tracking", False):
            if self._tracker is not None:
                self.reset()
            return detections

        # Se llama SIEMPRE, incluso con cero detecciones: un frame vacio es
        # informacion para el tracker (los tracks vivos envejecen y expiran).
        # Saltearlo dejaria vivo para siempre a un objeto que ya se fue de escena.
        rastreadas = self._tracker_para(conf_threshold).update(detections)

        if not getattr(cfg, "smoothing", False):
            if self._smoother is not None:
                self._smoother = None
                self._smoother_length = None
            return rastreadas

        return self._suavizar(rastreadas, int(getattr(cfg, "smoothing_length", 5)))
