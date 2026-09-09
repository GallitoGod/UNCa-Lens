from collections import deque
from typing import Literal, Optional, get_args
import asyncio
import base64
import datetime
import json
import re
import time
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Body, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
import numpy as np
import cv2
from pathlib import Path
from api.func.model_controller import ModelController
from api.func.render import (update_draw_config, get_draw_config, BOX_STYLES,
                             LABEL_MODES, ZONE_ANCHORS, StreamSession,
                             EXPORTS_DIR, nombre_seguro)
from fastapi.responses import FileResponse
from api.func.reader_pipeline.config_schema import (
    ModelConfig,
    build_config_template,
    anchor_defaults,
)

# Rutas absolutas relativas a este archivo (src/api/mainAPI.py → ../../)
_ROOT = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = _ROOT / "models"
CONFIGS_DIR = _ROOT / "configs"

# ".torchscript" es la extension que usa Ultralytics al exportar con torch.jit.save().
# El archivo es TorchScript igual que un .pt exportado asi —la extension no es parte del
# formato, torch.jit.load() mira el contenido del zip— pero sin esto el sistema no lo veia
# y el usuario tenia que renombrarlo, que es peor de lo que parece: los pesos y el config
# se buscan por BASENAME, asi que renombrar puede hacerlo chocar con otro modelo que ya
# ocupe ese nombre y quedar inalcanzable en silencio.
MODEL_EXTENSIONS = {".onnx", ".tflite", ".h5", ".keras", ".torchscript", ".pt", ".pth"}
# Orden de preferencia cuando un basename tiene varios archivos (ej: yolo.onnx + yolo.tflite).
# ".torchscript" va ANTES que ".pt"/".pth" a proposito: es TorchScript por construccion y
# siempre carga, mientras que un ".pt" puede ser un checkpoint pickle que pytorchLoader NO
# puede abrir (paso con models/best.pt en agosto: torch.jit.load exige TorchScript). Ante
# dos archivos del mismo modelo, se elige el que seguro funciona.
_EXTENSION_PREFERENCE = [".onnx", ".tflite", ".h5", ".keras", ".torchscript", ".pt", ".pth"]

app = FastAPI(
    title="UNCaLens — Sistema de Vision por Computadora",
    description=(
        "API para carga de modelos de deteccion y ejecucion de inferencias sobre "
        "imagenes y video. El streaming va por WebSocket `/video_stream`: el cliente "
        "envia frames JPEG binarios y recibe UN mensaje por frame, de dos formas "
        "posibles: **binario** (el frame JPEG ya compuesto por el backend) para "
        "deteccion y segmentacion, o **texto** (envelope JSON `{task, result, error}`) "
        "para clasificacion y para cualquier error. Desde el 2026-08-26 **el dibujo "
        "es responsabilidad del BACKEND** (supervision): el cliente es un thin client."
    ),
    version="2",
)

# Electron carga desde file:// — sin esto todos los fetch() y WS fallan por CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

controller = ModelController()

# Ultimos 50 errores de inferencia (in-memory)
_inference_errors: deque = deque(maxlen=50)


class DrawSettingsRequest(BaseModel):
    """
    Ajustes de dibujo del backend. Todos opcionales: el cliente puede mandar solo lo
    que cambio. Los colores llegan como "#RRGGBB" (lo que produce un <input
    type=color>) y se validan ACA, no en el hot path.

    Nombres en camelCase a proposito: son los mismos del drawSettings del cliente,
    que sigue siendo el dueno del estado y lo persiste en localStorage.
    """
    bboxColor: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$",
                                     description="Color de las cajas, formato #RRGGBB")
    labelColor: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$",
                                      description="Color del texto de las etiquetas, formato #RRGGBB")
    maskAlpha: Optional[float] = Field(default=None, ge=0.0, le=1.0,
                                       description="Opacidad de la mascara (segmentacion)")
    boxStyle: Optional[Literal["box", "round", "corner", "dot"]] = Field(
        default=None,
        description="Estilo de marca: rectangulo, redondeado, solo esquinas o punto")
    labelMode: Optional[Literal["completa", "corta", "ninguna"]] = Field(
        default=None,
        description=("Cuanto texto lleva cada deteccion: 'completa' (#id nombre conf), "
                     "'corta' (sin la confianza) o 'ninguna' (sin etiquetas). Con "
                     "modelos que disparan decenas de detecciones los carteles tapan "
                     "la escena entera; 'ninguna' es el complemento del estilo 'dot'."))
    smartLabels: Optional[bool] = Field(
        default=None,
        description=("Correr las etiquetas para que no se tapen entre si. Inerte con "
                     "labelMode='ninguna': no hay carteles que correr."))
    shading: Optional[bool] = Field(
        default=None,
        description="Rellenar el interior de la caja con el color de acento translucido")
    shadingAlpha: Optional[float] = Field(default=None, ge=0.0, le=1.0,
                                          description="Opacidad del sombreado de la caja (0..1)")
    autoScale: Optional[bool] = Field(
        default=None,
        description="Derivar grosor y escala de texto de la resolucion del frame")
    thickness: Optional[int] = Field(default=None, ge=1, le=20,
                                     description="Grosor del trazo, en px (solo si autoScale=false)")
    textScale: Optional[float] = Field(default=None, gt=0.0, le=5.0,
                                       description="Escala del texto (solo si autoScale=false)")
    tracking: Optional[bool] = Field(
        default=None,
        description=("Seguir cada objeto entre frames y mostrar su identidad (#id). "
                     "Solo tiene efecto sobre camara y video: una imagen suelta no es "
                     "una secuencia."))
    smoothing: Optional[bool] = Field(
        default=None,
        description=("Promediar la posicion de cada objeto en los ultimos n frames. "
                     "REQUIERE tracking: pedirlo lo prende solo, y apagar el tracking "
                     "lo apaga. Suaviza el temblequeo a costa de que la caja quede "
                     "unos px por detras del objeto en movimiento."))
    smoothingLength: Optional[int] = Field(
        default=None, ge=2, le=30,
        description="Cuantos frames promedia el suavizado (ventana)")
    traces: Optional[bool] = Field(
        default=None,
        description=("Dibujar la estela del recorrido de cada objeto rastreado. "
                     "REQUIERE tracking: pedirlo lo prende solo."))
    tracesLength: Optional[int] = Field(
        default=None, ge=2, le=200,
        description="Cuantos frames de recorrido conserva cada estela")
    zoneColor: Optional[str] = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$",
                                     description="Color del contorno y el contador de las zonas, formato #RRGGBB")
    zoneTotal: Optional[bool] = Field(
        default=None,
        description=("Acumular cuantos objetos DISTINTOS pasaron por cada zona, ademas "
                     "de cuantos hay ahora (el cartel pasa de '12' a '12 / 47'). "
                     "REQUIERE tracking: pedirlo lo prende solo, y apagar el tracking "
                     "lo apaga. Sin identidad no hay como distinguir el mismo objeto "
                     "durante 30 frames de 30 objetos."))
    zoneAnchor: Optional[Literal["centro", "inferior"]] = Field(
        default=None,
        description=("Que punto de la caja decide si una deteccion esta adentro de "
                     "una zona: 'centro' (sirve igual en vista aerea y de calle) o "
                     "'inferior' (donde el objeto toca el piso; correcto para camaras "
                     "de calle). Cambia el conteo, no el dibujo."))
    jpegQuality: Optional[int] = Field(default=None, ge=1, le=100,
                                       description="Calidad del re-encode del frame compuesto")


# Guarda de coherencia: el Literal de arriba y BOX_STYLES tienen que decir lo mismo.
# Si alguien agrega un estilo en render/draw_config.py y se olvida del endpoint, esto
# revienta al importar el modulo, no en produccion con un 422 misterioso.
assert set(get_args(DrawSettingsRequest.model_fields["boxStyle"].annotation.__args__[0])) == set(BOX_STYLES), (
    "El Literal de boxStyle quedo desincronizado de BOX_STYLES")
assert set(get_args(DrawSettingsRequest.model_fields["labelMode"].annotation.__args__[0])) == set(LABEL_MODES), (
    "El Literal de labelMode quedo desincronizado de LABEL_MODES")
assert set(get_args(DrawSettingsRequest.model_fields["zoneAnchor"].annotation.__args__[0])) == set(ZONE_ANCHORS), (
    "El Literal de zoneAnchor quedo desincronizado de ZONE_ANCHORS")


class ModelPathRequest(BaseModel):
    model_path: str = Field(description="Ruta absoluta o relativa al archivo del modelo (.onnx, .tflite, .h5, .keras, .pt, .pth)")


class SelectModelRequest(BaseModel):
    model_name: str = Field(description="Nombre base del modelo, sin extension. Debe existir models/<nombre>.* y configs/<nombre>.json")


class ConfidenceUpdateRequest(BaseModel):
    value: float = Field(ge=0.0, le=1.0, description="Umbral de confianza en [0, 1]. Se aplica en vivo al stream.")


def _find_model_file(model_name: str) -> str:
    """Busca en models/ el archivo con el basename indicado (orden de preferencia fijo)."""
    matches = [p for p in MODELS_DIR.glob(f"{model_name}.*")
               if p.suffix.lower() in MODEL_EXTENSIONS]
    if not matches:
        raise FileNotFoundError(
            f"No se encontro archivo de modelo para '{model_name}' en {MODELS_DIR}/")
    matches.sort(key=lambda p: _EXTENSION_PREFERENCE.index(p.suffix.lower()))
    return str(matches[0])


def _load_and_validate(model_path: str) -> dict:
    """Carga + validacion cruzada JSON↔modelo. Mapea fallos a errores HTTP honestos."""
    try:
        controller.load_model(model_path)
        validation = controller.validate_pipeline()
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except (ValidationError, ValueError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fallo inesperado al cargar el modelo: {e}")
    return validation


# ════════════════════════════════════════
# 1a Listar modelos disponibles
# ════════════════════════════════════════

@app.get("/get_models", summary="Listar modelos disponibles")
def get_models():
    """Modelos con config JSON valido Y archivo de pesos presente en models/.

    Los JSON de configs/ sin archivo de modelo (ej: plantillas) no se listan.
    """
    try:
        models = []
        for cfg in sorted(CONFIGS_DIR.glob("*.json")):
            has_weights = any(p.suffix.lower() in MODEL_EXTENSIONS
                              for p in MODELS_DIR.glob(f"{cfg.stem}.*"))
            if has_weights:
                models.append(cfg.stem)
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════
# 1a-bis Listar TODOS los pesos con su estado de config (vista Modelos)
# ════════════════════════════════════════

@app.get("/models", summary="Listar archivos de pesos con estado de config")
def list_models():
    """Escanea models/ y devuelve cada peso soportado con si tiene config JSON.

    A diferencia de /get_models (que lista solo los cargables = config+pesos, para el
    selector de inferencia), este lista TODOS los pesos para la vista de Modelos: por
    eso incluye los que todavia no tienen config (hasConfig=false). Reemplaza el viejo
    IPC 'models:list' (el frontend ya no toca disco — ver SDD: thin client sin disco).
    """
    try:
        models = []
        for p in sorted(MODELS_DIR.glob("*")):
            ext = p.suffix.lower()
            if ext not in MODEL_EXTENSIONS:
                continue
            base = p.stem
            has_config = (CONFIGS_DIR / f"{base}.json").exists()
            models.append({
                "file": p.name,
                "ext": ext.lstrip("."),
                "baseName": base,
                "hasConfig": has_config,
            })
        return {"models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════
# 1b Seleccionar modelo por nombre
# ════════════════════════════════════════

@app.post("/select_model", summary="Cargar un modelo por nombre")
def select_model(data: SelectModelRequest):
    """Busca el archivo en models/, arma el pipeline y corre una validacion post-carga.

    Si el JSON no coincide con lo que el modelo realmente devuelve, responde 422
    con el detalle (antes respondia "ok" y el error aparecia recien en el stream).
    """
    try:
        model_path = _find_model_file(data.model_name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    validation = _load_and_validate(model_path)
    return {
        "status": "ok",
        "message": f"Modelo cargado y validado: {data.model_name}",
        "validation": validation,
        # El umbral EFECTIVO con el que quedo el modelo, para que el cliente muestre el
        # numero que el sistema realmente usa y no uno propio.
        #
        # Existe porque el slider del cliente arrancaba en un 50% hardcodeado que nunca
        # se enviaba ni se leia: el panel decia 50% mientras el backend filtraba a 0,15
        # (lo que declara configs/best.json), y mas de la mitad de las detecciones
        # dibujadas y exportadas estaban por debajo del numero que el panel afirmaba.
        #
        # Se devuelve desde ACA y no se deja que el cliente lo lea del JSON del config
        # porque eso seria INFERIRLO: esto es lo que el controller tiene cargado, que es
        # la unica respuesta que no puede quedar desincronizada. Mismo criterio que el
        # "estado efectivo" de /config/draw y del ack de geometria.
        "confidence": controller.confidence_threshold,
    }


# ════════════════════════════════════════
# 1c Cargar modelo por path directo
# ════════════════════════════════════════

@app.post("/model/load", summary="Cargar un modelo por ruta directa")
def load_model(data: ModelPathRequest):
    validation = _load_and_validate(data.model_path)
    return {
        "status": "ok",
        "message": f"Modelo cargado y validado: {data.model_path}",
        "validation": validation,
        "confidence": controller.confidence_threshold,   # el efectivo, ver /select_model
    }


# ════════════════════════════════════════
# 2 Actualizar umbral de confianza
# ════════════════════════════════════════

@app.post("/config/confidence", summary="Actualizar umbral de confianza en vivo")
def update_confidence(data: ConfidenceUpdateRequest):
    try:
        controller.update_confidence(data.value)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    # El EFECTIVO leido del controller, no el pedido: misma regla que /config/draw y
    # que el ack de geometria. Hoy coinciden siempre (el endpoint valida el rango antes
    # de escribir), y justamente por eso devolver el leido no cuesta nada y deja de ser
    # una promesa que hay que revisar si algun dia el controller ajusta el valor.
    return {"status": "ok", "new_confidence": controller.confidence_threshold}


# ════════════════════════════════════════
# 2b Ajustes de dibujo (el backend dibuja desde el 2026-08-26)
# ════════════════════════════════════════

@app.post("/config/draw", summary="Actualizar los ajustes de dibujo en vivo")
def update_draw(data: DrawSettingsRequest):
    """
    Resucita, en los hechos, el viejo /config/colors: cuando el dibujo se mudo al
    cliente (Reforma 3) los colores dejaron de ser asunto del backend; ahora que el
    backend volvio a dibujar (paso 3 del plan del 2026-08-21) los necesita de vuelta.

    NO requiere modelo cargado: son ajustes del USUARIO, no del modelo, y cambiar de
    modelo no debe resetearlos. Se aplican al frame SIGUIENTE, no al que ya esta en
    vuelo: se pierde el cambio de color instantaneo que daba el dibujo client-side.
    Costo conocido y aceptado.
    """
    cfg = update_draw_config(
        bbox_color=data.bboxColor,
        label_color=data.labelColor,
        mask_alpha=data.maskAlpha,
        box_style=data.boxStyle,
        label_mode=data.labelMode,
        smart_labels=data.smartLabels,
        shading=data.shading,
        shading_alpha=data.shadingAlpha,
        auto_scale=data.autoScale,
        thickness=data.thickness,
        text_scale=data.textScale,
        tracking=data.tracking,
        smoothing=data.smoothing,
        smoothing_length=data.smoothingLength,
        traces=data.traces,
        traces_length=data.tracesLength,
        zone_color=data.zoneColor,
        zone_anchor=data.zoneAnchor,
        zone_total=data.zoneTotal,
        jpeg_quality=data.jpegQuality,
    )
    # Se devuelve el estado EFECTIVO completo, no el pedido: asi el cliente puede
    # re-sincronizar su UI si el backend venia con otros valores (p.ej. tras un
    # reinicio) sin tener que adivinar.
    return {
        "status": "ok",
        "draw": {
            "bboxColor": cfg.bbox_color,
            "labelColor": cfg.label_color,
            "maskAlpha": cfg.mask_alpha,
            "boxStyle": cfg.box_style,
            "labelMode": cfg.label_mode,
            "smartLabels": cfg.smart_labels,
            "shading": cfg.shading,
            "shadingAlpha": cfg.shading_alpha,
            "autoScale": cfg.auto_scale,
            "thickness": cfg.thickness,
            "textScale": cfg.text_scale,
            "tracking": cfg.tracking,
            "smoothing": cfg.smoothing,
            "smoothingLength": cfg.smoothing_length,
            "traces": cfg.traces,
            "tracesLength": cfg.traces_length,
            "zoneColor": cfg.zone_color,
            "zoneAnchor": cfg.zone_anchor,
            "zoneTotal": cfg.zone_total,
            "jpegQuality": cfg.jpeg_quality,
        },
    }


# ════════════════════════════════════════
# 3 Descargar modelo
# ════════════════════════════════════════

@app.post("/model/unload", summary="Liberar el modelo cargado")
def unload_model():
    controller.unload_model()
    return {"status": "ok", "message": "Modelo descargado."}


# ════════════════════════════════════════
# 4 WebSocket streaming con inferencia
# ════════════════════════════════════════

def _decode_control(message: dict):
    """
    El mensaje de CONTROL que trae el cliente, o None si lo que llego es un frame.

    LA TRAMPA, y por eso esto corre ANTES que _decode_frame: el canal de texto YA
    esta ocupado por los frames en base64 (compatibilidad). Un mensaje de control en
    texto caeria en esa rama, no decodificaria como imagen, y el cliente recibiria
    'frame_invalido' por un mensaje que no tiene nada de invalido.

    El discriminador no es ambiguo: cuenta como control solo si el texto parsea como
    OBJETO JSON con una clave "type" de tipo string. Un JPEG en base64 nunca parsea
    asi — empieza con "/9j/" o con "data:image/jpeg;base64,".

    El chequeo de la primera llave es lo que evita pagar un json.loads sobre cada
    frame base64, que puede pesar cientos de KB.
    """
    if message.get("bytes") is not None:
        return None
    text = message.get("text")
    if not text or not text.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
        return parsed
    return None


def _responder_control(control: dict, session: StreamSession) -> dict:
    """
    Atiende un mensaje de control y devuelve SU respuesta.

    La invariante del stream no cambia: UN mensaje por mensaje recibido. Un control
    se contesta con un ack —nunca con silencio— porque el cliente no distingue "no me
    contestaron todavia" de "se perdio", y porque romper el "siempre responde"
    reintroduce el deadlock que el timeout de 3 s solo tapa.
    """
    tipo = control.get("type")
    if tipo == "export_start":
        # Abre el volcado de detecciones de ESTA conexion. Va por el WS y no por HTTP
        # por lo mismo que la geometria: un POST no sabe a que stream le esta hablando,
        # y lo que se exporta son las detecciones de este stream.
        return session.start_export(controller.model_name)
    if tipo == "export_stop":
        return session.stop_export()
    if tipo == "zone_reset":
        # Pone en cero el acumulado de una zona ("id") o de todas (sin "id"). Va por el
        # mismo canal que la geometria y por la misma razon: el contador vive en ESTA
        # conexion. No se re-emite al reconectar — una conexion nueva ya nace en cero.
        zid = control.get("id")
        if zid is not None and not isinstance(zid, str):
            return {"type": "zone_reset_ack", "zone": None,
                    "error": "el 'id' de la zona tiene que ser un texto"}
        return session.reset_zone_count(zid)
    if tipo == "geometry":
        # La geometria se direcciona sola: llego por la conexion a la que pertenece.
        # Un POST no sabria a que WebSocket le esta hablando, y ademas competiria con
        # los frames en vuelo (no estaria definido si el frame N se compone con la
        # zona vieja o la nueva). Por el mismo canal, el orden ES el orden.
        return session.set_geometry(control)
    return {"type": "control_error",
            "error": "tipo de control desconocido: %s" % tipo}


def _decode_frame(message: dict):
    """Acepta frames JPEG binarios (protocolo actual) o base64 (compatibilidad)."""
    data = message.get("bytes")
    if data is None:
        text = message.get("text") or ""
        if "," in text:  # data URL: "data:image/jpeg;base64,...."
            text = text.split(",", 1)[1]
        try:
            data = base64.b64decode(text)
        except Exception:
            return None
    img_np = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(img_np, cv2.IMREAD_COLOR)


@app.websocket("/video_stream")
async def video_stream(websocket: WebSocket):
    """Protocolo: el cliente envia un frame JPEG (binario) y recibe SIEMPRE UN mensaje
    por frame, en una de dos formas (desde el 2026-08-26, paso 3 del plan del
    2026-08-21):

      BINARIO -> el frame JPEG ya compuesto por el backend (deteccion; segmentacion
                 cuando exista). El cliente lo pinta y listo: no parsea nada.
      TEXTO   -> envelope JSON {task, result, error}:
                   {"task": "classification", "result": [{"cls": 3, "score": 0.91}], "error": null}
                   {"task": null, "result": null, "error": "frame_invalido"}
                 Clasificacion va por aca porque su resultado es TEXTO, no geometria:
                 componerlo en el backend obligaria a re-encodear un frame entero para
                 estampar tres renglones. Y TODOS los errores van por aca, de cualquier
                 tarea.

    El cliente discrimina por el TIPO del dato recibido (string vs Blob), no por un
    campo. El despacho es por strategy.output_kind, NO por 'task': agregar un tipo
    nuevo no hace crecer este handler.

    Ademas del frame, el cliente puede mandar mensajes de CONTROL por el canal de texto
    (desde el 2026-09-09): un objeto JSON con clave "type" que no es un frame. Hoy el
    unico es "geometry", que declara las zonas poligonales de ESTA conexion y se contesta
    con un ack con el estado efectivo. Se discrimina ANTES de intentar decodificar el
    frame, porque el canal de texto ya estaba ocupado por los frames en base64 (ver
    _decode_control). No es una excepcion a la invariante: sigue habiendo UN mensaje de
    respuesta por cada mensaje recibido.

    SIEMPRE se responde (aunque el frame sea invalido o falle la inferencia) para que
    el cliente nunca quede esperando un frame que no va a llegar. Romper eso
    reintroduce el deadlock del stream.
    """
    await websocket.accept()

    # Memoria de ESTA conexion (tracking/suavizado/trazas del Tier B). Vive y muere
    # con el WebSocket: ver render/session.py para por que ese es el dueno correcto.
    # 'stateful=false' lo manda el camino one-shot de imagenes, que no es una
    # secuencia y no debe recordar nada entre fotos sueltas.
    session = StreamSession(
        stateful=websocket.query_params.get("stateful", "true").lower() != "false")

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            # Canal de CONTROL: la geometria de zonas viaja por aca, no por HTTP.
            # Se mira antes que nada porque comparte el canal de texto con los frames
            # en base64 (ver _decode_control).
            control = _decode_control(message)
            if control is not None:
                await websocket.send_json(_responder_control(control, session))
                continue

            response = {"task": None, "result": None, "error": None}
            frame_bytes = None            # != None -> la respuesta va BINARIA
            img_bgr = _decode_frame(message)

            if img_bgr is None:
                response["error"] = "frame_invalido"
            elif not controller.is_loaded:
                response["error"] = "no_model"
            else:
                try:
                    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

                    def _procesar():
                        """Inferencia + (segun la tarea) composicion del frame.

                        Las dos cosas van en UNA sola llamada al threadpool: ambas son
                        bloqueantes y no deben congelar el event loop (los endpoints
                        REST tienen que seguir respondiendo mientras corre el stream).
                        """
                        result = controller.inference(img_rgb)
                        if controller.output_kind == "frame":
                            # Memoria entre frames, SOLO para las tareas geometricas:
                            # clasificacion no produce un sv.Detections y no tiene nada
                            # que rastrear. sync() va ANTES de process(): si cambio el
                            # modelo hay que olvidar los tracks viejos antes de usarlos,
                            # no despues.
                            session.sync(controller.pipeline_generation)
                            t_track = time.perf_counter()
                            result = session.process(
                                result, get_draw_config(),
                                controller.confidence_threshold)
                            controller.perf.push_track(
                                (time.perf_counter() - t_track) * 1000)
                            # DESPUES de process(), no antes: asi las filas llevan el
                            # tracker_id, que es lo unico que permite reconstruir el
                            # recorrido de un objeto a partir del archivo.
                            session.export_frame(
                                result, controller.model_name,
                                time.perf_counter() * 1000)
                            # El render recibe el img_bgr que YA teniamos decodificado:
                            # no hay un decode extra por frame.
                            return None, controller.render_result(result, img_bgr, session)
                        return (controller.active_task,
                                controller.serialize_result(result)), None

                    loop = asyncio.get_event_loop()
                    envelope, frame_bytes = await loop.run_in_executor(None, _procesar)
                    if envelope is not None:
                        response["task"], response["result"] = envelope
                except Exception as e:
                    _inference_errors.append({
                        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                        "error": str(e),
                    })
                    response["error"] = "inference_error"
                    frame_bytes = None      # ante error se responde JSON, SIEMPRE

            # UN mensaje por frame, en una de las dos formas.
            if frame_bytes is not None:
                await websocket.send_bytes(frame_bytes)
            else:
                await websocket.send_json(response)

    except WebSocketDisconnect:
        pass
    finally:
        # Sin esto, un volcado en curso quedaria sin su cierre y el archivo no seria un
        # JSON valido. Corre igual si el cliente se va sin avisar.
        session.close()


# ════════════════════════════════════════
# 4b Descarga del volcado de detecciones
# ════════════════════════════════════════

@app.get("/exports/{name}", summary="Descargar un volcado de detecciones")
def download_export(name: str):
    """Sirve un archivo de exports/ para que el cliente lo baje como cualquier descarga.

    Por que HTTP y no el WebSocket: el archivo puede tener cientos de miles de filas y
    mandarlo por el canal de control seria empujar megabytes de JSON por donde viajan
    los frames. El WS abre y cierra el volcado (es estado de la conexion); bajarlo es
    una descarga comun, igual que la del video grabado.

    422 si el nombre no es seguro, 404 si no existe.
    """
    if not nombre_seguro(name):
        raise HTTPException(status_code=422, detail=f"Nombre de archivo inseguro: {name}")
    ruta = EXPORTS_DIR / name
    if not ruta.is_file():
        raise HTTPException(status_code=404, detail=f"No existe el volcado '{name}'.")
    return FileResponse(ruta, media_type="application/json", filename=name)


# ════════════════════════════════════════
# 5 Obtener logs de inferencia
# ════════════════════════════════════════

@app.get("/logs/inference", summary="Ultimos errores de inferencia")
def get_inference_logs():
    return {"logs": list(_inference_errors)}


# ════════════════════════════════════════
# 6 Metricas de rendimiento
# ════════════════════════════════════════

@app.get("/metrics", summary="Metricas de rendimiento del pipeline")
def get_metrics():
    stats = controller.perf.stats()
    if stats is None:
        return {"status": "no_data", "metrics": None}
    return {"status": "ok", "metrics": stats}


# ════════════════════════════════════════
# 7 Templates de config + escritura de config (single source of truth)
# ════════════════════════════════════════

_VALID_MODEL_TYPES = {"detection", "classification", "segmentation"}
# Nombre de config seguro: sin separadores ni "..". Mismo criterio que el IPC del front.
_SAFE_CONFIG_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


@app.get("/config/template/{model_type}", summary="Defaults de config por tipo de modelo")
def config_template(model_type: str):
    """Defaults generados desde el schema Pydantic (no duplicados en el frontend).

    Para detection incluye ademas los defaults de anchors (pack_format anchor_deltas).
    """
    if model_type not in _VALID_MODEL_TYPES:
        raise HTTPException(status_code=404, detail=f"model_type desconocido: {model_type}")
    return {
        "config": build_config_template(model_type),
        "anchor_defaults": anchor_defaults() if model_type == "detection" else None,
    }


@app.post("/configs/{name}", summary="Validar y guardar una config de modelo")
def write_config(name: str, body: dict = Body(...)):
    """Valida el body contra ModelConfig (estricto) y lo escribe en configs/<name>.json.

    422 si el nombre es inseguro o el body no cumple el schema.
    """
    if not _SAFE_CONFIG_NAME.match(name):
        raise HTTPException(status_code=422, detail=f"nombre de config inseguro: '{name}'")
    try:
        cfg = ModelConfig.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=json.loads(e.json()))

    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIGS_DIR / f"{name}.json"
    path.write_text(
        json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {"ok": True, "path": str(path)}


@app.get("/configs/{name}", summary="Leer la config existente de un modelo")
def read_config(name: str):
    """Devuelve configs/<name>.json parseado, o config:null si no existe.

    Reemplaza el viejo IPC 'configs:read'. Contrato pensado para el wizard:
    - faltante  -> 200 {config: null}  (el wizard arranca con el template del backend).
    - corrupto  -> 500  (el frontend cae al template igual; regla SDD 4.1.4: no bloquea).
    - nombre inseguro -> 422.
    """
    if not _SAFE_CONFIG_NAME.match(name):
        raise HTTPException(status_code=422, detail=f"nombre de config inseguro: '{name}'")
    path = CONFIGS_DIR / f"{name}.json"
    if not path.exists():
        return {"config": None}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"config corrupta o ilegible: {e}")
    return {"config": config}


# ════════════════════════════════════════
# 8 Subida de pesos por multipart (reemplaza el IPC 'models:import')
# ════════════════════════════════════════

# Tamano de chunk para volcar el upload a disco sin cargarlo entero en RAM.
_UPLOAD_CHUNK = 1024 * 1024  # 1 MiB


@app.post("/models/upload", summary="Subir un archivo de pesos a models/")
async def upload_model(file: UploadFile = File(...)):
    """Recibe UN peso por multipart y lo escribe en models/<archivo>.

    Valida extension y nombre seguro ANTES de leer el stream (fail fast) y copia en
    chunks para soportar archivos grandes sin agotar memoria. Sobrescribe si ya existe
    (mismo comportamiento que el viejo copyFileSync del IPC). El frontend sube de a un
    archivo (un request por archivo) para poder reportar progreso y errores por archivo.
    """
    # Path(...).name descarta cualquier componente de ruta que venga en el filename.
    filename = Path(file.filename or "").name
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()

    if ext not in MODEL_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"extension no soportada: '{ext}'")
    if not _SAFE_CONFIG_NAME.match(stem):
        raise HTTPException(status_code=422, detail=f"nombre de archivo inseguro: '{stem}'")

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODELS_DIR / f"{stem}{ext}"
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
    except OSError as e:
        # Limpia el archivo parcial para no dejar un peso corrupto a medio escribir.
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"fallo al escribir el modelo: {e}")
    finally:
        await file.close()

    return {"ok": True, "file": dest.name}
