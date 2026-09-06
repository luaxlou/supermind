import pytest

from supermind_memory import redaction
from supermind_memory.redaction import redact_text, redact_uri


def test_common_credentials_and_private_keys_are_replaced_with_one_safe_marker():
    secrets = (
        "AKIA" + ("A1B2" * 4),
        "sk-proj-" + ("Ab3d" * 10),
        "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx",
        "eyJabcdefghijk.abcdefghijklmnop.qrstuvwxyzABCDE",
        "-----BEGIN PRIVATE " + "KEY-----\nopaque-private-material\n-----END PRIVATE KEY-----",
    )
    redacted = redact_text("\n".join(secrets))

    assert all(secret not in redacted for secret in secrets)
    assert redacted.count("[REDACTED]") == len(secrets)


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ('{"password":"correct-horse-battery-staple"}', "correct-horse-battery-staple"),
        ("'client_secret': 'quoted secret with spaces'", "quoted secret with spaces"),
        ("password: 'it''s a yaml credential'", "yaml credential"),
        ('api_key: "escaped \\"quoted\\" secret"', 'quoted'),
        ("https://example.test/callback?access_token=oauth-secret-1234567890&state=ok", "oauth-secret-1234567890"),
        ("https://example.test/#/callback?client_secret=fragment-secret&state=ok", "fragment-secret"),
        ("https://example.test/?%61ccess_token=encoded%20credential", "credential"),
        ("https://account:uri-credential@example.test/source", "uri-credential"),
        ("project:client_secret=identifier-secret-1234567890", "identifier-secret-1234567890"),
        ("Authorization: Bearer short-credential", "short-credential"),
        ("'Authorization': Bearer short-credential", "short-credential"),
        ("Authorization: Basic YWNjb3VudDpwYXNz", "YWNjb3VudDpwYXNz"),
        ("Bearer standalone-credential", "standalone-credential"),
        ("Basic YWNjb3VudDpwYXNz", "YWNjb3VudDpwYXNz"),
        ('{"Authorization":"Bearer short-credential"}', "short-credential"),
        ("-----BEGIN RSA PRIVATE " + "KEY-----\nprivate material\n-----END RSA PRIVATE KEY-----", "private material"),
        ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nencrypted material\n-----END ENCRYPTED PRIVATE KEY-----", "encrypted material"),
        ("eyJabcdefghijk.abcdefghijklmnop.qrstuvwxyzABCDE", "eyJabcdefghijk"),
        ("AKIA" + ("A1B2" * 4), "AKIA" + ("A1B2" * 4)),
        ("ASIAA1B2A1B2A1B2A1B2", "ASIAA1B2A1B2A1B2A1B2"),
        ("ghp_" + ("A1b2" * 8), "ghp_A1b2"),
        ("N7vQ2mX9pL4sT8wZ1cR6yK3dF5hJ0uB2gE9a", "N7vQ2mX9"),
    ],
)
def test_structured_and_uri_secrets_are_redacted_idempotently(value, secret):
    for sanitize in (redact_text, redact_uri):
        once = sanitize(value)
        assert secret not in once
        assert "[REDACTED]" in once
        assert sanitize(once) == once


@pytest.mark.parametrize(
    "value",
    [
        "/" + "Users/fixture-user/AbC123XyZ789aBc456DeF012/README.md",
        "file:///" + "Users/fixture-user/AbC123XyZ789aBc456DeF012/README.md",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "req-01a06b67-c237-76f0-9948-c71dc956f2d0",
        "generation-20260906T043612492265Z-0375fde0e2d744c3910081946ba92b70",
    ],
)
def test_safe_paths_hashes_and_opaque_identifiers_remain_byte_stable(value):
    assert redact_text(value) == value
    assert redact_uri(value) == value
    assert redaction.redact_identifier(value) == value


def test_secret_identifier_identity_depends_only_on_sanitized_text():
    first = redaction.redact_identifier("project:client_secret=first-credential")
    second = redaction.redact_identifier("project:client_secret=second-credential")
    assert first == second
    assert first != "project:client_secret=first-credential"
    assert redaction.redact_identifier(first) == first


def test_authentication_contract_prose_is_not_treated_as_a_credential():
    value = "Returns a JWT bearer token. Basic authentication supports OAuth authorization code."
    assert redact_text(value) == value


@pytest.mark.parametrize("value", ["'Authorization': Bearer tiny", '"Authorization": Basic dTpw'])
def test_quoted_authorization_keys_redact_short_scheme_credentials(value):
    once = redact_text(value)
    assert "tiny" not in once and "dTpw" not in once
    assert redact_text(once) == once


@pytest.mark.parametrize("key", ("Authorization", "'Authorization'", '"Authorization"'))
@pytest.mark.parametrize(("scheme", "credential"), (("Bearer", "tiny"), ("Basic", "dTpw")))
@pytest.mark.parametrize(("before_scheme", "after_scheme"), (("\n  ", " "), (" ", "\r\n\t")))
def test_multiline_authorization_redacts_short_scheme_credentials(key, scheme, credential, before_scheme, after_scheme):
    value = f"{key}:{before_scheme}{scheme}{after_scheme}{credential}\nscope: public"
    once = redact_text(value)
    assert credential not in once
    assert "scope: public" in once
    assert redact_text(once) == once


@pytest.mark.parametrize("style", ("|", ">", "|-", ">+", "|2"))
def test_yaml_block_credentials_are_redacted_as_complete_scalars(style):
    value = f"settings:\n  'password': {style}\n    first block credential\n\n    second block credential\n  mode: safe\n"
    once = redact_text(value)
    assert "first block credential" not in once
    assert "second block credential" not in once
    assert "mode: safe" in once
    assert redact_text(once) == once


@pytest.mark.parametrize("style", ("|", ">"))
@pytest.mark.parametrize("properties", (
    "&credential", "!!str", "&credential !!str", "!!str &credential",
    "!secret", "!<tag:example.test,2026:secret>",
))
def test_yaml_block_scalar_properties_do_not_expose_credentials(style, properties):
    value = f"settings:\n  password: {properties} {style}\n    first property credential\n\n    second property credential\n  mode: safe\n"
    once = redact_text(value)
    assert "first property credential" not in once
    assert "second property credential" not in once
    assert "mode: safe" in once
    assert redact_text(once) == once


@pytest.mark.parametrize("style", ("|", ">"))
@pytest.mark.parametrize("properties", ("&credential\n!!str", "!!str\n&credential"))
@pytest.mark.parametrize(("prefix", "indent", "following"), (
    ("", "  ", "mode: safe\n"),
    ("settings:\n  ", "    ", "  mode: safe\nnext: safe\n"),
    ("items:\n  - ", "      ", "    mode: safe\n  - mode: next\n"),
))
def test_multiline_yaml_properties_respect_sensitive_block_boundaries(style, properties, prefix, indent, following):
    header = properties.replace("\n", "\n" + indent)
    value = (
        "Returns a JWT bearer token.\n"
        f"{prefix}password: {header} {style}\n"
        f"{indent}  first property credential\n{indent}  second property credential\n"
        + following
    )
    once = redact_text(value)
    assert once == "Returns a JWT bearer token.\n" + prefix + "password: [REDACTED]\n" + following
    assert redact_text(once) == once


@pytest.mark.parametrize("style", ("|", ">"))
@pytest.mark.parametrize("properties", (
    "&credential\n  !!str\n ", "!!str\n  &credential\n ",
    "&credential # anchor comment\n\n  !!str", "!!str\r\n  &credential",
    "\n  &credential\n  !!str", "\n  !!str\n  &credential",
))
def test_multiline_yaml_property_separators_preserve_siblings(style, properties):
    value = f"password: {properties} {style}\n    first property credential\n    second property credential\nmode: safe\n"
    once = redact_text(value)
    assert once == "password: [REDACTED]\nmode: safe\n"
    assert redact_text(once) == once


@pytest.mark.parametrize("following", (
    "  notes: |\n    public instructions\nnext: safe\n",
    "!!str |\n    public instructions\nnext: safe\n",
))
def test_incomplete_yaml_properties_do_not_consume_sibling_blocks(following):
    value = "settings:\n  password: &credential\n" + following
    assert following in redact_text(value)


@pytest.mark.parametrize("prefix", ("/", "file:///"))
def test_safe_absolute_path_spans_survive_dotted_ancestors(prefix):
    path = prefix + "Users/fixture-user/cache.v1/N7vQ2mX9pL4sT8wZ1cR6yK3dF5hJ0uB2gE9a/README.md"
    assert redact_text(path) == path
    assert redact_uri(path) == path
    assert redact_text(f"Source: {path} ready") == f"Source: {path} ready"
    assert redact_text(path + ' password="outside-credential"') == path + ' password="[REDACTED]"'


def test_final_yaml_scalar_consumption_preserves_boundaries(yaml_scalar_secret):
    from supermind_memory.redaction import redact_text

    payload, secrets = yaml_scalar_secret
    safe = "Before ordinary prose.\n" + payload + "path: /safe/project.py\nhash: " + "a" * 64 + "\n"
    result = redact_text(safe)
    assert all(secret not in result for secret in secrets)
    assert "scope: public\npath: /safe/project.py\nhash: " + "a" * 64 + "\n" in result
    assert result.startswith("Before ordinary prose.\n")
    if "mode: safe" in payload:
        assert "mode: safe" in result
    assert redact_text(result) == result


@pytest.mark.parametrize("ending", ("\n", "\r\n", "\r"))
@pytest.mark.parametrize("style", ("|", ">"))
@pytest.mark.parametrize("key_style", ("implicit", "explicit", "standalone"))
def test_final_yaml_block_line_endings_preserve_safe_siblings(ending, style, key_style):
    header = {
        "implicit": "password:",
        "explicit": f"? password{ending}:",
        "standalone": f"?{ending}  password{ending}:",
    }[key_style]
    value = ending.join((
        f"{header} {style}", "  first scalar material", "  second scalar material", "mode: safe", "",
    ))
    result = redact_text(value)
    assert result == f"{header} [REDACTED]{ending}mode: safe{ending}"
    assert redact_text(result) == result
