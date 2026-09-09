# tasks/detection.py — estrategia de DETECCION de objetos.
# Posee el armado del pipeline y el loop de inferencia detection-especificos, que
# antes vivian en el ModelController. El controller ahora solo invoca el runner que
# build_detection_pipeline devuelve, sin conocer adapters, shapes ni indices.

import time
import cv2
import numpy as np

from ..logger import run_warmup, make_dummy_input
from ..reader_pipeline import Model_loader
from ..input_pipeline import build_preprocessor, generate_input_adapter
from ..output_pipeline import buildPostprocessor, generate_output_adapter
from ..output_pipeline.unpackers.registry import unpack_out
from ..output_pipeline.unpackers.anchor_gen import generate_efficientdet_anchors
from .strategy import TaskStrategy
from .domain import detections_from_array, array_from_detections
from ..render import get_draw_config, annotators_for

# boxes_scores ya entrega [x1,y1,x2,y2,conf,cls] en formato estandar del sistema.
# Aplicar el adapter encima reordena mal las coords (swapea x/y de vuelta a yxyx).
# raw y yolo_flat si necesitan el adapter porque salen en el espacio del tensor sin reordenar.
_NEEDS_ADAPTER = {"raw", "yolo_flat", "tflite_detpost", "anchor_deltas"}


def build_detection_pipeline(config, model_path, logger):
    """
    Arma el pipeline completo de deteccion y devuelve un 'runner' autocontenido.

    runner(img, debug=False) -> (result, timings):
      - result : sv.Detections (supervision) con xyxy/confidence/class_id en px de
                 la imagen original. Es el TIPO DE DOMINIO de la tarea desde el
                 2026-08-26; el (N,6) crudo sobrevive solo adentro del pipeline,
                 hasta detections_from_array() en el ultimo paso.
      - timings: dict {pre_ms, inf_ms, post_ms} para alimentar el PerfMeter del controller.

    Todo el conocimiento detection-especifico (decision del adapter, normalizacion de
    shape, validacion de indices del tensor_structure) vive aca.
    """
    predict_fn = Model_loader.load(model_path, config.runtime, logger)
    preprocess_fn = build_preprocessor(config.input, config.runtime)
    input_adapter = generate_input_adapter(config.input)

    # Warmup opcional: primeras inferencias suelen ser lentas (alocacion/JIT).
    w = config.runtime.warmup
    if w.enabled and w.runs > 0:
        dummy_input = make_dummy_input(preprocess_fn, input_adapter, config.input)
        run_warmup(predict_fn, dummy_input, runs=w.runs, logger=logger)

    # anchor_deltas (EfficientDet/SSD crudos): la tabla de anchors NO viaja en el JSON,
    # se genera al cargar a partir de output.anchor_config y se deja en runtimeShapes.
    if config.output.pack_format == "anchor_deltas":
        ac = config.output.anchor_config
        if ac is None:
            raise ValueError(
                "pack_format 'anchor_deltas' requiere 'anchor_config' en output "
                "para poder generar la tabla de anchors.")
        rs = config.runtime.runtimeShapes
        rs.anchors = generate_efficientdet_anchors(
            config.input.height, config.input.width, ac)
        rs.box_variance = list(ac.box_variance)
        logger.info(f"Anchors generados: {rs.anchors.shape[0]} "
                    f"(niveles {ac.min_level}-{ac.max_level}, "
                    f"{ac.num_scales} escalas x {len(ac.aspect_ratios)} aspects)")

    unpack_fn = unpack_out(config.output)
    output_adapter = generate_output_adapter(config.output.tensor_structure)
    postprocess_fn = buildPostprocessor(config.output, config.runtime)

    pack_fmt = (getattr(config.output, "pack_format", "raw") or "raw").lower()
    needs_adapter = pack_fmt in _NEEDS_ADAPTER

    # Nombres de clase (opcional). Se resuelven UNA vez al armar: el hot path solo
    # indexa. Si el JSON no trae label_map se dibuja el id numerico, como siempre.
    label_map = getattr(config.output, "label_map", None) or None
    if label_map:
        logger.info(f"label_map con {len(label_map)} nombres de clase.")

    def run(img, debug=False):
        # 1. preprocess -> (tensor, meta). El meta (orig size + letterbox) viaja con
        #    el frame hasta el post; cada inferencia es autocontenida (reforma 8).
        t_pre0 = time.perf_counter()
        pre, frame_meta = preprocess_fn(img)
        adapted_input = input_adapter(pre)
        t_pre1 = time.perf_counter()

        # 2. inferencia del backend
        t_inf0 = time.perf_counter()
        raw_output = predict_fn(adapted_input)
        t_inf1 = time.perf_counter()

        # 3. desempaquetado + normalizacion de shape -> matriz 2D (N,K)
        t_post0 = time.perf_counter()
        unpacked = unpack_fn(raw_output, getattr(config, "runtime", None))

        if isinstance(unpacked, (list, tuple)):
            if len(unpacked) == 0:
                unpacked = np.empty((0, 6), dtype=np.float32)
            elif len(unpacked) == 1 and hasattr(unpacked[0], "ndim"):
                unpacked = unpacked[0]
            else:
                raise ValueError(
                    f"unpack_fn devolvio {len(unpacked)} outputs; normalizacion ambigua.")

        unpacked = np.asarray(unpacked)
        if unpacked.ndim == 3 and unpacked.shape[0] == 1:
            unpacked = unpacked[0]
        if unpacked.ndim == 1:
            unpacked = unpacked[None, :]

        # 4. adapter SOLO si el pack_format lo necesita (boxes_scores ya viene estandar)
        if needs_adapter:
            if unpacked.shape[0] > 0:
                # Validacion: los indices declarados en el JSON deben caber en el tensor real
                ts = config.output.tensor_structure
                max_idx = max([*ts.coordinates.values(), ts.confidence_index, ts.class_index])
                if max_idx >= unpacked.shape[1]:
                    raise ValueError(
                        f"tensor_structure declara indices hasta {max_idx} pero el tensor "
                        f"desempaquetado tiene {unpacked.shape[1]} columnas. Revisar "
                        "'coordinates'/'confidence_index'/'class_index' en el JSON.")
            # UNA llamada con el tensor entero, no una por fila: con heads crudos
            # (anchor_deltas entrega ~19k anchors) el list comprehension era el costo
            # dominante del postproceso. Ver generate_output_adapter().
            adapted_output = output_adapter(unpacked)
        else:
            adapted_output = unpacked  # ya en [x1,y1,x2,y2,conf,cls]

        if debug:
            # input_width/height son constantes de carga (runtimeShapes);
            # el tamano original y el letterbox son del frame actual (meta).
            rs = config.runtime.runtimeShapes
            logger.debug("[DBG] input/orig: input=%dx%d orig=%dx%d letter=%s",
                         rs.input_width, rs.input_height,
                         frame_meta.get("orig_width", 0),
                         frame_meta.get("orig_height", 0),
                         frame_meta)

        # 5. postprocess: conf filter + top-k + NMS + undo letterbox (usa el meta)
        arr = postprocess_fn(adapted_output, frame_meta)

        # 6. al tipo de dominio. Toda la geometria que sale de tasks/ es
        #    sv.Detections: es lo que consumen los annotators/ByteTrack/zonas de
        #    supervision (paso 3), y lo que le da lugar a la mascara de SEG.
        result = detections_from_array(arr)

        # Los nombres viajan DENTRO del sv.Detections (data['class_name']), que es
        # donde supervision los espera. Asi render_detection no necesita conocer el
        # config del modelo y sigue siendo una funcion de modulo.
        if label_map and len(result):
            result.data["class_name"] = np.array(
                [_class_name(label_map, int(c)) for c in result.class_id])
        if debug:
            logger.debug("Inferencia ejecutada: %d detecciones. Primeras: %s",
                         len(result), result.xyxy[:3])
        t_post1 = time.perf_counter()

        timings = {
            "pre_ms": (t_pre1 - t_pre0) * 1000,
            "inf_ms": (t_inf1 - t_inf0) * 1000,
            "post_ms": (t_post1 - t_post0) * 1000,
        }
        return result, timings

    return run


def _class_name(label_map, class_id: int) -> str:
    """Nombre de la clase, o el id como string si el label_map no lo cubre."""
    if 0 <= class_id < len(label_map):
        return str(label_map[class_id])
    return str(class_id)


def _labels_for(dets, modo: str = "completa") -> list:
    """
    Textos de las etiquetas: "<nombre> <conf>", o "#<id> <nombre> <conf>" cuando el
    tracking esta prendido. Usa data['class_name'] si el pipeline lo adjunto (hay
    label_map) y cae al id numerico de clase si no.

    'modo' es el label_mode de la DrawConfig (ver LABEL_MODES en render/draw_config):
    "corta" omite la confianza. El modo "ninguna" NO llega hasta aca — render_detection
    ni siquiera llama a esta funcion, porque este bucle es por deteccion y con ~70 cajas
    por frame es trabajo real que no hay motivo de hacer para tirarlo.

    El tracker_id se antepone porque es lo que hace que el tracking se VEA: sin el
    numero en pantalla, rastrear no cambia un solo pixel. Los tracks todavia no
    confirmados llegan con tracker_id = -1 y se dibujan SIN prefijo: "#-1" no seria
    informacion, seria ruido que el usuario tendria que aprender a ignorar. El prefijo
    se conserva tambien en "corta": es lo mas corto de la etiqueta y lo unico que no se
    puede deducir mirando el frame.
    """
    names = dets.data.get("class_name") if dets.data else None
    conf = dets.confidence
    tids = dets.tracker_id
    con_confianza = modo != "corta"
    out = []
    for i in range(len(dets)):
        name = str(names[i]) if names is not None else str(int(dets.class_id[i]))
        if con_confianza:
            score = float(conf[i]) if conf is not None else 0.0
            etiqueta = f"{name} {score:.2f}"
        else:
            etiqueta = name
        if tids is not None and int(tids[i]) >= 0:
            etiqueta = f"#{int(tids[i])} {etiqueta}"
        out.append(etiqueta)
    return out


def render_detection(result, img_bgr, draw_cfg=None, session=None) -> bytes:
    """
    Compone las cajas sobre el frame y devuelve el JPEG listo para mandar por el WS.

    Es el corazon del paso 3 (2026-08-26): el cliente ya no dibuja nada, recibe esto.
    'img_bgr' es el frame que el handler del WS ya tenia decodificado — no hay decode
    extra. Los annotators de supervision escriben IN-PLACE, por eso el .copy(): el
    frame original no es nuestro.

    'session' (opcional) es la memoria de la conexion: de ahi salen los annotators con
    ESTADO, hoy las trazas. None dibuja todo lo demas igual.
    """
    cfg = draw_cfg if draw_cfg is not None else get_draw_config()

    # La resolucion entra en la busqueda de annotators porque el grosor y la escala
    # del texto se derivan de ella (auto_scale). Es (ancho, alto), no el shape de numpy.
    h, w = img_bgr.shape[:2]
    # Una zona se dibuja aunque no haya ni una deteccion: "0 adentro" es informacion,
    # y una zona que desaparece cuando la escena se vacia se lee como un bug.
    hay_zonas = session is not None and session.tiene_geometria

    if len(result) == 0 and not hay_zonas:
        # Nada que dibujar: se re-encodea el frame tal cual, sin copiarlo. Las trazas
        # tampoco pintan nada sin detecciones (verificado), asi que este camino sigue
        # siendo seguro: nadie escribe sobre un frame que no copiamos.
        scene = img_bgr
    else:
        ann = annotators_for(cfg, (w, h))
        # El .copy() va aca (antes estaba en la llamada al box): los annotators
        # escriben IN-PLACE y ahora hay mas de uno encadenado sobre la misma escena.
        scene = img_bgr.copy()
        # El orden es el de las capas y no es negociable: primero el relleno, despues
        # el contorno, al final las etiquetas. Al reves el sombreado se comeria el
        # trazo de la caja y el texto que van arriba.
        #
        # Las ZONAS van primero de todo, debajo incluso del sombreado: son geometria
        # de la escena, no del resultado, y el resultado es lo que el usuario esta
        # mirando. Su contador puede quedar tapado si hay decenas de carteles encima,
        # y para eso existe el modo de etiqueta "ninguna" (#27) — que se hizo antes
        # que esto justamente por eso.
        if hay_zonas:
            scene = session.anotar_zonas(scene, result, cfg, (w, h))
        if ann.shade is not None:
            scene = ann.shade.annotate(scene=scene, detections=result)
        # Las trazas van DEBAJO de la caja: son contexto historico y no deben competir
        # con la marca del objeto actual, que es lo que el usuario esta mirando.
        if session is not None:
            scene = session.anotar_trazas(scene, result, cfg, (w, h))
        scene = ann.box.annotate(scene=scene, detections=result)
        # ann.label es None cuando el usuario apago las etiquetas (label_mode
        # "ninguna"). Se pregunta por el annotator y no por la config para no leer el
        # mismo estado desde dos lados; ademas asi ni se arma la lista de textos.
        if ann.label is not None:
            scene = ann.label.annotate(
                scene=scene, detections=result,
                labels=_labels_for(result, cfg.label_mode))

    ok, buf = cv2.imencode(".jpg", scene, [int(cv2.IMWRITE_JPEG_QUALITY), int(cfg.jpeg_quality)])
    if not ok:
        raise RuntimeError("cv2.imencode fallo al comprimir el frame compuesto.")
    return buf.tobytes()


def serialize_detection(result):
    """
    Serializa el resultado de dominio (sv.Detections) al formato del envelope:
    lista de [x1,y1,x2,y2,conf,cls] redondeados a 2 decimales.

    El contrato del WebSocket NO cambia con la llegada de supervision: el cliente
    sigue recibiendo exactamente las mismas filas que antes. Recien el paso 3
    (render en el backend) cambia esto por un JPEG binario.

    No traga: si 'result' no es un sv.Detections, array_from_detections propaga
    un TypeError con el tipo real.
    """
    return [[round(float(v), 2) for v in row] for row in array_from_detections(result)]


# Estrategia exportada: la consume el registry.
# serialize sigue existiendo aunque el WS ya no lo use para deteccion: lo consumen
# los tests y cualquier consumidor que quiera las cajas como dato (ver riesgo 3 del
# spec del paso 3). El transporte al cliente es 'frame'.
detection_strategy = TaskStrategy(
    task="detection",
    build_pipeline=build_detection_pipeline,
    serialize=serialize_detection,
    output_kind="frame",
    render=render_detection,
)
