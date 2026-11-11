"""Host-only adapter: route six contracts to the pinned dedicated interpreter.
All storage/provider settings come from Hermes plugin settings, never hardcoded credentials.
"""
import json
import os
from pathlib import Path
import subprocess
from .corpus_core.contracts import CONTRACTS, schema_for


def register(ctx):
    config = {key: ctx.get_config(key) for key in ('authorized_root', 'settings_file', 'runtime_python', 'runtime_root')}
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in config.values()):
        raise ValueError('corpus deployment requires explicit absolute runtime and storage settings')
    def handler_for(name):
        def handler(args, **kwargs):
            env = dict(os.environ)
            for key in ('PYTHONPATH', 'PYTHONHOME', 'CORPUS_OFFLINE', 'CORPUS_SETTINGS_FILE', 'CORPUS_SANDBOX_ROOT'):
                env.pop(key, None)
            env['PYTHONDONTWRITEBYTECODE'] = '1'
            try:
                result = subprocess.run([config['runtime_python'], '-I', '-B', str(Path(__file__).with_name('_bridge.py')),
                                         config['runtime_root'], config['authorized_root'], config['settings_file'], name],
                                        input=json.dumps(args, ensure_ascii=False, allow_nan=False),
                                        text=True, capture_output=True, env=env, timeout=180)
                if result.returncode:
                    raise RuntimeError('dedicated corpus runtime failed')
                parsed = json.loads(result.stdout)
                if type(parsed) is not dict or type(parsed.get('ok')) is not bool:
                    raise ValueError('invalid runtime envelope')
                return json.dumps(parsed, ensure_ascii=False, allow_nan=False)
            except subprocess.TimeoutExpired:
                return json.dumps({'ok':False,'result':None,'warnings':[], 'error':{'code':'RUNTIME_TIMEOUT','message':'Runtime outcome unknown; inspect operation receipt before retry','retryable':False,'details':{}}})
            except Exception:
                return json.dumps({'ok':False,'result':None,'warnings':[], 'error':{'code':'RUNTIME_UNAVAILABLE','message':'Dedicated corpus runtime unavailable; inspect deployment configuration','retryable':False,'details':{}}})
        return handler
    for name in CONTRACTS:
        schema = schema_for(name)
        ctx.register_tool(name=name, toolset='corpus', schema=schema, handler=handler_for(name), description=schema['description'])
