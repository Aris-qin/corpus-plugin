from __future__ import annotations
import errno,os,socket,fcntl
from enum import Enum
from pathlib import Path
class WorkerState(str,Enum):
 STOPPED='STOPPED'; STARTING='STARTING'; READY='READY'; DEGRADED='DEGRADED'; BACKOFF='BACKOFF'
_ALLOWED={WorkerState.STOPPED:{WorkerState.STARTING},WorkerState.STARTING:{WorkerState.READY,WorkerState.DEGRADED,WorkerState.BACKOFF},WorkerState.READY:{WorkerState.DEGRADED,WorkerState.STOPPED},WorkerState.DEGRADED:{WorkerState.READY,WorkerState.BACKOFF,WorkerState.STOPPED},WorkerState.BACKOFF:{WorkerState.STARTING,WorkerState.STOPPED}}
class Worker:
 def __init__(self,pidfile,socket_path): self.pidfile=Path(pidfile); self.socket_path=Path(socket_path); self.state=WorkerState.STOPPED; self._lock=None
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
