from pathlib import Path

from perimeter_core.config import InferenceConfig
from perimeter_inference.server import InferenceServer


def test_relative_model_path_is_resolved(tmp_path, monkeypatch):
    # Сервер стартует с cwd движка, поэтому относительный путь обязан
    # разрешаться от каталога вызывающего, иначе движок не найдёт веса.
    model = tmp_path / "weights"
    model.mkdir()
    monkeypatch.chdir(tmp_path)
    srv = InferenceServer(InferenceConfig(), model_path="weights")
    assert Path(srv.model_path).is_absolute()
    assert Path(srv.model_path) == model.resolve()


def test_absolute_path_kept(tmp_path):
    srv = InferenceServer(InferenceConfig(), model_path=str(tmp_path))
    assert Path(srv.model_path) == tmp_path.resolve()


def test_empty_path_stays_empty():
    assert InferenceServer(InferenceConfig()).model_path == ""
