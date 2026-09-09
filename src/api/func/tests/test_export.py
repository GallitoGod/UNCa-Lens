# test_export.py — volcado de detecciones a disco (Tier C §5.2 del catalogo).
#
# Lo que este archivo tiene que fijar:
#
#   1. QUE EL ARCHIVO SEA VALIDO PASE LO QUE PASE. Se escribe incrementalmente, asi que
#      un JSON a medio cerrar es basura. El caso que importa no es el feliz, es el de la
#      conexion que se corta sin avisar.
#   2. QUE LLEVE EL tracker_id. Es lo unico que convierte una pila de cajas por frame en
#      el RECORRIDO de un objeto, que es para lo que el usuario lo pide.
#   3. QUE LA PROCEDENCIA ESTE EN CADA FILA. Cambiar de modelo a mitad de un volcado no
#      lo corta; lo que evita que el archivo mienta es que cada fila diga con que modelo
#      se produjo.

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

import api.mainAPI as main
from api.func.render import StreamSession
from api.func.render.export import DetectionExport, nombre_seguro
from api.func.tasks.domain import detections_from_array, empty_detections


@pytest.fixture
def carpeta(tmp_path):
    return tmp_path / "exports"


def _dets(n=2, tracker=True):
    filas = [[10 * i, 20 * i, 10 * i + 40, 20 * i + 50, 0.9 - i * 0.1, i] for i in range(n)]
    d = detections_from_array(np.array(filas, dtype=np.float32))
    if tracker:
        d.tracker_id = np.array(list(range(n)))
    d.data["class_name"] = np.array(["auto"] * n)
    return d


def _leer(exp):
    return json.loads(exp.ruta.read_text(encoding="utf-8"))


# ── 1. El archivo siempre queda valido ──────────────────────────────────────

def test_un_volcado_vacio_igual_es_json_valido(carpeta):
    """Ni un frame recibido. El archivo tiene que abrirse igual, con una lista vacia."""
    exp = DetectionExport(modelo="best", carpeta=carpeta)
    exp.close()
    assert _leer(exp) == []


def test_close_es_idempotente(carpeta):
    exp = DetectionExport(carpeta=carpeta)
    exp.append(_dets(), "best", 0.0)
    exp.close()
    exp.close()
    assert len(_leer(exp)) == 2


def test_la_sesion_cierra_el_archivo_al_cerrarse(carpeta, monkeypatch):
    """
    EL caso que importa: el cliente se va sin mandar export_stop. Sin el cierre, el
    JSON queda sin su corchete final y el archivo entero es basura.
    """
    monkeypatch.setattr("api.func.render.export.EXPORTS_DIR", carpeta)
    s = StreamSession()
    s.start_export("best")
    s.export_frame(_dets(), "best", 0.0)
    ruta = s._export.ruta
    s.close()                       # como si se hubiera cortado la conexion
    assert json.loads(ruta.read_text(encoding="utf-8"))


def test_arrancar_dos_veces_no_pierde_el_primero(carpeta, monkeypatch):
    """Cerrar el anterior conserva lo que ya se habia escrito; dejarlo colgado lo pierde."""
    monkeypatch.setattr("api.func.render.export.EXPORTS_DIR", carpeta)
    s = StreamSession()
    s.start_export("best")
    s.export_frame(_dets(), "best", 0.0)
    primero = s._export.ruta
    s.start_export("best")
    # Y el nombre del segundo TIENE que ser otro: la marca de tiempo tiene resolucion
    # de segundos, asi que sin sufijo el segundo abriria el mismo archivo en modo "w"
    # y truncaria al primero.
    assert s._export.ruta != primero
    assert len(json.loads(primero.read_text(encoding="utf-8"))) == 2
    s.close()


# ── 2. El contenido ─────────────────────────────────────────────────────────

def test_una_fila_por_deteccion_por_frame(carpeta):
    exp = DetectionExport(carpeta=carpeta)
    for f in range(3):
        exp.append(_dets(n=2), "best", f * 33.0)
    exp.close()
    filas = _leer(exp)
    assert len(filas) == 6
    assert [f["frame"] for f in filas] == [0, 0, 1, 1, 2, 2]


def test_el_esquema_es_el_de_supervision(carpeta):
    """
    Se usa sv.JSONSink.parse_detection_data a proposito: asi el archivo es el mismo que
    produciria supervision y cualquier cosa que ya lo lea sirve, en vez de un formato
    inventado por nosotros.
    """
    exp = DetectionExport(carpeta=carpeta)
    exp.append(_dets(n=1), "best", 0.0)
    exp.close()
    fila = _leer(exp)[0]
    for campo in ("x_min", "y_min", "x_max", "y_max", "class_id",
                  "confidence", "tracker_id", "class_name"):
        assert campo in fila, campo


def test_lleva_el_tracker_id(carpeta):
    """Es lo unico que permite reconstruir el recorrido de un objeto entre frames."""
    exp = DetectionExport(carpeta=carpeta)
    for f in range(4):
        exp.append(_dets(n=2), "best", f * 33.0)
    exp.close()
    filas = _leer(exp)
    # Agrupar por identidad es, literalmente, obtener los movimientos.
    recorridos = {}
    for f in filas:
        recorridos.setdefault(f["tracker_id"], []).append(f["frame"])
    assert sorted(recorridos) == [0, 1]
    assert all(v == [0, 1, 2, 3] for v in recorridos.values())


def test_sin_tracking_el_archivo_sigue_siendo_valido(carpeta):
    """
    Sin seguimiento no hay tracker_id: el archivo vale igual (son las cajas de cada
    instante) pero no hay nada que una un frame con el siguiente. Es informacion menos
    rica, no un error.
    """
    exp = DetectionExport(carpeta=carpeta)
    exp.append(_dets(n=2, tracker=False), "best", 0.0)
    exp.close()
    filas = _leer(exp)
    assert len(filas) == 2
    # null y no "": el parseador de supervision pone string vacio, que en CSV es una
    # celda vacia pero en JSON es una mentira de tipo. Se normaliza a proposito.
    assert all(f["tracker_id"] is None for f in filas)


def test_cada_fila_dice_de_que_modelo_vino(carpeta):
    """
    Cambiar de modelo NO corta el volcado (sync() no lo toca). Lo que evita que el
    archivo mienta es que la procedencia este en la fila, no en el nombre del archivo.
    """
    exp = DetectionExport(modelo="best", carpeta=carpeta)
    exp.append(_dets(n=1), "best", 0.0)
    exp.append(_dets(n=1), "yolov7-tiny", 33.0)
    exp.close()
    assert [f["model"] for f in _leer(exp)] == ["best", "yolov7-tiny"]


def test_el_tiempo_es_relativo_al_arranque(carpeta):
    """
    'ms' desde que empezo el volcado, no el reloj de la maquina: es lo que hace que dos
    archivos se puedan comparar y que un recorrido tenga escala temporal.
    """
    exp = DetectionExport(carpeta=carpeta)
    exp.append(_dets(n=1), "best", 1_000_000.0)
    exp.append(_dets(n=1), "best", 1_000_033.0)
    exp.close()
    assert [f["ms"] for f in _leer(exp)] == [0.0, 33.0]


def test_los_frames_vacios_cuentan_pero_no_escriben(carpeta):
    """
    'En el frame 1 no habia nada' es un dato. Si el contador no avanzara, el numero de
    frame de las filas siguientes seria mentira.
    """
    exp = DetectionExport(carpeta=carpeta)
    exp.append(_dets(n=1), "best", 0.0)
    exp.append(empty_detections(), "best", 33.0)
    exp.append(_dets(n=1), "best", 66.0)
    exp.close()
    assert exp.frames == 3 and exp.filas == 2
    assert [f["frame"] for f in _leer(exp)] == [0, 2]


# ── 3. La sesion y el canal de control ──────────────────────────────────────

def test_la_sesion_nace_sin_exportar():
    assert StreamSession().exportando is False


def test_export_frame_sin_volcado_abierto_no_hace_nada():
    """El hot path llama a esto en CADA frame: sin volcado abierto tiene que ser inerte."""
    s = StreamSession()
    s.export_frame(_dets(), "best", 0.0)
    assert s.exportando is False


def test_cambiar_de_modelo_NO_corta_el_volcado(carpeta, monkeypatch):
    """
    A diferencia del tracker y del acumulado de zona, esto sobrevive a sync(): cortar
    una grabacion porque el usuario cambio de modelo seria perder trabajo, y el archivo
    se explica solo porque cada fila lleva el modelo.
    """
    monkeypatch.setattr("api.func.render.export.EXPORTS_DIR", carpeta)
    s = StreamSession()
    s.sync(1)
    s.start_export("best")
    s.export_frame(_dets(), "best", 0.0)
    s.sync(2)                       # cambio de modelo
    assert s.exportando is True
    s.export_frame(_dets(), "yolov7-tiny", 33.0)
    ack = s.stop_export()
    assert ack["rows"] == 4


def test_start_y_stop_por_el_websocket(carpeta, monkeypatch):
    monkeypatch.setattr("api.func.render.export.EXPORTS_DIR", carpeta)
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json({"type": "export_start"})
        a = ws.receive_json()
        assert a["type"] == "export_ack" and a["recording"] is True and a["file"]
        ws.send_json({"type": "export_stop"})
        b = ws.receive_json()
        assert b["recording"] is False and b["file"] == a["file"]


def test_stop_sin_start_no_rompe_nada():
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json({"type": "export_stop"})
        a = ws.receive_json()
        assert a["type"] == "export_ack" and a["recording"] is False and a["file"] is None
        # Y el stream sigue vivo.
        ws.send_bytes(b"basura")
        assert ws.receive_json()["error"] == "frame_invalido"


# ── 4. La descarga ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("nombre, ok", [
    ("detecciones-20260909-120000-best.json", True),
    ("../../secretos.json", False),
    ("con espacio.json", False),
    ("sin_extension", False),
    ("archivo.txt", False),
])
def test_nombres_seguros(nombre, ok):
    assert nombre_seguro(nombre) is ok


def test_descarga_por_http(carpeta, monkeypatch):
    monkeypatch.setattr("api.func.render.export.EXPORTS_DIR", carpeta)
    monkeypatch.setattr(main, "EXPORTS_DIR", carpeta)
    exp = DetectionExport(modelo="best", carpeta=carpeta)
    exp.append(_dets(n=2), "best", 0.0)
    exp.close()

    client = TestClient(main.app)
    r = client.get("/exports/%s" % exp.nombre)
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_descarga_de_algo_que_no_existe(carpeta, monkeypatch):
    monkeypatch.setattr(main, "EXPORTS_DIR", carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    assert TestClient(main.app).get("/exports/no-existe.json").status_code == 404


def test_descarga_rechaza_un_nombre_inseguro():
    """Path traversal: el nombre se valida ANTES de tocar el disco."""
    assert TestClient(main.app).get("/exports/..%2F..%2Fconfigs%2Fbest.json").status_code in (404, 422)
