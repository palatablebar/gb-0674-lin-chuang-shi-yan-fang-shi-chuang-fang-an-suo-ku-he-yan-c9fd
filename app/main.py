from contextlib import asynccontextmanager
from importlib import import_module
from fastapi import FastAPI, Request, Body
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from app.config import ROOT, META, MODULES, DB_PATH
from app.errors import BusinessError
from app.engine import evaluate
from app import store
from app.archive import serialize
from app.correction import parse_spec
from app.web import templates

@asynccontextmanager
async def lifespan(app):
    store.save(DB_PATH,evaluate())
    yield
app=FastAPI(title=META['title'],lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'app/static')),name='static')
@app.exception_handler(BusinessError)
async def business_error(request:Request,error:BusinessError):
    return JSONResponse({'code':error.code,'detail':error.message,**error.extra},status_code=error.status)
@app.get('/')
def home():
    return RedirectResponse('/'+MODULES[0],status_code=303)
@app.get('/healthz')
def health():
    return {'status':'ok','project':META['short']}
@app.post('/api/runs')
def create(payload:dict=Body(default={})):
    return store.save(DB_PATH,evaluate(payload))
@app.post('/api/runs/{run_id}/rerun')
def rerun(run_id:str):
    previous=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in previous:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    return store.save(DB_PATH,evaluate({'dataset':previous['source']},snapshot=previous['input_snapshot']))
@app.post('/api/runs/{run_id}/correction')
def correct(run_id:str,payload:dict=Body(...)):
    base=store.get(DB_PATH,run_id)
    if 'input_snapshot' not in base:
        raise BusinessError('INPUT_SNAPSHOT_MISSING','旧批缺少源快照，请重新导入')
    if 'correction' in base['input_snapshot']:
        raise BusinessError('CORRECTION_CHAIN','本批次已是重归属结果，请基于其原始批次发起更正',409,
                            {'base_run_id':base.get('correction',{}).get('base_run_id','')})
    spec=parse_spec(payload)
    snapshot={'bytes_b64':base['input_snapshot']['bytes_b64'],'context':base['input_snapshot']['context'],'correction':spec}
    previous=store.official(DB_PATH)
    try:
        result=evaluate({'dataset':base['source']},snapshot=snapshot)
    except Exception:
        store.set_official(DB_PATH,previous or run_id)
        raise
    result['correction']['base_run_id']=run_id
    if result['balance']['status']=='可锁库':
        saved=store.save_and_switch(DB_PATH,result)
    else:
        saved=store.save(DB_PATH,result)
        store.set_official(DB_PATH,previous or run_id)
    current=store.official(DB_PATH)
    return {'run':saved,'official':current,'previous_official':previous,'switched':current==saved['manifest']['run_id']}
@app.get('/api/official')
def current_official():
    return {'run_id':store.official(DB_PATH)}
@app.get('/correction')
def correction_page(request:Request):
    run_id=request.query_params.get('run_id')
    state=store.get(DB_PATH,run_id) if run_id else store.save(DB_PATH,evaluate())
    official=store.official(DB_PATH)
    return templates.TemplateResponse(request=request,name='correction.html',
        context={'meta':META,'key':'correction','state':state,'view':None,'official':official,
                 'switched':official==state['manifest']['run_id']})
@app.get('/api/runs')
def runs():
    return store.history(DB_PATH)
@app.get('/api/runs/{run_id}')
def run(run_id:str):
    return store.get(DB_PATH,run_id)
@app.post('/api/runs/{run_id}/review')
def review(run_id:str,payload:dict=Body(...)):
    revision=payload.get('revision')
    if not isinstance(revision,int) or revision<1: raise BusinessError('BAD_REVISION','版本号须为正整数')
    return store.review(DB_PATH,run_id,revision)
@app.get('/api/export/{run_id}')
def export(run_id:str):
    result=store.get(DB_PATH,run_id)
    return Response(serialize(result),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{run_id}.zip"'})
for key in MODULES:
    app.include_router(import_module(f'app.modules.{key}.handler').router)
