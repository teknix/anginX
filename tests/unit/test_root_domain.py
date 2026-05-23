from app import extract_root_domain


def test_two_label():
    root, slug = extract_root_domain('app.1.com')
    assert root == '1.com'
    assert slug == '1com'


def test_subdomain():
    root, slug = extract_root_domain('sub.app.1.com')
    assert root == '1.com'
    assert slug == '1com'


def test_deep_subdomain():
    root, slug = extract_root_domain('a.b.c.example.com')
    assert root == 'example.com'
    assert slug == 'examplecom'


def test_slug_strips_dot():
    _, slug = extract_root_domain('x.co.uk')
    assert '.' not in slug
    assert slug == 'couk'


def test_numeric_tld():
    root, slug = extract_root_domain('app.1.com')
    assert slug == '1com'
