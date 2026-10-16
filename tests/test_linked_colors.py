from garvee_crawler.collector import linked_product_urls, BASE


def test_only_same_swatch_group_and_deduplication():
    group = '''<div><a href="/products/beige" title="Beige"><img alt="Beige"></a>
    <a href="/products/black" title="Black"><img alt="Black"></a></div>'''
    unrelated = '<div><a href="/products/other" title="Other"><img alt="Other"></a></div>'
    assert linked_product_urls(group + group + unrelated, BASE+'/products/beige') == [BASE+'/products/black']


def test_no_external_or_reviews():
    html = '''<div><a href="/products/beige" title="Beige"><img alt="Beige"></a>
    <a href="https://example.com/products/black" title="Black"><img alt="Black"></a>
    <a href="/products/beige/reviews" title="Reviews"><img alt="Reviews"></a></div>'''
    assert linked_product_urls(html, BASE+'/products/beige') == []
