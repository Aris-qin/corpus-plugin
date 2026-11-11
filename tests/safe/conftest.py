import os
import socket
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption('--sandbox-root', required=True)


def pytest_configure(config):
    sandbox = Path(config.getoption('--sandbox-root')).resolve()
    boundary = sandbox
    if sandbox == Path('/') or not sandbox.is_dir():
        raise pytest.UsageError('explicit sandbox root must exist and cannot be filesystem root')
    basetemp = config.getoption('basetemp')
    if not basetemp or not Path(basetemp).resolve().is_relative_to(sandbox):
        raise pytest.UsageError('explicit --basetemp beneath sandbox root is required before any fixture runs')
    for variable in ('HOME', 'TMPDIR'):
        if not os.environ.get(variable) or not Path(os.environ[variable]).resolve().is_relative_to(boundary):
            raise pytest.UsageError(variable + ' must be explicitly isolated beneath candidate boundary')
    os.environ['CORPUS_OFFLINE'] = '1'
    os.environ.pop('CORPUS_SETTINGS_FILE', None)
    for variable in ('CORPUS_SANDBOX_ROOT', 'CORPUS_DATA_ROOT'):
        if not os.environ.get(variable) or not Path(os.environ[variable]).resolve().is_relative_to(boundary):
            raise pytest.UsageError(variable + ' must be explicitly isolated beneath candidate boundary')
    os.environ['CORPUS_AUTHORIZED_SANDBOX_ROOT'] = str(sandbox)
    import corpus_core
    if not Path(corpus_core.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[2]):
        raise pytest.UsageError('tests must import local candidate source')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('ordinary tests must be offline')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)


@pytest.fixture
def root(request, tmp_path):
    allowed = Path(request.config.getoption('--sandbox-root')).resolve(strict=True)
    assert tmp_path.resolve().is_relative_to(allowed)
    os.environ['CORPUS_TEST_ROOT'] = str(allowed)
    return tmp_path / 'candidate'


@pytest.fixture
def simulated_approval_boundary(monkeypatch):
    from corpus_core.service import Corpus
    monkeypatch.setattr(Corpus, 'require_sensitive_actions', lambda self: None)
