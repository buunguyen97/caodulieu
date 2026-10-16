import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from garvee_crawler.collector import connect
from garvee_crawler.web import Dashboard, make_handler


def test_dashboard_validation(tmp_path):
    app = Dashboard(tmp_path)
    with pytest.raises(ValueError):
        app.start({'limit': -1})
    with pytest.raises(ValueError):
        app.start({'urls': 'https://example.com/products/a'})
    app.state['running'] = True
    with pytest.raises(ValueError):
        app.start({})


def test_http_catalog_and_write_guard(tmp_path):
    app = Dashboard(tmp_path)
    db = connect(tmp_path / 'catalog.sqlite')
    p = {'title': 'Test chair', 'variants': [{'price': 12345, 'sku': '001', 'available': True}], 'images': []}
    db.execute('INSERT INTO products VALUES (?,?,?)', ('https://www.garvee.com/products/test', 'now', json.dumps(p)))
    db.commit()
    db.close()
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}') as client:
            assert client.get('/api/status').json()['products'] == 1
            assert client.get('/api/products?q=001').json()['items'][0]['price'] == 123.45
            assert client.get('/api/products?q=missing').json()['total'] == 0
            assert client.post('/api/start', json={}).status_code == 403
            assert client.get('/api/status', headers={'Host': 'evil.example'}).status_code == 403
            assert client.post('/api/start', json={'limit': -1}, headers={'X-Dashboard': '1'}).status_code == 400
    finally:
        server.shutdown()
        server.server_close()
