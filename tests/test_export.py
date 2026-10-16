import csv
import json

import pytest

from garvee_crawler.collector import connect, product_url, validate
from garvee_crawler.export import export, product_rows


@pytest.fixture
def product():
    return {'title': 'Chair, "Blue"\nNew', 'handle': 'chair', 'options': [{'name': 'Color'}],
            'images': ['//cdn.example/a.jpg', '//cdn.example/b.jpg', '//cdn.example/c.jpg'],
            'variants': [{'sku': '001', 'price': 11999, 'compare_at_price': None, 'option1': 'Blue'},
                         {'sku': '002', 'price': 0, 'option1': 'Red'}]}


def test_variants_and_extra_images(product):
    rows = list(product_rows(product))
    assert len(rows) == 3
    assert rows[0]['Price'] == '119.99'
    assert rows[0]['Compare-at price'] == ''
    assert rows[1]['Price'] == '0.00'
    assert rows[1]['Option1 value'] == 'Red'
    assert 'SKU' not in rows[2]
    assert rows[2]['Image position'] == 3
    assert 'Inventory quantity' not in rows[0]


def test_csv_roundtrip(tmp_path, product):
    db = connect(tmp_path / 'db.sqlite')
    db.execute('INSERT INTO products VALUES (?,?,?)', ('url','now',json.dumps(product)))
    target = tmp_path / 'products.csv'
    assert export(db, 'templates/product_template.csv', target) == 3
    with target.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    assert rows[0]['Title'] == product['title']
    assert rows[0]['SKU'] == '001'
    assert rows[0]['Cost per item'] == ''
    db.close()


def test_urls():
    assert product_url('https://garvee.com/collections/a/products/foo?variant=2') == 'https://www.garvee.com/products/foo'
    with pytest.raises(ValueError):
        product_url('https://example.com/products/foo')


def test_incomplete_payload():
    with pytest.raises(ValueError):
        validate({'title': 'Missing variants'})


def test_export_current_run_excludes_history_and_resolves_alias(tmp_path, product):
    db = connect(tmp_path / 'catalog.sqlite')
    db.execute('CREATE TABLE run_urls (url TEXT PRIMARY KEY)')
    db.execute('INSERT INTO products VALUES (?,?,?)', ('https://www.garvee.com/products/chair','now',json.dumps(product)))
    old = dict(product, handle='old-product', title='History')
    db.execute('INSERT INTO products VALUES (?,?,?)', ('https://www.garvee.com/products/history','before',json.dumps(old)))
    db.executemany('INSERT INTO run_urls VALUES (?)', [('https://www.garvee.com/products/old-chair',),('https://www.garvee.com/products/chair',)])
    db.execute('INSERT INTO aliases VALUES (?,?)', ('https://www.garvee.com/products/old-chair','https://www.garvee.com/products/chair'))
    target = tmp_path / 'current.csv'
    assert export(db, 'templates/product_template.csv', target, current_run=True) == 3
    with target.open(encoding='utf-8-sig', newline='') as f:
        assert {r['URL handle'] for r in csv.DictReader(f)} == {'chair'}
    assert export(db, 'templates/product_template.csv', tmp_path / 'all.csv') == 6
    db.execute('DELETE FROM run_urls')
    assert export(db, 'templates/product_template.csv', target, current_run=True) == 0
    db.close()


@pytest.mark.parametrize('compare,expected', [(None,''),(0,''),(11999,''),(10000,''),(15000,'150.00')])
def test_compare_price_normalization(product, compare, expected):
    product['variants'][0]['compare_at_price'] = compare
    assert next(product_rows(product))['Compare-at price'] == expected


def test_unavailable_inventory_policy(product):
    product['variants'][0]['available'] = False
    product['variants'][1]['available'] = True
    rows = list(product_rows(product))
    assert rows[0]['Inventory quantity'] == 0
    assert rows[0]['Inventory tracker'] == 'shopify'
    assert rows[0]['Continue selling when out of stock'] == 'deny'
    assert rows[0]['Fulfillment service'] == 'manual'
    assert 'Inventory quantity' not in rows[1]
    assert 'Inventory tracker' not in rows[2]


def test_redirect_resolution(product):
    import asyncio
    import httpx
    from garvee_crawler.collector import Source, BASE

    async def scenario():
        source = Source(delay=0)
        await source.client.aclose()
        def respond(request):
            if request.url.path == '/products/old.js':
                return httpx.Response(404)
            if request.url.path == '/products/old':
                return httpx.Response(301, headers={'location': '/api/internal/redirect-301?to=https%3A%2F%2Fwww.garvee.com%2Fproducts%2Fchair'})
            return httpx.Response(200, json=product)
        source.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            result = await source.fetch(BASE + '/products/old')
            assert result['_resolved_url'] == BASE + '/products/chair'
            assert result['handle'] == 'chair'
        finally:
            await source.client.aclose()
    asyncio.run(scenario())
