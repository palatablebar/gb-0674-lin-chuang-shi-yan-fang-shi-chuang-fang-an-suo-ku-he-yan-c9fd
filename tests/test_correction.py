import io, json, zipfile
import pytest
from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
from app.correction import parse_spec
from app.errors import BusinessError

FULL_SWAP = {'pair': ['00003', '00004'],
             'records': ['00003-V1', '00003-V2', '00003-R1', '00004-V1', '00004-V2', '00004-R1'],
             'reason': '入组时筛选号互换，凭证 JC-2026-118'}

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)

def clean_run(c):
    return c.post('/api/runs', json={'dataset': 'clean'}).json()

def correct(c, run_id, spec):
    return c.post(f'/api/runs/{run_id}/correction', json=spec)

def test_lockable_correction_switches_official(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    r = correct(c, rid, FULL_SWAP)
    assert r.status_code == 200
    data = r.json()
    assert data['run']['balance']['status'] == '可锁库'
    assert data['run']['archive']['status'] == '可冻结'
    new_id = data['run']['manifest']['run_id']
    assert new_id != rid and data['switched'] is True
    assert c.get('/api/official').json()['run_id'] == new_id
    # 采信、方案、访视、锁库、归档引用同一归属
    primaries = {x['primary_id'] for x in data['run']['selected_results'] if x['subject'] == '00003'}
    assert primaries == {'00004-V1', '00004-V2'}
    intake = {row['id']: row for row in data['run']['views']['intake']['rows']}
    assert intake['00003-V1']['subject'] == '00004'
    assert data['run']['correction']['base_run_id'] == rid

def test_retry_same_correction_adds_nothing(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    a = correct(c, rid, FULL_SWAP).json()
    shuffled = {'pair': ['00004', '00003'], 'records': list(reversed(FULL_SWAP['records'])), 'reason': '入组时筛选号互换，凭证 JC-2026-118'}
    b = correct(c, rid, shuffled).json()
    assert a['run']['manifest']['run_id'] == b['run']['manifest']['run_id']
    assert len(c.get('/api/runs').json()) == 2
    run = b['run']
    assert len(run['context']['subjects']) == 20
    assert run['balance']['expected'] == 40
    assert {r['subject'] for r in run['records']} == {f'{i:05d}' for i in range(1, 21)}
    assert {r['visit'] for r in run['records']} == {'V1', 'V2'}
    assert run['balance']['completed'] == 40

def test_unit_incomplete_blocks_and_restores_official(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    bad = {'pair': ['00003', '00004'], 'records': ['00003-V1', '00004-V1', '00004-R1'], 'reason': '逐条改号'}
    r = correct(c, rid, bad)
    assert r.status_code == 422
    data = r.json()
    assert data['code'] == 'CORRECTION_BLOCKED'
    unit = [b for b in data['blocks'] if b['code'] == 'UNIT_INCOMPLETE']
    assert unit and '00003-R1' in unit[0]['detail']
    assert c.get('/api/official').json()['run_id'] == rid
    assert len(c.get('/api/runs').json()) == 1

def test_one_sided_swap_blocked(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    one_way = {'pair': ['00003', '00004'], 'records': ['00003-V1', '00003-R1', '00003-V2'], 'reason': '单向改号'}
    r = correct(c, rid, one_way)
    assert r.status_code == 422
    assert 'SWAP_ONE_SIDED' in {b['code'] for b in r.json()['blocks']}

def test_unknown_record_and_subject_blocked(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    ghost = dict(FULL_SWAP, records=FULL_SWAP['records'] + ['GHOST-1'])
    assert 'RECORD_UNKNOWN' in {b['code'] for b in correct(c, rid, ghost).json()['blocks']}
    outsider = dict(FULL_SWAP, pair=['00003', '00099'])
    assert 'SUBJECT_UNKNOWN' in {b['code'] for b in correct(c, rid, outsider).json()['blocks']}
    stranger = dict(FULL_SWAP, records=FULL_SWAP['records'] + ['00005-V1'])
    assert 'PAIR_MISMATCH' in {b['code'] for b in correct(c, rid, stranger).json()['blocks']}

def test_failed_recalculation_keeps_previous_official(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    good_id = correct(c, rid, FULL_SWAP).json()['run']['manifest']['run_id']
    assert c.get('/api/official').json()['run_id'] == good_id
    bad = {'pair': ['00003', '00004'], 'records': ['00003-V1', '00004-V1', '00004-R1'], 'reason': '重试'}
    assert correct(c, rid, bad).status_code == 422
    assert c.get('/api/official').json()['run_id'] == good_id

def test_unlockable_recalculation_restores_official(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    good_id = correct(c, rid, FULL_SWAP).json()['run']['manifest']['run_id']
    cross = {'pair': ['00003', '00004'], 'records': ['00003-V1', '00003-R1', '00004-V2'], 'reason': '跨访视互换'}
    r = correct(c, rid, cross)
    assert r.status_code == 200
    data = r.json()
    assert data['run']['balance']['status'] == '待处理'
    assert len(data['run']['deviations']) == 4
    assert data['switched'] is False
    assert data['official'] == good_id
    assert c.get('/api/official').json()['run_id'] == good_id

def test_original_numbers_preserved(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    run = correct(c, rid, FULL_SWAP).json()['run']
    assert run['input_snapshot']['bytes_b64'] == base['input_snapshot']['bytes_b64']
    mapping = {m['id']: m for m in run['correction']['mapping']}
    assert mapping['00003-V1']['old_subject'] == '00003'
    assert mapping['00003-V1']['new_subject'] == '00004'
    intake = {row['id']: row for row in run['views']['intake']['rows']}
    assert intake['00003-V1']['original_subject'] == '00003'
    assert intake['00005-V1']['original_subject'] == ''
    original = c.get(f'/api/runs/{rid}').json()
    assert 'correction' not in original
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/export/{run['manifest']['run_id']}").content))
    correction = json.loads(z.read('correction.json'))
    assert correction['base_run_id'] == rid
    assert {m['old_subject'] for m in correction['mapping']} == {'00003', '00004'}

def test_rerun_corrected_run_stable(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    new_id = correct(c, rid, FULL_SWAP).json()['run']['manifest']['run_id']
    again = c.post(f'/api/runs/{new_id}/rerun').json()
    assert again['manifest']['run_id'] == new_id
    assert again['correction']['base_run_id'] == rid

def test_chain_correction_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    new_id = correct(c, rid, FULL_SWAP).json()['run']['manifest']['run_id']
    r = correct(c, new_id, FULL_SWAP)
    assert r.status_code == 409
    assert r.json()['base_run_id'] == rid

def test_dirty_unit_includes_doomed_rows(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = c.post('/api/runs', json={}).json()
    rid = base['manifest']['run_id']
    partial = {'pair': ['00003', '00004'], 'reason': '入组时筛选号互换',
               'records': ['00003-V1', '00003-R1', '00003-V2', 'BAD1', '00004-V1', '00004-R1', '00004-V2']}
    r = correct(c, rid, partial)
    assert r.status_code == 422
    unit = [b for b in r.json()['blocks'] if b['code'] == 'UNIT_INCOMPLETE']
    assert unit and 'BAD2' in unit[0]['detail']
    full = dict(partial, records=partial['records'] + ['BAD2'])
    data = correct(c, rid, full).json()
    assert data['run']['balance']['status'] == '待处理'
    assert data['switched'] is False and data['official'] == rid
    bad1 = [q for q in data['run']['quarantine'] if q['id'] == 'BAD1'][0]
    assert bad1['raw']['subject'] == '00004' and bad1['raw']['original_subject'] == '00003'

def test_enrolled_rebased_to_new_subject():
    subjects = {f'{i:05d}': '2026-10-01T00:00:00+08:00' for i in range(1, 21)}
    subjects['00004'] = '2026-10-02T00:00:00+08:00'
    state = evaluate({'dataset': 'clean', 'overrides': {'subjects': subjects}, 'correction': FULL_SWAP})
    mapping = {m['id']: m for m in state['correction']['mapping']}
    assert mapping['00003-V1']['new_enrolled'] == '2026-10-02T00:00:00+08:00'
    assert mapping['00004-V1']['new_enrolled'] == '2026-10-01T00:00:00+08:00'
    assert mapping['00003-V1']['old_enrolled'] == '2026-10-01T00:00:00+08:00'
    assert state['balance']['status'] == '可锁库'

def test_parse_spec_shape():
    with pytest.raises(BusinessError):
        parse_spec({'pair': ['00003', '00003'], 'records': ['x'], 'reason': 'r'})
    with pytest.raises(BusinessError):
        parse_spec({'pair': ['00003', '00004'], 'records': [], 'reason': 'r'})
    with pytest.raises(BusinessError):
        parse_spec({'pair': ['00003', '00004'], 'records': ['x'], 'reason': ' '})
    spec = parse_spec({'pair': [' 00004 ', '00003'], 'records': [' b ', 'a', 'a'], 'reason': ' r '})
    assert spec == {'pair': ['00003', '00004'], 'records': ['a', 'b'], 'reason': 'r'}

def test_correction_page_shows_attribution_visits_and_blocks(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    base = clean_run(c); rid = base['manifest']['run_id']
    form = c.get(f'/correction?run_id={rid}')
    assert form.status_code == 200 and '发起重归属' in form.text
    new_id = correct(c, rid, FULL_SWAP).json()['run']['manifest']['run_id']
    page = c.get(f'/correction?run_id={new_id}')
    assert '新旧归属' in page.text and '受影响访视' in page.text
    assert '00003' in page.text and '00004' in page.text and '已切换' in page.text
    cross = {'pair': ['00003', '00004'], 'records': ['00003-V1', '00003-R1', '00004-V2'], 'reason': '跨访视互换'}
    blocked_id = correct(c, rid, cross).json()['run']['manifest']['run_id']
    blocked = c.get(f'/correction?run_id={blocked_id}')
    assert '阻断原因' in blocked.text and '保留旧版' in blocked.text
    for k in main.MODULES:
        assert c.get(f'/{k}?run_id={new_id}').status_code == 200
    assert '原归属' in c.get(f'/intake?run_id={new_id}').text
