import os
import smtplib
import threading
import time
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

DATABASE_URL   = os.environ.get('DATABASE_URL', '')
CHECK_INTERVAL = int(os.environ.get('CHECK_INTERVAL', 60))


def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS monitors (
                    id           SERIAL PRIMARY KEY,
                    name         TEXT    NOT NULL,
                    url          TEXT    NOT NULL,
                    active       INTEGER NOT NULL DEFAULT 1,
                    last_status  TEXT,
                    last_checked TIMESTAMP,
                    notify_email TEXT,
                    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS checks (
                    id           SERIAL PRIMARY KEY,
                    monitor_id   INTEGER NOT NULL,
                    status       TEXT    NOT NULL,
                    status_code  INTEGER,
                    response_ms  INTEGER,
                    checked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
            ''')
        conn.commit()

def check_url(monitor):
    mid   = monitor['id']
    url   = monitor['url']
    start = time.time()
    status, code, ms = 'down', None, None
    try:
        resp   = requests.get(url, timeout=15, allow_redirects=True)
        ms     = int((time.time() - start) * 1000)
        code   = resp.status_code
        status = 'up' if resp.status_code < 400 else 'down'
    except Exception:
        ms = int((time.time() - start) * 1000)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO checks (monitor_id, status, status_code, response_ms) VALUES (%s,%s,%s,%s)',
                (mid, status, code, ms)
            )
            cur.execute(
                'UPDATE monitors SET last_status=%s, last_checked=CURRENT_TIMESTAMP WHERE id=%s',
                (status, mid)
            )
            cur.execute('''
                DELETE FROM checks WHERE monitor_id=%s AND id NOT IN
                (SELECT id FROM checks WHERE monitor_id=%s ORDER BY id DESC LIMIT 1000)
            ''', (mid, mid))
        conn.commit()

    if status == 'down' and monitor['last_status'] != 'down':
        threading.Thread(target=send_alert, args=(monitor,), daemon=True).start()

def send_alert(monitor):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM settings')
            cfg = {r['key']: r['value'] for r in cur.fetchall()}
    to_addr = monitor['notify_email'] or cfg.get('notify_email','')
    if not to_addr or not cfg.get('smtp_user'):
        return
    subject = f"[ALERT] {monitor['name']} is DOWN"
    body    = (f"Monitor: {monitor['name']}\n"
               f"URL: {monitor['url']}\n"
               f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From']    = cfg.get('smtp_user')
    msg['To']      = to_addr
    try:
        with smtplib.SMTP(cfg.get('smtp_host','smtp.gmail.com'), int(cfg.get('smtp_port', 587))) as s:
            s.starttls()
            s.login(cfg.get('smtp_user'), cfg.get('smtp_pass',''))
            s.sendmail(cfg.get('smtp_user'), [to_addr], msg.as_string())
    except Exception as e:
        print(f"Mail error: {e}")

def monitoring_loop():
    while True:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT * FROM monitors WHERE active=1')
                    monitors = cur.fetchall()
            for m in monitors:
                threading.Thread(target=check_url, args=(m,), daemon=True).start()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(CHECK_INTERVAL)


# ── 唯一のHTMLルート ──────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

# ── API ──────────────────────────────────────────────────────────────────────
@app.route('/api/monitors')
def api_monitors():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM monitors ORDER BY created_at DESC')
            monitors = [dict(m) for m in cur.fetchall()]
            for m in monitors:
                if m.get('last_checked'):
                    m['last_checked'] = str(m['last_checked'])
                if m.get('created_at'):
                    m['created_at'] = str(m['created_at'])
    return jsonify(monitors)

@app.route('/api/monitors', methods=['POST'])
def api_add_monitor():
    data  = request.json
    name  = (data.get('name') or '').strip()
    url   = (data.get('url') or '').strip()
    email = (data.get('notify_email') or '').strip()
    if not name or not url:
        return jsonify({'error': 'Name and URL are required'}), 400
    if not url.startswith(('http://','https://')):
        url = 'https://' + url
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'INSERT INTO monitors (name, url, notify_email) VALUES (%s,%s,%s) RETURNING id',
                (name, url, email)
            )
            new_id = cur.fetchone()['id']
        conn.commit()
    return jsonify({'id': new_id, 'name': name, 'url': url}), 201

@app.route('/api/monitors/<int:mid>', methods=['DELETE'])
def api_delete_monitor(mid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM monitors WHERE id=%s', (mid,))
        conn.commit()
    return jsonify({'ok': True})

@app.route('/api/monitors/<int:mid>/toggle', methods=['POST'])
def api_toggle_monitor(mid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT active FROM monitors WHERE id=%s', (mid,))
            m = cur.fetchone()
            if not m:
                return jsonify({'error': 'Not found'}), 404
            cur.execute('UPDATE monitors SET active=%s WHERE id=%s', (0 if m['active'] else 1, mid))
        conn.commit()
    return jsonify({'ok': True})

@app.route('/api/monitors/<int:mid>/checks')
def api_checks(mid):
    hours = int(request.args.get('hours', 24))
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT checked_at, response_ms, status, status_code FROM checks WHERE monitor_id=%s AND checked_at>=%s ORDER BY checked_at ASC',
                (mid, since)
            )
            rows = cur.fetchall()
    return jsonify([{'t': str(r['checked_at']), 'ms': r['response_ms'], 'status': r['status'], 'code': r['status_code']} for r in rows])

@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT key, value FROM settings')
            cfg = {r['key']: r['value'] for r in cur.fetchall()}
    # パスワードは返さない
    cfg.pop('smtp_pass', None)
    return jsonify(cfg)

@app.route('/api/settings', methods=['POST'])
def api_save_settings():
    data = request.json
    with get_db() as conn:
        with conn.cursor() as cur:
            for k in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'notify_email']:
                if k in data:
                    cur.execute(
                        'INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value',
                        (k, data[k])
                    )
        conn.commit()
    return jsonify({'ok': True})


init_db()
threading.Thread(target=monitoring_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
