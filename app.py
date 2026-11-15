import io
import os
import secrets
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash


def create_app(config=None):
    # The archive keeps directory assets intact when uploading via GitHub's file picker.
    root = Path(__file__).resolve().parent
    archive_mode = not (root / 'templates').is_dir()
    app = Flask(__name__, static_folder=None if archive_mode else 'static')
    if archive_mode:
        with zipfile.ZipFile(root / 'assets.zip') as bundle:
            templates = {name[10:]: bundle.read(name).decode('utf-8')
                         for name in bundle.namelist() if name.startswith('templates/')}
            assets = {name[7:]: bundle.read(name)
                      for name in bundle.namelist() if name.startswith('static/')}
        app.jinja_loader = DictLoader(templates)

        @app.get('/static/<path:filename>', endpoint='static')
        def archived_static(filename):
            if filename not in assets:
                abort(404)
            content_type = 'text/css' if filename.endswith('.css') else 'application/javascript'
            return app.response_class(assets[filename], mimetype=content_type)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY'),
        ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD'),
        INVITE_CODE=os.environ.get('INVITE_CODE'),
        DATABASE=str(Path(os.environ.get('DATA_DIR', 'data')) / 'gifts.sqlite3'),
        DEADLINE=os.environ.get('DEADLINE', '2026-11-17T23:59:00+08:00'),
        MAX_CONTENT_LENGTH=6 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=os.environ.get('RENDER') == 'true',
    )
    if config:
        app.config.update(config)
    for key in ('SECRET_KEY', 'ADMIN_PASSWORD', 'INVITE_CODE'):
        if not app.config.get(key):
            raise RuntimeError(f'Please configure {key}')
    if len(app.config['ADMIN_PASSWORD']) < 12:
        raise RuntimeError('ADMIN_PASSWORD must contain at least 12 characters')
    deadline = datetime.fromisoformat(app.config['DEADLINE'])
    if deadline.tzinfo is None:
        raise RuntimeError('DEADLINE must include a timezone offset')
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(app.config['DATABASE'])) as db:
        db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS people (
                id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, gender TEXT NOT NULL,
                password TEXT NOT NULL, title TEXT NOT NULL, link TEXT NOT NULL,
                note TEXT NOT NULL, image BLOB);
            CREATE TABLE IF NOT EXISTS pairs (giver TEXT PRIMARY KEY, recipient TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS choices (id TEXT PRIMARY KEY, giver TEXT NOT NULL,
                owner TEXT NOT NULL, position INTEGER NOT NULL, UNIQUE(giver, owner));
            CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, created REAL NOT NULL);
        ''')

    def db():
        if 'db' not in g:
            g.db = sqlite3.connect(app.config['DATABASE'], timeout=20)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    def drawn():
        return db().execute('SELECT 1 FROM pairs LIMIT 1').fetchone() is not None

    def closed():
        return datetime.now(timezone.utc) > deadline

    def me():
        return db().execute('SELECT * FROM people WHERE id=?', (session.get('person'),)).fetchone()

    def admin_required():
        if not session.get('admin'):
            abort(403)

    @app.before_request
    def csrf():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        if request.method == 'POST':
            if not secrets.compare_digest(session['csrf'].encode(), request.form.get('csrf', '').encode()):
                abort(400, '頁面已過期，請重新整理後再試。')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response

    @app.context_processor
    def context():
        return dict(csrf_token=session['csrf'], deadline_label=deadline.strftime('%Y/%m/%d %H:%M'),
                    deadline_zone=deadline.strftime('UTC%z'))

    def limit_attempts(identity):
        key = (request.remote_addr or '') + ':' + identity[:80]
        now = time.time()
        connection = db()
        with connection:
            connection.execute('DELETE FROM attempts WHERE created < ?', (now - 600,))
            count = connection.execute('SELECT count(*) FROM attempts WHERE key=?', (key,)).fetchone()[0]
            if count >= 15:
                abort(429, '嘗試次數過多，請等 10 分鐘再試。')
            connection.execute('INSERT INTO attempts VALUES (?,?)', (key, now))

    def gift_fields(existing=None):
        title = request.form.get('title', '').strip()
        link = request.form.get('link', '').strip()
        note = request.form.get('note', '').strip()
        gender = request.form.get('gender', '').strip()
        if gender == '其他／自填':
            gender = request.form.get('gender_other', '').strip() or '其他'
        if not gender or len(gender) > 40:
            raise ValueError('請選擇性別，或選擇不透露。')
        if not title or len(title) > 120 or len(note) > 1000 or len(link) > 2000:
            raise ValueError('請填商品名稱（120 字以內），備註限 1,000 字。')
        if link:
            try:
                parts = urlsplit(link)
                valid = parts.scheme in ('http', 'https') and parts.hostname and not parts.username and not parts.password
            except ValueError:
                valid = False
            if not valid:
                raise ValueError('購買連結必須是完整的 http:// 或 https:// 網址。')
        image = existing['image'] if existing else None
        upload = request.files.get('image')
        if upload and upload.filename:
            try:
                raw = upload.stream.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise ValueError('圖片太大，請使用小於 5 MB 的圖片。')
                with Image.open(io.BytesIO(raw)) as pic:
                    if pic.format not in ('JPEG', 'PNG', 'WEBP') or pic.width * pic.height > 20_000_000:
                        raise ValueError('請上傳 JPG、PNG 或 WebP 圖片，最多 2,000 萬像素。')
                    pic = ImageOps.exif_transpose(pic).convert('RGB')
                    pic.thumbnail((1400, 1400))
                    output = io.BytesIO()
                    pic.save(output, 'JPEG', quality=85)
                    image = output.getvalue()
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise ValueError('無法讀取圖片，請換一張 JPG、PNG 或 WebP。')
        if not link and not image:
            raise ValueError('請至少提供購買連結或一張圖片。')
        return gender, title, link, note, image

    @app.get('/health')
    def health():
        db().execute('SELECT 1').fetchone()
        return {'ok': True}

    @app.get('/')
    def home():
        if me():
            return redirect(url_for('dashboard'))
        return render_template('home.html', drawn=drawn(), closed=closed())

    @app.post('/join')
    def join():
        name = request.form.get('name', '').strip()
        limit_attempts('join')
        if not secrets.compare_digest(request.form.get('invite', '').encode(), app.config['INVITE_CODE'].encode()):
            flash('活動邀請碼不正確，請向負責人確認。', 'error')
            return redirect(url_for('home'))
        password = request.form.get('password', '')
        try:
            if not name or len(name) > 40 or not 8 <= len(password) <= 128:
                raise ValueError('請填姓名（40 字以內），個人通行碼需為 8–128 字。')
            fields = gift_fields()
            person = secrets.token_urlsafe(18)
            connection = db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if drawn() or closed():
                    raise ValueError('報名已截止或已配對，無法新增參加者。')
                connection.execute('INSERT INTO people VALUES (?,?,?,?,?,?,?,?)',
                                   (person, name, fields[0], generate_password_hash(password), *fields[1:]))
            session.clear()
            session['person'] = person
            return redirect(url_for('dashboard'))
        except (ValueError, sqlite3.IntegrityError) as error:
            flash(str(error) if isinstance(error, ValueError) else '這個姓名已報名，請登入；同名者請加上暱稱。', 'error')
            return redirect(url_for('home'))

    @app.post('/login')
    def login():
        name = request.form.get('name', '').strip()
        limit_attempts('login:' + name)
        person = db().execute('SELECT * FROM people WHERE name=?', (name,)).fetchone()
        password = request.form.get('password', '')
        if person and len(password) <= 128 and check_password_hash(person['password'], password):
            session.clear()
            session['person'] = person['id']
            return redirect(url_for('dashboard'))
        flash('姓名或個人通行碼不正確。', 'error')
        return redirect(url_for('home'))

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('home'))

    @app.get('/me')
    def dashboard():
        person = me()
        if not person:
            return redirect(url_for('home'))
        recipient = db().execute('SELECT p.name,p.gender FROM pairs a JOIN people p ON p.id=a.recipient WHERE a.giver=?', (person['id'],)).fetchone()
        choices = db().execute('SELECT c.id,p.title,p.link,p.note,p.image IS NOT NULL AS has_image FROM choices c JOIN people p ON p.id=c.owner WHERE c.giver=? ORDER BY c.position', (person['id'],)).fetchall()
        return render_template('dashboard.html', person=person, recipient=recipient, choices=choices, closed=closed())

    @app.post('/gift')
    def update_gift():
        person = me()
        if not person:
            abort(403)
        try:
            fields = gift_fields(person)
            connection = db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if drawn() or closed():
                    raise ValueError('已截止或完成配對，禮物資料已鎖定。')
                connection.execute('UPDATE people SET gender=?,title=?,link=?,note=?,image=? WHERE id=?', (*fields, person['id']))
            flash('願望已更新！', 'success')
        except ValueError as error:
            flash(str(error), 'error')
        return redirect(url_for('dashboard'))

    @app.get('/image/<token>')
    def gift_image(token):
        person = me()
        if not person and not session.get('admin'):
            abort(403)
        if token == 'mine' and person:
            row = person
        elif session.get('admin'):
            row = db().execute('SELECT image FROM people WHERE id=?', (token,)).fetchone()
        else:
            row = db().execute('SELECT p.image FROM choices c JOIN people p ON p.id=c.owner WHERE c.id=? AND c.giver=?', (token, person['id'])).fetchone()
        if not row or not row['image']:
            abort(404)
        return send_file(io.BytesIO(row['image']), mimetype='image/jpeg')

    @app.route('/admin', methods=['GET', 'POST'])
    def admin():
        if request.method == 'POST':
            limit_attempts('admin')
            if secrets.compare_digest(request.form.get('password', '').encode(), app.config['ADMIN_PASSWORD'].encode()):
                session.clear()
                session['admin'] = True
                return redirect(url_for('admin'))
            flash('負責人密碼不正確。', 'error')
        people = db().execute('SELECT id,name,gender,title,link,note,image IS NOT NULL AS has_image FROM people ORDER BY rowid').fetchall() if session.get('admin') else []
        return render_template('admin.html', people=people, drawn=drawn())

    @app.post('/admin/draw')
    def draw():
        admin_required()
        connection = db()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if drawn():
                    raise ValueError('已完成配對，結果不會重新抽取。')
                ids = [row[0] for row in connection.execute('SELECT id FROM people')]
                if len(ids) < 4:
                    raise ValueError('至少需要 4 人，才能排除自己的禮物並提供 3 個選項。')
                rng = secrets.SystemRandom()
                # Rejection sampling produces a uniform random derangement.
                recipients = ids.copy()
                while True:
                    rng.shuffle(recipients)
                    if all(a != b for a, b in zip(ids, recipients)):
                        break
                for giver, recipient in zip(ids, recipients):
                    connection.execute('INSERT INTO pairs VALUES (?,?)', (giver, recipient))
                    options = [recipient] + rng.sample([i for i in ids if i not in (giver, recipient)], 2)
                    rng.shuffle(options)
                    for position, owner in enumerate(options):
                        connection.execute('INSERT INTO choices VALUES (?,?,?,?)', (secrets.token_urlsafe(18), giver, owner, position))
            flash('配對完成！大家現在可以登入查看自己的任務。', 'success')
        except ValueError as error:
            flash(str(error), 'error')
        return redirect(url_for('admin'))

    @app.errorhandler(413)
    def too_large(error):
        return render_template('error.html', message='圖片太大，請使用小於 5 MB 的圖片。'), 413

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(429)
    def http_error(error):
        return render_template('error.html', message=error.description), error.code

    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=int(os.environ.get('PORT', 5000)))
