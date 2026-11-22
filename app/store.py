import sqlite3, json
from pathlib import Path
from app.errors import BusinessError

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    con.execute("CREATE TABLE IF NOT EXISTS official (id INTEGER PRIMARY KEY CHECK(id=1), run_id TEXT NOT NULL)")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def official(path):
    with connect(path) as con:
        row=con.execute('SELECT run_id FROM official WHERE id=1').fetchone()
    return row['run_id'] if row else None

def set_official(path, run_id):
    with connect(path) as con:
        if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
            raise BusinessError('NOT_FOUND','批次不存在',404)
        con.execute('INSERT INTO official(id,run_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id',(run_id,))
    return run_id

def save_and_switch(path, result):
    """保存重归属结果并切换正式结果，同一事务；任一步失败整体回滚，旧版正式结果保留。"""
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
        con.execute('INSERT INTO official(id,run_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id',(run_id,))
    return get(path,run_id)
