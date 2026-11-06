# Demos — check that your environment works

Each file here is a small, self-contained test of one part of the environment.
They do not depend on each other or on anything else in the repo, so run any
of them, in any order, from the repo root:

| Run this | It checks | You should see |
|---|---|---|
| `bash demos/check_tools.sh` | every tool is installed | a ✅ on every line |
| `python demos/hello_web.py` | **port forwarding** (port 8080) | a web page in your browser |
| `python demos/hello_api.py` | FastAPI + port 8000 | JSON in your browser, and `/docs` |
| `python demos/hello_sqlite.py` | SQLModel + SQLite | three rows printed, and `demos/hello.db` |
| `bash demos/hello_docker.sh` | Docker can run containers | `Hello from Docker!` |
| `bash demos/hello_postgres.sh` | Docker + Postgres + `psql` | a query result, then cleanup |
| `python demos/hello_mcp.py` | the MCP SDK (`MCPServer`) | `add(2, 3) = 5` |
| `demos/hello_notebook.ipynb` | Jupyter + `%%sql` cells | open it, **Run All**, see a table |

**Start with `check_tools.sh`, then `hello_web.py`.** Those two catch most
problems.

## Opening a forwarded port

`hello_web.py` and `hello_api.py` keep running until you press **Ctrl-C**.
While they run:

- **Codespaces:** open the **PORTS** tab (next to TERMINAL) and click the globe
  icon next to the port. The URL is real — try it on your phone.
- **Local dev container:** go to `http://localhost:8080` (or `:8000`).

If the page does not load, check that the program is still running in the
terminal, and that the port shows up in the PORTS tab.

## Something failed?

Post the full terminal output on Zulip. Include which demo you ran, and whether
you are in a codespace or a local dev container.
