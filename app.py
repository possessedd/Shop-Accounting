import os, sys, webbrowser, shutil, sqlite3, threading, time, logging, secrets, hmac, configparser
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from contextlib import contextmanager
from functools import wraps
from logging.handlers import RotatingFileHandler
from flask import Flask, request, session, redirect, jsonify, g, render_template, abort
from jinja2 import DictLoader
from werkzeug.exceptions import HTTPException

BASE = os.path.dirname(sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__))
CFG = os.path.join(BASE, 'config.ini')
D = dict(shop_name='Мій магазин', host='0.0.0.0', port='5000', cashier_password='1111',
         owner_password='admin', backup_dir='', backup_every_hours='4', backup_keep='60',
         allow_negative_stock='yes', receipt_footer='Дякуємо за покупку!', secret_key='')
cp = configparser.ConfigParser(interpolation=None)
cp.read(CFG, encoding='utf-8')
C = dict(D)
if cp.has_section('main'):
    C.update(cp['main'])
if not C['secret_key'] or not os.path.exists(CFG):
    C['secret_key'] = C['secret_key'] or secrets.token_hex(32)
    cp['main'] = C
    with open(CFG, 'w', encoding='utf-8') as f:
        cp.write(f)

DB = os.path.join(BASE, 'data', 'shop.db')
os.makedirs(os.path.dirname(DB), exist_ok=True)
SCHEMA = """
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, barcode TEXT UNIQUE, name TEXT NOT NULL,
 unit TEXT NOT NULL DEFAULT 'шт', price INTEGER NOT NULL DEFAULT 0, cost INTEGER NOT NULL DEFAULT 0,
 qty REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS moves(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, product_id INTEGER NOT NULL,
 kind TEXT NOT NULL, qty REAL NOT NULL, price INTEGER NOT NULL DEFAULT 0, cost INTEGER NOT NULL DEFAULT 0, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, total INTEGER NOT NULL,
 pay TEXT NOT NULL DEFAULT 'cash', given INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sale_items(id INTEGER PRIMARY KEY, sale_id INTEGER NOT NULL, product_id INTEGER,
 name TEXT NOT NULL, qty REAL NOT NULL, price INTEGER NOT NULL, cost INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_sales_ts ON sales(ts);
CREATE INDEX IF NOT EXISTS ix_items_sale ON sale_items(sale_id);
"""
MOVE = 'INSERT INTO moves(ts,product_id,kind,qty,price,cost,note) VALUES(?,?,?,?,?,?,?)'


def connect():
    c = sqlite3.connect(DB, timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.create_function('ul', 1, lambda s: s.lower() if s else '')
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    return c


_c = connect(); _c.executescript(SCHEMA); _c.close()

app = Flask(__name__)
app.secret_key = C['secret_key']
app.config.update(SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(days=30))


def db():
    if 'db' not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def _close(_):
    c = g.pop('db', None)
    if c:
        c.close()


@contextmanager
def tx():
    c = db()
    c.execute('BEGIN IMMEDIATE')
    try:
        yield c
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def cents(v):
    try:
        return int((Decimal(str(v).replace(',', '.').strip() or '0') * 100).quantize(Decimal(1), ROUND_HALF_UP))
    except Exception:
        raise ValueError('Невірне число')


def fl(v):
    try:
        return round(float(str(v).replace(',', '.') or 0), 3)
    except Exception:
        raise ValueError('Невірна кількість')


@app.template_filter('m')
def _m(c):
    return f"{(c or 0) / 100:.2f}"


@app.template_filter('n')
def _n(v):
    return f"{v:.3f}".rstrip('0').rstrip('.')


@app.context_processor
def _ctx():
    return dict(shop=C['shop_name'], role=session.get('role'))


def need(*roles):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            if session.get('role') not in roles:
                if request.path.startswith('/api/'):
                    return jsonify(error='Потрібен вхід'), 401
                return redirect('/login?next=' + request.path)
            return f(*a, **k)
        return w
    return deco


@app.errorhandler(Exception)
def _err(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception('error')
    if request.path.startswith('/api/'):
        ve = isinstance(e, ValueError)
        return jsonify(error=str(e) if ve else 'Внутрішня помилка (див. logs/app.log)'), 400 if ve else 500
    return 'Помилка. Деталі у logs/app.log', 500


FAILS = {}


@app.route('/login', methods=['GET', 'POST'])
def login():
    err = ''
    if request.method == 'POST':
        ip = request.headers.get('CF-Connecting-IP') or request.remote_addr
        n, t = FAILS.get(ip, (0, 0))
        if n >= 5 and time.time() - t < 300:
            err = 'Забагато спроб. Зачекайте 5 хвилин.'
        else:
            pw = request.form.get('pw', '').encode()
            r = ('owner' if hmac.compare_digest(pw, C['owner_password'].encode()) else
                 'cashier' if hmac.compare_digest(pw, C['cashier_password'].encode()) else None)
            if r:
                FAILS.pop(ip, None)
                session.permanent = True
                session['role'] = r
                nx = request.args.get('next', '')
                if not nx.startswith('/') or nx.startswith('//'):
                    nx = '/' if r == 'owner' else '/pos'
                return redirect(nx)
            FAILS[ip] = (n + 1, time.time())
            err = 'Невірний пароль'
    return render_template('login.html', err=err)


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.get('/pos')
@need('owner', 'cashier')
def pos():
    return render_template('pos.html')


def pj(p):
    return dict(id=p['id'], barcode=p['barcode'], name=p['name'], unit=p['unit'], price=p['price'], cost=p['cost'], qty=p['qty'])


@app.get('/api/lookup')
@need('owner', 'cashier')
def api_lookup():
    q = request.args.get('q', '').strip()
    c = db()
    exact = c.execute('SELECT * FROM products WHERE barcode=? AND active=1', (q,)).fetchone()
    items = []
    if q and not exact and not (q.isdigit() and len(q) >= 8):
        like = '%' + q.lower() + '%'
        items = c.execute('SELECT * FROM products WHERE active=1 AND (ul(name) LIKE ? OR barcode LIKE ?) ORDER BY name LIMIT 10',
                          (like, like)).fetchall()
    return jsonify(exact=pj(exact) if exact else None, items=[pj(i) for i in items])


@app.post('/api/sale')
@need('owner', 'cashier')
def api_sale():
    d = request.get_json()
    items = [i for i in d['items'] if fl(i['qty']) > 0]
    if not items:
        raise ValueError('Порожній чек')
    pay = 'card' if d.get('pay') == 'card' else 'cash'
    with tx() as t:
        rows, total = [], 0
        for i in items:
            p = t.execute('SELECT * FROM products WHERE id=? AND active=1', (i['id'],)).fetchone()
            if not p:
                raise ValueError('Товар не знайдено')
            q = fl(i['qty'])
            if C['allow_negative_stock'] != 'yes' and p['qty'] < q:
                raise ValueError(f"Недостатньо на складі: {p['name']} (є {_n(p['qty'])})")
            rows.append((p, q))
            total += round(p['price'] * q)
        given = cents(d.get('given') or 0) if pay == 'cash' else total
        if given and given < total:
            raise ValueError('Отримано менше, ніж сума чека')
        ts = now()
        sid = t.execute('INSERT INTO sales(ts,total,pay,given) VALUES(?,?,?,?)', (ts, total, pay, given or total)).lastrowid
        for p, q in rows:
            t.execute('INSERT INTO sale_items(sale_id,product_id,name,qty,price,cost) VALUES(?,?,?,?,?,?)',
                      (sid, p['id'], p['name'], q, p['price'], p['cost']))
            t.execute('UPDATE products SET qty=round(qty-?,3) WHERE id=?', (q, p['id']))
            t.execute(MOVE, (ts, p['id'], 'sale', -q, p['price'], p['cost'], f'Чек {sid}'))
    return jsonify(id=sid, total=total, change=max(0, (given or total) - total))


@app.get('/receipt/<int:sid>')
@need('owner', 'cashier')
def receipt(sid):
    s = db().execute('SELECT * FROM sales WHERE id=?', (sid,)).fetchone() or abort(404)
    items = db().execute('SELECT * FROM sale_items WHERE sale_id=?', (sid,)).fetchall()
    return render_template('receipt.html', s=s, items=items, footer=C['receipt_footer'])


def stats(c, a, b):
    n, rev = c.execute('SELECT COUNT(*),COALESCE(SUM(total),0) FROM sales WHERE ts>=? AND ts<?', (a, b)).fetchone()
    pr = c.execute('SELECT COALESCE(SUM(ROUND(i.price*i.qty)-ROUND(i.cost*i.qty)),0) FROM sale_items i '
                   'JOIN sales s ON s.id=i.sale_id WHERE s.ts>=? AND s.ts<?', (a, b)).fetchone()[0]
    return n, rev, int(pr)


@app.get('/')
@need('owner', 'cashier')
def index():
    if session['role'] != 'owner':
        return redirect('/pos')
    today = date.today()
    d1 = request.args.get('from') or today.isoformat()
    d2 = request.args.get('to') or d1
    try:
        end = (date.fromisoformat(d2) + timedelta(days=1)).isoformat()
        date.fromisoformat(d1)
    except ValueError:
        return redirect('/')
    a, b = d1 + ' 00:00:00', end + ' 00:00:00'
    c = db()
    n, rev, pr = stats(c, a, b)
    sales = c.execute('SELECT * FROM sales WHERE ts>=? AND ts<? ORDER BY id DESC LIMIT 300', (a, b)).fetchall()
    items = {}
    for i in c.execute('SELECT * FROM sale_items WHERE sale_id IN (SELECT id FROM sales WHERE ts>=? AND ts<? ORDER BY id DESC LIMIT 300)', (a, b)):
        items.setdefault(i['sale_id'], []).append(i)
    iso = lambda d: d.isoformat()
    quick = [('Сьогодні', iso(today), iso(today)), ('Вчора', iso(today - timedelta(1)), iso(today - timedelta(1))),
             ('7 днів', iso(today - timedelta(6)), iso(today)), ('Місяць', iso(today.replace(day=1)), iso(today))]
    return render_template('index.html', d1=d1, d2=d2, n=n, rev=rev, pr=pr, sales=sales, items=items, quick=quick)


@app.get('/products')
@need('owner')
def products():
    q = request.args.get('q', '').strip().lower()
    like = '%' + q + '%'
    rows = db().execute("SELECT * FROM products WHERE active=1 AND (?='' OR ul(name) LIKE ? OR barcode LIKE ?) ORDER BY name",
                        (q, like, like)).fetchall()
    cost = sum(max(r['qty'], 0) * r['cost'] for r in rows)
    retail = sum(max(r['qty'], 0) * r['price'] for r in rows)
    return render_template('products.html', rows=rows, q=q, cost=cost, retail=retail)


@app.route('/product/<int:pid>', methods=['GET', 'POST'])
@need('owner')
def product(pid):
    c = db()
    p = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone() if pid else None
    err = ''
    if request.method == 'POST':
        f = request.form
        try:
            if f.get('archive') and p:
                c.execute('UPDATE products SET active=0,barcode=NULL WHERE id=?', (pid,))
                return redirect('/products')
            name = f['name'].strip()
            if not name:
                raise ValueError('Вкажіть назву')
            bc = f.get('barcode', '').strip() or None
            price, cost, qty = cents(f.get('price')), cents(f.get('cost')), fl(f.get('qty'))
            unit = f.get('unit', '').strip() or 'шт'
            with tx() as t:
                if p:
                    t.execute('UPDATE products SET barcode=?,name=?,unit=?,price=?,cost=?,qty=? WHERE id=?',
                              (bc, name, unit, price, cost, qty, pid))
                    if abs(qty - p['qty']) > 1e-9:
                        t.execute(MOVE, (now(), pid, 'adjust', round(qty - p['qty'], 3), price, cost, 'Корекція залишку'))
                    if price != p['price']:
                        t.execute(MOVE, (now(), pid, 'reprice', 0, price, cost, f"Переоцінка {p['price']/100:.2f} -> {price/100:.2f}"))
                else:
                    pid = t.execute('INSERT INTO products(barcode,name,unit,price,cost,qty) VALUES(?,?,?,?,?,?)',
                                    (bc, name, unit, price, cost, qty)).lastrowid
                    if qty:
                        t.execute(MOVE, (now(), pid, 'in', qty, price, cost, 'Початковий залишок'))
            return redirect('/products')
        except sqlite3.IntegrityError:
            err = 'Такий штрихкод уже існує'
        except ValueError as e:
            err = str(e)
    if request.method == 'POST':
        d = request.form.to_dict()
    elif p:
        d = dict(dict(p), price=_m(p['price']), cost=_m(p['cost']), qty=_n(p['qty']))
    else:
        d = dict(barcode=request.args.get('barcode', ''), unit='шт', qty='0')
    return render_template('product.html', d=d, p=p, err=err)


@app.post('/api/product')
@need('owner')
def api_product():
    d = request.get_json()
    name = d['name'].strip()
    if not name:
        raise ValueError('Вкажіть назву')
    try:
        pid = db().execute('INSERT INTO products(barcode,name,price,cost) VALUES(?,?,?,?)',
                           (d.get('barcode') or None, name, cents(d.get('price')), cents(d.get('cost')))).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError('Такий штрихкод уже існує')
    return jsonify(pj(db().execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()))


@app.get('/doc/<kind>')
@need('owner')
def doc(kind):
    if kind not in ('in', 'out'):
        abort(404)
    return render_template('doc.html', kind=kind)


@app.post('/api/doc')
@need('owner')
def api_doc():
    d = request.get_json()
    kind, note = d['kind'], (d.get('note') or '')[:200]
    if not d['lines']:
        raise ValueError('Додайте товари')
    with tx() as t:
        for l in d['lines']:
            p = t.execute('SELECT * FROM products WHERE id=?', (l['id'],)).fetchone()
            q = fl(l['qty'])
            if not p or q <= 0:
                raise ValueError('Невірний рядок документа')
            if kind == 'in':
                cost = cents(l.get('cost') or 0) or p['cost']
                price = cents(l.get('price') or 0) or p['price']
                t.execute('UPDATE products SET qty=round(qty+?,3),cost=?,price=? WHERE id=?', (q, cost, price, p['id']))
                t.execute(MOVE, (now(), p['id'], 'in', q, price, cost, note))
                if price != p['price']:
                    t.execute(MOVE, (now(), p['id'], 'reprice', 0, price, cost, f"Переоцінка {p['price']/100:.2f} -> {price/100:.2f}"))
            else:
                t.execute('UPDATE products SET qty=round(qty-?,3) WHERE id=?', (q, p['id']))
                t.execute(MOVE, (now(), p['id'], 'writeoff', -q, p['price'], p['cost'], note))
    return jsonify(ok=1)


STATE = {'ext': ''}
BK = os.path.join(BASE, 'backups')


def _files(d):
    return sorted(f for f in os.listdir(d) if f.startswith('shop_') and f.endswith('.db')) if os.path.isdir(d) else []


def newest():
    f = _files(BK)
    return f[-1] if f else None


def prune(d):
    for f in _files(d)[:-max(1, int(C['backup_keep']))]:
        os.remove(os.path.join(d, f))


def sync_ext():
    ext, n = C['backup_dir'].strip(), newest()
    if not ext or not n:
        return
    try:
        os.makedirs(ext, exist_ok=True)
        dst = os.path.join(ext, n)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(BK, n), dst + '.tmp')
            os.replace(dst + '.tmp', dst)
            prune(ext)
        STATE['ext'] = 'OK, ' + now()
    except Exception as e:
        STATE['ext'] = 'НЕ ВДАЛОСЯ: ' + str(e)


def backup():
    os.makedirs(BK, exist_ok=True)
    name = datetime.now().strftime('shop_%Y%m%d_%H%M%S.db')
    tmp = os.path.join(BK, name + '.tmp')
    s, t = sqlite3.connect(DB, timeout=30), sqlite3.connect(tmp)
    try:
        s.backup(t)
    finally:
        t.close(); s.close()
    os.replace(tmp, os.path.join(BK, name))
    prune(BK)
    sync_ext()
    return name


def scheduler():
    while True:
        try:
            n = newest()
            age = time.time() - os.path.getmtime(os.path.join(BK, n)) if n else 1e9
            if age >= float(C['backup_every_hours']) * 3600:
                backup()
            else:
                sync_ext()
        except Exception:
            app.logger.exception('backup')
        time.sleep(300)


@app.route('/backup', methods=['GET', 'POST'])
@need('owner')
def backup_page():
    msg = ''
    if request.method == 'POST':
        try:
            msg = 'Створено: ' + backup()
        except Exception as e:
            msg = 'Помилка: ' + str(e)
    return render_template('backup.html', msg=msg, last=newest(), ext=C['backup_dir'], st=STATE['ext'], files=_files(BK)[-10:][::-1])


T = {}
T['base.html'] = """<!doctype html><html lang="uk"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ shop }}</title><style>
body{font:15px system-ui,Segoe UI,sans-serif;margin:0;background:#f4f5f7;color:#222}
nav{background:#1f2937;padding:10px 14px;display:flex;gap:16px;flex-wrap:wrap}nav a{color:#e5e7eb;text-decoration:none}nav b{color:#fff}
main{padding:14px;max-width:1100px;margin:auto}table{border-collapse:collapse;width:100%;background:#fff}
th,td{padding:7px 9px;border-bottom:1px solid #e5e7eb;text-align:left}th{background:#eef0f3}.r{text-align:right}
input,select,button{font:inherit;padding:8px;border:1px solid #bbb;border-radius:6px}
button,.btn{background:#2563eb;color:#fff;border:0;cursor:pointer;padding:9px 14px;border-radius:6px;text-decoration:none;display:inline-block}
.sec{background:#6b7280}.card{display:inline-block;background:#fff;padding:10px 16px;margin:0 8px 8px 0;border-radius:8px}
.card b{display:block;font-size:22px}.err,.neg{color:#b91c1c}.dim{color:#666;font-size:13px}
</style><nav><b>{{ shop }}</b>
{% if role=='owner' %}<a href="/">Продажі</a><a href="/pos">Каса</a><a href="/products">Товари і залишки</a><a href="/doc/in">Прихід</a><a href="/doc/out">Списання</a><a href="/backup">Бекап</a>
{% elif role %}<a href="/pos">Каса</a>{% endif %}{% if role %}<a href="/logout" style="margin-left:auto">Вийти</a>{% endif %}</nav>
<main>{% block c %}{% endblock %}</main></html>"""

T['login.html'] = """{% extends 'base.html' %}{% block c %}<h2>Вхід</h2>
<form method="post"><input type="password" name="pw" placeholder="Пароль" autofocus> <button>Увійти</button></form>
<p class="err">{{ err }}</p>{% endblock %}"""

T['lk.js'] = """const $=s=>document.querySelector(s),f=c=>(c/100).toFixed(2);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let S=[],tm;
async function look(q){const r=await fetch('/api/lookup?q='+encodeURIComponent(q));if(r.status==401){location='/login';return{items:[]}}return r.json()}
function sugg(it){S=it;$('#sugg').innerHTML=it.map(p=>`<button class="sec" onclick="pick(${p.id})">${esc(p.name)} — ${f(p.price)}</button> `).join('')}
function pick(id){add(S.find(p=>p.id==id));sugg([])}
$('#q').addEventListener('input',e=>{clearTimeout(tm);const q=e.target.value.trim();if(q.length<2||/^\\d+$/.test(q)){sugg([]);return}tm=setTimeout(async()=>sugg((await look(q)).items),200)});
$('#q').addEventListener('keydown',async e=>{if(e.key!='Enter')return;const q=e.target.value.trim();if(!q)return;e.target.value='';
const d=await look(q);if(d.exact){add(d.exact);sugg([])}else if(d.items.length){add(d.items[0]);sugg([])}else notfound(q)});"""

T['pos.html'] = """{% extends 'base.html' %}{% block c %}
<div style="display:flex;gap:16px;flex-wrap:wrap"><div style="flex:2;min-width:340px">
<input id="q" autofocus autocomplete="off" placeholder="Скануйте штрихкод або введіть назву…" style="width:100%;box-sizing:border-box;font-size:18px">
<div id="sugg" style="margin:6px 0"></div><div id="msg" class="dim" style="margin:6px 0"></div>
<table><thead><tr><th>Товар</th><th class="r">Ціна</th><th>К-сть</th><th class="r">Сума</th><th></th></tr></thead><tbody id="tb"></tbody></table></div>
<div style="flex:1;min-width:240px"><div class="card" style="display:block"><span class="dim">До сплати</span><b id="total" style="font-size:40px">0.00</b></div>
<p><label><input type="radio" name="pay" value="cash" checked> Готівка</label> &nbsp; <label><input type="radio" name="pay" value="card"> Картка</label></p>
<p>Отримано: <input id="given" size="8" oninput="chg()"> Решта: <b id="change">0.00</b></p>
<p><label><input type="checkbox" id="prn" checked> Друкувати чек</label></p>
<button onclick="pay()" style="width:100%;font-size:20px;padding:14px">ПРОДАТИ (F9)</button>
<p><button class="sec" onclick="if(confirm('Очистити чек?')){cart.length=0;draw()}">Очистити</button></p></div></div>
<iframe id="rf" style="position:fixed;right:0;bottom:0;width:0;height:0;border:0"></iframe>
<script>{% include 'lk.js' %}
const cart=[];
function add(p){const l=cart.find(x=>x.id==p.id);if(l)l.qty=+(l.qty+1).toFixed(3);else cart.push({id:p.id,name:p.name,price:p.price,qty:1});draw();$('#q').focus()}
function notfound(q){$('#msg').innerHTML='Не знайдено: '+esc(q)+' — <a href="/product/0?barcode='+encodeURIComponent(q)+'" target="_blank">додати товар</a>'}
const sum=()=>cart.reduce((s,l)=>s+Math.round(l.price*l.qty),0);
function chg(){const g=Math.round(parseFloat(($('#given').value||'0').replace(',','.'))*100)||0;$('#change').textContent=g>sum()?f(g-sum()):'0.00'}
function draw(){$('#msg').textContent='';$('#tb').innerHTML=cart.map((l,i)=>`<tr><td>${esc(l.name)}</td><td class="r">${f(l.price)}</td>
<td><input type="number" step="0.001" min="0" value="${l.qty}" style="width:80px" onchange="cart[${i}].qty=parseFloat(this.value)||0;draw()"></td>
<td class="r">${f(Math.round(l.price*l.qty))}</td><td><button class="sec" onclick="cart.splice(${i},1);draw()">×</button></td></tr>`).join('');$('#total').textContent=f(sum());chg()}
async function pay(){if(!cart.some(l=>l.qty>0))return;
const r=await fetch('/api/sale',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:cart.filter(l=>l.qty>0).map(l=>({id:l.id,qty:l.qty})),pay:document.querySelector('[name=pay]:checked').value,given:$('#given').value})});
const d=await r.json();if(d.error){alert(d.error);return}
cart.length=0;draw();$('#given').value='';$('#msg').textContent='Чек №'+d.id+' — '+f(d.total)+' грн (решта '+f(d.change)+')';
if($('#prn').checked)$('#rf').src='/receipt/'+d.id+'?print=1&t='+Date.now();$('#q').focus()}
document.addEventListener('keydown',e=>{if(e.key=='F9'){e.preventDefault();pay()}});
</script>{% endblock %}"""

T['receipt.html'] = """<!doctype html><meta charset="utf-8"><title>Чек {{ s.id }}</title>
<style>@page{size:80mm auto;margin:2mm}body{font:12px/1.35 Consolas,'Courier New',monospace;width:72mm;margin:0 auto;color:#000}
.c{text-align:center}.l{display:flex;justify-content:space-between}hr{border:0;border-top:1px dashed #000}</style>
<div class="c"><b>{{ shop }}</b><br>Чек № {{ s.id }}<br>{{ s.ts }}</div><hr>
{% for i in items %}<div>{{ i.name }}</div><div class="l"><span>{{ i.qty|n }} x {{ i.price|m }}</span><span>{{ (i.price*i.qty)|round|int|m }}</span></div>{% endfor %}<hr>
<div class="l"><b>РАЗОМ</b><b>{{ s.total|m }} грн</b></div>
<div class="l"><span>{{ 'Готівка' if s.pay=='cash' else 'Картка' }}</span><span>{% if s.pay=='cash' %}Отримано {{ s.given|m }} / Решта {{ (s.given-s.total)|m }}{% endif %}</span></div>
<hr><div class="c">{{ footer }}</div>
{% if request.args.get('print') %}<script>onload=function(){window.focus();window.print()}</script>{% endif %}"""

T['index.html'] = """{% extends 'base.html' %}{% block c %}<meta http-equiv="refresh" content="30">
<form>З <input type="date" name="from" value="{{ d1 }}"> по <input type="date" name="to" value="{{ d2 }}"> <button>Показати</button>
{% for t,a,b in quick %}<a class="btn sec" href="/?from={{ a }}&to={{ b }}">{{ t }}</a> {% endfor %}</form><p></p>
<div class="card">Виручка<b>{{ rev|m }} грн</b></div><div class="card">Чеків<b>{{ n }}</b></div>
<div class="card">Прибуток (за закупівельною ціною)<b>{{ pr|m }} грн</b></div>
<table><tr><th>№</th><th>Час</th><th>Оплата</th><th class="r">Сума</th><th></th></tr>
{% for s in sales %}<tr><td>{{ s.id }}</td><td>{{ s.ts }}</td><td>{{ 'Готівка' if s.pay=='cash' else 'Картка' }}</td><td class="r">{{ s.total|m }}</td>
<td><a href="/receipt/{{ s.id }}" target="_blank">чек</a></td></tr>
<tr><td></td><td colspan="4" class="dim">{% for i in items[s.id] %}{{ i.name }} × {{ i.qty|n }}{{ '; ' if not loop.last }}{% endfor %}</td></tr>
{% else %}<tr><td colspan="5">Продажів немає</td></tr>{% endfor %}</table>{% endblock %}"""

T['products.html'] = """{% extends 'base.html' %}{% block c %}
<form><input name="q" value="{{ q }}" placeholder="Пошук: назва або штрихкод"> <button>Знайти</button> <a class="btn sec" href="/product/0">+ Новий товар</a></form><p></p>
<div class="card">Товарів<b>{{ rows|length }}</b></div><div class="card">Залишки (закупівля)<b>{{ cost|round|int|m }}</b></div><div class="card">Залишки (роздріб)<b>{{ retail|round|int|m }}</b></div>
<table><tr><th>Штрихкод</th><th>Назва</th><th class="r">Ціна</th><th class="r">Закупівля</th><th class="r">Залишок</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.barcode or '' }}</td><td>{{ r.name }}</td><td class="r">{{ r.price|m }}</td><td class="r">{{ r.cost|m }}</td>
<td class="r {{ 'neg' if r.qty<=0 }}">{{ r.qty|n }} {{ r.unit }}</td><td><a href="/product/{{ r.id }}">змінити</a></td></tr>{% endfor %}</table>{% endblock %}"""

T['product.html'] = """{% extends 'base.html' %}{% block c %}<h2>{{ 'Редагування товару' if p else 'Новий товар' }}</h2><p class="err">{{ err }}</p>
<form method="post"><p>Штрихкод<br><input name="barcode" value="{{ d.get('barcode') or '' }}"></p>
<p>Назва<br><input name="name" value="{{ d.get('name','') }}" size="40" required></p>
<p>Одиниця (шт, кг, л)<br><input name="unit" value="{{ d.get('unit','шт') }}" size="6"></p>
<p>Ціна продажу<br><input name="price" value="{{ d.get('price','') }}" size="10"></p>
<p>Закупівельна ціна<br><input name="cost" value="{{ d.get('cost','') }}" size="10"></p>
<p>Залишок (фактичний)<br><input name="qty" value="{{ d.get('qty','0') }}" size="10"></p>
<button>Зберегти</button> <a class="btn sec" href="/products">Назад</a>
{% if p %} <button name="archive" value="1" class="sec" onclick="return confirm('Прибрати товар зі списку?')">В архів</button>{% endif %}</form>{% endblock %}"""

T['doc.html'] = """{% extends 'base.html' %}{% block c %}<h2>{{ 'Прихід товару' if kind=='in' else 'Списання товару' }}</h2>
<input id="q" autofocus autocomplete="off" placeholder="Штрихкод або назва…" style="width:60%"><div id="sugg" style="margin:6px 0"></div>
<div id="nb" hidden class="card">Новий товар: <b id="nbc" style="font-size:15px"></b><input id="nn" placeholder="Назва"> <input id="np" size="7" placeholder="Ціна"> <input id="nc" size="7" placeholder="Закупівля"> <button onclick="mk()">Створити</button></div>
<table><thead><tr><th>Товар</th><th>К-сть</th>{% if kind=='in' %}<th>Закупівля</th><th>Ціна продажу</th>{% endif %}<th></th></tr></thead><tbody id="tb"></tbody></table>
<p>{{ 'Причина' if kind=='out' else 'Примітка (накладна)' }}: <input id="note" value="{{ 'Брак / псування' if kind=='out' }}" size="30"> <button onclick="save()">Провести</button></p>
<script>{% include 'lk.js' %}
const KIND="{{ kind }}",L=[];let bc='';
function add(p){const l=L.find(x=>x.id==p.id);if(l)l.qty=+l.qty+1;else L.push({id:p.id,name:p.name,qty:1,cost:f(p.cost),price:f(p.price)});$('#nb').hidden=true;draw();$('#q').focus()}
function notfound(q){bc=q;$('#nbc').textContent=q+' ';$('#nb').hidden=false;$('#nn').focus()}
async function mk(){const r=await fetch('/api/product',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({barcode:bc,name:$('#nn').value,price:$('#np').value,cost:$('#nc').value})});
const d=await r.json();if(d.error){alert(d.error);return}$('#nn').value=$('#np').value=$('#nc').value='';add(d)}
function draw(){$('#tb').innerHTML=L.map((l,i)=>`<tr><td>${esc(l.name)}</td><td><input type="number" step="0.001" min="0" value="${l.qty}" style="width:90px" onchange="L[${i}].qty=this.value"></td>`+
(KIND=='in'?`<td><input value="${l.cost}" size="7" onchange="L[${i}].cost=this.value"></td><td><input value="${l.price}" size="7" onchange="L[${i}].price=this.value"></td>`:'')+
`<td><button class="sec" onclick="L.splice(${i},1);draw()">×</button></td></tr>`).join('')}
async function save(){const r=await fetch('/api/doc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind:KIND,note:$('#note').value,lines:L})});
const d=await r.json();if(d.error){alert(d.error);return}alert('Проведено');L.length=0;draw();$('#q').focus()}
</script>{% endblock %}"""

T['backup.html'] = """{% extends 'base.html' %}{% block c %}<h2>Резервні копії</h2><p><b>{{ msg }}</b></p>
<p>Остання копія: <b>{{ last or 'ще немає' }}</b><br>Флешка/диск ({{ ext or 'не налаштовано — див. config.ini, backup_dir' }}): <b>{{ st or '—' }}</b></p>
<form method="post"><button>Зробити копію зараз</button></form><p class="dim">Автоматично — кожні кілька годин (config.ini). Локальні копії: папка backups.</p>
<ul>{% for f in files %}<li>{{ f }}</li>{% endfor %}</ul>{% endblock %}"""

app.jinja_loader = DictLoader(T)

if __name__ == '__main__':
    os.makedirs(os.path.join(BASE, 'logs'), exist_ok=True)
    h = RotatingFileHandler(os.path.join(BASE, 'logs', 'app.log'), maxBytes=1_000_000, backupCount=5, encoding='utf-8')
    app.logger.addHandler(h)
    app.logger.setLevel(logging.INFO)
    threading.Thread(target=scheduler, daemon=True).start()
    if getattr(sys, 'frozen', False):
        threading.Timer(2, lambda: webbrowser.open(f"http://localhost:{C['port']}/pos")).start()
    print(f"Running: http://localhost:{C['port']}")
    try:
        from waitress import serve
    except ImportError:
        app.run(host=C['host'], port=int(C['port']), threaded=True)
    else:
        serve(app, host=C['host'], port=int(C['port']), threads=8)
