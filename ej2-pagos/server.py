import hashlib, json, os, random, sqlite3, threading, time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.environ.get('DATA_DIR', HERE), 'data.db')
LOCK = threading.RLock()
STATE = {'bank': 'ok'}
LIM_TX, LIM_DAY, TIMEOUT = 300.0, 500.0, 2.0

def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def now():
    return datetime.now().strftime('%H:%M:%S')

def audit(c, key, event, detail):
    c.execute('INSERT INTO audit(ts,idem_key,event,detail) VALUES(?,?,?,?)', (now(), (key or '')[:8], event, detail))

SCHEMA = '''
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, name TEXT, balance REAL, spent_today REAL);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE, body_hash TEXT, account TEXT, amount REAL, status TEXT, detail TEXT, bank_ref TEXT, latency_ms INTEGER, attempts INTEGER DEFAULT 0, created REAL, recon_s REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, idem_key TEXT, event TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS bank_ledger(payment_id INTEGER PRIMARY KEY, ref TEXT);
'''

def seed(c):
    c.execute("INSERT OR REPLACE INTO accounts VALUES('001-123','María Pérez',800,0)")
    c.execute("INSERT OR REPLACE INTO accounts VALUES('001-456','Juan Mora',100,0)")

def init():
    c = db()
    c.executescript(SCHEMA)
    if not c.execute('SELECT 1 FROM accounts').fetchone():
        seed(c)
    c.commit()
    c.close()

def pub(r):
    return {k: r[k] for k in ('id', 'account', 'amount', 'status', 'detail', 'bank_ref', 'latency_ms')}

def code_for(s):
    return {'APROBADO': 200, 'RECHAZADO': 200, 'PROCESANDO': 202, 'PENDIENTE_CONCILIACION': 202, 'FALLIDO': 502}.get(s, 200)

def get(c, pid):
    return c.execute('SELECT * FROM payments WHERE id=?', (pid,)).fetchone()

def finish(c, pid, status, detail, ref, ms):
    r = get(c, pid)
    c.execute('UPDATE payments SET status=?,detail=?,bank_ref=?,latency_ms=? WHERE id=?', (status, detail, ref, ms, pid))
    audit(c, r['idem_key'], status, detail)

def release(c, pid):
    r = get(c, pid)
    c.execute('UPDATE accounts SET balance=balance+?, spent_today=spent_today-? WHERE id=?', (r['amount'], r['amount'], r['account']))

def call_bank(pid, mode):
    """Banco externo simulado: 'slow' procesa a los 4.5 s (después del timeout), 'down' falla."""
    out = {}
    def work():
        if mode == 'down':
            time.sleep(0.3)
            out['err'] = True
            return
        time.sleep(4.5 if mode == 'slow' else 0.4)
        ref = 'BNK-%06d' % random.randint(0, 999999)
        c = db()
        c.execute('INSERT OR REPLACE INTO bank_ledger(payment_id,ref) VALUES(?,?)', (pid, ref))
        c.commit()
        c.close()
        out['ref'] = ref
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(TIMEOUT)
    if t.is_alive():
        return 'timeout', None
    if out.get('err'):
        return 'error', None
    return 'ok', out['ref']

def pay(key, body):
    if not key:
        return 400, {'error': 'Falta el header Idempotency-Key'}
    h = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    acc = str(body.get('account', '')).strip()
    try:
        amt = float(body.get('amount', 0))
    except Exception:
        return 400, {'error': 'monto inválido'}
    with LOCK:
        c = db()
        try:
            ex = c.execute('SELECT * FROM payments WHERE idem_key=?', (key,)).fetchone()
            if ex:
                if ex['body_hash'] != h:
                    audit(c, key, 'CLAVE_REUTILIZADA', 'misma clave con otra solicitud')
                    c.commit()
                    return 422, {'error': 'La Idempotency-Key ya se usó con otra solicitud'}
                audit(c, key, 'REINTENTO', f"devuelve resultado guardado ({ex['status']}); no se cobra de nuevo")
                c.commit()
                d = pub(ex)
                d['replay'] = True
                return code_for(ex['status']), d
            cur = c.execute('INSERT INTO payments(idem_key,body_hash,account,amount,status,created) VALUES(?,?,?,?,?,?)',
                            (key, h, acc, amt, 'PROCESANDO', time.time()))
            pid = cur.lastrowid
            a = c.execute('SELECT * FROM accounts WHERE id=?', (acc,)).fetchone()
            reason = None
            if not a:
                reason = 'Cuenta inexistente'
            elif amt <= 0 or amt > LIM_TX:
                reason = f'Excede el límite por pago (${LIM_TX:.0f})'
            elif a['spent_today'] + amt > LIM_DAY:
                reason = f'Excede el límite diario (${LIM_DAY:.0f})'
            elif a['balance'] < amt:
                reason = 'Fondos insuficientes'
            if reason:
                finish(c, pid, 'RECHAZADO', reason, None, 0)
                c.commit()
                return code_for('RECHAZADO'), pub(get(c, pid))
            # retención de fondos para que dos pagos simultáneos no superen saldo/límite
            c.execute('UPDATE accounts SET balance=balance-?, spent_today=spent_today+? WHERE id=?', (amt, amt, acc))
            audit(c, key, 'INTENTO', f'cuenta ***{acc[-3:]} monto {amt}')
            c.commit()
        finally:
            c.close()
    t0 = time.time()
    res, ref = call_bank(pid, STATE['bank'])
    ms = int((time.time() - t0) * 1000)
    with LOCK:
        c = db()
        try:
            if res == 'ok':
                finish(c, pid, 'APROBADO', 'Pago aprobado', ref, ms)
            elif res == 'timeout':
                finish(c, pid, 'PENDIENTE_CONCILIACION', 'El banco no respondió a tiempo; se concilia después. No se reintenta el cobro.', None, ms)
            else:
                release(c, pid)
                finish(c, pid, 'FALLIDO', 'Banco no disponible. No se realizó ningún cobro.', None, ms)
            c.commit()
            r = get(c, pid)
            return code_for(r['status']), pub(r)
        finally:
            c.close()

def reconcile():
    n = 0
    with LOCK:
        c = db()
        try:
            for p in c.execute("SELECT * FROM payments WHERE status='PENDIENTE_CONCILIACION'").fetchall():
                n += 1
                led = c.execute('SELECT ref FROM bank_ledger WHERE payment_id=?', (p['id'],)).fetchone()
                if led:
                    finish(c, p['id'], 'APROBADO', 'Conciliado: el banco confirmó el pago', led['ref'], p['latency_ms'])
                    c.execute('UPDATE payments SET recon_s=? WHERE id=?', (round(time.time() - p['created'], 1), p['id']))
                    audit(c, p['idem_key'], 'CONCILIADO', 'ref ' + led['ref'])
                else:
                    k = p['attempts'] + 1
                    c.execute('UPDATE payments SET attempts=? WHERE id=?', (k, p['id']))
                    if k >= 3 and time.time() - p['created'] > 30:
                        release(c, p['id'])
                        finish(c, p['id'], 'FALLIDO', 'El banco no tiene registro tras 3 consultas; fondos liberados', None, p['latency_ms'])
                    else:
                        audit(c, p['idem_key'], 'CONCILIACION_PENDIENTE', f'el banco aún no confirma (consulta {k})')
            c.commit()
        finally:
            c.close()
    return n

def rows(sql, args=()):
    c = db()
    out = [dict(x) for x in c.execute(sql, args).fetchall()]
    c.close()
    return out

def metrics():
    P = rows('SELECT * FROM payments')
    lat = sorted(p['latency_ms'] for p in P if p['latency_ms'] and p['status'] in ('APROBADO', 'PENDIENTE_CONCILIACION', 'FALLIDO'))
    calls = [p for p in P if p['status'] in ('APROBADO', 'PENDIENTE_CONCILIACION', 'FALLIDO')]
    errs = [p for p in calls if p['status'] != 'APROBADO' or p['recon_s'] is not None]
    rec = [p['recon_s'] for p in P if p['recon_s'] is not None]
    c = db()
    replays = c.execute("SELECT COUNT(*) FROM audit WHERE event='REINTENTO'").fetchone()[0]
    dup = c.execute('SELECT COUNT(*) - COUNT(DISTINCT idem_key) FROM payments').fetchone()[0]
    c.close()
    return {
        'pagos_totales': len(P),
        'pagos_duplicados': dup,
        'reintentos_bloqueados': replays,
        'latencia_p95_ms': lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
        'tasa_errores_pct': round(100 * len(errs) / len(calls)) if calls else 0,
        'tiempo_conciliacion_s': round(sum(rec) / len(rec), 1) if rec else None,
        'pendientes_conciliacion': len([p for p in P if p['status'] == 'PENDIENTE_CONCILIACION']),
    }

def route(method, path, body, headers):
    p = [x for x in path.split('/') if x][1:]
    if method == 'GET' and p == ['health']:
        return 200, {'ok': True}
    if method == 'POST' and p == ['pay']:
        return pay(headers.get('Idempotency-Key'), body)
    if method == 'POST' and p == ['bank']:
        STATE['bank'] = body.get('mode', 'ok')
        return 200, {'bank': STATE['bank']}
    if method == 'POST' and p == ['reconcile']:
        return 200, {'revisados': reconcile()}
    if method == 'POST' and p == ['reset']:
        with LOCK:
            c = db()
            c.executescript('DELETE FROM payments; DELETE FROM audit; DELETE FROM bank_ledger;')
            seed(c)
            c.commit()
            c.close()
        return 200, {'ok': True}
    if method == 'GET' and p == ['accounts']:
        return 200, rows('SELECT * FROM accounts')
    if method == 'GET' and p == ['payments']:
        return 200, rows('SELECT id,idem_key,account,amount,status,detail,bank_ref,latency_ms FROM payments ORDER BY id DESC LIMIT 40')
    if method == 'GET' and p == ['audit']:
        return 200, rows('SELECT * FROM audit ORDER BY id DESC LIMIT 80')
    if method == 'GET' and p == ['metrics']:
        return 200, metrics()
    return 404, {'error': 'ruta no encontrada'}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def reply(self, code, obj, ctype='application/json'):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def handle_any(self, method):
        if method == 'GET' and self.path in ('/', '/index.html'):
            return self.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html')
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}') if n else {}
        try:
            code, obj = route(method, self.path.split('?')[0], body, self.headers)
        except Exception as e:
            code, obj = 500, {'error': str(e)}
        self.reply(code, obj)
    def do_GET(self): self.handle_any('GET')
    def do_POST(self): self.handle_any('POST')

if __name__ == '__main__':
    init()
    print('Servidor en http://localhost:8000', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8000), H).serve_forever()
