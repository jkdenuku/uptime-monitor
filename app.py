from flask import Flask, render_template, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import datetime
import os

app = Flask(__name__)

# Database config
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get(
    'DATABASE_URL', 
    'sqlite:///' + os.path.join(basedir, 'monitors.db')
)
# Fix for Render PostgreSQL URL
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


class Monitor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    interval = db.Column(db.Integer, default=5)  # minutes
    status = db.Column(db.String(10), default='unknown')  # up / down / unknown
    response_time = db.Column(db.Float, nullable=True)
    last_checked = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    checks = db.relationship('CheckLog', backref='monitor', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        recent_logs = CheckLog.query.filter_by(monitor_id=self.id)\
            .order_by(CheckLog.checked_at.desc()).limit(20).all()
        uptime = None
        if recent_logs:
            up_count = sum(1 for l in recent_logs if l.status == 'up')
            uptime = round(up_count / len(recent_logs) * 100, 1)
        return {
            'id': self.id,
            'name': self.name,
            'url': self.url,
            'interval': self.interval,
            'status': self.status,
            'response_time': self.response_time,
            'last_checked': self.last_checked.isoformat() if self.last_checked else None,
            'created_at': self.created_at.isoformat(),
            'uptime': uptime,
            'recent_logs': [l.to_dict() for l in reversed(recent_logs)]
        }


class CheckLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    monitor_id = db.Column(db.Integer, db.ForeignKey('monitor.id'), nullable=False)
    status = db.Column(db.String(10), nullable=False)
    response_time = db.Column(db.Float, nullable=True)
    status_code = db.Column(db.Integer, nullable=True)
    checked_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def to_dict(self):
        return {
            'status': self.status,
            'response_time': self.response_time,
            'status_code': self.status_code,
            'checked_at': self.checked_at.isoformat()
        }


def check_url(monitor_id):
    with app.app_context():
        monitor = Monitor.query.get(monitor_id)
        if not monitor:
            return
        try:
            start = datetime.datetime.utcnow()
            resp = requests.get(monitor.url, timeout=10, allow_redirects=True)
            elapsed = (datetime.datetime.utcnow() - start).total_seconds() * 1000
            status = 'up' if resp.status_code < 400 else 'down'
            status_code = resp.status_code
        except Exception:
            elapsed = None
            status = 'down'
            status_code = None

        monitor.status = status
        monitor.response_time = round(elapsed, 1) if elapsed else None
        monitor.last_checked = datetime.datetime.utcnow()

        log = CheckLog(
            monitor_id=monitor.id,
            status=status,
            response_time=monitor.response_time,
            status_code=status_code
        )
        db.session.add(log)
        # Keep only last 100 logs per monitor
        old_logs = CheckLog.query.filter_by(monitor_id=monitor.id)\
            .order_by(CheckLog.checked_at.desc()).offset(100).all()
        for old in old_logs:
            db.session.delete(old)
        db.session.commit()


def run_all_checks():
    with app.app_context():
        monitors = Monitor.query.all()
        for m in monitors:
            check_url(m.id)


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/monitors', methods=['GET'])
def get_monitors():
    monitors = Monitor.query.order_by(Monitor.created_at.desc()).all()
    return jsonify([m.to_dict() for m in monitors])


@app.route('/api/monitors', methods=['POST'])
def add_monitor():
    data = request.json
    if not data.get('name') or not data.get('url'):
        return jsonify({'error': 'name and url are required'}), 400
    url = data['url']
    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url
    monitor = Monitor(
        name=data['name'],
        url=url,
        interval=int(data.get('interval', 5))
    )
    db.session.add(monitor)
    db.session.commit()
    # Immediately check
    check_url(monitor.id)
    return jsonify(monitor.to_dict()), 201


@app.route('/api/monitors/<int:monitor_id>', methods=['DELETE'])
def delete_monitor(monitor_id):
    monitor = Monitor.query.get_or_404(monitor_id)
    db.session.delete(monitor)
    db.session.commit()
    return jsonify({'message': 'deleted'})


@app.route('/api/monitors/<int:monitor_id>/check', methods=['POST'])
def manual_check(monitor_id):
    check_url(monitor_id)
    monitor = Monitor.query.get_or_404(monitor_id)
    return jsonify(monitor.to_dict())


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_all_checks, 'interval', minutes=5)
    scheduler.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
