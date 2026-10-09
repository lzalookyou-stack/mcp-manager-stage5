"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings  # noqa: E402
from app.runtime import Runtime  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path):
    """每个测试用独立的临时数据目录，互不污染。"""
    return load_settings(
        host="127.0.0.1",
        port=8765,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def runtime(settings):
    rt = Runtime.create(settings)
    yield rt
    rt.close()


@pytest.fixture()
def service(runtime):
    return runtime.plugins