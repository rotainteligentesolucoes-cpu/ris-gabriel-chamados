import hmac
import re
import secrets

from flask import abort, request, session


TOKEN_KEY = "_csrf_token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FORM_PATTERN = re.compile(r"(<form\b[^>]*\bmethod\s*=\s*['\"]?post['\"]?[^>]*>)", re.IGNORECASE)


def csrf_token():
    token = session.get(TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[TOKEN_KEY] = token
    return token


def _is_web_request():
    return bool(request.endpoint and request.endpoint.startswith("web_bp."))


def _submitted_token():
    return request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")


def _inject_csrf(html, token):
    hidden = f'<input type="hidden" name="csrf_token" value="{token}">'
    html = FORM_PATTERN.sub(lambda match: match.group(1) + hidden, html)
    if 'name="csrf-token"' not in html:
        meta = f'<meta name="csrf-token" content="{token}">'
        html = html.replace("</head>", meta + "</head>", 1)
    if "window.__RIS_CSRF_INSTALLED__" not in html:
        script = """
<script>
(() => {
  if (window.__RIS_CSRF_INSTALLED__) return;
  window.__RIS_CSRF_INSTALLED__ = true;
  const originalFetch = window.fetch;
  window.fetch = function(input, init) {
    init = init || {};
    const method = String(init.method || 'GET').toUpperCase();
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const sameOrigin = !url || url.startsWith('/') || url.startsWith(window.location.origin);
    if (sameOrigin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const token = document.querySelector('meta[name="csrf-token"]')?.content;
      if (token) {
        const headers = new Headers(init.headers || (input && input.headers) || {});
        headers.set('X-CSRF-Token', token);
        init.headers = headers;
      }
    }
    return originalFetch.call(this, input, init);
  };
})();
</script>
"""
        html = html.replace("</body>", script + "</body>", 1)
    return html


def init_csrf(app):
    @app.before_request
    def protect_web_forms():
        if not _is_web_request():
            return None
        expected = csrf_token()
        if request.method in UNSAFE_METHODS:
            supplied = _submitted_token()
            if not supplied or not hmac.compare_digest(expected, supplied):
                abort(400, description="Token CSRF ausente ou invalido. Atualize a pagina e tente novamente.")
        return None

    @app.after_request
    def add_csrf_to_html(response):
        if not _is_web_request() or response.direct_passthrough:
            return response
        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type.lower():
            return response
        html = response.get_data(as_text=True)
        response.set_data(_inject_csrf(html, csrf_token()))
        response.headers["Content-Length"] = str(len(response.get_data()))
        return response

