# test_umbral_efectivo.py — el umbral de confianza que el backend REPORTA.
#
# Nace de un bug real, encontrado por el usuario el 2026-09-09 mirando un volcado de
# detecciones: el slider del cliente arrancaba en un 50% hardcodeado que NO se enviaba
# al cargar un modelo ni se leia de ningun lado. O sea que el panel decia 50% mientras
# el backend filtraba con lo que declaraba el config del modelo — 0,15 en 'best'. Mas de
# la mitad de las detecciones dibujadas y exportadas estaban por debajo del numero que
# el panel afirmaba estar aplicando.
#
# La correccion es que el backend DIGA cual esta usando, en vez de que el cliente lo
# infiera del JSON o le imponga uno propio. Estos tests cargan modelos de verdad porque
# lo que se esta fijando es justamente que el numero salga del pipeline armado y no de
# una lectura de archivo.

import cv2
import numpy as np
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

import api.mainAPI as main

# Umbrales declarados en configs/. Se escriben aca a proposito: si alguien los cambia,
# estos tests fallan y obligan a mirar si el cambio fue querido.
UMBRALES = {"best": 0.15, "yolov7-tiny": 0.25}


@pytest.fixture(scope="module")
def client():
    c = TestClient(main.app)
    yield c
    main.controller.unload_model()


def _cargar(client, nombre):
    r = client.post("/select_model", json={"model_name": nombre})
    assert r.status_code == 200, r.text
    return r.json()


def _foto_aerea():
    ruta = Path(__file__).parent / "testing_images" / "autos_desde_arriba.png"
    return cv2.cvtColor(cv2.imread(str(ruta)), cv2.COLOR_BGR2RGB)


def test_select_model_devuelve_el_umbral_efectivo(client):
    """Sin esto el cliente no tiene con que saber cual es, y se inventaba uno."""
    assert _cargar(client, "best")["confidence"] == pytest.approx(UMBRALES["best"])


def test_es_el_que_usa_el_controller_no_el_que_dice_el_json(client):
    """
    La diferencia entre reportar lo que el sistema USA y lo que se INFIERE que deberia
    usar. Se lee del controller cargado, no del archivo de config.
    """
    devuelto = _cargar(client, "best")["confidence"]
    assert devuelto == pytest.approx(main.controller.confidence_threshold)


def test_cada_modelo_trae_el_suyo_y_son_distintos(client):
    """
    Por esto el cliente ADOPTA el umbral del modelo en vez de imponerle el que tenga en
    pantalla: cada config esta calibrado aparte ('best' usa 0,15 por ser vista aerea con
    objetos chicos) y forzarle uno generico dejaria a unos modelos ciegos y a otros
    llenos de ruido.
    """
    a = _cargar(client, "best")["confidence"]
    b = _cargar(client, "yolov7-tiny")["confidence"]
    assert a == pytest.approx(UMBRALES["best"])
    assert b == pytest.approx(UMBRALES["yolov7-tiny"])
    assert a != b


def test_mover_el_umbral_devuelve_el_efectivo(client):
    _cargar(client, "yolov7-tiny")
    r = client.post("/config/confidence", json={"value": 0.42})
    assert r.status_code == 200
    assert r.json()["new_confidence"] == pytest.approx(0.42)
    assert main.controller.confidence_threshold == pytest.approx(0.42)


def test_recargar_el_modelo_vuelve_al_umbral_del_config(client):
    """
    update_confidence escribe en MEMORIA, no en el JSON. Recargar relee el config, asi
    que el cliente tiene que volver a adoptar lo que devuelve la carga o se quedaria
    mostrando un valor que el backend ya no tiene — el bug original, otra vez.
    """
    _cargar(client, "yolov7-tiny")
    client.post("/config/confidence", json={"value": 0.9})
    assert main.controller.confidence_threshold == pytest.approx(0.9)
    assert _cargar(client, "yolov7-tiny")["confidence"] == pytest.approx(UMBRALES["yolov7-tiny"])


def test_sin_modelo_no_hay_umbral_que_informar():
    """0.0 y no un valor inventado: el cliente lo muestra como '—' y deshabilita el control."""
    main.controller.unload_model()
    assert main.controller.confidence_threshold == 0.0


def test_el_umbral_filtra_de_verdad(client):
    """
    La otra mitad de la respuesta al usuario: el volcado NO agrega detecciones de baja
    confianza, escribe lo que el pipeline dejo pasar. Lo que estaba mal era el numero
    que el panel mostraba, no el filtro.
    """
    _cargar(client, "best")
    img = _foto_aerea()

    client.post("/config/confidence", json={"value": UMBRALES["best"]})
    bajas = main.controller.inference(img)
    client.post("/config/confidence", json={"value": 0.80})
    altas = main.controller.inference(img)

    assert len(bajas) > len(altas), "subir el umbral tiene que dejar pasar menos"
    # Y lo que queda respeta el piso: es lo que despues termina en el archivo.
    if len(altas):
        assert float(np.min(altas.confidence)) >= 0.80
