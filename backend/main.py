import os, hashlib, secrets, string
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
from collections import defaultdict
import time

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "changeme_admin_secret_2024")
DB_PATH      = os.getenv("DB_PATH", "auth.db")
login_attempts: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 5; RATE_LIMIT_WINDOW = 60
BOT_ACTION_QUEUE: list[dict] = []

app = FastAPI(title="Cheat Auth System", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def get_db():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row; return conn

def init_db():
    conn = get_db(); c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS license_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
            is_used INTEGER DEFAULT 0, is_lifetime INTEGER DEFAULT 0,
            expiry_days INTEGER DEFAULT 30, created_at TEXT DEFAULT (datetime('now')),
            redeemed_at TEXT, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, password_plain TEXT NOT NULL,
            discord_id TEXT, discord_name TEXT, hwid TEXT,
            is_banned INTEGER DEFAULT 0, ban_reason TEXT,
            expiry_date TEXT, is_lifetime INTEGER DEFAULT 0,
            auth_token TEXT, last_login TEXT, last_ip TEXT,
            created_at TEXT DEFAULT (datetime('now')));
    """); conn.commit(); conn.close()
init_db()

def hash_password(pw): return hashlib.sha256(pw.encode()).hexdigest()
def generate_key(prefix="CHEAT"):
    chars = string.ascii_uppercase + string.digits
    return prefix + "-" + "-".join("".join(secrets.choice(chars) for _ in range(5)) for _ in range(4))
def check_rate_limit(ip):
    now = time.time()
    login_attempts[ip] = [t for t in login_attempts[ip] if now-t < RATE_LIMIT_WINDOW]
    if len(login_attempts[ip]) >= RATE_LIMIT_MAX: return False
    login_attempts[ip].append(now); return True
def require_admin(admin_secret: str = Query(...)):
    if admin_secret != ADMIN_SECRET: raise HTTPException(403, "Invalid admin secret")

class RedeemKeyRequest(BaseModel):
    key: str; username: str; password: str
    discord_id: Optional[str]=None; discord_name: Optional[str]=None
class CheatLoginRequest(BaseModel):
    username: str; password: str; hwid: str
class BanRequest(BaseModel):
    target: str; target_type: str; reason: Optional[str]="No reason given"
class UnbanRequest(BaseModel):
    target: str; target_type: str
class ResetHWIDRequest(BaseModel): username: str
class ResetPasswordRequest(BaseModel): username: str; new_password: str
class DeleteUserRequest(BaseModel): username: str
class ExtendKeyRequest(BaseModel): username: str; days: int
class SendMessageRequest(BaseModel): channel_id: str; message: str
class ManageRoleRequest(BaseModel): discord_id: str; role_id: str; action: str

@app.post("/api/redeem_key")
async def redeem_key(req: RedeemKeyRequest):
    conn = get_db(); c = conn.cursor()
    try:
        row = c.execute("SELECT * FROM license_keys WHERE key=?", (req.key.strip().upper(),)).fetchone()
        if not row: raise HTTPException(404, "Key not found")
        if row["is_used"]: raise HTTPException(400, "Key already redeemed")
        if c.execute("SELECT id FROM users WHERE username=?", (req.username,)).fetchone():
            raise HTTPException(400, "Username taken")
        expiry = None if row["is_lifetime"] else (datetime.utcnow()+timedelta(days=row["expiry_days"])).isoformat()
        c.execute("INSERT INTO users (username,password_hash,password_plain,discord_id,discord_name,expiry_date,is_lifetime,created_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (req.username, hash_password(req.password), req.password, req.discord_id, req.discord_name, expiry, 1 if row["is_lifetime"] else 0))
        uid = c.lastrowid
        c.execute("UPDATE license_keys SET is_used=1,redeemed_at=datetime('now'),user_id=? WHERE key=?",(uid,row["key"]))
        conn.commit()
        return {"success":True,"username":req.username,"expiry":expiry or "Lifetime"}
    finally: conn.close()

@app.post("/api/cheat_login")
async def cheat_login(req: CheatLoginRequest, request: Request):
    ip = request.client.host
    if not check_rate_limit(ip): raise HTTPException(429,"Too many attempts. Wait 60s.")
    conn = get_db(); c = conn.cursor()
    try:
        u = c.execute("SELECT * FROM users WHERE username=?", (req.username,)).fetchone()
        if not u or u["password_hash"] != hash_password(req.password): raise HTTPException(401,"Invalid credentials")
        if u["is_banned"]: raise HTTPException(403, f"Banned: {u['ban_reason']}")
        if not u["is_lifetime"] and u["expiry_date"] and datetime.utcnow() > datetime.fromisoformat(u["expiry_date"]):
            raise HTTPException(403,"Subscription expired")
        if u["hwid"] and u["hwid"] != req.hwid: raise HTTPException(403,"HWID mismatch. Contact support.")
        if not u["hwid"]: c.execute("UPDATE users SET hwid=? WHERE id=?",(req.hwid,u["id"]))
        token = secrets.token_hex(32)
        c.execute("UPDATE users SET auth_token=?,last_login=datetime('now'),last_ip=? WHERE id=?",(token,ip,u["id"]))
        conn.commit()
        return {"success":True,"token":token,"username":u["username"],"expiry":u["expiry_date"] or "Lifetime","is_lifetime":bool(u["is_lifetime"])}
    finally: conn.close()

@app.get("/api/user_info")
async def user_info(discord_id: str):
    conn = get_db(); c = conn.cursor()
    try:
        u = c.execute("SELECT * FROM users WHERE discord_id=?",(discord_id,)).fetchone()
        if not u: raise HTTPException(404,"User not found")
        return {"username":u["username"],"discord_id":u["discord_id"],"discord_name":u["discord_name"],
                "hwid_bound":bool(u["hwid"]),"is_banned":bool(u["is_banned"]),"expiry_date":u["expiry_date"] or "Lifetime",
                "is_lifetime":bool(u["is_lifetime"]),"last_login":u["last_login"],"created_at":u["created_at"]}
    finally: conn.close()

@app.get("/admin/stats")
async def admin_stats(_=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        tk = c.execute("SELECT COUNT(*) FROM license_keys").fetchone()[0]
        uk = c.execute("SELECT COUNT(*) FROM license_keys WHERE is_used=1").fetchone()[0]
        tu = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bu = c.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
        return {"total_keys":tk,"used_keys":uk,"unused_keys":tk-uk,"total_users":tu,"active_users":tu-bu,"banned_users":bu}
    finally: conn.close()

@app.get("/admin/keys")
async def admin_keys(_=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        rows = c.execute("""SELECT lk.*,u.username,u.discord_id,u.discord_name,u.password_plain,u.hwid,
            u.expiry_date,u.last_login,u.last_ip,u.is_banned FROM license_keys lk
            LEFT JOIN users u ON lk.user_id=u.id ORDER BY lk.id DESC""").fetchall()
        return [dict(r) for r in rows]
    finally: conn.close()

@app.get("/admin/generate")
async def admin_generate(count:int=Query(1,ge=1,le=100), expiry_days:int=Query(30,ge=1), lifetime:bool=Query(False), _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        keys = []
        for _ in range(count):
            k = generate_key()
            c.execute("INSERT INTO license_keys (key,is_lifetime,expiry_days) VALUES (?,?,?)",(k,1 if lifetime else 0,expiry_days))
            keys.append(k)
        conn.commit(); return {"generated":count,"keys":keys}
    finally: conn.close()

@app.post("/admin/ban")
async def admin_ban(req: BanRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        col = {"discord_id":"discord_id","hwid":"hwid","username":"username"}.get(req.target_type)
        if not col: raise HTTPException(400,"Invalid target_type")
        r = c.execute(f"UPDATE users SET is_banned=1,ban_reason=? WHERE {col}=?",(req.reason,req.target))
        conn.commit()
        if r.rowcount==0: raise HTTPException(404,"User not found")
        return {"success":True}
    finally: conn.close()

@app.post("/admin/unban")
async def admin_unban(req: UnbanRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        col = {"discord_id":"discord_id","hwid":"hwid","username":"username"}.get(req.target_type)
        if not col: raise HTTPException(400,"Invalid target_type")
        r = c.execute(f"UPDATE users SET is_banned=0,ban_reason=NULL WHERE {col}=?",(req.target,))
        conn.commit()
        if r.rowcount==0: raise HTTPException(404,"User not found")
        return {"success":True}
    finally: conn.close()

@app.post("/admin/reset_hwid")
async def admin_reset_hwid(req: ResetHWIDRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        r = c.execute("UPDATE users SET hwid=NULL WHERE username=?",(req.username,))
        conn.commit()
        if r.rowcount==0: raise HTTPException(404,"User not found")
        return {"success":True}
    finally: conn.close()

@app.post("/admin/reset_password")
async def admin_reset_password(req: ResetPasswordRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        r = c.execute("UPDATE users SET password_hash=?,password_plain=? WHERE username=?",(hash_password(req.new_password),req.new_password,req.username))
        conn.commit()
        if r.rowcount==0: raise HTTPException(404,"User not found")
        return {"success":True}
    finally: conn.close()

@app.post("/admin/delete_user")
async def admin_delete_user(req: DeleteUserRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        u = c.execute("SELECT id FROM users WHERE username=?",(req.username,)).fetchone()
        if not u: raise HTTPException(404,"User not found")
        c.execute("UPDATE license_keys SET user_id=NULL,is_used=0,redeemed_at=NULL WHERE user_id=?",(u["id"],))
        c.execute("DELETE FROM users WHERE id=?",(u["id"],))
        conn.commit(); return {"success":True}
    finally: conn.close()

@app.post("/admin/extend_key")
async def admin_extend_key(req: ExtendKeyRequest, _=Depends(require_admin)):
    conn = get_db(); c = conn.cursor()
    try:
        u = c.execute("SELECT * FROM users WHERE username=?",(req.username,)).fetchone()
        if not u: raise HTTPException(404,"User not found")
        base = datetime.fromisoformat(u["expiry_date"]) if u["expiry_date"] else datetime.utcnow()
        new_exp = (base + timedelta(days=req.days)).isoformat()
        c.execute("UPDATE users SET expiry_date=?,is_lifetime=0 WHERE username=?",(new_exp,req.username))
        conn.commit(); return {"success":True,"new_expiry":new_exp}
    finally: conn.close()

@app.post("/admin/bot/send_message")
async def bot_send_message(req: SendMessageRequest, _=Depends(require_admin)):
    BOT_ACTION_QUEUE.append({"type":"send_message","channel_id":req.channel_id,"message":req.message})
    return {"success":True,"queued":True}

@app.post("/admin/bot/manage_role")
async def bot_manage_role(req: ManageRoleRequest, _=Depends(require_admin)):
    BOT_ACTION_QUEUE.append({"type":"manage_role","discord_id":req.discord_id,"role_id":req.role_id,"action":req.action})
    return {"success":True,"queued":True}

@app.get("/admin/bot/poll")
async def bot_poll(_=Depends(require_admin)):
    actions = BOT_ACTION_QUEUE.copy(); BOT_ACTION_QUEUE.clear(); return actions

@app.get("/", response_class=HTMLResponse)
async def admin_panel():
    panel_path = os.path.join(os.path.dirname(__file__), "panel.html")
    with open(panel_path) as f: return f.read()
