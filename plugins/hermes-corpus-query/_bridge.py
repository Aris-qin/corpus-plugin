from pathlib import Path
import sys, os, json
runtime, boundary, settings, tool = sys.argv[1:]
sys.path.insert(0, runtime)
os.environ.update(CORPUS_AUTHORIZED_SANDBOX_ROOT=boundary, CORPUS_SETTINGS_FILE=settings, PYTHONDONTWRITEBYTECODE='1')
from corpus_core.plugin import handler_for
print(handler_for(tool)(json.loads(sys.stdin.read())))
