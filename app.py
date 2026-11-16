import io
import os
import secrets
import sqlite3
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from jinja2 import DictLoader
from werkzeug.security import check_password_hash, generate_password_hash


def match_people(people, previous=None):
    """Randomized bipartite matching; finite search even for impossible rosters."""
    rng = secrets.SystemRandom()
    allowed = {}
    names = {p['name']: p for p in people}
    for p in people:
        partner = p['partner']
        if partner and partner not in names:
            raise ValueError(f"{p['name']} 填寫的伴侶「{partner}」不在名單中，請先修正伴侶姓名或等對方報名。")
        allowed[p['id']] = [q['id'] for q in people if q['id'] != p['id']
            and q['name'] != partner and q['partner'] != p['name']
            and ('祺' not in p['name'] or q['gender'] == '女')]

    def solve(blocked=None):
        owners = {}
        def augment(giver, seen):
            options = allowed[giver].copy()
            rng.shuffle(options)
            for recipient in options:
                if (giver, recipient) == blocked or recipient in seen:
                    continue
                seen.add(recipient)
                if recipient not in owners or augment(owners[recipient], seen):
                    owners[recipient] = giver
                    return True
            return False
        order = list(allowed)
        rng.shuffle(order)
        for giver in order:
            if not augment(giver, set()):
                return None
        return {giver: recipient for recipient, giver in owners.items()}

    result = solve()
    if result is None:
        raise ValueError('目前名單無法同時滿足不抽自己、情侶避配及名字含「祺」只能送女生的規則。請調整名單或性別／伴侶資料；原配對保持不變。')
    if previous and result == previous:
        edges = list(previous.items())
        rng.shuffle(edges)
        for edge in edges:
            alternative = solve(edge)
            if alternative:
                return alternative
        raise ValueError('目前限制下只有原本這一種配對，無法重抽出不同結果；原配對保持不變。')
    return result


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
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        with db:
            db.execute('BEGIN IMMEDIATE')
            if 'partner' not in {r[1] for r in db.execute('PRAGMA table_info(people)')}:
                db.execute("ALTER TABLE people ADD COLUMN partner TEXT NOT NULL DEFAULT ''")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('deadline', ?)", (deadline.isoformat(),))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('revision', '0')")

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
        return datetime.now(timezone.utc) > current_deadline()

    def current_deadline():
        return datetime.fromisoformat(db().execute("SELECT value FROM settings WHERE key='deadline'").fetchone()[0])

    def revision():
        return db().execute("SELECT value FROM settings WHERE key='revision'").fetchone()[0]

    def bump_revision():
        db().execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def partner_field(name):
        partner = request.form.get('partner', '').strip() if request.form.get('couple') == 'yes' else ''
        if request.form.get('couple') == 'yes' and (not partner or partner == name or len(partner) > 40):
            raise ValueError('請填寫伴侶的完整報名姓名，不能填自己，最多 40 字。')
        return partner

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
        local = current_deadline().astimezone(timezone(timedelta(hours=8)))
        return dict(csrf_token=session['csrf'], deadline_label=local.strftime('%Y/%m/%d %H:%M'),
                    deadline_zone='台灣時間 UTC+08:00', deadline_input=local.strftime('%Y-%m-%dT%H:%M'))

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
            partner = partner_field(name)
            person = secrets.token_urlsafe(18)
            connection = db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if closed():
                    raise ValueError('報名已截止或已配對，無法新增參加者。')
                connection.execute('INSERT INTO people (id,name,gender,password,title,link,note,image,partner) VALUES (?,?,?,?,?,?,?,?,?)',
                                   (person, name, fields[0], generate_password_hash(password), *fields[1:], partner))
                bump_revision()
            admin_session = session.get('admin')
            session.clear()
            if admin_session:
                session['admin'] = True
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
            admin_session = session.get('admin')
            session.clear()
            if admin_session:
                session['admin'] = True
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
            partner = partner_field(person['name'])
            connection = db()
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if closed():
                    raise ValueError('已截止或完成配對，禮物資料已鎖定。')
                connection.execute('UPDATE people SET gender=?,title=?,link=?,note=?,image=?,partner=? WHERE id=?', (*fields, partner, person['id']))
                bump_revision()
            flash('願望已更新！', 'success')
        except ValueError as error:
            flash(str(error), 'error')
        return redirect(url_for('dashboard'))

    @app.get('/image/<token>')
    def gift_image(token):
        person = me()
        if not person:
            abort(403)
        if token == 'mine' and person:
            row = person
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
                participant = session.get('person')
                session.clear()
                if participant:
                    session['person'] = participant
                session['admin'] = True
                return redirect(url_for('admin'))
            flash('負責人密碼不正確。', 'error')
        people = db().execute('SELECT id,name,gender,partner FROM people ORDER BY rowid').fetchall() if session.get('admin') else []
        return render_template('admin.html', people=people, drawn=drawn(), revision=revision())

    @app.get('/admin/results')
    def admin_results():
        admin_required()
        results = db().execute('''SELECT giver.name AS giver, recipient.name AS recipient,
            recipient.title AS title, recipient.link AS link
            FROM pairs JOIN people AS giver ON giver.id=pairs.giver_id
            JOIN people AS recipient ON recipient.id=pairs.recipient_id
            ORDER BY giver.rowid''').fetchall()
        pending = db().execute('''SELECT p.name FROM people p LEFT JOIN pairs x ON x.giver_id=p.id
            WHERE x.giver_id IS NULL ORDER BY p.rowid''').fetchall()
        return render_template('admin_results.html', results=results, pending=pending)

    @app.post('/admin/deadline')
    def set_deadline():
        admin_required()
        try:
            value = datetime.fromisoformat(request.form.get('deadline', ''))
            if value.tzinfo is not None:
                raise ValueError()
            value = value.replace(tzinfo=timezone(timedelta(hours=8)))
            with db():
                db().execute("UPDATE settings SET value=? WHERE key='deadline'", (value.isoformat(),))
            flash('截止時間已更新（台灣時間）。已配對的名單仍會鎖定。', 'success')
        except ValueError:
            flash('請填寫有效的日期與時間。', 'error')
        return redirect(url_for('admin'))

    @app.post('/admin/remove/<person_id>')
    def remove_person(person_id):
        admin_required()
        connection = db()
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            if request.form.get('revision') != revision():
                flash('名單已更新，請重新確認後操作。', 'error')
            else:
                person = connection.execute('SELECT name FROM people WHERE id=?', (person_id,)).fetchone()
                if person:
                    connection.execute('DELETE FROM choices')
                    connection.execute('DELETE FROM pairs')
                    connection.execute('DELETE FROM people WHERE id=?', (person_id,))
                    connection.execute("UPDATE people SET partner='' WHERE partner=?", (person['name'],))
                    bump_revision()
                    flash('已移除參加者。原配對已取消，請確認名單後重新配對，並通知大家重新查看。', 'success')
        return redirect(url_for('admin'))

    @app.post('/admin/participant/<person_id>')
    def edit_participant(person_id):
        admin_required()
        connection = db()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if request.form.get('revision') != revision():
                    raise ValueError('名單已更新，請重新確認。')
                if drawn():
                    raise ValueError('配對後無法修改性別與情侶資料。')
                person = connection.execute('SELECT name FROM people WHERE id=?', (person_id,)).fetchone()
                if not person:
                    abort(404)
                partner = partner_field(person['name'])
                gender = request.form.get('gender', '').strip()
                if not gender or len(gender) > 40:
                    raise ValueError('請填有效性別，女生請填「女」。')
                connection.execute('UPDATE people SET gender=?,partner=? WHERE id=?', (gender, partner, person_id))
                bump_revision()
            flash('參加者資料已更新，禮物仍保持隱藏。', 'success')
        except ValueError as error:
            flash(str(error), 'error')
        return redirect(url_for('admin'))

    @app.post('/admin/draw')
    def draw():
        admin_required()
        connection = db()
        try:
            with connection:
                connection.execute('BEGIN IMMEDIATE')
                if request.form.get('revision') != revision():
                    raise ValueError('名單或配對已更新，請重新確認後再抽籤。')
                people = connection.execute('SELECT id,name,gender,partner FROM people').fetchall()
                ids = [row['id'] for row in people]
                if len(ids) < 4:
                    raise ValueError('至少需要 4 人，才能排除自己的禮物並提供 3 個選項。')
                rng = secrets.SystemRandom()
                previous = dict(connection.execute('SELECT giver,recipient FROM pairs').fetchall())
                if previous and request.form.get('redraw') != 'yes':
                    raise ValueError('已完成配對，請使用重新抽籤。')
                matches = match_people(people, previous)
                connection.execute('DELETE FROM choices')
                connection.execute('DELETE FROM pairs')
                for giver, recipient in matches.items():
                    connection.execute('INSERT INTO pairs VALUES (?,?)', (giver, recipient))
                    options = [recipient] + rng.sample([i for i in ids if i not in (giver, recipient)], 2)
                    rng.shuffle(options)
                    for position, owner in enumerate(options):
                        connection.execute('INSERT INTO choices VALUES (?,?,?,?)', (secrets.token_urlsafe(18), giver, owner, position))
                bump_revision()
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
