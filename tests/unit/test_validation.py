import pytest
from app import validate_domain, validate_name, validate_port, validate_path, ValidationError


class TestValidateDomain:
    def test_valid_two_label(self):
        validate_domain('app.1.com')

    def test_valid_subdomain(self):
        validate_domain('sub.app.1.com')

    def test_rejects_single_label(self):
        with pytest.raises(ValidationError, match='fully qualified'):
            validate_domain('myapp')

    def test_rejects_bare_localhost(self):
        with pytest.raises(ValidationError, match='fully qualified'):
            validate_domain('localhost')

    def test_rejects_empty(self):
        with pytest.raises(ValidationError, match='required'):
            validate_domain('')

    def test_rejects_none(self):
        with pytest.raises(ValidationError, match='required'):
            validate_domain(None)

    def test_accepts_uppercase_by_normalizing(self):
        # uppercase is normalized to lowercase, not rejected
        validate_domain('App.1.com')

    def test_rejects_injection_attempt(self):
        # newline injection
        with pytest.raises(ValidationError):
            validate_domain('app.1.com\nserver_name evil.com')

    def test_rejects_semicolon(self):
        with pytest.raises(ValidationError):
            validate_domain('app;evil.com')

    def test_rejects_space(self):
        with pytest.raises(ValidationError):
            validate_domain('app .1.com')

    def test_accepts_hyphens(self):
        validate_domain('my-app.1.com')

    def test_accepts_dots(self):
        validate_domain('a.b.c.d.com')


class TestValidateName:
    def test_valid(self):
        validate_name('myapp')

    def test_valid_with_hyphen(self):
        validate_name('my-app')

    def test_valid_with_underscore(self):
        validate_name('my_app')

    def test_rejects_empty(self):
        with pytest.raises(ValidationError, match='required'):
            validate_name('')

    def test_rejects_dots(self):
        with pytest.raises(ValidationError):
            validate_name('my.app')

    def test_accepts_uppercase_by_normalizing(self):
        # uppercase is normalized to lowercase, not rejected
        validate_name('MyApp')

    def test_rejects_starts_with_hyphen(self):
        with pytest.raises(ValidationError):
            validate_name('-myapp')

    def test_rejects_injection(self):
        with pytest.raises(ValidationError):
            validate_name('app; rm -rf /')


class TestValidatePort:
    def test_valid(self):
        assert validate_port(8080) == 8080

    def test_valid_min(self):
        assert validate_port(1) == 1

    def test_valid_max(self):
        assert validate_port(65535) == 65535

    def test_rejects_zero(self):
        with pytest.raises(ValidationError, match='1–65535'):
            validate_port(0)

    def test_rejects_too_high(self):
        with pytest.raises(ValidationError, match='1–65535'):
            validate_port(65536)

    def test_rejects_string(self):
        with pytest.raises(ValidationError, match='integer'):
            validate_port('abc')

    def test_rejects_none(self):
        with pytest.raises(ValidationError, match='integer'):
            validate_port(None)

    def test_accepts_string_int(self):
        assert validate_port('8080') == 8080


class TestValidatePath:
    def test_valid_root(self):
        validate_path('/')

    def test_valid_subpath(self):
        validate_path('/rtc')

    def test_valid_nested(self):
        validate_path('/api/v1/rtc')

    def test_rejects_empty(self):
        with pytest.raises(ValidationError, match='required'):
            validate_path('')

    def test_rejects_none(self):
        with pytest.raises(ValidationError):
            validate_path(None)

    def test_rejects_missing_leading_slash(self):
        with pytest.raises(ValidationError):
            validate_path('rtc')

    def test_rejects_traversal(self):
        with pytest.raises(ValidationError):
            validate_path('/../etc/passwd')

    def test_rejects_space(self):
        with pytest.raises(ValidationError):
            validate_path('/foo bar')

    def test_rejects_newline_injection(self):
        with pytest.raises(ValidationError):
            validate_path('/rtc\nlocation /evil { proxy_pass http://x; }')

    def test_rejects_too_long(self):
        with pytest.raises(ValidationError):
            validate_path('/' + 'a' * 300)
