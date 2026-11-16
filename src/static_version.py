"""Cache-busting for the stylesheets and scripts every page loads.

`static_url("styles/app.css")` is `/static/styles/app.css?v=<file's modified time>`, so
after a deploy a browser fetches the changed file instead of mixing new markup with an old
cached stylesheet. Every Jinja2Templates that renders `base.html` needs `add_to`.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

STATIC_DIR = Path(__file__).parent / "static"


def static_url(path: str) -> str:
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


def add_to(templates: Jinja2Templates) -> Jinja2Templates:
    templates.env.globals["static_url"] = static_url
    return templates
