# test_torchscript_ext.py — reconocer la extension .torchscript.
#
# Ultralytics exporta TorchScript con extension '.torchscript'. El archivo es TorchScript
# igual que un '.pt' exportado con torch.jit.save() —la extension NO es parte del formato,
# torch.jit.load() mira el contenido del zip— pero el sistema no lo tenia en su lista
# blanca y el modelo era invisible.
#
# La salida obvia era renombrarlo a '.pt', y es peor de lo que parece: los pesos Y el
# config se buscan por BASENAME, asi que renombrar puede hacerlo chocar con otro modelo
# que ya ocupe ese nombre. Ahi el que gana es el de mayor preferencia de extension y el
# otro queda inalcanzable EN SILENCIO — que es como llegan los peores bugs.

import pytest
from fastapi.testclient import TestClient

import api.mainAPI as main


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    models = tmp_path / "models"
    configs = tmp_path / "configs"
    models.mkdir()
    configs.mkdir()
    monkeypatch.setattr(main, "MODELS_DIR", models)
    monkeypatch.setattr(main, "CONFIGS_DIR", configs)
    return TestClient(main.app), models, configs


def test_torchscript_esta_en_la_lista_blanca():
    assert ".torchscript" in main.MODEL_EXTENSIONS


def test_las_dos_listas_no_se_desincronizan():
    """
    MODEL_EXTENSIONS decide QUE se acepta y _EXTENSION_PREFERENCE decide CUAL gana: si una
    tiene una extension que la otra no, _find_model_file revienta con ValueError al buscar
    el indice, y el sintoma seria un 500 al seleccionar un modelo perfectamente valido.
    """
    assert set(main._EXTENSION_PREFERENCE) == set(main.MODEL_EXTENSIONS)
    assert len(main._EXTENSION_PREFERENCE) == len(main.MODEL_EXTENSIONS)


def test_torchscript_le_gana_a_pt():
    """
    Ante el mismo modelo en los dos formatos gana el que SEGURO carga: '.torchscript' es
    TorchScript por construccion, mientras que un '.pt' puede ser un checkpoint pickle que
    torch.jit.load NO abre (paso con models/best.pt en agosto).
    """
    pref = main._EXTENSION_PREFERENCE
    assert pref.index(".torchscript") < pref.index(".pt")
    assert pref.index(".torchscript") < pref.index(".pth")
    # Pero no le gana a onnx: si el usuario tiene los dos, onnx es el camino rapido.
    assert pref.index(".onnx") < pref.index(".torchscript")


def test_find_model_file_lo_encuentra(ctx, monkeypatch):
    _, models, _ = ctx
    (models / "vacas.torchscript").write_bytes(b"no importa el contenido aca")
    assert main._find_model_file("vacas").endswith("vacas.torchscript")


def test_con_los_dos_formatos_elige_torchscript_sobre_pt(ctx):
    _, models, _ = ctx
    (models / "vacas.torchscript").write_bytes(b"x")
    (models / "vacas.pt").write_bytes(b"x")
    assert main._find_model_file("vacas").endswith("vacas.torchscript")


def test_se_lista_como_peso_disponible(ctx):
    """GET /models: todos los pesos, tengan config o no."""
    client, models, _ = ctx
    (models / "vacas.torchscript").write_bytes(b"x")
    r = client.get("/models")
    assert r.status_code == 200
    archivos = {m["file"]: m for m in r.json()["models"]}
    assert "vacas.torchscript" in archivos
    assert archivos["vacas.torchscript"]["baseName"] == "vacas"
    assert archivos["vacas.torchscript"]["hasConfig"] is False


def test_con_config_se_vuelve_seleccionable(ctx):
    """GET /get_models: solo los que tienen config Y pesos."""
    client, models, configs = ctx
    (models / "vacas.torchscript").write_bytes(b"x")
    (configs / "vacas.json").write_text("{}", encoding="utf-8")
    assert "vacas" in client.get("/get_models").json()["models"]


def test_se_puede_subir_por_el_endpoint(ctx):
    """Antes el upload lo rechazaba por extension y no habia forma de meterlo por la app."""
    client, models, _ = ctx
    r = client.post("/models/upload",
                    files={"file": ("vacas.torchscript", b"contenido", "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert (models / "vacas.torchscript").exists()


def test_una_extension_inventada_sigue_rechazandose(ctx):
    """La lista blanca sigue siendo lista blanca."""
    client, _, _ = ctx
    r = client.post("/models/upload",
                    files={"file": ("vacas.pesos", b"x", "application/octet-stream")})
    assert r.status_code == 422
