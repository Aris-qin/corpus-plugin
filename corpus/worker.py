from __future__ import annotations
import errno,os,socket,fcntl,json,hashlib
from enum import Enum
from pathlib import Path
class WorkerState(str,Enum):
 STOPPED='STOPPED'; STARTING='STARTING'; READY='READY'; DEGRADED='DEGRADED'; BACKOFF='BACKOFF'
_ALLOWED={WorkerState.STOPPED:{WorkerState.STARTING},WorkerState.STARTING:{WorkerState.READY,WorkerState.DEGRADED,WorkerState.BACKOFF},WorkerState.READY:{WorkerState.DEGRADED,WorkerState.STOPPED},WorkerState.DEGRADED:{WorkerState.READY,WorkerState.BACKOFF,WorkerState.STOPPED},WorkerState.BACKOFF:{WorkerState.STARTING,WorkerState.STOPPED}}
class Worker:
 def __init__(self,pidfile,socket_path,db=None,db_path=None,embedding_dim=1024):
  self.pidfile=Path(pidfile); self.socket_path=Path(socket_path); self.state=WorkerState.STOPPED; self._lock=None
  self._db=db
 def _default_query(self, params):
  from .db import heading_match_jaccard, text_keyword_hit
  if self._db is None: raise RuntimeError('default query requires db injection')
  emb=params.get('embedding') or params.get('query_embedding')
  if emb is None: raise ValueError('query requires embedding')
  keys=str(params.get('query','')).lower().split(); out=[]
  for cid,dist in self._db.vector_search(emb,project=params.get('project'),pmid_filter=params.get('pmid_filter'),top_k=int(params.get('top_k',10))):
   row=self._db.conn.execute('SELECT chunk_id,pmid,text,heading_path,snippet,ordinal,level,path,parent_id,generation FROM chunks WHERE chunk_id=?',(cid,)).fetchone()
   if row:
    hm,_=heading_match_jaccard(keys,row[3] or '') if keys else (False,0); hit=text_keyword_hit(row[2] or '',keys) if keys else 0; out.append({'chunk_id':row[0],'pmid':row[1],'text':row[2],'heading_path':row[3] or '','snippet':row[4],'ordinal':row[5],'level':row[6],'path':row[7],'parent_id':row[8],'generation':row[9],'heading_match':hm,'score':1/(1+float(dist))})
  return {'schema_version':1,'results':out}
 def _default_score(self, params):
  if self._db is None: raise RuntimeError('default score requires db injection')
  allowed=('pmid','project','formula_version','rubric_version','quality_final','quality_status','source','source_event_id','criterion_validity','outcome_reliability','conclusion_data_consistency','prior_score','canonical_input_json','override_reason','override_by','scored_at')
  row=self._db.insert_quality_v3(**{k:params[k] for k in allowed if k in params}); return {'schema_version':1,'evidence':row,'inserted':row.get('inserted',False)}
 def transition(self,state):
  state=WorkerState(state)
  if state not in _ALLOWED[self.state]: raise ValueError(f'invalid transition {self.state}->{state}')
  self.state=state
 def acquire(self):
  self.pidfile.parent.mkdir(parents=True,exist_ok=True); self._lock=open(self.pidfile,'a+')
  try: fcntl.flock(self._lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError as e:
   self._lock.close(); self._lock=None
   if e.errno in (errno.EACCES,errno.EAGAIN): raise RuntimeError('worker already running')
   raise
  self._lock.seek(0); self._lock.truncate(); self._lock.write(str(os.getpid())); self._lock.flush()
 def release(self):
  if self._lock: fcntl.flock(self._lock,fcntl.LOCK_UN); self._lock.close(); self._lock=None
  self.state=WorkerState.STOPPED
 def clear_stale_socket(self):
  if not self.socket_path.exists(): return False
  try:
   s=socket.socket(socket.AF_UNIX); s.settimeout(.1); s.connect(str(self.socket_path)); s.close(); return False
  except OSError: self.socket_path.unlink(); return True
 @staticmethod
 def backoff_delays(attempts=3): return [2**i for i in range(min(attempts,3))]

 def handle(self, request, *, query_handler=None, score_handler=None):
  if request.get('schema_version') != 1 or not request.get('request_id'):
   return {'schema_version':1,'request_id':request.get('request_id',''),'ok':False,'error':{'code':'invalid_request'}}
  try:
   command=request.get('command'); params=request.get('params') or request
   if command == 'health': result={'state':self.state.value}
   elif command == 'query': result=query_handler(params) if query_handler else self._default_query(params)
   elif command == 'score': result=score_handler(params) if score_handler else self._default_score(params)
   else: raise ValueError(f'unknown command: {command}')
   return {'schema_version':1,'request_id':request['request_id'],'ok':True,'result':result}
  except Exception as exc:
   return {'schema_version':1,'request_id':request.get('request_id',''),'ok':False,'error':{'code':'handler_error','message':str(exc)}}

 def serve_once(self, conn, *, query_handler=None, score_handler=None):
  from .ipc import FrameReader, encode_frame
  reader=FrameReader()
  while True:
   data=conn.recv(65536)
   if not data: break
   for req in reader.feed(data): conn.sendall(encode_frame(self.handle(req, query_handler=query_handler, score_handler=score_handler)))

 def start(self):
  self.clear_stale_socket(); self.acquire(); self.transition(WorkerState.STARTING)
  self.socket_path.parent.mkdir(parents=True,exist_ok=True)
  sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); sock.bind(str(self.socket_path)); os.chmod(self.socket_path,0o600); sock.listen(16)
  self.transition(WorkerState.READY); return sock
