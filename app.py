import os
import smtplib
import threading
import time
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-in-production')

DATABASE_URL  = os.environ.get('DATABASE_URL', '')
MAIL_SERVER   = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
MAIL_PORT     = int(os.environ.get('MAIL_PORT', 587))
MAIL_USER     = os.environ.get('MAIL_USER', '')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
MAIL_FROM     = os.environ.get('MAIL_FROM', MAIL_USER)
MAIL_TO       = os.environ.get('MAIL_TO', '')
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
            # 直近1000件だけ保持
            cur.execute('''
                DELETE FROM checks WHERE monitor_id=%s AND id NOT IN
                (SELECT id FROM checks WHERE monitor_id=%s ORDER BY id DESC LIMIT 1000)
            ''', (mid, mid))
        conn.commit()

    if status == 'down' and monitor['last_status'] != 'down':
        send_alert(monitor, status)

def send_alert(monitor, status):
    to_addr = monitor['notify_email'] or MAIL_TO
    if not to_addr or not MAIL_USER:
        return
    subject = f"[ALERT] {monitor['name']} is DOWN"
    body    = (f"Monitor: {monitor['name']}\n"
               f"URL: {monitor['url']}\n"
               f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From']    = MAIL_FROM
    msg['To']      = to_addr
    try:
        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as s:
            s.starttls()
            s.login(MAIL_USER, MAIL_PASSWORD)
            s.sendmail(MAIL_FROM, [to_addr], msg.as_string())
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


@app.route('/')
def index():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM monitors ORDER BY created_at DESC')
            monitors = cur.fetchall()
    total = len(monitors)
    up    = sum(1 for m in monitors if m['last_status'] == 'up')
    down  = sum(1 for m in monitors if m['last_status'] == 'down')
    return render_template('index.html', monitors=monitors, total=total, up=up, down=down)

@app.route('/monitor/add', methods=['GET','POST'])
def add_monitor():
    if request.method == 'POST':
        name  = request.form.get('name','').strip()
        url   = request.form.get('url','').strip()
        email = request.form.get('notify_email','').strip()
        if not name or not url:
            flash('Name and URL are required.', 'error')
            return redirect(url_for('add_monitor'))
        if not url.startswith(('http://','https://')):
            url = 'https://' + url
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO monitors (name, url, notify_email) VALUES (%s,%s,%s)', (name, url, email))
            conn.commit()
        flash(f'Monitor "{name}" added!', 'success')
        return redirect(url_for('index'))
    return render_template('add.html')

@app.route('/monitor/<int:mid>')
def monitor_detail(mid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM monitors WHERE id=%s', (mid,))
            m = cur.fetchone()
            if not m:
                return redirect(url_for('index'))
            cur.execute('SELECT * FROM checks WHERE monitor_id=%s ORDER BY checked_at DESC LIMIT 200', (mid,))
            checks = cur.fetchall()
            since = datetime.utcnow() - timedelta(hours=24)
            cur.execute('SELECT status FROM checks WHERE monitor_id=%s AND checked_at>=%s', (mid, since))
            rows = cur.fetchall()
    uptime_24h = None
    if rows:
        uptime_24h = round(sum(1 for r in rows if r['status'] == 'up') / len(rows) * 100, 2)
    return render_template('detail.html', monitor=m, checks=checks, uptime_24h=uptime_24h)

@app.route('/monitor/<int:mid>/delete', methods=['POST'])
def delete_monitor(mid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM monitors WHERE id=%s', (mid,))
        conn.commit()
    flash('Monitor deleted.', 'success')
    return redirect(url_for('index'))

@app.route('/monitor/<int:mid>/toggle', methods=['POST'])
def toggle_monitor(mid):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT active FROM monitors WHERE id=%s', (mid,))
            m = cur.fetchone()
            if m:
                cur.execute('UPDATE monitors SET active=%s WHERE id=%s', (0 if m['active'] else 1, mid))
        conn.commit()
    return redirect(url_for('index'))

@app.route('/api/monitor/<int:mid>/chart')
def chart_data(mid):
    hours = int(request.args.get('hours', 24))
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT checked_at, response_ms, status FROM checks WHERE monitor_id=%s AND checked_at>=%s ORDER BY checked_at ASC',
                (mid, since)
            )
            rows = cur.fetchall()
    return jsonify([{'t': str(r['checked_at']), 'ms': r['response_ms'], 'status': r['status']} for r in rows])

@app.route('/api/status')
def api_status():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT id, name, url, last_status, last_checked FROM monitors')
            monitors = cur.fetchall()
    return jsonify([dict(m) for m in monitors])

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    with get_db() as conn:
        if request.method == 'POST':
            with conn.cursor() as cur:
                for k in ['smtp_host', 'smtp_port', 'smtp_user', 'smtp_pass', 'notify_email']:
                    cur.execute(
                        'INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value',
                        (k, request.form.get(k, ''))
                    )
            conn.commit()
            flash('Settings saved!', 'success')
            return redirect(url_for('settings'))
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM settings')
            cfg = {row['key']: row['value'] for row in cur.fetchall()}
    return render_template('settings.html', cfg=cfg)


# gunicornでも動くようにモジュールレベルで初期化
init_db()
threading.Thread(target=monitoring_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
