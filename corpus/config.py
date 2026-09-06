from __future__ import annotations
import os
from pathlib import Path
try: import tomllib
except ImportError: tomllib=None
DEFAULT_DB=Path('/home/node/.openclaw/workspace/projects/_corpus/corpus.db')
def load_config(path=None):
 p=Path(path or os.environ.get('CORPUS_CONFIG','corpus.toml')); d=tomllib.loads(p.read_text()) if p.exists() and tomllib else {}; x=d.setdefault('paths',{}); x.setdefault('corpus_db',str(DEFAULT_DB)); x.setdefault('worker_socket','/tmp/corpus-worker.sock')
 if not x.get('vec_ext'):
  try:
   import sqlite_vec; x['vec_ext']=sqlite_vec.get_loadable_path()
  except Exception: x['vec_ext']=''
 for k in ('embedding','query','scoring'): d.setdefault(k,{})
 return d
config=load_config()
