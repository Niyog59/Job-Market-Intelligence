"""Display-safe listing data: sanitized descriptions, links, and freshness."""

import pytest
from gh_fakes import T0, FakeClock, board, gh_job, registry
from test_history import History
from test_listing_storage import long_job

from jobmarket import cli, db
from jobmarket.listings import listing_detail, plain_text, safe_description_html, safe_link
from jobmarket.migrate import apply_migrations
from jobmarket.sources import sync_registry

# --- display-safe HTML (no database) ---------------------------------------------------------


def test_structure_is_kept_and_markup_is_rebuilt_from_an_allowlist():
    markup = (
        '<h1 class="x">About</h1><div style="color:red"><p onclick="steal()">We <b>build</b> '
        "<i>things</i>.</p></div><ul><li>SQL</li><li>Python</li></ul><h5>Benefits</h5>"
    )
    assert safe_description_html(markup) == (
        "<h2>About</h2><p>We <strong>build</strong> <em>things</em>.</p>"
        "<ul><li>SQL</li><li>Python</li></ul><h4>Benefits</h4>"
    )


@pytest.mark.parametrize(
    "markup",
    [
        "<script>alert(1)</script>",
        "<style>p{}</style>",
        '<iframe src="https://evil.example"></iframe>',
        '<img src="x" onerror="alert(1)">',
        "<form><input name=a><button>Go</button></form>",
        "<svg><script>alert(1)</script></svg>",
    ],
)
def test_active_content_is_removed_with_its_content(markup):
    assert safe_description_html(f"<p>Hello</p>{markup}<p>World</p>") == "<p>Hello</p><p>World</p>"


def test_only_https_links_survive_and_open_safely():
    out = safe_description_html(
        '<a href="https://acme.example/benefits" target="_self" onclick="x()">Benefits</a> '
        '<a href="javascript:alert(1)">Click</a> <a href="http://acme.example">Old</a> '
        '<a href="mailto:jobs@acme.example">Mail</a>'
    )
    assert out == (
        '<a href="https://acme.example/benefits" rel="nofollow noopener noreferrer" '
        'target="_blank">Benefits</a> Click Old Mail'
    )
    assert safe_link("https://acme.example/a b") is None
    assert safe_link(" https://acme.example/ok ") == "https://acme.example/ok"


def test_text_is_escaped_and_unbalanced_tags_are_closed():
    out = safe_description_html("<p>5 &lt; 7 &amp; <strong>bold <em>both</p>tail")
    assert out == "<p>5 &lt; 7 &amp; <strong>bold <em>both</em></strong></p>tail"
    assert "&lt;script&gt;" in safe_description_html("&lt;script&gt;alert(1)&lt;/script&gt;")


def test_block_boundaries_keep_words_apart():
    assert safe_description_html("<div>One</div><div>Two</div>") == "One<br>Two"
    text = plain_text(safe_description_html("<h2>Role</h2><p>Do work.</p><ul><li>SQL</li></ul>"))
    assert text == "Role\n\nDo work.\n\n  - SQL"


# --- listing detail ---------------------------------------------------------------------------


@pytest.fixture
def h(schema_conn):
    apply_migrations(schema_conn)
    sync_registry(schema_conn, registry(board("acme")), FakeClock()())
    return History(schema_conn)


@pytest.mark.db
def test_listing_detail_has_safe_text_links_and_freshness(h):
    job = gh_job(1, content='<p>Use <a href="javascript:x()">SQL</a>.</p><script>x()</script>')
    h.collect(T0, [job])
    posting = h.scalar("SELECT posting_id FROM source_postings")
    detail = listing_detail(h.conn, posting)
    assert detail.description.status == "available"
    assert detail.description.safe_html == "<p>Use SQL.</p>"
    assert detail.posting_url == "https://job-boards.greenhouse.io/acme/jobs/1"
    assert detail.availability_state == "observed"
    assert detail.first_discovered_at == T0 and detail.content_seen_at == T0
    assert "Greenhouse" in detail.description.source_note
    assert listing_detail(h.conn, posting + 99) is None


@pytest.mark.db
def test_listing_detail_for_an_omitted_description(h):
    h.collect(T0, [long_job(1)])
    detail = listing_detail(h.conn, h.scalar("SELECT posting_id FROM source_postings"))
    assert (detail.description.status, detail.description.safe_html) == ("omitted_too_long", "")


@pytest.mark.db
def test_cli_listing(h, monkeypatch, isolated_schema, capsys):
    real_connect = db.connect
    monkeypatch.setattr(
        db,
        "connect",
        lambda s, autocommit=False, options=None: real_connect(
            s, autocommit=autocommit, options=f"-c search_path={isolated_schema}"
        ),
    )
    h.collect(T0, [gh_job(1, content="<h2>Role</h2><ul><li>Build pipelines</li></ul>")])
    posting = h.scalar("SELECT posting_id FROM source_postings")
    assert cli.main(["listing", str(posting)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Role" in out and "  - Build pipelines" in out and "posting page: https://" in out
