# model_controller.py
import os
import time
import threading
import numpy as np
from .logger import setup_model_logger, PerfMeter
from .reader_pipeline import load_model_config
from .tasks.registry import get_strategy

'''
    Administrador de pipelines del backend, dependiente de los eventos del cliente.
    Debe ser capaz de:
        1_ Cargar modelos (despachando por model_type a la estrategia correcta)
        2_ Configurar propiedades (umbral de confianza en vivo)
        3_ Ejecutar la inferencia (delegando en el runner de la estrategia activa)
        4_ Liberar recursos

    Patron aplicado: Strategy. El controller NO conoce los detalles de cada pipeline
    (adapters, shapes, indices): eso vive en tasks/<tipo>.py. El controller solo elige
    la estrategia segun config.model_type, invoca su runner y alimenta las metricas.
'''


class ModelController:

    def __init__(self):
        # RLock: validate_pipeline() llama a inference() con el lock ya tomado.
        self._lock = threading.RLock()
        # Runner de la estrategia activa: (img, debug) -> (result, timings). None = sin modelo.
        self._runner = None
        # Estrategia activa (TaskStrategy): da el task y el serializador para el envelope.
        self._strategy = None
        self.model_format = None
        # Nombre base del modelo cargado ('best', 'yolov7-tiny'). No lo usa el pipeline:
        # existe para que lo que SALE del sistema pueda decir de donde vino — hoy, cada
        # fila del volcado de detecciones (render/export.py), que si no seria un archivo
        # de numeros sin procedencia.
        self.model_name = None
        self.config = None
        self.logger = None
        self.perf = PerfMeter(window=300)
        self._frame_idx = 0
        self._log_every = 60
        # Generacion del pipeline: avanza en CADA carga y CADA descarga. No identifica
        # al modelo, identifica al ESTADO del controller, y su unico consumidor es la
        # memoria por conexion del stream (render/session.py): el cliente mantiene
        # abierto el mismo WebSocket cuando cambia de modelo, asi que la sesion no
        # tiene forma de enterarse sola de que los tracks que viene arrastrando
        # nacieron con otro pipeline (donde los class_id significaban otra cosa).
        # Comparando generaciones se da cuenta y se olvida de todo.
        #
        # Monotona, por el mismo motivo que la version de DrawConfig: si volviera
        # atras, una sesion podria creerse vigente contra un pipeline distinto que
        # por casualidad tuvo ese mismo numero.
        self._generation = 0

    @property
    def is_loaded(self) -> bool:
        """True si hay un modelo cargado y listo para inferir."""
        return self._runner is not None

    @property
    def active_task(self):
        """model_type de la estrategia activa (para etiquetar el envelope), o None."""
        return self._strategy.task if self._strategy is not None else None

    @property
    def output_kind(self) -> str:
        """
        Como viaja la respuesta de la tarea activa por el WS: "json" (envelope de
        texto) o "frame" (JPEG binario ya compuesto). Sin modelo: "json", porque lo
        unico que se puede responder es un error, y los errores SIEMPRE son JSON.
        """
        return self._strategy.output_kind if self._strategy is not None else "json"

    @property
    def confidence_threshold(self) -> float:
        """
        Umbral de confianza vigente, o 0.0 sin modelo. Lo consume el tracker: todo lo
        que llega a el ya paso este filtro, asi que es el piso real de la escena.
        """
        if self.config is None:
            return 0.0
        return float(self.config.output.confidence_threshold)

    @property
    def pipeline_generation(self) -> int:
        """
        Numero de generacion del pipeline vigente. Cambia cuando cambia el modelo
        (carga o descarga). Lo consume StreamSession para invalidar su memoria: ver
        el comentario de _generation en __init__.
        """
        return self._generation

    def render_result(self, result, img_bgr, session=None) -> bytes:
        """
        Compone el resultado sobre el frame y devuelve el JPEG (solo tareas "frame").
        Mide el tiempo y lo empuja al bucket draw_ms del PerfMeter — que existe
        separado justamente para que este costo no se esconda dentro de post_ms.

        'session' es la memoria de la conexion que pide el frame. El controller no la
        interpreta: la pasa. Es un singleton de proceso y no puede ser dueno de estado
        por conexion, pero el render sí necesita los annotators con estado que ella
        guarda (las trazas).
        """
        strategy = self._strategy
        if strategy is None or strategy.render is None:
            raise RuntimeError(
                "La estrategia activa no renderiza frames: no se puede componer la salida.")
        t0 = time.perf_counter()
        frame = strategy.render(result, img_bgr, None, session)
        self.perf.push_draw((time.perf_counter() - t0) * 1000)
        return frame

    def serialize_result(self, result):
        """Serializa el resultado de dominio al formato JSON del envelope (segun el tipo)."""
        if self._strategy is None:
            raise RuntimeError("No hay modelo cargado: no se puede serializar el resultado.")
        return self._strategy.serialize(result)

    def load_model(self, model_path: str):
        """
        Carga el modelo y arma el pipeline despachando por model_type.
        Atomico: el estado del controller solo se actualiza si TODO el armado salio bien.
        Si algo falla, el controller queda descargado y la excepcion SE PROPAGA para que
        la API responda con el error real:
          - UnknownModelType  (tipo no registrado)      -> 422
          - TaskNotImplemented (CLS/SEG sin pipeline)     -> 501
          - ValueError/FileNotFoundError del armado       -> 422/404
        """
        logger = setup_model_logger(os.path.basename(model_path).split(".")[0])
        with self._lock:
            try:
                config = load_model_config(model_path)
                strategy = get_strategy(config.model_type)          # UnknownModelType si no existe
                runner = strategy.build_pipeline(config, model_path, logger)  # TaskNotImplemented (CLS/SEG)
            except Exception:
                logger.exception(f"Fallo la carga de '{model_path}'. El controller queda descargado.")
                self.unload_model()
                raise

            # Commit atomico del nuevo pipeline
            self.perf.reset()
            self._frame_idx = 0
            self._generation += 1
            self.model_format = os.path.splitext(model_path)[1].lower()
            self.model_name = os.path.splitext(os.path.basename(model_path))[0]
            self.config = config
            self._strategy = strategy
            self._runner = runner
            self.logger = logger

            logger.info(f"Modelo cargado correctamente (task={strategy.task}).")

    def validate_pipeline(self) -> dict:
        """
        Validacion cruzada JSON <-> modelo: corre una inferencia end-to-end sobre un
        frame dummy para detectar al cargar (y no en pleno stream) que el contrato
        declarado en el JSON coincide con lo que el modelo realmente devuelve.
        """
        with self._lock:
            if self._runner is None:
                raise RuntimeError("No hay modelo cargado para validar.")
            dummy = np.zeros((480, 640, 3), dtype=np.uint8)
            try:
                result = self.inference(dummy)
            except Exception as e:
                raise ValueError(
                    "Validacion post-carga fallida: el modelo no produce la salida que "
                    f"el JSON declara. Detalle: {e}"
                ) from e
            finally:
                # La corrida dummy no debe contaminar metricas ni contadores
                self.perf.reset()
                self._frame_idx = 0
            self.logger.info(
                f"Validacion post-carga OK ({len(result)} detecciones sobre frame dummy).")
            return {"dummy_detections": len(result)}

    def inference(self, img):
        """
        Delega en el runner de la estrategia activa y alimenta las metricas.
        Devuelve el resultado de dominio crudo (deteccion: sv.Detections); la
        serializacion al cliente la hace la frontera via serialize_result().
        El controller NO conoce el tipo: solo lo pasa a serialize_result(), que
        despacha por estrategia. Por eso agregar supervision no lo toco.
        """
        with self._lock:
            if self._runner is None:
                raise RuntimeError("No hay modelo cargado.")
            t0 = time.perf_counter()
            # El logging detallado del pipeline se gatea con la misma cadencia que PERF.
            debug = (self._frame_idx % self._log_every == 0)
            result, timings = self._runner(img, debug=debug)
            total_ms = (time.perf_counter() - t0) * 1000

            self.perf.push(timings["pre_ms"], timings["inf_ms"], timings["post_ms"], total_ms)

            self._frame_idx += 1
            if self._frame_idx % self._log_every == 0:
                s = self.perf.stats()
                if s:
                    self.logger.debug(
                        "PERF n=%d avg=%.2fms p95=%.2fms fps=%.2f | pre=%.2f inf=%.2f post=%.2f",
                        s["n"], s["avg_ms"], s["p95_ms"], s["fps_avg"],
                        s["pre_avg_ms"], s["inf_avg_ms"], s["post_avg_ms"],
                    )
            return result

    def update_confidence(self, new_threshold: float):
        """Valida y aplica el umbral. Lanza si no hay modelo o el valor esta fuera de rango."""
        if self.config is None:
            raise RuntimeError("No hay modelo cargado: no se puede actualizar el umbral.")
        if not (0.0 <= new_threshold <= 1.0):
            raise ValueError(f"Umbral de confianza fuera de rango [0, 1]: {new_threshold}")
        # El postprocesador lee este campo en cada llamada -> efecto inmediato ("en vivo")
        self.config.output.confidence_threshold = float(new_threshold)
        if self.logger:
            self.logger.info(f"Confianza actualizada a {new_threshold}.")

    def unload_model(self):
        with self._lock:
            # Descargar tambien es un cambio de generacion: lo que viniera recordando
            # una sesion del stream dejo de tener sentido en este mismo instante.
            self._generation += 1
            self._runner = None
            self._strategy = None
            self.model_format = None
            self.model_name = None
            self.config = None


"""
1_ Obtener el frame---------------------------------------(hecho) <-- mainAPI.py
2_ Preprocesar frame--------------------------------------(hecho) <-- input_transformer.py
3_ Adaptar el preproceso generico a la IA especifica------(hecho) <-- input_adapter.py
4_ Generar la inferencia----------------------------------(hecho) <-- model_loader.py
5_ Adaptar raw_output al formato generico del controlador-(hecho) <-- output_adapter.py
6_ Postprocesar detections--------------------------------(hecho) <-- output_transformer.py
7_ Devolver al cliente------------------------------------(hecho) <-- mainAPI.py

Los pasos 2-6 (armado + loop) viven ahora en tasks/<tipo>.py; el controller solo
elige la estrategia, invoca su runner y mide. Ver tasks/detection.py.
"""
