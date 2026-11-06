"""Serves one web page on port 8080 — the quickest test of port forwarding.

    python demos/hello_web.py

Then open it in your browser:

  - Codespaces: open the PORTS tab (next to TERMINAL), find port 8080, and
    click the globe icon. You get a real https URL — try it on your phone too.
  - Local dev container: http://localhost:8080

If the page loads, port forwarding works. Ctrl-C to stop.
"""

import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8080

PAGE = """<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Hello from your codespace</title></head>
  <body style="font-family: system-ui, sans-serif; max-width: 40em; margin: 4em auto;">
    <h1>✅ Port forwarding works</h1>
    <p>This page was just generated inside your container,
       <code>{host}</code>, at {time}.</p>
    <p>Reload the page and the time changes — it is live, not a file.</p>
  </body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.format(
            host=socket.gethostname(),
            time=time.strftime("%H:%M:%S %Z"),
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# 0.0.0.0, never 127.0.0.1: inside a container, 127.0.0.1 is reachable only
# from inside the container, so the port forward would find nothing.
server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
print(f"Serving on port {PORT}. Open it from the PORTS tab. Ctrl-C to stop.")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nStopped.")
