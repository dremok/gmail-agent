import pytest

from gmail_agent.mime import html_to_text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        (
            "<table><tr><td>Total</td><td>120 EUR</td></tr>"
            "<tr><th>Due</th><td>2026-11-01</td></tr></table>",
            "Total 120 EUR\nDue 2026-11-01",
        ),
        ("a<br>b<br/>c<br />d</br>e", "a\nb\nc\nd\ne"),
        ("<p>a</p><hr/><title/><p>b</p>", "a\n\nb"),
        ("<p>One</p><blockquote>Quoted</blockquote>After", "One\n\nQuoted\nAfter"),
        ("<ul><li>first</li><li>second</li></ul>tail", "first\nsecond\ntail"),
        ("<div>A</div>\n   <div>B</div>", "A\nB"),
        ("<p>Hard\nwrapped   source</p>\n<p>Next</p>", "Hard wrapped source\n\nNext"),
        ("<pre>line 1\nline 2</pre>", "line 1\nline 2"),
        ("<span>a</span> <span>b</span>", "a b"),
        ("Hej&nbsp;där &amp; <b>du</b>", "Hej där & du"),
        ("<head><title>T</title><style>p{}</style></head><p>Body</p><script>x()</script>", "Body"),
        ("<p>Paid</p><br><br><br>Bye", "Paid\n\nBye"),
    ],
)
def test_html_to_text_lays_out_like_a_browser(html, text):
    assert html_to_text(html) == text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        (
            '<a href="https://example.com/inv/12">Your invoice</a>',
            "Your invoice (https://example.com/inv/12)",
        ),
        ('<a href="https://x.test/a?b=1&amp;c=2">Go</a>', "Go (https://x.test/a?b=1&c=2)"),
        ('<a href="https://example.com/">https://example.com</a>', "https://example.com"),
        ('<a href="http://www.example.com">www.example.com</a>', "www.example.com"),
        ('<a href="mailto:Bob@Example.com">bob@example.com</a>', "bob@example.com"),
        (
            '<a href="mailto:bob@example.com?subject=Hi">Mail Bob</a>',
            "Mail Bob (mailto:bob@example.com?subject=Hi)",
        ),
        ('<a href="#top">Back to top</a>', "Back to top"),
        ("<a>No target</a>", "No target"),
        ('<a href="https://x.test"><div>Button</div></a>after', "Button (https://x.test)\nafter"),
        ('<a href="https://x.test/home"><img src="logo.png"></a>', "(https://x.test/home)"),
        (
            '<a href="https://a.test">one<a href="https://b.test">two</a>',
            "one (https://a.test)two (https://b.test)",
        ),
        ('<p>See <a href="https://x.test/t">terms</a>.</p>', "See terms (https://x.test/t)."),
        ('Unclosed <a href="https://x.test/u">link', "Unclosed link (https://x.test/u)"),
    ],
)
def test_links_keep_their_url(html, text):
    assert html_to_text(html) == text
