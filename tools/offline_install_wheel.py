import sys

sys.dont_write_bytecode = True

import ensurepip
import os
from pathlib import Path
import runpy
import socket


def main():
    boundary = Path(os.environ['CORPUS_AUTHORIZED_SANDBOX_ROOT']).resolve(strict=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    wheel = Path(sys.argv[1]).resolve(strict=True)
    home = Path(os.environ['HOME']).resolve(strict=True)
    temporary = Path(os.environ['TMPDIR']).resolve(strict=True)
    if boundary == Path('/') or not all(path.is_relative_to(boundary) for path in (prefix, wheel, home, temporary)):
        raise SystemExit('wheel, installation venv, HOME and TMPDIR must be inside explicit sandbox')
    bundled = sorted((Path(ensurepip.__file__).parent / '_bundled').glob('pip-*.whl'))
    if len(bundled) != 1:
        raise SystemExit('exactly one interpreter-bundled pip wheel required; no network bootstrap')
    sys.path.insert(0, str(bundled[0]))
    def denied(*args, **kwargs):
        raise RuntimeError('offline wheel installer forbids network')
    socket.create_connection = denied
    socket.socket.connect = denied
    socket.getaddrinfo = denied
    os.environ['PIP_CONFIG_FILE'] = os.devnull
    from pip._vendor.distro import distro
    distro._distro = distro.LinuxDistribution(root_dir=str(home), include_lsb=False, include_uname=False, include_oslevel=False)
    print('installer: interpreter-bundled pip; distribution metadata root:', home, flush=True)
    print('wheel:', wheel, 'target venv:', prefix, flush=True)
    sys.argv = ['pip', 'install', '--no-index', '--no-deps', '--no-cache-dir', '--disable-pip-version-check', str(wheel)]
    runpy.run_module('pip', run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
