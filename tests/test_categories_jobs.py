import asyncio
import json
from types import SimpleNamespace

import pytest

from garvee_crawler.categories import collection_url, collection_payload, discover_collection
from garvee_crawler.jobs import OutputLock, begin_job, finish_job, list_jobs


def html(handles, next_page=False, cursor='cursor', handle='tennis'):
    payload = {'products':[{'handle':h} for h in handles], 'collectionHandle':handle,
               'pageInfo':{'hasNextPage':next_page,'endCursor':cursor}}
    stream = 'a:["$","Component",null,'+json.dumps(payload)+']\n'
    mid = len(stream)//2
    return ''.join('<script>self.__next_f.push('+json.dumps([1,c])+')</script>' for c in (stream[:mid],stream[mid:]))


def test_collection_parser_ignores_recommendations():
    document = html(['one','two']) + '<a href="/products/recommendation">Recommendation</a>'
    assert [p['handle'] for p in collection_payload(document,'tennis')['products']] == ['one','two']
    with pytest.raises(ValueError):
        collection_payload(document, 'missing')


def test_collection_cursor_and_dedup():
    class FakeSource:
        calls = []
        async def get(self, url):
            self.calls.append(url)
            return SimpleNamespace(text=html(['a','b'],True) if len(self.calls)==1 else html(['b','c'],cursor='last'))
    source = FakeSource()
    result = asyncio.run(discover_collection(source,'https://www.garvee.com/collections/tennis?filter.v.availability=true'))
    assert len(result)==3
    assert 'after=cursor' in source.calls[1]
    assert 'filter.v.availability=true' in source.calls[1]


def test_repeated_cursor_fails():
    class FakeSource:
        async def get(self, url, **kwargs):
            if kwargs.get('method') == 'POST':
                return SimpleNamespace(text='1:'+json.dumps({'products':[{'handle':'a'}], 'pageInfo':{'hasNextPage':True,'endCursor':'cursor'}}))
            return SimpleNamespace(text=html(['a'],True))
    with pytest.raises(ValueError, match='lặp'):
        asyncio.run(discover_collection(FakeSource(),'https://www.garvee.com/collections/tennis'))


def test_stale_html_uses_public_load_more():
    class FakeSource:
        posted = False
        async def get(self, url, **kwargs):
            if kwargs.get('method') == 'POST':
                self.posted = True
                args=json.loads(kwargs['content'])[0]
                assert args['handle']=='tennis' and args['cursor']=='cursor'
                return SimpleNamespace(text='1:'+json.dumps({'products':[{'handle':'b'}], 'pageInfo':{'hasNextPage':False,'endCursor':'last'}}))
            return SimpleNamespace(text=html(['a'],True))
    source=FakeSource()
    assert len(asyncio.run(discover_collection(source,'https://www.garvee.com/collections/tennis')))==2
    assert source.posted


def test_collection_url_validation():
    with pytest.raises(ValueError):
        collection_url('https://example.com/collections/tennis')
    with pytest.raises(ValueError):
        collection_url('https://www.garvee.com/products/item')
    assert collection_url('https://garvee.com/collections/tennis?page=3&after=abc') == 'https://www.garvee.com/collections/tennis'


def test_writer_lock_released(tmp_path):
    with OutputLock(tmp_path):
        with pytest.raises(ValueError, match='tác vụ khác'):
            with OutputLock(tmp_path):
                pass
    with OutputLock(tmp_path):
        pass


def test_cli_cannot_write_while_dashboard_holds_lock(tmp_path):
    import subprocess
    import sys
    with OutputLock(tmp_path):
        result = subprocess.run([sys.executable,'-m','garvee_crawler.cli','--output',str(tmp_path),'--export-only'],
                                capture_output=True, timeout=20)
    assert result.returncode != 0
    assert not (tmp_path/'catalog.sqlite').exists()


def test_job_csv_snapshot_is_immutable(tmp_path):
    job = begin_job(tmp_path,'Tennis','https://www.garvee.com/collections/tennis')
    (tmp_path/'products.csv').write_text('first',encoding='utf-8')
    finish_job(tmp_path,job,'completed',{'products':1,'csv_rows':7,'failed_products':0})
    (tmp_path/'products.csv').write_text('second',encoding='utf-8')
    assert (tmp_path/'runs'/job['id']/'products.csv').read_text()=='first'
    assert list_jobs(tmp_path)[0]['downloadable']


def test_delete_history_preserves_catalog_and_other_runs(tmp_path):
    from garvee_crawler.jobs import delete_job
    first = begin_job(tmp_path, 'First', 'source')
    second = begin_job(tmp_path, 'Second', 'source')
    (tmp_path/'products.csv').write_text('latest')
    (tmp_path/'catalog.sqlite').write_text('catalog')
    delete_job(tmp_path, first['id'])
    assert [j['id'] for j in list_jobs(tmp_path)] == [second['id']]
    assert (tmp_path/'products.csv').read_text() == 'latest'
    assert (tmp_path/'catalog.sqlite').read_text() == 'catalog'
    with pytest.raises(ValueError):
        delete_job(tmp_path, '../products.csv')
    with OutputLock(tmp_path):
        with pytest.raises(ValueError):
            delete_job(tmp_path, second['id'])
    assert (tmp_path/'runs'/second['id']/'job.json').exists()
