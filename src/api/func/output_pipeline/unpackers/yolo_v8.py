# api/func/output_pipeline/unpackers/yolo_v8.py
from __future__ import annotations
import numpy as np


def build_yolo_v8(output_cfg):
    """
    Head "Detect" de Ultralytics v8 en adelante (v8/v9/v11 y las variantes 'seg'/'pose'
    en su parte de deteccion). NO sirve para v5/v7: para esos esta yolo_flat.

    **Entrada:** tensor (1, 4+C, N) o (4+C, N) — o sea, TRANSPUESTO: una fila por
    caracteristica y una columna por candidato, al reves que todos nuestros otros
    unpackers de deteccion.
    **Salida:** filas ya en el formato estandar [x1, y1, x2, y2, score, class_id], asi
    que NO usa output_adapter (esta fuera de _NEEDS_ADAPTER, como boxes_scores).

    Eso ultimo es una decision con dos motivos, y el segundo es el que manda:

    - **Correccion.** El orden de columnas del head de Ultralytics es FIJO (siempre
      4 de caja y despues las clases), no varia por modelo. El adapter existe para
      modelos cuyo orden cambia; aca no hay nada que configurar, y de hecho este
      unpacker ya lee arr[:, :4] y arr[:, 4:] a mano. Dejar 'coordinates' gobernando
      algo que en realidad esta cableado seria una perilla desconectada.
    - **Costo.** El adapter corre en PYTHON, una llamada por fila. yolov7 llega aca con
      pocas cajas (su head ya trae NMS), pero este emite ~8400 candidatos crudos por
      frame: medido, 22,2 ms contra 0,13 ms haciendo lo mismo vectorizado en numpy
      (170x). Era, de lejos, lo mas caro de todo el pipeline de este modelo.

    Las DOS diferencias con yolo_flat, que son la razon de que este archivo exista:

    1. **Esta transpuesto.** yolo_flat recibe (N, 5+C). Este llega (4+C, N). Si se le
       pasara el tensor de v8 a yolo_flat, to_2d() colapsaria (1,14,8400) a (14,8400) y
       leeria CATORCE detecciones de basura — sin lanzar ninguna excepcion. El silencio
       es peor que el error, por eso la orientacion se decide explicitamente aca abajo.

    2. **No hay objectness.** v5/v7 traen [cx,cy,w,h,obj,p0..pC] y el score es
       obj * max(p). v8 elimino esa columna: trae [cx,cy,w,h,p0..pC] y el score ES
       directamente max(p). Multiplicar por una quinta columna inexistente estaria
       tomando la probabilidad de la clase 0 como si fuera objectness.

    **Coordenadas:** se devuelven TAL CUAL salen del modelo. El head emite cxcywh en
    pixeles del tensor de entrada (medido: cx hasta 637 sobre un input de 640), asi que
    el JSON debe declarar out_coords_space="tensor_pixels" y el postprocesador se ocupa
    del undo del letterbox. No se escala nada aca a proposito: el trabajo del unpacker
    es la FORMA, no la escala (ver la deuda de "lazy scaling" en CLAUDE.md §4).

    **NMS:** el head no lo trae (los exports de Ultralytics con nms=False emiten los
    ~8400 candidatos crudos), asi que el JSON debe pedir apply_nms=true.
    """
    ts = getattr(output_cfg, "tensor_structure", None)
    n_clases = getattr(ts, "num_classes", None) if ts is not None else None
    # Cantidad de filas esperada si el JSON declaro las clases: 4 de caja + C de score.
    esperado = (4 + int(n_clases)) if n_clases else None

    def fn(raw_output, sh=None):
        # Los predict_fn devuelven una LISTA de tensores (ONNX: session.run siempre
        # lista, aunque el grafo tenga una sola salida). Cada unpacker la desenvuelve
        # por su cuenta: el runner solo normaliza la SALIDA del unpacker, no su entrada.
        if isinstance(raw_output, (list, tuple)):
            if len(raw_output) == 0:
                return np.empty((0, 6), dtype=np.float32)
            if len(raw_output) != 1:
                raise ValueError(
                    f"yolo_v8 recibio {len(raw_output)} outputs; el head Detect emite uno "
                    "solo. Si el modelo es 'seg' o 'pose', su segunda salida necesita otro "
                    "unpacker.")
            raw_output = raw_output[0]

        arr = np.asarray(raw_output, dtype=np.float32)

        # Batch: (1, F, N) -> (F, N). Se saca aca y no con to_2d() porque to_2d no
        # sabe nada de orientacion y justamente eso es lo que hay que resolver.
        if arr.ndim == 3 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.ndim != 2 or arr.size == 0:
            return np.empty((0, 6), dtype=np.float32)

        # Orientar a (N, F). Con num_classes declarado la decision es exacta; si falta,
        # se cae a la heuristica de que las caracteristicas son SIEMPRE muchas menos que
        # los candidatos (14 contra 8400 en este modelo).
        if esperado is not None and arr.shape[1] == esperado:
            pass                        # ya viene (N, F)
        elif esperado is not None and arr.shape[0] == esperado:
            arr = arr.T
        elif arr.shape[0] < arr.shape[1]:
            arr = arr.T
        arr = np.ascontiguousarray(arr) 

        if arr.shape[1] < 5:            # 4 de caja + al menos 1 clase
            return np.empty((0, 6), dtype=np.float32)

        cajas = arr[:, :4]
        clases = arr[:, 4:]

        # Sin objectness: el score es directamente el mejor puntaje de clase. Las
        # activaciones ya vienen aplicadas por el head (medido: los valores caen en
        # [0,1]), asi que no hay sigmoide ni softmax que aplicar.
        mejor = np.argmax(clases, axis=1)
        score = clases[np.arange(clases.shape[0]), mejor].astype(np.float32, copy=False)

        # cxcywh -> xyxy, vectorizado. Es lo que haria el output_adapter fila por fila.
        cx, cy, w, h = cajas[:, 0], cajas[:, 1], cajas[:, 2], cajas[:, 3]
        medio_w, medio_h = w * 0.5, h * 0.5

        salida = np.empty((arr.shape[0], 6), dtype=np.float32)
        salida[:, 0] = cx - medio_w
        salida[:, 1] = cy - medio_h
        salida[:, 2] = cx + medio_w
        salida[:, 3] = cy + medio_h
        salida[:, 4] = score
        salida[:, 5] = mejor
        return salida

    return fn
