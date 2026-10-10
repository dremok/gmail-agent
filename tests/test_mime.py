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
        ("a<br>b<br/>c<br />d", "a\nb\nc\nd"),
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
