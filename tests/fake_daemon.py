"""内存版 agent-wechat 守护进程（只实现适配器用到的接口），用于离线测试。"""
import asyncio
from datetime import datetime, timezone
import json

from aiohttp import web

TOKEN = "test-token"
ACC = "acc1"


class FakeDaemon:
    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.events: list[dict] = []  # 已经存在的(历史)事件
        self.stream_script: list[list[dict | str]] = []  # 每次连接要推的事件；"DROP" 表示断开
        self.stream_calls: list[str] = []  # 每次连接的 after
        self.sends: list[dict] = []
        self.send_headers: list[dict] = []
        self.uploads: list[dict] = []
        self.assets: dict[str, list[dict]] = {}
        self.content: dict[str, tuple[bytes, str]] = {}
        self.acks: list[str] = []
        self.authenticated = True
        self.reject_quote = False
        self.job_state = "completed"
        self.text_state = "accepted"  # 真守护进程常返回 queued
        self.send_status_state = "sent"
        self.member_items = [{"id": "wxid_alice", "displayName": "Alice"}]
        self.avatars: dict[str, bytes] = {}  # 已缓存的头像
        self.avatar_remote: dict[str, bytes] = {}  # 刷新后才会出现的头像
        self.avatar_refreshes: list[str] = []
        self.no_account = False  # True = 守护进程里一个账号都没有
        self.created: list[dict] = []
        self.login_bodies: list[dict] = []
        self.login_states = ["succeeded"]  # 每次查登录进度依次返回的状态
        self.qr_png = b""
        self.qr_fetches: list[str] = []
        self.runner = None
        self.port = 0

    def app(self):
        app = web.Application(middlewares=[self.auth], client_max_size=64 * 1024 * 1024)
        r = app.router
        r.add_get("/v1/accounts", self.accounts)
        r.add_post("/v1/accounts", self.create_account)
        r.add_post(f"/v1/accounts/{ACC}/auth/login", self.login)
        r.add_get(f"/v1/accounts/{ACC}/operations/{{oid}}", self.operation)
        r.add_get(f"/v1/accounts/{ACC}/operations/{{oid}}/qr", self.qr)
        r.add_get(f"/v1/accounts/{ACC}", self.account)
        r.add_get(f"/v1/accounts/{ACC}/capabilities", self.caps)
        r.add_get(f"/v1/accounts/{ACC}/events", self.events_page)
        r.add_get(f"/v1/accounts/{ACC}/events/stream", self.stream)
        r.add_post(f"/v1/accounts/{ACC}/consumers/{{c}}/ack", self.ack)
        r.add_get(f"/v1/accounts/{ACC}/messages/{{mid}}", self.message)
        r.add_post(f"/v1/accounts/{ACC}/media/lookup", self.lookup)
        r.add_post(f"/v1/accounts/{ACC}/messages/{{mid}}/media-transfers", self.transfer)
        r.add_get(f"/v1/accounts/{ACC}/media/{{aid}}/content", self.media_content)
        r.add_post(f"/v1/accounts/{ACC}/uploads", self.upload)
        r.add_post(f"/v1/accounts/{ACC}/sends", self.send)
        r.add_get(f"/v1/accounts/{ACC}/jobs/{{jid}}", self.job)
        r.add_get(f"/v1/accounts/{ACC}/sends/{{sid}}", self.send_status)
        r.add_get(f"/v1/accounts/{ACC}/threads/{{tid}}", self.thread)
        r.add_get(f"/v1/accounts/{ACC}/threads/{{tid}}/messages", self.thread_messages)
        r.add_get(f"/v1/accounts/{ACC}/groups/{{gid}}/members", self.members)
        r.add_get(f"/v1/accounts/{ACC}/groups/{{gid}}/lookup", self.lookup_group)
        r.add_get(f"/v1/accounts/{ACC}/avatars/{{uid}}", self.avatar)
        r.add_post(f"/v1/accounts/{ACC}/avatars/{{uid}}/refresh", self.avatar_refresh)
        return app

    @web.middleware
    async def auth(self, request, handler):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": {"code": "unauthorized"}}, status=401)
        return await handler(request)

    async def start(self):
        self.runner = web.AppRunner(self.app())
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self):
        await self.runner.cleanup()

    # handlers
    async def create_account(self, req):
        self.created.append(await req.json())
        self.no_account = False
        return web.json_response({"id": ACC}, status=201)

    async def login(self, req):
        self.login_bodies.append(await req.json())
        return web.json_response({"id": "op1", "kind": "login", "state": "running",
                                  "result": {"qrAvailable": True, "qrGeneration": 1}}, status=202)

    async def operation(self, req):
        state = self.login_states.pop(0) if self.login_states else "succeeded"
        if state == "succeeded":
            self.authenticated = True
        return web.json_response({"id": req.match_info["oid"], "kind": "login", "state": state,
                                  "result": {"qrAvailable": True, "qrGeneration": 1}})

    async def qr(self, req):
        self.qr_fetches.append(req.query.get("generation", ""))
        return web.Response(body=self.qr_png, content_type="image/png")

    async def accounts(self, req):
        if self.no_account:
            return web.json_response({"items": []})
        return web.json_response({"items": [{
            "id": ACC, "alias": "dafeiyu", "authenticated": self.authenticated,
            "ownUsername": "wxid_bot" if self.authenticated else None}]})

    async def account(self, req):
        return web.json_response({"id": ACC, "authenticated": self.authenticated, "ownUsername": "wxid_bot"})

    async def caps(self, req):
        return web.json_response({"quotedTextSend": True, "textMentions": True})

    async def events_page(self, req):
        after = int(req.query.get("after", "0"))
        items = [e for e in self.events if int(e["cursor"]) > after]
        nxt = items[-1]["cursor"] if items else str(after)
        return web.json_response({"items": items, "nextCursor": nxt})

    async def stream(self, req):
        self.stream_calls.append(req.query.get("after", ""))
        script = self.stream_script.pop(0) if self.stream_script else []
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        await resp.write(b": hello\n\n")
        for item in script:
            if item == "DROP":
                break
            ev = item
            await resp.write(f"id: {ev['cursor']}\ndata: {json.dumps(ev)}\n\n".encode())
            await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.3)
        return resp

    async def ack(self, req):
        body = await req.json()
        self.acks.append(body["cursor"])
        return web.json_response({"ok": True})

    async def message(self, req):
        m = self.messages.get(req.match_info["mid"])
        if not m:
            return web.json_response({"error": {"code": "not_found"}}, status=404)
        return web.json_response(m)

    async def lookup(self, req):
        body = await req.json()
        return web.json_response({"items": [
            {"messageId": m, "assets": self.assets.get(m, []), "error": None} for m in body["messageIds"]]})

    async def transfer(self, req):
        return web.json_response({"id": "job_dl", "state": "completed"}, status=202)

    async def media_content(self, req):
        data, ctype = self.content[req.match_info["aid"]]
        return web.Response(body=data, content_type=ctype)

    async def upload(self, req):
        reader = await req.multipart()
        part = await reader.next()
        data = await part.read()
        rec = {"name": part.filename, "ctype": part.headers.get("Content-Type"), "data": data}
        self.uploads.append(rec)
        return web.json_response({"id": f"src{len(self.uploads)}", "name": part.filename})

    async def send(self, req):
        body = await req.json()
        if self.reject_quote and ("quote" in body or "mentions" in body):
            return web.json_response({"error": {"code": "quote_unsupported"}}, status=422)
        if not req.headers.get("Idempotency-Key"):
            return web.json_response({"error": {"code": "key_required"}}, status=400)
        self.sends.append(body)
        self.send_headers.append(dict(req.headers))
        if "kind" in body and body["kind"] in ("image", "voice", "video", "file"):
            return web.json_response({"id": f"job{len(self.sends)}", "state": "queued"}, status=202)
        return web.json_response({"id": f"send{len(self.sends)}", "state": self.text_state}, status=202)

    async def send_status(self, req):
        return web.json_response({"id": req.match_info["sid"], "state": self.send_status_state})

    async def job(self, req):
        return web.json_response({"id": req.match_info["jid"], "state": self.job_state})

    async def thread_messages(self, req):
        tid = req.match_info["tid"]
        return web.json_response({"items": [m for m in self.messages.values() if m.get("threadId") == tid]})

    async def thread(self, req):
        return web.json_response({"id": req.match_info["tid"], "displayName": "名字-" + req.match_info["tid"]})

    async def members(self, req):
        return web.json_response({"items": self.member_items})

    async def avatar(self, req):
        uid = req.match_info["uid"]
        if uid not in self.avatars:
            return web.json_response({"error": {"code": "media_not_cached"}}, status=404)
        return web.Response(body=self.avatars[uid], content_type="image/jpeg")

    async def avatar_refresh(self, req):
        uid = req.match_info["uid"]
        self.avatar_refreshes.append(uid)
        if uid in self.avatar_remote:
            self.avatars[uid] = self.avatar_remote[uid]
        return web.json_response({"id": "op1", "kind": "metadata", "state": "queued"}, status=202)

    async def lookup_group(self, req):
        return web.json_response({"id": req.match_info["gid"], "items": [
            {"id": "wxid_alice", "nickname": "alice_raw", "roomDisplayName": "群里的Alice"},
            {"id": "wxid_bob", "nickname": "Bob", "roomDisplayName": None}]})


def msg(mid, thread, sender, mtype=1, body="", **kw):
    m = {"id": mid, "accountId": ACC, "threadId": thread, "serverId": str(abs(hash(mid)) % 10**9 + 1),
         "senderId": sender, "type": mtype, "direction": "incoming", "body": body,
         "createdAt": datetime.now(timezone.utc).isoformat(), "mentions": {"state": "known", "userIds": [], "self": False, "all": False, "source": None},
         "version": "1", "media": []}
    m.update(kw)
    return m


def committed(cursor, mid, thread, origin="live"):
    return {"cursor": str(cursor), "kind": "message.committed",
            "data": {"messageId": mid, "threadId": thread, "origin": origin}, "createdAt": "2026-11-02T12:00:00Z"}
