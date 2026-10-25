"""Owner-scoped portal and token provisioning, shared by all exposure profiles."""
import asyncio
import os
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from usvisa.shared.network import websocket_url
from usvisa.shared.discovery import quick_origin, saved_origin, save_private_json
from usvisa.shared.locking import InstanceLock
from .auth import authenticate, digest, normalize_username, session_user
from .broker import BrokerAdmin, BrokerRuntime
from .repository import Repository
from .service import Coordinator


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class NewNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


def create_app(config, *, repository=None, admin=None, run_broker=True):
    owned_repository = repository is None
    instance_lock = InstanceLock(config.database.with_suffix(".lock")) if owned_repository and run_broker else None
    try:
        repository = repository or Repository(config.database)
    except BaseException:
        if instance_lock:
            instance_lock.close()
        raise
    service = Coordinator(repository, config.group, now=datetime.now(timezone.utc))
    admin = admin or BrokerAdmin(config)
    runtime = BrokerRuntime(config, repository, service)
    public_origin = {"value": config.origin}
    origin_file = config.database.parent / "public-origin.json"
    if owned_repository:
        if os.getenv("EXPOSE_MODE") == "cloudflare_quick" and os.getenv("QUICK_TUNNEL_METRICS"):
            public_origin["value"] = saved_origin(origin_file, config.origin, allow_local=config.insecure_local)
        save_private_json(origin_file, {"origin": public_origin["value"]})
    locks = {}
    login_slots = asyncio.Semaphore(2)

    async def follow_tunnel():
        metrics = os.getenv("QUICK_TUNNEL_METRICS", "")
        while True:
            try:
                origin = await run_in_threadpool(quick_origin, metrics)
                if origin != public_origin["value"]:
                    await run_in_threadpool(save_private_json, origin_file, {"origin": origin})
                    public_origin["value"] = origin
                    print("Public portal address: " + origin, flush=True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        follower = None
        try:
            if run_broker:
                await admin.bootstrap()
                await runtime.start()
                if os.getenv("EXPOSE_MODE") == "cloudflare_quick" and os.getenv("QUICK_TUNNEL_METRICS"):
                    follower = asyncio.create_task(follow_tunnel())
            yield
        finally:
            if follower:
                follower.cancel()
                with suppress(asyncio.CancelledError):
                    await follower
            await runtime.close()
            await admin.close()
            if owned_repository:
                repository.close()
            if instance_lock:
                instance_lock.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.repository = repository
    app.state.public_origin = public_origin

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Default Pydantic responses can echo password/token input.
        return JSONResponse({"detail": "Invalid request fields"}, status_code=422)
    root = Path(__file__).parent
    templates = Jinja2Templates(directory=root / "templates")
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")

    @app.middleware("http")
    async def boundary(request, call_next):
        from urllib.parse import urlsplit
        origin = public_origin["value"]
        if request.headers.get("host", "").lower() != urlsplit(origin).netloc.lower():
            return JSONResponse({"detail": "Invalid host"}, status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != origin:
                return JSONResponse({"detail": "Invalid origin"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 16_384:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid content length"}, status_code=400)
            # Also bound chunked requests before validation/Argon2.
            if len(await request.body()) > 16_384:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                                 "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        if not config.insecure_local:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    def current(request):
        user = session_user(repository, request.cookies.get("visa_session"))
        if not user:
            raise HTTPException(401, "Sign in required")
        if user["disabled"]:
            raise HTTPException(403, "Account disabled")
        if request.method not in {"GET", "HEAD"}:
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), user["csrf"]):
                raise HTTPException(403, "Invalid CSRF token")
        return user

    def node_info(row):
        return {"nodeId": row["node_id"], "name": row["name"], "lifecycle": row["lifecycle"],
                "active": bool(row["active"]), "minDate": row["min_date"],
                "lastSeenAt": row["last_seen_at"] / 1_000_000 if row["last_seen_at"] is not None else None}

    def owned_node(node_id, owner):
        with repository.transaction() as work:
            row = work.connection.execute("SELECT nodes.*, portal_nodes.name FROM nodes JOIN portal_nodes "
                                          "USING(node_id) WHERE node_id=? AND owner_id=?",
                                          (str(node_id), owner)).fetchone()
            if row is None:
                raise HTTPException(404, "Node not found")
            return dict(row)

    def connection(node_id):
        origin = public_origin["value"]
        return {"serverUrl": origin, "websocketUrl": websocket_url(origin),
                "vhost": "visa", "inboxQueue": f"visa.node.{node_id}.inbox"}

    def operation_key(request):
        value = request.headers.get("idempotency-key", "")
        if not 1 <= len(value) <= 100 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise HTTPException(422, "Provide Idempotency-Key (1 to 100 ASCII characters)")
        return value

    async def issue(request, user, *, name=None, node_id=None):
        key = operation_key(request)
        action = "create" if node_id is None else f"rotate:{node_id}"
        # Serialize an owner's provisioning: retries cannot rotate a just-issued
        # secret while the first operation is still awaiting the Management API.
        lock = locks.setdefault(user["user_id"], asyncio.Lock())
        async with lock:
            if node_id is not None:
                row = owned_node(node_id, user["user_id"])
                if row["lifecycle"] == "revoked":
                    raise HTTPException(403, "Node revoked")
            with repository.transaction() as work:
                db = work.connection
                op = db.execute("SELECT * FROM operations WHERE owner_id=? AND key=?",
                                (user["user_id"], key)).fetchone()
                if op:
                    if op["action"] != action:
                        raise HTTPException(422, "Idempotency-Key was used for another action")
                    node_id = UUID(op["node_id"])
                    if op["completed"]:
                        return JSONResponse({"nodeId": str(node_id), "connection": connection(node_id),
                                             "requiresTokenRotation": True})
                else:
                    if node_id is None:
                        node_id = uuid4()
                        db.execute("INSERT INTO nodes(node_id, username, lifecycle) VALUES(?, ?, 'provisioning')",
                                   (str(node_id), f"node_{node_id}"))
                        db.execute("INSERT INTO portal_nodes(node_id, owner_id, name) VALUES(?, ?, ?)",
                                   (str(node_id), user["user_id"], name.strip()))
                    db.execute("INSERT INTO operations(owner_id, key, action, node_id) VALUES(?, ?, ?, ?)",
                               (user["user_id"], key, action, str(node_id)))
            secret = secrets.token_urlsafe(32)
            try:
                await admin.provision(node_id, secret)
            except Exception:
                raise HTTPException(503, "Broker provisioning unavailable; retry with the same Idempotency-Key") from None
            with repository.transaction() as work:
                work.connection.execute("UPDATE portal_nodes SET token_hash=? WHERE node_id=?",
                                        (digest(secret), str(node_id)))
                work.connection.execute("UPDATE nodes SET lifecycle='ready' WHERE node_id=? AND lifecycle!='revoked'",
                                        (str(node_id),))
                work.connection.execute("UPDATE operations SET completed=1 WHERE owner_id=? AND key=?",
                                        (user["user_id"], key))
            return JSONResponse({"nodeId": str(node_id), "token": f"{node_id}.{secret}",
                                 "connection": connection(node_id)}, status_code=201 if action == "create" else 200)

    @app.get("/health/ready")
    async def health():
        ready = (runtime.ready and runtime.connection is not None and not runtime.connection.is_closed
                 and runtime.connection.connected.is_set())
        return JSONResponse({"ready": ready}, status_code=200 if ready else 503)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse(request=request, name="portal.html", context={"page": "login", "city": config.group.city})

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        if not session_user(repository, request.cookies.get("visa_session")):
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request=request, name="portal.html", context={"page": "nodes", "city": config.group.city})

    @app.get("/api/v1/csrf")
    async def csrf():
        token = secrets.token_urlsafe(32)
        response = JSONResponse({"csrfToken": token})
        response.set_cookie("visa_login_csrf", token, max_age=3600, httponly=True,
                            secure=not config.insecure_local, samesite="strict")
        return response

    @app.post("/api/v1/session")
    async def login(request: Request, body: Login):
        token = request.cookies.get("visa_login_csrf", "")
        if not token or not secrets.compare_digest(token, request.headers.get("x-csrf-token", "")):
            raise HTTPException(403, "Invalid CSRF token")
        try:
            username = normalize_username(body.username)
        except ValueError:
            raise HTTPException(422, "Enter a username without spaces (1 to 254 characters)") from None
        async with login_slots:
            status, user = await run_in_threadpool(authenticate, repository, username, body.password,
                                                 request.client.host if request.client else "unknown")
        if status != "ok":
            raise HTTPException(429 if status == "limited" else 401,
                                "Try again later" if status == "limited" else "Invalid username or password")
        response = JSONResponse({"userId": user["userId"], "username": user["username"], "csrfToken": user["csrfToken"]})
        response.set_cookie("visa_session", user["session"], max_age=43_200, httponly=True,
                            secure=not config.insecure_local, samesite="strict")
        response.delete_cookie("visa_login_csrf")
        return response

    @app.delete("/api/v1/session", status_code=204)
    async def logout(request: Request):
        current(request)
        with repository.transaction() as work:
            work.connection.execute("DELETE FROM sessions WHERE session_hash=?",
                                    (digest(request.cookies["visa_session"]),))
        from fastapi import Response
        response = Response(status_code=204)
        response.delete_cookie("visa_session")
        return response

    @app.get("/api/v1/me")
    async def me(request: Request):
        user = current(request)
        return {"userId": user["user_id"], "username": user["username"], "csrfToken": user["csrf"]}

    @app.get("/api/v1/nodes")
    async def nodes(request: Request):
        user = current(request)
        with repository.transaction() as work:
            rows = work.connection.execute("SELECT nodes.*, portal_nodes.name FROM nodes JOIN portal_nodes "
                                           "USING(node_id) WHERE owner_id=? ORDER BY portal_nodes.rowid",
                                           (user["user_id"],)).fetchall()
        return {"nodes": [node_info(row) for row in rows]}

    @app.post("/api/v1/nodes")
    async def create_node(request: Request, body: NewNode):
        if not body.name.strip():
            raise HTTPException(422, "Enter a node name")
        return await issue(request, current(request), name=body.name)

    @app.post("/api/v1/nodes/{node_id}/token")
    async def rotate(request: Request, node_id: UUID):
        return await issue(request, current(request), node_id=node_id)

    @app.post("/api/v1/nodes/{node_id}/revoke", status_code=204)
    async def revoke(request: Request, node_id: UUID):
        user = current(request)
        lock = locks.setdefault(user["user_id"], asyncio.Lock())
        async with lock:
            owned_node(node_id, user["user_id"])
            service.revoke(node_id, now=datetime.now(timezone.utc))
            try:
                await admin.revoke(node_id)
            except Exception:
                raise HTTPException(503, "Local access revoked; retry to finish revocation at broker") from None

    return app
