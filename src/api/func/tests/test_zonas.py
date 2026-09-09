# test_zonas.py — zonas poligonales (pendiente #24c, spec 2026-08-28).
#
# Lo que este archivo tiene que fijar, en orden de importancia:
#
#   1. LOS DOS CAJONES. La geometria sobrevive al cambio de modelo y el tracker no.
#      Es la decision de fondo del spec y la unica que, si se rompe, se rompe en
#      silencio: la zona simplemente desaparece a mitad de una comparacion entre dos
#      modelos, que es justo el caso de uso que la feature existe para servir.
#   2. EL DISCRIMINADOR DEL CANAL DE TEXTO. Los frames en base64 ya usaban ese canal.
#      Si un frame se confunde con control (o al reves) el sintoma es un
#      'frame_invalido' inexplicable.
#   3. QUE LA ZONA NO FILTRE. Es una capa de LECTURA: prenderla no puede cambiar ni
#      una deteccion. Es la misma promesa que ya se le exige al tracking.

import numpy as np
import pytest
from fastapi.testclient import TestClient

import api.mainAPI as main
from api.func.render import (
    GeometriaInvalida,
    SceneGeometry,
    StreamSession,
    get_draw_config,
    parsear_geometria,
    reset_draw_config,
    update_draw_config,
)
from api.func.render.geometry import MAX_ZONAS
from api.func.tasks.detection import render_detection
from api.func.tasks.domain import detections_from_array, empty_detections


# Un cuadrado centrado que deja libre el 10% de cada borde.
CUADRADO = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


def mensaje(zonas=None, w=640, h=480, **extra):
    """Un mensaje de control 'geometry' bien formado."""
    payload = {"type": "geometry", "frame": {"w": w, "h": h},
               "zones": [{"id": "z1", "points": CUADRADO}] if zonas is None else zonas}
    payload.update(extra)
    return payload


@pytest.fixture(autouse=True)
def _draw_limpio():
    """Los ajustes de dibujo son un singleton de proceso: sin esto los casos se pisan."""
    reset_draw_config()
    yield
    reset_draw_config()


@pytest.fixture
def sesion_con_zona():
    s = StreamSession()
    s.set_geometry(mensaje())
    return s


def _dets(*cajas):
    filas = [[x1, y1, x2, y2, 0.9, 0] for (x1, y1, x2, y2) in cajas]
    return detections_from_array(np.array(filas, dtype=np.float32))


def _frame(w=640, h=480):
    # Ruido y no negro: dos frames negros comprimen igual aunque uno tenga una linea
    # oscura dibujada, y varios casos comparan bytes de JPEG.
    rng = np.random.default_rng(7)
    return rng.integers(0, 255, (h, w, 3), dtype=np.uint8)


# ── 1. LOS DOS CAJONES ──────────────────────────────────────────────────────

def test_la_geometria_sobrevive_al_cambio_de_modelo(sesion_con_zona):
    """
    EL test del spec. sync() olvida el tracker porque las identidades nacidas con
    otro pipeline no significan nada; la zona describe LA ESCENA, y comparar dos
    modelos sobre la misma zona es el caso de uso del banco de pruebas.
    """
    sesion_con_zona.sync(1)
    sesion_con_zona.sync(2)      # cambio de modelo
    assert len(sesion_con_zona.geometria) == 1
    assert sesion_con_zona.tiene_geometria


def test_el_mismo_sync_que_conserva_la_zona_si_borra_el_tracker(sesion_con_zona):
    """Los dos cajones, en un solo caso: el reseteo alcanza a uno y no al otro."""
    cfg = update_draw_config(tracking=True)
    sesion_con_zona.sync(1)
    sesion_con_zona.process(_dets((10, 10, 50, 50)), cfg, 0.3)
    assert sesion_con_zona.tracking_activo

    sesion_con_zona.sync(2)
    assert not sesion_con_zona.tracking_activo, "el tracker tiene que olvidarse"
    assert len(sesion_con_zona.geometria) == 1, "la zona NO"


def test_reset_explicito_tampoco_toca_la_geometria(sesion_con_zona):
    sesion_con_zona.reset()
    assert len(sesion_con_zona.geometria) == 1


def test_una_foto_suelta_igual_puede_tener_zona():
    """
    stateful=false declara que no hay memoria TEMPORAL que construir, no que no haya
    escena: contar vehiculos en una region de una imagen es un uso legitimo. Por eso
    el cajon de la escena no se gatea por _stateful y el del tracker si.
    """
    s = StreamSession(stateful=False)
    ack = s.set_geometry(mensaje())
    assert ack["error"] is None and s.tiene_geometria


def test_la_sesion_nace_sin_geometria():
    s = StreamSession()
    assert not s.tiene_geometria and s.geometria.vacia


# ── 2. PARSEO Y VALIDACION ──────────────────────────────────────────────────

def test_parseo_de_un_mensaje_valido():
    (w, h), zonas = parsear_geometria(mensaje())
    assert (w, h) == (640, 480)
    assert len(zonas) == 1 and zonas[0].id == "z1"
    assert zonas[0].puntos.shape == (4, 2)


@pytest.mark.parametrize("payload, trozo", [
    ({"type": "geometry"}, "frame"),
    (mensaje(w=0), "positivo"),
    (mensaje(zonas=[{"id": "z1", "points": [[0, 0], [1, 1]]}]), "vertices"),
    (mensaje(zonas=[{"points": CUADRADO}]), "id"),
    (mensaje(zonas=[{"id": "z1", "points": CUADRADO},
                    {"id": "z1", "points": CUADRADO}]), "repetido"),
    (mensaje(zonas=[{"id": "z1", "points": [[0.1, 0.1], [0.5, 0.5], [0.9, 0.9]]}]), "area"),
    (mensaje(zonas=[{"id": "z1", "points": [[0.1, "x"], [0.5, 0.5], [0.9, 0.9]]}]), "numeros"),
    (mensaje(zonas="no soy una lista"), "lista"),
])
def test_geometria_invalida_dice_por_que(payload, trozo):
    """El motivo viaja al ack: un rechazo mudo deja al usuario sin saber que arreglar."""
    with pytest.raises(GeometriaInvalida) as e:
        parsear_geometria(payload)
    assert trozo in str(e.value)


def test_demasiadas_zonas():
    muchas = [{"id": "z%d" % i, "points": CUADRADO} for i in range(MAX_ZONAS + 1)]
    with pytest.raises(GeometriaInvalida):
        parsear_geometria(mensaje(zonas=muchas))


def test_las_lineas_de_conteo_se_rechazan_explicitamente():
    """
    Todavia no existen (van en la tanda siguiente por este mismo canal). Se rechaza
    en vez de ignorarlas: un cliente que las mande y no vea nada no tendria como
    darse cuenta de que el backend no las entiende.
    """
    payload = mensaje(lines=[{"id": "l1", "a": [0.1, 0.5], "b": [0.9, 0.5]}])
    with pytest.raises(GeometriaInvalida) as e:
        parsear_geometria(payload)
    assert "linea" in str(e.value)


def test_puntos_fuera_del_frame_se_recortan_en_vez_de_rechazarse():
    """
    Arrastrar un vertice mas alla del borde es un gesto legitimo ("que la zona llegue
    hasta el borde"). Recortar evita obligar al cliente a hacer aritmetica defensiva.
    """
    _, zonas = parsear_geometria(mensaje(
        zonas=[{"id": "z1", "points": [[-0.5, -0.2], [1.7, 0.0], [1.2, 1.9], [0.0, 1.1]]}]))
    p = zonas[0].puntos
    assert p.min() >= 0.0 and p.max() <= 1.0


def test_un_mensaje_invalido_conserva_la_geometria_anterior(sesion_con_zona):
    ack = sesion_con_zona.set_geometry(
        mensaje(zonas=[{"id": "mala", "points": [[0, 0], [1, 1]]}]))
    assert ack["error"] is not None
    assert ack["zones"] == 1, "el ack informa el estado EFECTIVO, no el pedido"
    assert len(sesion_con_zona.geometria) == 1


def test_el_mensaje_es_declarativo_y_completo(sesion_con_zona):
    """Mandar la lista vacia BORRA las zonas: no es un delta, es todo el estado."""
    ack = sesion_con_zona.set_geometry(mensaje(zonas=[]))
    assert ack["error"] is None and ack["zones"] == 0
    assert not sesion_con_zona.tiene_geometria


def test_varias_zonas_a_la_vez():
    s = StreamSession()
    ack = s.set_geometry(mensaje(zonas=[
        {"id": "a", "points": [[0.0, 0.0], [0.4, 0.0], [0.4, 1.0], [0.0, 1.0]]},
        {"id": "b", "points": [[0.6, 0.0], [1.0, 0.0], [1.0, 1.0], [0.6, 1.0]]},
    ]))
    assert ack["zones"] == 2


# ── 3. CONTEO, ANCLAJE Y CACHE ──────────────────────────────────────────────

def test_cuenta_las_detecciones_de_adentro(sesion_con_zona):
    cfg = get_draw_config()
    dets = _dets((300, 220, 340, 260),     # centro (320,240): adentro
                 (0, 0, 20, 20))            # centro (10,10): afuera
    viva = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))[0]
    viva.poligono.trigger(dets)
    assert viva.poligono.current_count == 1


def test_el_anclaje_cambia_lo_que_se_cuenta(sesion_con_zona):
    """
    No es cosmetico y por eso el usuario lo elige: la MISMA caja se cuenta o no segun
    desde donde mire la camara. Esta caja tiene el centro adentro de la zona y el
    borde inferior afuera (cruza el borde de abajo, y=432 en pixeles).
    """
    dets = _dets((300, 400, 340, 460))     # centro y=430 adentro; base y=460 afuera

    cfg = update_draw_config(zone_anchor="centro")
    viva = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))[0]
    viva.poligono.trigger(dets)
    assert viva.poligono.current_count == 1

    cfg = update_draw_config(zone_anchor="inferior")
    viva = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))[0]
    viva.poligono.trigger(dets)
    assert viva.poligono.current_count == 0


def test_las_zonas_vivas_se_cachean(sesion_con_zona):
    """Construir un PolygonZone rasteriza una mascara: no es algo para hacer por frame."""
    cfg = get_draw_config()
    a = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))
    b = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))
    assert a is b


@pytest.mark.parametrize("cambio", [
    {"zone_anchor": "inferior"},   # cambia QUE se cuenta
    {"zone_color": "#FF0000"},     # cambia como se ve
])
def test_tocar_la_config_rehace_las_zonas(sesion_con_zona, cambio):
    a = sesion_con_zona.geometria.zonas_vivas(get_draw_config(), (640, 480))
    b = sesion_con_zona.geometria.zonas_vivas(update_draw_config(**cambio), (640, 480))
    assert a is not b


def test_cambiar_la_resolucion_rehace_las_zonas(sesion_con_zona):
    cfg = get_draw_config()
    a = sesion_con_zona.geometria.zonas_vivas(cfg, (640, 480))
    b = sesion_con_zona.geometria.zonas_vivas(cfg, (1280, 960))
    assert a is not b
    # Misma zona normalizada, el doble de pixeles.
    assert b[0].poligono.polygon.max() == pytest.approx(a[0].poligono.polygon.max() * 2, abs=2)


def test_la_zona_se_escala_a_pixeles(sesion_con_zona):
    viva = sesion_con_zona.geometria.zonas_vivas(get_draw_config(), (640, 480))[0]
    esperado = np.array([[64, 48], [576, 48], [576, 432], [64, 432]])
    assert np.array_equal(viva.poligono.polygon, esperado)


def test_geometria_de_otro_aspecto_no_se_dibuja(sesion_con_zona, caplog):
    """
    Es una ASERCION, no una funcion: sin persistencia esto no puede pasar en uso
    normal, asi que si pasa es un bug. Loguea con los dos tamanos y no dibuja.
    """
    with caplog.at_level("WARNING"):
        vivas = sesion_con_zona.geometria.zonas_vivas(get_draw_config(), (1920, 1080))
    assert vivas == ()
    assert "640x480" in caplog.text and "1920x1080" in caplog.text


def test_el_mismo_aspecto_en_otro_tamano_si_se_dibuja(sesion_con_zona):
    """La zona es normalizada: 640x480 y 1280x960 son la misma escena."""
    assert len(sesion_con_zona.geometria.zonas_vivas(get_draw_config(), (1280, 960))) == 1


# ── 4. EL RENDER ────────────────────────────────────────────────────────────

def test_la_zona_no_cambia_ninguna_deteccion(sesion_con_zona):
    """Capa de LECTURA, no filtro. Misma promesa que ya se le exige al tracking."""
    dets = _dets((300, 220, 340, 260), (0, 0, 20, 20))
    antes = dets.xyxy.copy()
    render_detection(dets, _frame(), get_draw_config(), sesion_con_zona)
    assert len(dets) == 2
    assert np.array_equal(dets.xyxy, antes)


def test_la_zona_cambia_el_frame(sesion_con_zona):
    img = _frame()
    dets = _dets((300, 220, 340, 260))
    con = render_detection(dets, img, get_draw_config(), sesion_con_zona)
    sin = render_detection(dets, img, get_draw_config(), StreamSession())
    assert con != sin


def test_la_zona_se_dibuja_aunque_no_haya_detecciones(sesion_con_zona):
    """
    Una zona que dice "0" es informacion; una que desaparece cuando la escena se
    vacia se lee como un bug. Ademas este es el camino que NO copiaba el frame.
    """
    img = _frame()
    con = render_detection(empty_detections(), img, get_draw_config(), sesion_con_zona)
    sin = render_detection(empty_detections(), img, get_draw_config(), StreamSession())
    assert con != sin


def test_render_sin_sesion_sigue_andando():
    """session=None (tests, llamadas sueltas) no puede romper el dibujo."""
    assert len(render_detection(_dets((10, 10, 50, 50)), _frame(), get_draw_config(), None)) > 0


# ── 5. EL CANAL DE CONTROL DEL WEBSOCKET ────────────────────────────────────

def test_el_control_se_contesta_con_un_ack():
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json(mensaje())
        ack = ws.receive_json()
    assert ack == {"type": "geometry_ack", "zones": 1, "lines": 0, "error": None}


def test_el_ack_llega_por_una_conexion_y_no_por_la_otra():
    """
    La razon de que esto viaje por el WS y no por HTTP: la geometria pertenece a UNA
    conexion. Un POST a un singleton no sabria a cual le esta hablando.
    """
    client = TestClient(main.app)
    with client.websocket_connect("/video_stream") as a, \
         client.websocket_connect("/video_stream") as b:
        a.send_json(mensaje())
        assert a.receive_json()["zones"] == 1
        b.send_json(mensaje(zonas=[]))
        assert b.receive_json()["zones"] == 0


def test_el_ws_sigue_respondiendo_uno_por_uno():
    """La invariante anti-deadlock: un mensaje por mensaje, sea control o frame."""
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        for _ in range(3):
            ws.send_json(mensaje())
            assert ws.receive_json()["type"] == "geometry_ack"
            ws.send_bytes(b"\xff\xd8\xff\xe0 basura")
            assert ws.receive_json()["error"] == "frame_invalido"


def test_un_frame_en_base64_no_se_confunde_con_control():
    """
    LA TRAMPA del canal: _decode_frame ya usaba el texto para base64. Un JPEG en
    base64 no parsea como objeto JSON, asi que tiene que seguir el camino de frame
    (y fallar como frame invalido, que es lo que es: no es un JPEG de verdad).
    """
    import base64
    falso = base64.b64encode(b"\xff\xd8\xff\xe0 no soy un jpeg").decode()
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_text(falso)
        assert ws.receive_json()["error"] == "frame_invalido"
        ws.send_text("data:image/jpeg;base64," + falso)
        assert ws.receive_json()["error"] == "frame_invalido"


def test_control_desconocido_se_contesta_y_no_rompe_el_stream():
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json({"type": "telepatia"})
        resp = ws.receive_json()
        assert resp["type"] == "control_error" and "telepatia" in resp["error"]
        ws.send_bytes(b"basura")
        assert ws.receive_json()["error"] == "frame_invalido"


def test_geometria_invalida_por_el_ws_no_mata_la_conexion():
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json(mensaje(zonas=[{"id": "z", "points": [[0, 0], [1, 1]]}]))
        ack = ws.receive_json()
        assert ack["type"] == "geometry_ack" and ack["error"] is not None
        ws.send_json(mensaje())
        assert ws.receive_json()["error"] is None


# ── 6. EL ENDPOINT DE DIBUJO ────────────────────────────────────────────────

def test_endpoint_acepta_los_ajustes_de_zona():
    client = TestClient(main.app)
    r = client.post("/config/draw", json={"zoneColor": "#FF8800", "zoneAnchor": "inferior"})
    assert r.status_code == 200
    draw = r.json()["draw"]
    assert draw["zoneColor"] == "#FF8800" and draw["zoneAnchor"] == "inferior"


@pytest.mark.parametrize("body", [
    {"zoneAnchor": "diagonal"},     # no esta en ZONE_ANCHORS
    {"zoneColor": "naranja"},       # no es #RRGGBB
])
def test_endpoint_rechaza_ajustes_de_zona_invalidos(body):
    assert TestClient(main.app).post("/config/draw", json=body).status_code == 422


def test_defaults_de_zona():
    """
    Ambar y no el cian de las cajas: una zona del mismo color que las detecciones se
    confunde con ellas justo cuando hay muchas, que es cuando la zona sirve. Y centro
    y no borde inferior: el primer modelo propio del usuario es de vista aerea.
    """
    cfg = reset_draw_config()
    assert cfg.zone_color == "#FFB020"
    assert cfg.zone_anchor == "centro"

# ── 7. EL ACUMULADO: cuantos objetos DISTINTOS pasaron ──────────────────────
#
# Vive en el cajon del TRACKER aunque la geometria viva en el de la escena, y esa
# asimetria es el punto: el poligono describe la escena y sobrevive al cambio de
# modelo; lo contado ahi adentro pertenece a las identidades de ESE modelo.

def _correr(sesion, cfg, cajas_por_frame, conf=0.3):
    """Pasa varios frames por tracking + zonas, como hace el handler del WS."""
    escena = _frame()
    for cajas in cajas_por_frame:
        dets = _dets(*cajas) if cajas else empty_detections()
        dets = sesion.process(dets, cfg, conf)
        sesion.anotar_zonas(escena.copy(), dets, cfg, (640, 480))


def _cruzando(x0, paso, n, y=200):
    """Un objeto que avanza en linea recta: n frames, una caja por frame."""
    return [[(x0 + i * paso, y, x0 + i * paso + 40, y + 50)] for i in range(n)]


def test_sin_acumulado_el_cartel_es_solo_la_ocupacion(sesion_con_zona):
    """El default no cambia: la zona responde cuantos hay AHORA."""
    cfg = reset_draw_config()
    assert cfg.zone_total is False
    _correr(sesion_con_zona, cfg, _cruzando(200, 10, 6))
    assert sesion_con_zona.conteo_de("z1") == 0, "sin el toggle no se acumula nada"


def test_pedir_acumulado_prende_el_tracking_solo():
    """
    Misma coherencia que smoothing y traces, y por el mismo motivo: sin tracker_id no
    hay como distinguir el mismo objeto durante 30 frames de 30 objetos.
    """
    cfg = update_draw_config(zone_total=True)
    assert cfg.tracking is True and cfg.zone_total is True


def test_apagar_el_tracking_apaga_el_acumulado():
    update_draw_config(zone_total=True)
    cfg = update_draw_config(tracking=False)
    assert cfg.zone_total is False


def test_un_objeto_que_pasa_cuenta_UNA_vez(sesion_con_zona):
    """30 frames del mismo auto son un auto, no treinta. Es la razon de ser del tracking."""
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    assert sesion_con_zona.conteo_de("z1") == 1


def test_dos_objetos_distintos_cuentan_dos(sesion_con_zona):
    cfg = update_draw_config(zone_total=True)
    frames = [[(200 + i * 5, 150, 240 + i * 5, 200), (300 + i * 5, 300, 340 + i * 5, 350)]
              for i in range(20)]
    _correr(sesion_con_zona, cfg, frames)
    assert sesion_con_zona.conteo_de("z1") == 2


def test_lo_que_pasa_de_largo_por_afuera_no_cuenta(sesion_con_zona):
    """La zona es un filtro de lectura: un objeto que nunca entra no suma."""
    cfg = update_draw_config(zone_total=True)
    # y=460 esta debajo del borde inferior de la zona (0.9 * 480 = 432).
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20, y=455))
    assert sesion_con_zona.conteo_de("z1") == 0


def test_el_acumulado_no_baja_cuando_el_objeto_se_va(sesion_con_zona):
    """Que es, justamente, lo que lo distingue de la ocupacion instantanea."""
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    antes = sesion_con_zona.conteo_de("z1")
    _correr(sesion_con_zona, cfg, [[] for _ in range(15)])   # la escena se vacia
    assert antes == 1 and sesion_con_zona.conteo_de("z1") == 1


# ── Los reseteos: donde muere el tracker, muere la cuenta ───────────────────

def test_cambiar_de_modelo_resetea_la_cuenta_pero_NO_la_zona(sesion_con_zona):
    """
    El caso que justifica que los dos hechos vivan en cajones distintos: la zona
    tiene que sobrevivir para poder comparar dos modelos, y la cuenta NO, porque las
    identidades del modelo viejo no significan nada en el nuevo.
    """
    cfg = update_draw_config(zone_total=True)
    sesion_con_zona.sync(1)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    assert sesion_con_zona.conteo_de("z1") == 1

    sesion_con_zona.sync(2)
    assert sesion_con_zona.conteo_de("z1") == 0
    assert len(sesion_con_zona.geometria) == 1


def test_mover_el_umbral_resetea_la_cuenta(sesion_con_zona):
    """
    LA TRAMPA VERIFICADA CONTRA LA LIBRERIA: un ByteTrackTracker recien construido
    vuelve a numerar DESDE 0. Si el conjunto de ids sobreviviera a la reconstruccion,
    el primer objeto nuevo llegaria con un id ya visto y NO SE CONTARIA — y el sintoma
    no seria un error, seria un numero corto sin explicacion.
    """
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20), conf=0.3)
    assert sesion_con_zona.conteo_de("z1") == 1
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20), conf=0.5)   # otro umbral
    # Se reconstruyo el tracker: la cuenta arranca de nuevo y vuelve a contar 1, en vez
    # de tragarse el objeto por creer que ya lo habia visto.
    assert sesion_con_zona.conteo_de("z1") == 1


def test_apagar_el_tracking_borra_la_cuenta(sesion_con_zona):
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    assert sesion_con_zona.conteo_de("z1") == 1
    sesion_con_zona.process(_dets((10, 10, 50, 50)), update_draw_config(tracking=False), 0.3)
    assert sesion_con_zona.conteo_de("z1") == 0


def test_borrar_una_zona_borra_su_cuenta_y_la_nueva_no_la_hereda(sesion_con_zona):
    """
    Los ids de zona se REUSAN (el cliente numera z1, z2, ... y rellena los huecos), asi
    que sin podar, una zona nueva heredaria la cuenta de la que se acaba de borrar.
    """
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    assert sesion_con_zona.conteo_de("z1") == 1
    sesion_con_zona.set_geometry(mensaje(zonas=[]))          # se borra z1
    sesion_con_zona.set_geometry(mensaje())                  # se dibuja otra, tambien z1
    assert sesion_con_zona.conteo_de("z1") == 0


def test_mover_un_vertice_NO_resetea_la_cuenta(sesion_con_zona):
    """
    Deliberado: el usuario esta ajustando la region, y perder la cuenta en cada
    arrastre haria el numero inutil justo mientras se lo acomoda. Para eso esta el
    boton de resetear.
    """
    cfg = update_draw_config(zone_total=True)
    _correr(sesion_con_zona, cfg, _cruzando(200, 5, 20))
    sesion_con_zona.set_geometry(mensaje(
        zonas=[{"id": "z1", "points": [[0.12, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]}]))
    assert sesion_con_zona.conteo_de("z1") == 1


def test_reset_a_mano_de_una_zona_y_de_todas():
    s = StreamSession()
    s.set_geometry(mensaje(zonas=[{"id": "a", "points": CUADRADO},
                                  {"id": "b", "points": CUADRADO}]))
    s._zona_vistos = {"a": {1, 2, 3}, "b": {7}}
    ack = s.reset_zone_count("a")
    assert ack == {"type": "zone_reset_ack", "zone": "a", "error": None}
    assert s.conteo_de("a") == 0 and s.conteo_de("b") == 1
    s.reset_zone_count()
    assert s.conteo_de("b") == 0


def test_una_foto_suelta_no_acumula():
    """Un frame no es una secuencia: no hay nada que acumular ni tracker que lo cuente."""
    s = StreamSession(stateful=False)
    s.set_geometry(mensaje())
    cfg = update_draw_config(zone_total=True)
    _correr(s, cfg, _cruzando(200, 5, 3))
    assert s.conteo_de("z1") == 0


# ── El cartel ──────────────────────────────────────────────────────────────

def test_el_acumulado_cambia_el_frame(sesion_con_zona):
    """El cartel pasa de "12" a "12 / 47": son pixeles distintos."""
    img = _frame()
    dets = _dets((300, 220, 340, 260))
    sin = render_detection(dets, img, reset_draw_config(), StreamSession())
    cfg = update_draw_config(zone_total=True)
    sesion_con_zona.process(dets, cfg, 0.3)
    con = render_detection(dets, img, cfg, sesion_con_zona)
    assert con != sin


def test_el_acumulado_no_cambia_ninguna_deteccion(sesion_con_zona):
    """Sigue siendo una capa de lectura, tambien cuando cuenta."""
    cfg = update_draw_config(zone_total=True)
    dets = _dets((300, 220, 340, 260), (0, 0, 20, 20))
    dets = sesion_con_zona.process(dets, cfg, 0.3)
    antes = dets.xyxy.copy()
    render_detection(dets, _frame(), cfg, sesion_con_zona)
    assert len(dets) == 2 and np.array_equal(dets.xyxy, antes)


# ── El endpoint y el canal de control ──────────────────────────────────────

def test_endpoint_acepta_zone_total_y_prende_el_tracking():
    r = TestClient(main.app).post("/config/draw", json={"zoneTotal": True})
    assert r.status_code == 200
    draw = r.json()["draw"]
    assert draw["zoneTotal"] is True and draw["tracking"] is True


def test_endpoint_apagar_tracking_devuelve_zone_total_apagado():
    client = TestClient(main.app)
    client.post("/config/draw", json={"zoneTotal": True})
    draw = client.post("/config/draw", json={"tracking": False}).json()["draw"]
    assert draw["zoneTotal"] is False


def test_reset_por_el_websocket():
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json(mensaje())
        ws.receive_json()
        ws.send_json({"type": "zone_reset", "id": "z1"})
        assert ws.receive_json() == {"type": "zone_reset_ack", "zone": "z1", "error": None}
        ws.send_json({"type": "zone_reset"})          # sin id: todas
        assert ws.receive_json()["error"] is None


def test_reset_con_id_invalido_no_rompe_el_stream():
    main.controller.unload_model()
    with TestClient(main.app).websocket_connect("/video_stream") as ws:
        ws.send_json({"type": "zone_reset", "id": 7})
        assert ws.receive_json()["error"] is not None
        ws.send_bytes(b"\xff\xd8 basura")
        assert ws.receive_json()["error"] == "frame_invalido"


def test_zone_total_nace_apagado():
    assert reset_draw_config().zone_total is False
