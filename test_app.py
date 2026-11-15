import io
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image
from app import create_app


class GiftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(TESTING=True, SECRET_KEY='test-key', ADMIN_PASSWORD='admin-test-password',
                           INVITE_CODE='friends', DATABASE=str(Path(self.temp.name) / 'test.db'),
                           DEADLINE='2099-11-17T23:59:00+08:00')
        self.app = create_app(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def post(self, client, path, data=None):
        client.get('/')
        with client.session_transaction() as session:
            token = session['csrf']
        return client.post(path, data={'csrf': token, **(data or {})}, follow_redirects=True)

    def join(self, number, image=False):
        client = self.app.test_client()
        data = dict(name=f'朋友{number}', gender='不透露', password='my-password', invite='friends',
                    title=f'禮物{number}', link=f'https://example.com/gift{number}', note='黑色')
        if image:
            output = io.BytesIO()
            Image.new('RGB', (20, 20), 'red').save(output, 'PNG')
            output.seek(0)
            data['image'] = (output, 'gift.png')
        result = self.post(client, '/join', data)
        self.assertIn('願望收到了'.encode(), result.data)
        return client

    def admin(self):
        client = self.app.test_client()
        self.post(client, '/admin', {'password': 'admin-test-password'})
        return client

    def rows(self, sql):
        with closing(sqlite3.connect(self.config['DATABASE'])) as connection:
            return connection.execute(sql).fetchall()

    def test_draw_invariants_private_images_and_persistence(self):
        clients = [self.join(i, image=True) for i in range(10)]
        admin = self.admin()
        self.post(admin, '/admin/draw')
        pairs = self.rows('SELECT giver,recipient FROM pairs')
        self.assertEqual(len(pairs), 10)
        self.assertEqual(len({p[1] for p in pairs}), 10)
        for giver, recipient in pairs:
            self.assertNotEqual(giver, recipient)
            options = self.rows(f"SELECT owner FROM choices WHERE giver='{giver}'")
            owners = [r[0] for r in options]
            self.assertEqual(len(set(owners)), 3)
            self.assertIn(recipient, owners)
            self.assertNotIn(giver, owners)
        before = clients[0].get('/me').data
        self.post(admin, '/admin/draw')
        self.assertEqual(pairs, self.rows('SELECT giver,recipient FROM pairs'))
        self.assertEqual(before, clients[0].get('/me').data)
        with clients[0].session_transaction() as session:
            person = session['person']
        allowed = self.rows(f"SELECT id FROM choices WHERE giver='{person}'")[0][0]
        forbidden = self.rows(f"SELECT id FROM choices WHERE giver!='{person}'")[0][0]
        self.assertEqual(clients[0].get('/image/' + allowed).status_code, 200)
        self.assertEqual(clients[0].get('/image/' + forbidden).status_code, 404)
        self.assertEqual(self.app.test_client().get('/image/' + allowed).status_code, 403)
        new_app = create_app(self.config)
        new_client = new_app.test_client()
        self.post(new_client, '/login', dict(name='朋友0', password='my-password'))
        self.assertIn('哪一份'.encode(), new_client.get('/me').data)
        # No owner identifiers or correctness markers are serialized in the choice cards.
        for _, recipient in pairs:
            self.assertNotIn(recipient.encode(), before)

    def test_locks_and_minimum(self):
        clients = [self.join(i) for i in range(3)]
        admin = self.admin()
        self.assertIn('至少需要 4 人'.encode(), self.post(admin, '/admin/draw').data)
        self.join(3)
        self.post(admin, '/admin/draw')
        self.assertIn('已鎖定'.encode(), self.post(clients[0], '/gift', dict(gender='男', title='changed', link='https://example.com', note='')).data)
        data = dict(name='late', gender='女', password='my-password', invite='friends', title='Late', link='https://example.com')
        self.assertIn('報名已截止'.encode(), self.post(self.app.test_client(), '/join', data).data)

    def test_validation_csrf_auth_deadline(self):
        client = self.app.test_client()
        self.assertEqual(client.post('/join').status_code, 400)
        self.assertEqual(self.post(client, '/admin/draw').status_code, 403)
        data = dict(name='Test', gender='男', password='my-password', invite='friends', title='gift', link='javascript:alert(1)')
        self.assertIn('完整的'.encode(), self.post(client, '/join', data).data)
        data['link'] = 'https://example.com'
        data['image'] = (io.BytesIO(b'not an image'), 'fake.png')
        self.assertIn('無法讀取圖片'.encode(), self.post(client, '/join', data).data)
        past = create_app({**self.config, 'DEADLINE': '2000-01-01T00:00:00+08:00'}).test_client()
        data.pop('image')
        self.assertIn('報名已截止'.encode(), self.post(past, '/join', data).data)

    def test_simultaneous_draw_is_atomic(self):
        for i in range(4):
            self.join(i)
        admins = [self.admin(), self.admin()]
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda c: self.post(c, '/admin/draw'), admins))
        self.assertTrue(all(r.status_code == 200 for r in results))
        self.assertEqual(len(self.rows('SELECT * FROM pairs')), 4)
        self.assertEqual(len(self.rows('SELECT * FROM choices')), 12)


if __name__ == '__main__':
    unittest.main()
