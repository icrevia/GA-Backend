from fastapi.testclient import TestClient
from fastapi import FastAPI, WebSocket

app = FastAPI()

@app.websocket('/')
async def ws_app(ws: WebSocket):
    await ws.accept()
    print("AUTHORIZATION HEADER:", repr(ws.headers.get('authorization')))
    await ws.close()

client = TestClient(app)
with client.websocket_connect('/', headers={'Authorization': 'Bearer a, Bearer b'}) as ws:
    pass
