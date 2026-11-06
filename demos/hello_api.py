"""A two-route FastAPI app on port 8000 — tests FastAPI, uvicorn, and the API port.

    python demos/hello_api.py

Then, from the PORTS tab, open port 8000 in your browser and try:

    /              a JSON response
    /hello/Ada     a path parameter
    /docs          the interactive docs FastAPI writes for you

Or from a second terminal:  curl localhost:8000/hello/Ada

Ctrl-C to stop.
"""

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="Hello API")


@app.get("/")
def root():
    return {"message": "Hello from FastAPI. Try /hello/yourname or /docs"}


@app.get("/hello/{name}")
def hello(name: str):
    return {"message": f"Hello, {name}!"}


if __name__ == "__main__":
    # 0.0.0.0, never 127.0.0.1 — otherwise the port forward finds nothing.
    uvicorn.run(app, host="0.0.0.0", port=8000)
