"""HTML pages of the settings page: the layout and the headers every page is sent with.

Pages are rendered on the server, with no scripts. Every value put into a page goes
through :func:`escape` first.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from starlette.responses import HTMLResponse

STYLE = """
  :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
  body { max-width: 56rem; margin: 3rem auto; padding: 0 1rem; line-height: 1.5; }
  .narrow { max-width: 28rem; }
  .error { color: #c62828; font-weight: 600; }
  .notice { padding: .5rem .75rem; border-left: 4px solid #f9a825; }
  .hint { font-size: .875rem; opacity: .75; }
  label { display: block; margin-top: .75rem; font-weight: 600; }
  label.inline { display: inline; font-weight: normal; }
  input[type=text] { width: 100%; padding: .5rem; font-size: 1rem; box-sizing: border-box; }
  .actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: 1rem; }
  td form { display: inline; }
  button { padding: .375rem .75rem; font-size: 1rem; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: .375rem .5rem; border-bottom: 1px solid #8884;
    vertical-align: top; }
  nav { display: flex; gap: 1rem; align-items: baseline; }
  nav form { margin-left: auto; }
"""


def escape(text: object) -> str:
    """``text`` made safe for HTML content and quoted attributes."""
    return html.escape(str(text), quote=True)


def layout(title: str, body: str, *, narrow: bool = False) -> str:
    """A whole page around ``body``, which is trusted HTML: escape what goes into it."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{escape(title)}</title>
<style>{STYLE}</style>
</head>
<body{' class="narrow"' if narrow else ""}>
{body}
</body>
</html>"""


def page_response(
    content: str, *, status_code: int = 200, form_action_origins: Iterable[str] = ()
) -> HTMLResponse:
    """``content`` with the headers every page shares: no scripts, no framing, no caching.

    Forms may post to this origin and to ``form_action_origins``: ``form-action`` also
    governs where the redirect after a POST may go (the identity provider, Google).
    """
    form_action = " ".join(["'self'", *form_action_origins])
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            # Not no-referrer: under it a form's POST carries `Origin: null`.
            "Referrer-Policy": "same-origin",
            "Cache-Control": "no-store",
        },
    )


def message_page(title: str, text: str, *, status_code: int = 200, link: str = "") -> HTMLResponse:
    """A short page: a heading, one paragraph (escaped here) and an optional link back."""
    back = f'<p><a href="{escape(link)}">Continue</a></p>' if link else ""
    body = f"<h1>{escape(title)}</h1><p>{escape(text)}</p>{back}"
    return page_response(layout(title, body, narrow=True), status_code=status_code)
