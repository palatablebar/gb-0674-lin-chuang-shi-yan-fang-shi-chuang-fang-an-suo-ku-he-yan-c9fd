from importlib import import_module
from pathlib import Path
from copy import deepcopy
import json, time, base64
from app.config import ROOT, MODULES, MAX_BYTES
from app.errors import BusinessError
from app.manifest import digest, make_manifest
from app.correction import apply_correction, parse_spec

def evaluate(payload=None, until=7, snapshot=None):
    payload=payload or {}
    dataset=payload.get('dataset','dirty')
    if dataset not in {'dirty','clean','legacy'}: raise BusinessError('BAD_DATASET','样例名称无效')
    if not isinstance(payload.get('overrides',{}),dict): raise BusinessError('BAD_CONTEXT','参数须为对象')
    raw=(base64.b64decode(snapshot['bytes_b64'],validate=True) if snapshot is not None else (str(payload['csv_text']).encode() if 'csv_text' in payload else (ROOT/'sample_data'/f'{dataset}.csv').read_bytes()))
    if len(raw)>MAX_BYTES: raise BusinessError('TOO_LARGE','文件超过2MB',413)
    context=deepcopy(snapshot['context']) if snapshot is not None else json.loads((ROOT/'sample_data/context.json').read_text())
    context.update(deepcopy(payload.get('overrides',{})))
    correction=deepcopy(snapshot['correction']) if snapshot is not None and 'correction' in snapshot else payload.get('correction')
    if correction is not None: correction=parse_spec(correction)
    start=time.perf_counter()
    state={'raw_bytes':raw,'context':context,'records':[],'quarantine':[], 'counts':{},'views':{},'source':dataset}
    for index,key in enumerate(MODULES,1):
        if index>until: break
        import_module(f'app.modules.{key}.rules').apply(state)
        if index==1 and correction: apply_correction(state,correction)
    state['input_snapshot']={'bytes_b64':base64.b64encode(raw).decode(),'context':deepcopy(context)}
    if correction: state['input_snapshot']['correction']=deepcopy(correction)
    state.pop('raw_bytes',None)
    state['manifest']=make_manifest(digest(raw,context,correction),dataset,time.perf_counter()-start)
    return state
