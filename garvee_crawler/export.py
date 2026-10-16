import csv
import json
from decimal import Decimal
from pathlib import Path


def money(value):
    return "" if value is None else format(Decimal(str(value)) / 100, '.2f')


def image_url(value):
    if isinstance(value, dict):
        value = value.get('src', '')
    return 'https:' + value if value and value.startswith('//') else value or ''


def product_rows(p):
    images = p.get('images', [])
    variants = p['variants']
    for i in range(max(len(variants), len(images))):
        row = {'URL handle': p['handle']}
        if i == 0:
            row.update({'Title': p['title'], 'Description': p.get('description', ''),
                        'Vendor': p.get('vendor', ''), 'Type': p.get('type', ''),
                        'Tags': ', '.join(p.get('tags', [])),
                        'Status': 'Draft', 'Published on online store': 'FALSE'})
        if i < len(variants):
            v = variants[i]
            compare = v.get('compare_at_price')
            valid_compare = compare is not None and Decimal(str(compare)) > Decimal(str(v['price']))
            row.update({'SKU': v.get('sku', ''), 'Variant Barcodes': v.get('barcode') or '',
                        'Price': money(v.get('price')), 'Compare-at price': money(compare) if valid_compare else '',
                        'Weight value (grams)': v.get('weight', ''), 'Weight unit for display': 'g',
                        'Requires shipping': str(v.get('requires_shipping', True)).upper(),
                        'Variant image URL': image_url(v.get('featured_image'))})
            # Destination policy: unavailable source variants must not be sold.
            # Availability is not an exact count for variants still available.
            if v.get('available') is False:
                row.update({'Inventory tracker': 'shopify', 'Inventory quantity': 0,
                            'Continue selling when out of stock': 'deny',
                            'Fulfillment service': 'manual'})
            for n, option in enumerate(p.get('options', [])[:3], 1):
                row[f'Option{n} name'] = option.get('name', '') if isinstance(option, dict) else option
                row[f'Option{n} value'] = v.get(f'option{n}') or ''
        if i < len(images):
            row.update({'Product image URL': image_url(images[i]), 'Image position': i + 1})
        yield row


def selected_products(db):
    return list(db.execute('''SELECT DISTINCT p.url,p.fetched_at,p.payload FROM products p
        JOIN run_urls r ON p.url=r.url OR p.url IN
        (SELECT canonical FROM aliases WHERE url=r.url) ORDER BY p.url'''))


def export(db, template, destination, current_run=False):
    with open(template, encoding='utf-8-sig', newline='') as f:
        fields = next(csv.reader(f))
    required = {'Title', 'URL handle', 'SKU', 'Price', 'Product image URL'}
    if not required.issubset(fields):
        raise ValueError('Template thiếu các cột bắt buộc')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.with_suffix('.tmp').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        records = ((r[2],) for r in selected_products(db)) if current_run else db.execute('SELECT payload FROM products ORDER BY url')
        for (payload,) in records:
            for row in product_rows(json.loads(payload)):
                writer.writerow(row)
                count += 1
    destination.with_suffix('.tmp').replace(destination)
    return count
