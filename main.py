"""GOLDREX — Telegram Mini App backend.
FastAPI (REST + WebSocket o'yinlar) + aiogram bot (polling) bitta jarayonda.
Render: Start command ->  uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import os, json, time, hmac, hashlib, asyncio, random, math, base64, logging, collections
from urllib.parse import parse_qsl
from contextlib import asynccontextmanager

import chess
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import (create_engine, text, MetaData, Table, Column, BigInteger, Integer, String, Text,
                        PrimaryKeyConstraint)
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile,
                           CallbackQuery, Message, MenuButtonWebApp)

log = logging.getLogger("goldrex")
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = [int(x) for x in os.getenv("ADMINS", "").replace(" ", "").split(",") if x]
WEBAPP_URL = os.getenv("WEBAPP_URL", "")          # https://sizning-app.onrender.com
DB_URL = os.getenv("DATABASE_URL", "sqlite:///goldrex.db")
if DB_URL.startswith("postgres://"): DB_URL = DB_URL.replace("postgres://", "postgresql+psycopg2://", 1)
elif DB_URL.startswith("postgresql://"): DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg2://", 1)

STAKES = [3, 20, 100, 500, 1000, 10000]
PACKS = [(12000, 2000), (24000, 4000), (36000, 6000), (48000, 8000), (60000, 10000)]   # so'm -> tanga
TIMES = {"chess": 600, "dr": 360, "c4": 360}      # umumiy vaqt (soniya)
MOVE_LIMIT = 60                                    # har bir yurishga 1 daqiqa
SN_MIN, SN_MAX, SN_DUR, SN_COUNTDOWN, LOBBY_WAIT, SN_GO = 5, 10, 300, 15, 180, 3
SN_R, STEP, TURN, FOOD_N, EAT2, HIT2 = 1000, 7, 0.2, 160, 22 ** 2, 14 ** 2
DEFAULTS = dict(win_share=80, min_withdraw=3000, coin_som=5, ref_reward=5, welcome_bonus=5, contest_share=90,
                on_chess=1, on_dr=1, on_c4=1, on_sn=1)
CSTAKES = [3, 20, 100, 500, 1000]                  # konkur: tanga bilan
CSOM = [1350, 2450, 5550, 10750]                   # konkur: so'm bilan
C_MIN, C_MAX, C_CD, C_WAIT = 20, 100, 120, 1800    # min, max, hisoblagich (s), kutish (s)

# ------------------------------------------------------------------ DB
engine = create_engine(DB_URL, pool_pre_ping=True)
md = MetaData()
Table("users", md, Column("id", BigInteger, primary_key=True, autoincrement=False), Column("name", String(200)),
      Column("username", String(100)), Column("joined", BigInteger),
      *[Column(c, BigInteger, server_default="0") for c in ("coins", "wins", "losses", "deposited", "withdrawn", "ref_paid", "refs")],
      Column("ref", BigInteger))
Table("channels", md, Column("id", Integer, primary_key=True), Column("kind", String(10)), Column("chat", String(300)),
      Column("title", String(200)), Column("reward", BigInteger, server_default="0"))
Table("done", md, Column("uid", BigInteger), Column("task", Integer), PrimaryKeyConstraint("uid", "task"))
Table("done2", md, Column("uid", BigInteger), Column("task", Integer), Column("day", String(10)),
      PrimaryKeyConstraint("uid", "task", "day"))
Table("cards", md, Column("id", Integer, primary_key=True), Column("info", Text))
Table("deposits", md, Column("id", Integer, primary_key=True), Column("uid", BigInteger), Column("som", BigInteger),
      Column("coins", BigInteger), Column("status", String(10), server_default="new"), Column("img", Text), Column("ts", BigInteger))
Table("cdeps", md, Column("id", Integer, primary_key=True), Column("uid", BigInteger), Column("amt", BigInteger),
      Column("status", String(10), server_default="new"), Column("img", Text), Column("ts", BigInteger))
Table("withdraws", md, Column("id", Integer, primary_key=True), Column("uid", BigInteger), Column("coins", BigInteger),
      Column("som", BigInteger), Column("card", String(100)), Column("status", String(10), server_default="new"), Column("ts", BigInteger))
Table("shop", md, Column("id", Integer, primary_key=True), Column("icon", String(10)), Column("title", String(200)), Column("price", BigInteger))
Table("settings", md, Column("k", String(50), primary_key=True), Column("v", String(50)))

def run(sql, **p):
    with engine.begin() as cx:
        r = cx.execute(text(sql), p)
        return [dict(x._mapping) for x in r.fetchall()] if r.returns_rows else r.rowcount

def one(sql, **p):
    rows = run(sql, **p); return rows[0] if rows else None

SETTINGS = {}
def cfg(k): return int(SETTINGS.get(k, DEFAULTS[k]))
def load_settings():
    SETTINGS.update({r["k"]: r["v"] for r in run("SELECT k,v FROM settings")})

def init_db():
    md.create_all(engine)
    if not one("SELECT 1 AS x FROM shop"):
        for ic, t, pr in (("⭐", "35 ta TG yulduz", 2000), ("🎁", "Telegram Premium", 50000),
                          ("🎮", "PUBG UC", 3000), ("👑", "VIP paket", 100000)):
            run("INSERT INTO shop(icon,title,price) VALUES(:i,:t,:p)", i=ic, t=t, p=pr)
    load_settings()

def level(u): return min(100, 1 + (u["wins"] * 10 + u["losses"] * 2) // 50)
def add(uid, n): run("UPDATE users SET coins=coins+:n WHERE id=:i", n=n, i=uid)
def take(uid, n): return run("UPDATE users SET coins=coins-:n WHERE id=:i AND coins>=:n", n=n, i=uid) == 1

NAMES = {}
def nm(uid):
    if uid not in NAMES:
        r = one("SELECT name FROM users WHERE id=:i", i=uid); NAMES[uid] = r["name"] if r else str(uid)
    return NAMES[uid]

# ------------------------------------------------------------------ Auth
def check_init(init):
    try:
        d = dict(parse_qsl(init, keep_blank_values=True)); h = d.pop("hash", None)
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if not h or not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h): return None
        if time.time() - int(d.get("auth_date", 0)) > 86400 * 3: return None
        u = json.loads(d["user"]); u["start_param"] = d.get("start_param"); return u
    except Exception: return None

def ensure_user(u):
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or str(u["id"])
    row = one("SELECT * FROM users WHERE id=:i", i=u["id"])
    if not row:
        sp = str(u.get("start_param") or ""); ref = int(sp) if sp.isdigit() and int(sp) != u["id"] and one("SELECT 1 AS x FROM users WHERE id=:i", i=int(sp)) else None
        # yangi foydalanuvchiga start bonusi (faqat 1 marta: foydalanuvchi bazada qoladi)
        run("INSERT INTO users(id,name,username,joined,ref,coins) VALUES(:i,:n,:u,:j,:r,:c)", i=u["id"], n=name,
            u=u.get("username"), j=int(time.time()), r=ref, c=cfg("welcome_bonus"))
        row = one("SELECT * FROM users WHERE id=:i", i=u["id"])
    elif row["name"] != name:
        run("UPDATE users SET name=:n, username=:u WHERE id=:i", n=name, u=u.get("username"), i=u["id"]); row["name"] = name
    NAMES[u["id"]] = name
    return row

def auth(x_init: str = Header("")):
    u = check_init(x_init)
    if not u: raise HTTPException(401, "auth")
    return ensure_user(u)

def adm(u=Depends(auth)):
    if u["id"] not in ADMINS: raise HTTPException(403, "admin")
    return u

def tlink(c): return c if c.startswith("http") else "https://t.me/" + c.lstrip("@")

async def need_sub(uid):
    out = []
    for c in run("SELECT * FROM channels WHERE kind='force'"):
        try:
            m = await bot.get_chat_member(c["chat"], uid); ok = m.status not in ("left", "kicked")
        except Exception: ok = True            # bot kanalda admin bo'lmasa bloklamaymiz
        if not ok: out.append({"title": c["title"], "url": tlink(c["chat"])})
    return out

async def pay_ref(u):
    if u["ref"] and not u["ref_paid"]:
        run("UPDATE users SET ref_paid=1 WHERE id=:i", i=u["id"]); r = cfg("ref_reward")
        add(u["ref"], r); run("UPDATE users SET refs=refs+1 WHERE id=:i", i=u["ref"])
        try: await bot.send_message(u["ref"], f"🎉 Do'stingiz qo'shildi! +{r} tanga")
        except Exception: pass

# ------------------------------------------------------------------ Bot
bot = Bot(TOKEN); dp = Dispatcher()

async def notify_admins(txt, markup=None, photo=None):
    for a in ADMINS:
        try:
            if photo: await bot.send_photo(a, BufferedInputFile(photo, "chek.jpg"), caption=txt, reply_markup=markup)
            else: await bot.send_message(a, txt, reply_markup=markup)
        except Exception: pass

def app_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 O'YNASH", web_app=WebAppInfo(url=WEBAPP_URL))]])

@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    ensure_user({"id": m.from_user.id, "first_name": m.from_user.first_name, "last_name": m.from_user.last_name,
                 "username": m.from_user.username, "start_param": command.args})
    await m.answer("🦁 <b>GOLDREX</b> — O'yin • Do'stlik • G'alaba\nPastdagi tugma orqali o'ynang!", parse_mode="HTML", reply_markup=app_kb())

async def process_deposit(did, ok):
    d = one("SELECT * FROM deposits WHERE id=:i", i=did)
    if not d or run("UPDATE deposits SET status=:s, img='' WHERE id=:i AND status='new'", s="ok" if ok else "no", i=did) != 1: return False
    if ok:
        add(d["uid"], d["coins"]); run("UPDATE users SET deposited=deposited+:s WHERE id=:i", s=d["som"], i=d["uid"])
    try: await bot.send_message(d["uid"], f"✅ To'lov tasdiqlandi! +{d['coins']} tanga" if ok else "❌ To'lov rad etildi.")
    except Exception: pass
    return True

async def process_withdraw(wid, ok):
    w = one("SELECT * FROM withdraws WHERE id=:i", i=wid)
    if not w or run("UPDATE withdraws SET status=:s WHERE id=:i AND status='new'", s="ok" if ok else "no", i=wid) != 1: return False
    if ok: run("UPDATE users SET withdrawn=withdrawn+:c WHERE id=:i", c=w["coins"], i=w["uid"])
    else: add(w["uid"], w["coins"])
    try: await bot.send_message(w["uid"], "✅ Pul kartangizga yuborildi!" if ok else "❌ So'rov rad etildi, tangalar qaytarildi.")
    except Exception: pass
    return True

async def process_cdep(did, ok):
    d = one("SELECT * FROM cdeps WHERE id=:i", i=did)
    if not d or run("UPDATE cdeps SET status=:s, img='' WHERE id=:i AND status='new'", s="ok" if ok else "no", i=did) != 1: return False
    if ok:
        if await tourneys[("som", d["amt"])].add_player(d["uid"]): msg = "✅ To'lov tasdiqlandi, siz konkurga qo'shildingiz!"
        else:
            msg = "✅ To'lov tasdiqlandi, lekin konkur boshlanib bo'lgan. Admin pulni qaytaradi."
            await notify_admins(f"⚠️ Qaytarish kerak: {d['uid']} — {d['amt']:,} so'm")
    else: msg = "❌ To'lov rad etildi."
    try: await bot.send_message(d["uid"], msg)
    except Exception: pass
    return True

@dp.callback_query(F.data.regexp(r"^(dep|wd|cd):(ok|no):\d+$"))
async def admin_cb(c: CallbackQuery):
    if c.from_user.id not in ADMINS: return await c.answer()
    kind, res, i = c.data.split(":")
    done = await {"dep": process_deposit, "wd": process_withdraw, "cd": process_cdep}[kind](int(i), res == "ok")
    await c.answer("Bajarildi" if done else "Ko'rib chiqilgan")
    try:
        mark = "\n\nNatija: " + ("✅" if res == "ok" else "❌")
        if c.message.photo: await c.message.edit_caption(caption=(c.message.caption or "") + mark)
        else: await c.message.edit_text((c.message.text or "") + mark)
    except Exception: pass

# ------------------------------------------------------------------ Realtime holati
clients, user_game, queues, in_queue = {}, {}, {}, {}
duels, snakes, chat_log = set(), set(), collections.deque(maxlen=100)
chat_last = {}
in_contest, tourneys = set(), {}

async def send(uid, obj):
    ws = clients.get(uid)
    if ws:
        try: await ws.send_text(json.dumps(obj, separators=(",", ":")))
        except Exception: pass

# ------------------------------------------------------------------ 2 kishilik o'yinlar
class Duel:
    kind = ""
    def __init__(s, a, b, stake):
        s.p = [a, b]; s.stake = stake; s.turn = 0; s.over = False; s.clock = [TIMES[s.kind]] * 2
        s.t0 = time.time(); s.quiet = 0; s.setup(); duels.add(s); user_game[a] = user_game[b] = s
    def extra(s, i): return {}
    def snap(s, i):
        e = time.time() - s.t0; c = list(s.clock)
        if not s.over: c[s.turn] = max(0, c[s.turn] - e)
        d = dict(t="state", game=s.kind, you=i, turn=s.turn, stake=s.stake, names=[nm(u) for u in s.p], clock=c,
                 mv=max(0, MOVE_LIMIT - e), over=s.over, prize=int(s.stake * 2 * cfg("win_share") / 100))
        d.update(s.extra(i)); return d
    async def bcast(s):
        for i, u in enumerate(s.p): await send(u, s.snap(i))
    async def move(s, uid, m):
        if s.over or uid not in s.p: return
        i = s.p.index(uid)
        if i != s.turn: return
        e = time.time() - s.t0
        if e > MOVE_LIMIT or s.clock[i] - e <= 0: return await s.end(1 - i, "Vaqt tugadi")
        r = s.apply(i, m)
        if r is None: return await send(uid, s.snap(i))
        s.clock[i] -= e; s.t0 = time.time()
        if "end" in r: return await s.end(r["end"], r.get("why", ""))
        if r.get("switch", True): s.turn = 1 - s.turn
        await s.bcast()
    async def check_time(s):
        e = time.time() - s.t0
        if not s.over and (e >= MOVE_LIMIT or s.clock[s.turn] - e <= 0): await s.end(1 - s.turn, "Vaqt tugadi")
    async def end(s, w, why=""):                       # w: g'olib indeksi, -1 = durang
        if s.over: return
        s.over = True; duels.discard(s)
        prize = 0
        if w == -1:
            for u in s.p: add(u, s.stake)
        else:
            prize = int(s.stake * 2 * cfg("win_share") / 100); add(s.p[w], prize)
            run("UPDATE users SET wins=wins+1 WHERE id=:i", i=s.p[w]); run("UPDATE users SET losses=losses+1 WHERE id=:i", i=s.p[1 - w])
        for i, u in enumerate(s.p):
            user_game.pop(u, None)
            await send(u, s.snap(i))
            await send(u, dict(t="end", game=s.kind, res="draw" if w == -1 else "win" if w == i else "lose", prize=prize if w == i else 0, why=why))

class Chess(Duel):
    kind = "chess"
    def setup(s): s.b = chess.Board(); s.last = None
    def extra(s, i):
        lm = {}
        if s.turn == i and not s.over:
            for m in s.b.legal_moves: lm.setdefault(chess.square_name(m.from_square), []).append(chess.square_name(m.to_square))
        return dict(fen=s.b.fen(), lm=lm, last=s.last, check=s.b.is_check())
    def apply(s, i, m):
        try:
            mv = chess.Move.from_uci(str(m["from"]) + str(m["to"])); p = s.b.piece_at(mv.from_square)
            if p and p.piece_type == chess.PAWN and chess.square_rank(mv.to_square) in (0, 7): mv.promotion = chess.QUEEN
            if mv not in s.b.legal_moves: return None
        except Exception: return None
        s.b.push(mv); s.last = [m["from"], m["to"]]
        if s.b.is_checkmate(): return dict(end=i, why="Mot!")
        if s.b.is_game_over(): return dict(end=-1, why="Durang")
        return {}

DIRS = [(-1, -1), (-1, 1), (1, -1), (1, 1)]
def inb(r, c): return 0 <= r < 8 and 0 <= c < 8
def sd(v): return (v > 0) - (v < 0)

def caps(b, r, c):
    v = b[r][c]; s = sd(v); out = []
    for dr, dc in DIRS:
        if abs(v) == 1:
            r1, c1, r2, c2 = r + dr, c + dc, r + 2 * dr, c + 2 * dc
            if inb(r2, c2) and sd(b[r1][c1]) == -s and b[r2][c2] == 0: out.append((r2, c2, (r1, c1)))
        else:
            rr, cc = r + dr, c + dc
            while inb(rr, cc) and b[rr][cc] == 0: rr += dr; cc += dc
            if inb(rr, cc) and sd(b[rr][cc]) == -s:
                cap = (rr, cc); r2, c2 = rr + dr, cc + dc
                while inb(r2, c2) and b[r2][c2] == 0: out.append((r2, c2, cap)); r2 += dr; c2 += dc
    return out

def simple(b, r, c):
    v = b[r][c]; s = sd(v); out = []
    for dr, dc in DIRS:
        if abs(v) == 1:
            if dr == -s and inb(r + dr, c + dc) and b[r + dr][c + dc] == 0: out.append((r + dr, c + dc, None))
        else:
            rr, cc = r + dr, c + dc
            while inb(rr, cc) and b[rr][cc] == 0: out.append((rr, cc, None)); rr += dr; cc += dc
    return out

def dlegal(b, s, chain):
    if chain:
        m = caps(b, *chain); return {chain: m} if m else {}
    pcs = [(r, c) for r in range(8) for c in range(8) if sd(b[r][c]) == s]
    allc = {p: caps(b, *p) for p in pcs if caps(b, *p)}
    return allc or {p: simple(b, *p) for p in pcs if simple(b, *p)}

class Draughts(Duel):
    kind = "dr"
    def setup(s):
        s.b = [[(-1 if r < 3 else 1 if r > 4 else 0) if (r + c) % 2 == 1 else 0 for c in range(8)] for r in range(8)]
        s.chain = None
    def extra(s, i):
        lm = []
        if s.turn == i and not s.over:
            for (fr, fc), ms in dlegal(s.b, 1 if i == 0 else -1, s.chain).items(): lm += [[fr, fc, m[0], m[1]] for m in ms]
        return dict(b=s.b, lm=lm, chain=s.chain)
    def apply(s, i, m):
        sg = 1 if i == 0 else -1
        try: fr, fc = m["f"]; tr, tc = m["to"]
        except Exception: return None
        L = dlegal(s.b, sg, s.chain); mv = next((x for x in L.get((fr, fc), []) if (x[0], x[1]) == (tr, tc)), None)
        if not mv: return None
        b = s.b; v = b[fr][fc]; b[fr][fc] = 0
        if mv[2]: b[mv[2][0]][mv[2][1]] = 0
        if abs(v) == 1 and tr == (0 if sg == 1 else 7): v = 2 * sg
        b[tr][tc] = v
        s.quiet = 0 if (mv[2] or abs(v) == 1) else s.quiet + 1
        if mv[2] and caps(b, tr, tc): s.chain = (tr, tc); return {"switch": False}
        s.chain = None
        if not dlegal(b, -sg, None): return dict(end=i, why="Raqibda yurish qolmadi")
        if s.quiet >= 60: return dict(end=-1, why="Durang")
        return {}

class Connect4(Duel):
    kind = "c4"
    def setup(s): s.b = [[0] * 7 for _ in range(6)]
    def extra(s, i): return dict(b=s.b)
    def apply(s, i, m):
        try: col = int(m["col"]); assert 0 <= col < 7
        except Exception: return None
        for r in range(5, -1, -1):
            if s.b[r][col] == 0: s.b[r][col] = i + 1; break
        else: return None
        k = i + 1
        for r in range(6):
            for c in range(7):
                for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                    if all(0 <= r + dr * j < 6 and 0 <= c + dc * j < 7 and s.b[r + dr * j][c + dc * j] == k for j in range(4)):
                        return dict(end=i, why="4 ta qator!")
        if all(s.b[0]): return dict(end=-1, why="Taxta to'ldi")
        return {}

DUELS = {"chess": Chess, "dr": Draughts, "c4": Connect4}

# ------------------------------------------------------------------ Snake Lite (5-10 kishi, doira arena)
class SnakeG:
    def __init__(s, players, stake):
        s.p = players; s.stake = stake; s.over = False; s.tick = 0; s.food = {}; s.fid = 0; s.fa = []; s.fr = []
        s.t_start = time.time() + SN_GO; s.t_end = s.t_start + SN_DUR; s.sn = []; n = len(players)
        for k, u in enumerate(players):
            ang = 2 * math.pi * k / n; x, y = SN_R * .7 * math.cos(ang), SN_R * .7 * math.sin(ang); a = ang + math.pi
            tr = collections.deque((x - math.cos(a) * STEP * j, y - math.sin(a) * STEP * j) for j in range(24))
            s.sn.append(dict(u=u, x=x, y=y, a=a, ta=a, tr=tr, L=12.0, alive=True, score=0, died=0))
        for _ in range(FOOD_N): s.add_food()
        s.fa = []; snakes.add(s)
        for u in players: user_game[u] = s
    def add_food(s, x=None, y=None, v=1):
        if x is None:
            r = SN_R * .95 * math.sqrt(random.random()); th = random.random() * 2 * math.pi; x, y = r * math.cos(th), r * math.sin(th)
        if x * x + y * y > (SN_R * .97) ** 2: return
        s.fid += 1; s.food[s.fid] = (x, y, v); s.fa.append([s.fid, int(x), int(y), v])
    def kill(s, k):
        z = s.sn[k]; z["alive"] = False; z["died"] = time.time()
        for px, py in list(z["tr"])[::3]: s.add_food(px + random.uniform(-6, 6), py + random.uniform(-6, 6), 1)
    def step(s):
        if s.over or time.time() < s.t_start: return
        s.tick += 1; dying = set()
        for k, z in enumerate(s.sn):
            if not z["alive"]: continue
            d = (z["ta"] - z["a"] + math.pi) % (2 * math.pi) - math.pi
            z["a"] += max(-TURN, min(TURN, d))
            z["x"] += math.cos(z["a"]) * STEP; z["y"] += math.sin(z["a"]) * STEP
            z["tr"].appendleft((z["x"], z["y"]))
            while len(z["tr"]) > 2 * int(z["L"]): z["tr"].pop()
            if z["x"] ** 2 + z["y"] ** 2 > SN_R ** 2: dying.add(k); continue
            for fid, (fx, fy, v) in list(s.food.items()):
                if (fx - z["x"]) ** 2 + (fy - z["y"]) ** 2 < EAT2:
                    del s.food[fid]; s.fr.append(fid); z["score"] += v; z["L"] += .5 * v
        for k, z in enumerate(s.sn):
            if not z["alive"] or k in dying: continue
            for j, o in enumerate(s.sn):
                if j != k and o["alive"] and any((px - z["x"]) ** 2 + (py - z["y"]) ** 2 < HIT2 for px, py in list(o["tr"])[::2]):
                    dying.add(k); break
        for k in dying: s.kill(k)
        while len(s.food) < FOOD_N: s.add_food()
    def done(s):
        return time.time() >= s.t_end or sum(z["alive"] for z in s.sn) <= 1
    async def bcast(s):
        now = time.time()
        d = {"t": "sn", "left": max(0, int(s.t_end - now)), "go": max(0, math.ceil(s.t_start - now)),
             "s": [[int(z["alive"]), z["score"], int(z["x"]), int(z["y"]), round(z["a"], 2), [int(c) for p in list(z["tr"])[::2] for c in p]] for z in s.sn],
             "fa": s.fa, "fr": s.fr}
        if s.tick % 100 == 0: d["ff"] = [[i, int(f[0]), int(f[1]), f[2]] for i, f in s.food.items()]
        s.fa = []; s.fr = []; msg = json.dumps(d, separators=(",", ":"))
        for u in s.p:
            ws = clients.get(u)
            if ws:
                try: await ws.send_text(msg)
                except Exception: pass
    def start_msg(s, u):
        return dict(t="snstart", you=s.p.index(u), names=[nm(x) for x in s.p], R=SN_R, stake=s.stake, dur=SN_DUR,
                    food=[[i, int(f[0]), int(f[1]), f[2]] for i, f in s.food.items()])
    def resign(s, u):
        k = s.p.index(u)
        if s.sn[k]["alive"]: s.kill(k)
    async def end(s):
        if s.over: return
        s.over = True; snakes.discard(s); n = len(s.p)
        order = sorted(range(n), key=lambda k: (s.sn[k]["alive"], s.sn[k]["score"], s.sn[k]["died"]), reverse=True)
        pool = int(s.stake * n * cfg("win_share") / 100); prizes = {}
        for r, k in enumerate(order[:3]):
            prizes[k] = int(pool * (.5, .3, .2)[r]); add(s.p[k], prizes[k]); run("UPDATE users SET wins=wins+1 WHERE id=:i", i=s.p[k])
        for k in order[3:]: run("UPDATE users SET losses=losses+1 WHERE id=:i", i=s.p[k])
        top = [dict(n=nm(s.p[k]), s=s.sn[k]["score"], p=prizes.get(k, 0)) for k in order]
        await s.bcast()
        for k, u in enumerate(s.p):
            user_game.pop(u, None)
            await send(u, dict(t="end", game="sn", res="win" if k in prizes else "lose", prize=prizes.get(k, 0), rank=order.index(k) + 1, top=top))

class Lobby:
    def __init__(s, stake): s.stake = stake; s.p = []; s.first = 0; s.cd = None
    async def info(s):
        now = time.time()
        for u in s.p:
            await send(u, dict(t="lobby", n=len(s.p), min=SN_MIN, max=SN_MAX, stake=s.stake,
                               cd=max(0, int(s.cd - now)) if s.cd else -1, wait=max(0, int(s.first + LOBBY_WAIT - now))))
    async def join(s, uid):
        if not take(uid, s.stake): return await send(uid, dict(t="err", m="Tanga yetarli emas"))
        s.p.append(uid); in_queue[uid] = ("sn", s.stake)
        if len(s.p) == 1: s.first = time.time()
        if len(s.p) >= SN_MIN and not s.cd: s.cd = time.time() + SN_COUNTDOWN
        if len(s.p) >= SN_MAX: return await s.start()
        await s.info()
    async def leave(s, uid):
        if uid in s.p:
            s.p.remove(uid); add(uid, s.stake); in_queue.pop(uid, None)
            if len(s.p) < SN_MIN: s.cd = None
            await s.info()
    async def start(s):
        g = SnakeG(list(s.p), s.stake)
        for u in s.p: in_queue.pop(u, None)
        s.p = []; s.cd = None
        for u in g.p: await send(u, g.start_msg(u))
    async def tick(s):
        if not s.p: return
        now = time.time()
        if len(s.p) < SN_MIN and now - s.first > LOBBY_WAIT:
            for u in s.p:
                add(u, s.stake); in_queue.pop(u, None); await send(u, dict(t="lobby_cancel"))
            s.p = []; s.cd = None; return
        if s.cd and now >= s.cd and len(s.p) >= SN_MIN: return await s.start()
        await s.info()

lobbies = {st: Lobby(st) for st in STAKES}

# ------------------------------------------------------------------ KONKUR (Shashka, olib tashlash turniri)
class ContestDr(Draughts):
    cb = None
    async def end(s, w, why=""):
        if s.over: return
        if w == -1:                                   # durang bo'lmaydi: dona ko'pi yutadi
            cn = [sum(1 for r in s.b for v in r if v and (v > 0) == (i == 0)) for i in (0, 1)]
            w = 0 if cn[0] > cn[1] else 1 if cn[1] > cn[0] else random.randint(0, 1); why = "Durang: dona ko'pi hisobida"
        s.over = True; duels.discard(s)
        for i, u in enumerate(s.p):
            user_game.pop(u, None); await send(u, s.snap(i))
            await send(u, dict(t="cres", res="win" if w == i else "lose", why=why))
        await s.cb(s.p[w], s.p[1 - w])

class Tourney:
    def __init__(s, cur, amt): s.cur, s.amt = cur, amt; s.reset()
    def reset(s):
        s.p = []; s.state = "lobby"; s.first = 0; s.cd = None; s.alive = []; s.places = {}; s.nxt = []; s.los = []
        s.wait = 0; s.bronze = None; s.semi = False; s.tot = 0
    async def info(s):
        now = time.time()
        for u in s.p:
            await send(u, dict(t="clobby", n=len(s.p), min=C_MIN, max=C_MAX, amt=s.amt, cur=s.cur,
                               cd=max(0, int(s.cd - now)) if s.cd else -1))
    async def add_player(s, uid):
        if s.state != "lobby" or uid in s.p: return False
        s.p.append(uid); in_contest.add(uid)
        if len(s.p) == 1: s.first = time.time()
        if len(s.p) >= C_MIN and not s.cd: s.cd = time.time() + C_CD
        if len(s.p) >= C_MAX: await s.start()
        else: await s.info()
        return True
    async def tick(s):
        if s.state != "lobby" or not s.p: return
        now = time.time()
        if len(s.p) < C_MIN and now - s.first > C_WAIT:
            for u in s.p:
                in_contest.discard(u)
                if s.cur == "coin": add(u, s.amt)
                await send(u, dict(t="cmsg", m="Konkur bekor bo'ldi, " + ("tanga qaytarildi" if s.cur == "coin" else "admin pulni qaytaradi")))
            if s.cur == "som": await notify_admins(f"⚠️ Konkur {s.amt:,} so'm bekor bo'ldi. Pulni qaytaring: " + ", ".join(map(str, s.p)))
            return s.reset()
        if s.cd and now >= s.cd and len(s.p) >= C_MIN: return await s.start()
        await s.info()
    async def start(s):
        s.state = "run"; s.alive = list(s.p); s.tot = len(s.p); asyncio.create_task(s.round())
    async def round(s):
        a = s.alive[:]; random.shuffle(a); n = len(a); ms = []; s.nxt = []; s.los = []; s.semi = False
        if n == 2:
            ms = [(a[0], a[1], "final")]
            if s.bronze: ms.append((s.bronze[0], s.bronze[1], "bronze")); s.bronze = None
        elif n == 4:
            s.semi = True; ms = [(a[0], a[1], "semi"), (a[2], a[3], "semi")]
        else:
            if n % 2:
                bye = a.pop()
                if n in (3, 5):                        # 3 yoki 5 kishi: juftsiz o'yinchi avtomatik 3-o'rin
                    s.places[3] = bye; await send(bye, dict(t="cmsg", m="🥉 Siz 3-o'rinni avtomatik oldingiz!"))
                else:
                    s.nxt.append(bye); await send(bye, dict(t="cmsg", m="Juftingiz yo'q, keyingi bosqichga o'tdingiz"))
            tag = "final" if n == 3 else "normal"
            ms = [(a[i], a[i + 1], tag) for i in range(0, len(a), 2)]
        s.wait = len(ms)
        for x, y, tag in ms: asyncio.create_task(s.launch(x, y, tag))
    async def launch(s, x, y, tag):
        ox, oy = x in clients, y in clients
        if not (ox and oy):                            # ulanmagan o'yinchi yutqazadi
            w, l = (x, y) if ox else (y, x) if oy else random.sample([x, y], 2)
            return await s.result(w, l, tag)
        g = ContestDr(x, y, 0); g.cb = lambda w, l: s.result(w, l, tag); await g.bcast()
    async def result(s, w, l, tag):
        if tag == "final": s.places[1] = w; s.places[2] = l
        elif tag == "bronze": s.places[3] = w
        else:
            s.nxt.append(w)
            if tag == "semi": s.los.append(l)
            else: in_contest.discard(l); await send(l, dict(t="cmsg", m="Konkurdan chiqdingiz"))
        s.wait -= 1
        if s.wait: return
        if {1, 2, 3} <= set(s.places): return await s.finish()
        if s.semi: s.bronze = tuple(s.los)
        s.alive = s.nxt; await asyncio.sleep(5)
        for u in s.alive: await send(u, dict(t="cmsg", m="Keyingi raund boshlanmoqda..."))
        await s.round()
    async def finish(s):
        pool = int(s.tot * s.amt * cfg("contest_share") / 100); lines = []
        for pl, sh in ((1, .5), (2, .3), (3, .2)):
            u = s.places[pl]; pr = int(pool * sh); lines.append(f"{pl}-o'rin: {nm(u)} ({u}) — {pr:,}")
            if s.cur == "coin": add(u, pr)
            run("UPDATE users SET wins=wins+1 WHERE id=:i", i=u)
            await send(u, dict(t="cfin", place=pl, prize=pr, cur=s.cur))
            try: await bot.send_message(u, f"🏆 Konkur: {pl}-o'rin! Yutuq: {pr:,} " + ("tanga" if s.cur == "coin" else "so'm. Admin siz bilan bog'lanadi"))
            except Exception: pass
        if s.cur == "som": await notify_admins(f"💵 Konkur {s.amt:,} so'm tugadi. Yutuqlarni to'lang:\n" + "\n".join(lines))
        for u in s.p: in_contest.discard(u)
        s.reset()

for _a in CSTAKES: tourneys[("coin", _a)] = Tourney("coin", _a)
for _a in CSOM: tourneys[("som", _a)] = Tourney("som", _a)

# ------------------------------------------------------------------ Matchmaking
async def enqueue(uid, game, stake):
    if uid in user_game or uid in in_queue or uid in in_contest: return await send(uid, dict(t="err", m="Siz allaqachon o'yin/navbat/konkurdasiz"))
    if stake not in STAKES or game not in ("chess", "dr", "c4", "sn") or not cfg("on_" + game):
        return await send(uid, dict(t="err", m="Bu o'yin hozir mavjud emas"))
    if await need_sub(uid): return await send(uid, dict(t="err", m="Avval kanallarga obuna bo'ling"))
    row = one("SELECT coins FROM users WHERE id=:i", i=uid)
    if not row or row["coins"] < stake: return await send(uid, dict(t="err", m="Tanga yetarli emas"))
    if game == "sn": return await lobbies[stake].join(uid)
    q = queues.setdefault((game, stake), []); q[:] = [x for x in q if x in clients and x not in user_game]
    if not q:
        q.append(uid); in_queue[uid] = (game, stake); return await send(uid, dict(t="queued", game=game, stake=stake))
    opp = q.pop(0); in_queue.pop(opp, None)
    if not take(opp, stake):
        q.append(uid); in_queue[uid] = (game, stake); return await send(uid, dict(t="queued", game=game, stake=stake))
    if not take(uid, stake):
        add(opp, stake); return await send(uid, dict(t="err", m="Tanga yetarli emas"))
    pl = [opp, uid]; random.shuffle(pl)
    g = DUELS[game](pl[0], pl[1], stake); await g.bcast()

async def cancel(uid):
    k = in_queue.pop(uid, None)
    if not k: return
    if k[0] == "sn": in_queue[uid] = k; await lobbies[k[1]].leave(uid)
    else:
        q = queues.get(k, [])
        if uid in q: q.remove(uid)
    await send(uid, dict(t="cancelled"))

async def handle(uid, m):
    t = m.get("t")
    if t == "queue": await enqueue(uid, m.get("game"), int(m.get("stake", 0)))
    elif t == "cancel": await cancel(uid)
    elif t == "move":
        g = user_game.get(uid)
        if isinstance(g, Duel): await g.move(uid, m)
    elif t == "angle":
        g = user_game.get(uid)
        if isinstance(g, SnakeG) and not g.over:
            try: g.sn[g.p.index(uid)]["ta"] = float(m["a"])
            except Exception: pass
    elif t == "resign":
        g = user_game.get(uid)
        if isinstance(g, Duel): await g.end(1 - g.p.index(uid), "Taslim bo'lindi")
        elif isinstance(g, SnakeG): g.resign(uid)
    elif t == "chat":
        txt = str(m.get("text", "")).strip()[:200]
        if txt and time.time() - chat_last.get(uid, 0) > 1:
            chat_last[uid] = time.time(); msg = dict(t="chat", uid=uid, name=nm(uid), text=txt, ts=int(time.time()))
            chat_log.append(msg)
            for u in list(clients): await send(u, msg)
    elif t == "chat_hist": await send(uid, dict(t="chat_hist", m=list(chat_log)))
    elif t == "cjoin":
        T = tourneys.get((m.get("cur"), int(m.get("amt", 0))))
        if not T or T.cur != "coin": return await send(uid, dict(t="err", m="Noto'g'ri konkur"))
        if uid in user_game or uid in in_queue or uid in in_contest: return await send(uid, dict(t="err", m="Siz allaqachon o'yinda/konkurdasiz"))
        if await need_sub(uid): return await send(uid, dict(t="err", m="Avval kanallarga obuna bo'ling"))
        if T.state != "lobby": return await send(uid, dict(t="err", m="Bu konkur boshlanib bo'lgan"))
        if not take(uid, T.amt): return await send(uid, dict(t="err", m="Tanga yetarli emas"))
        await T.add_player(uid)
    elif t == "cleave":
        for T in tourneys.values():
            if T.cur == "coin" and T.state == "lobby" and uid in T.p:
                T.p.remove(uid); in_contest.discard(uid); add(uid, T.amt)
                await send(uid, dict(t="cancelled")); await T.info()
    elif t == "sync":
        g = user_game.get(uid)
        if isinstance(g, Duel): await send(uid, g.snap(g.p.index(uid)))
        elif isinstance(g, SnakeG): await send(uid, g.start_msg(uid))

async def game_loop():
    n = 0
    while True:
        t0 = time.time(); n += 1
        try:
            for g in list(snakes):
                g.step()
                if g.done(): await g.end()
                elif n % 2 == 0: await g.bcast()
            if n % 10 == 0:
                for g in list(duels): await g.check_time()
            if n % 20 == 0:
                for lb in lobbies.values(): await lb.tick()
                for T in list(tourneys.values()): await T.tick()
            if n % 200 == 0:
                for u in list(clients): await send(u, dict(t="online", n=len(clients)))
        except Exception: log.exception("loop")
        await asyncio.sleep(max(0, 0.05 - (time.time() - t0)))

# ------------------------------------------------------------------ FastAPI
@asynccontextmanager
async def lifespan(app):
    init_db(); tasks = [asyncio.create_task(game_loop())]
    try:
        await bot.delete_webhook()
        if WEBAPP_URL: await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎮 O'ynash", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception: log.exception("bot setup")
    tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
    yield
    for t in tasks: t.cancel()

app = FastAPI(lifespan=lifespan)
if os.path.isdir("static"): app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def index(): return FileResponse("index.html")

@app.get("/health")
async def health(): return {"ok": True}

@app.websocket("/ws")
async def ws_ep(ws: WebSocket, init: str = ""):
    await ws.accept(); u = check_init(init)
    if not u: return await ws.close(4001)
    uid = ensure_user(u)["id"]; old = clients.get(uid); clients[uid] = ws
    if old:
        try: await old.close()
        except Exception: pass
    await send(uid, dict(t="hello", online=len(clients)))
    g = user_game.get(uid)
    if g: await handle(uid, {"t": "sync"})
    try:
        while True: await handle(uid, json.loads(await ws.receive_text()))
    except WebSocketDisconnect: pass
    except Exception: log.exception("ws")
    finally:
        if clients.get(uid) is ws:
            clients.pop(uid, None)
            if uid in in_queue: await cancel(uid)

@app.get("/api/me")
async def api_me(u=Depends(auth)):
    ns = await need_sub(u["id"])
    if not ns: await pay_ref(u)
    u = one("SELECT * FROM users WHERE id=:i", i=u["id"]); me = await bot.me()
    return dict(user=u, level=level(u), need_sub=ns, admin=u["id"] in ADMINS, online=len(clients), bot=me.username,
                ref_link=f"https://t.me/{me.username}?start={u['id']}", stakes=STAKES, packs=PACKS,
                cfg={k: cfg(k) for k in DEFAULTS}, in_game=u["id"] in user_game)

@app.get("/api/rating")
async def api_rating(kind: str = "coins", u=Depends(auth)):
    ex = {"coins": "coins", "level": "(wins*10+losses*2)", "wins": "wins"}.get(kind, "coins")
    rows = run(f"SELECT id,name,coins,wins,losses FROM users ORDER BY {ex} DESC LIMIT 20")
    for r in rows: r["val"] = r["coins"] if kind == "coins" else level(r) if kind == "level" else r["wins"]
    me = one(f"SELECT COUNT(*)+1 AS n FROM users WHERE {ex} > (SELECT {ex} FROM users WHERE id=:i)", i=u["id"])["n"]
    return dict(rows=rows, me=me)

# ---------------- Vazifalar (har kuni 1 marta)
def today(): return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 18000))
TSTART = {}

@app.get("/api/tasks")
async def api_tasks(u=Depends(auth)):
    done = {r["task"] for r in run("SELECT task FROM done2 WHERE uid=:i AND day=:d", i=u["id"], d=today())}
    ts = [dict(id=c["id"], title=c["title"], reward=c["reward"], kind=c["kind"], url=tlink(c["chat"]), done=c["id"] in done)
          for c in run("SELECT * FROM channels WHERE kind IN ('task','link','video') ORDER BY id")]
    return dict(tasks=ts, refs=u["refs"], reward=cfg("ref_reward"))

@app.post("/api/tasks/start")
async def api_tstart(b: dict, u=Depends(auth)):
    TSTART[(u["id"], int(b["id"]))] = time.time(); return {"ok": True}

@app.post("/api/tasks/claim")
async def api_claim(b: dict, u=Depends(auth)):
    c = one("SELECT * FROM channels WHERE id=:i AND kind IN ('task','link','video')", i=int(b["id"]))
    if not c: raise HTTPException(404)
    if one("SELECT 1 AS x FROM done2 WHERE uid=:u AND task=:t AND day=:d", u=u["id"], t=c["id"], d=today()):
        return {"ok": False, "m": "Bugun bajarilgan"}
    st = TSTART.get((u["id"], c["id"]))
    if not st or time.time() - st < (4 if c["kind"] == "task" else 3): return {"ok": False, "m": "Avval havolani oching, biroz kuting"}
    if c["kind"] == "task":
        try: ok = (await bot.get_chat_member(c["chat"], u["id"])).status not in ("left", "kicked")
        except Exception: ok = False
        if not ok: return {"ok": False, "m": "Avval kanalga obuna bo'ling"}
    run("INSERT INTO done2(uid,task,day) VALUES(:u,:t,:d)", u=u["id"], t=c["id"], d=today())
    TSTART.pop((u["id"], c["id"]), None); add(u["id"], c["reward"])
    return {"ok": True, "m": f"+{c['reward']} tanga!"}

@app.get("/api/shop")
async def api_shop(u=Depends(auth)): return run("SELECT * FROM shop ORDER BY id")

@app.post("/api/shop/buy")
async def api_buy(b: dict, u=Depends(auth)):
    it = one("SELECT * FROM shop WHERE id=:i", i=int(b["id"]))
    if not it: raise HTTPException(404)
    if not take(u["id"], it["price"]): return {"ok": False, "m": "Tanga yetarli emas"}
    await notify_admins(f"🛒 Buyurtma: {it['icon']} {it['title']} ({it['price']} tanga)\n{u['name']} | ID {u['id']} | @{u['username'] or '-'}")
    return {"ok": True, "m": "Buyurtma qabul qilindi, admin tez orada bog'lanadi"}

@app.get("/api/deposit")
async def api_dep(u=Depends(auth)):
    cards = run("SELECT info FROM cards"); return dict(packs=PACKS, card=random.choice(cards)["info"] if cards else None)

@app.post("/api/deposit")
async def api_dep_post(b: dict, u=Depends(auth)):
    i = int(b["i"]); som, coins = PACKS[i]
    if one("SELECT COUNT(*) AS n FROM deposits WHERE uid=:u AND status='new'", u=u["id"])["n"] >= 3: return {"ok": False, "m": "Kutilayotgan so'rovlar ko'p"}
    raw = str(b.get("img", "")).split(",")[-1]
    try: img = base64.b64decode(raw)
    except Exception: return {"ok": False, "m": "Rasm noto'g'ri"}
    if not img or len(img) > 1_500_000: return {"ok": False, "m": "Rasm juda katta"}
    did = run("INSERT INTO deposits(uid,som,coins,img,ts) VALUES(:u,:s,:c,:m,:t) RETURNING id", u=u["id"], s=som, c=coins, m=raw, t=int(time.time()))[0]["id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"dep:ok:{did}"),
                                                InlineKeyboardButton(text="❌ Rad", callback_data=f"dep:no:{did}")]])
    await notify_admins(f"💰 Depozit #{did}\n{u['name']} ({u['id']})\n{som:,} so'm → {coins} tanga", kb, img)
    return {"ok": True, "m": "Chek yuborildi. Admin tasdiqlagach tanga qo'shiladi."}

@app.post("/api/withdraw")
async def api_wd(b: dict, u=Depends(auth)):
    n = int(b.get("coins", 0)); card = str(b.get("card", "")).strip()[:60]
    if n < cfg("min_withdraw"): return {"ok": False, "m": f"Minimal chiqarish: {cfg('min_withdraw')} tanga"}
    if len(card) < 8: return {"ok": False, "m": "Karta raqamini kiriting"}
    if not take(u["id"], n): return {"ok": False, "m": "Tanga yetarli emas"}
    som = n * cfg("coin_som")
    wid = run("INSERT INTO withdraws(uid,coins,som,card,ts) VALUES(:u,:c,:s,:k,:t) RETURNING id", u=u["id"], c=n, s=som, k=card, t=int(time.time()))[0]["id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ To'landi", callback_data=f"wd:ok:{wid}"),
                                                InlineKeyboardButton(text="❌ Rad", callback_data=f"wd:no:{wid}")]])
    await notify_admins(f"💸 Pul chiqarish #{wid}\n{u['name']} ({u['id']})\n{n} tanga = {som:,} so'm\nKarta: {card}", kb)
    return {"ok": True, "m": "So'rov adminga yuborildi."}

# ---------------- Konkur
@app.get("/api/contest")
async def api_contest(u=Depends(auth)):
    return dict(t=[dict(cur=T.cur, amt=T.amt, n=len(T.p), me=u["id"] in T.p, run=T.state == "run") for T in tourneys.values()],
                alive=u["id"] in in_contest, min=C_MIN, max=C_MAX, share=cfg("contest_share"))

@app.post("/api/contest/deposit")
async def api_cdep(b: dict, u=Depends(auth)):
    amt = int(b["amt"]); T = tourneys.get(("som", amt))
    if not T or T.state != "lobby": return {"ok": False, "m": "Bu konkur hozir yopiq"}
    if u["id"] in in_contest: return {"ok": False, "m": "Siz allaqachon konkurdasiz"}
    if one("SELECT COUNT(*) AS n FROM cdeps WHERE uid=:u AND status='new'", u=u["id"])["n"] >= 2: return {"ok": False, "m": "Kutilayotgan so'rovlar ko'p"}
    raw = str(b.get("img", "")).split(",")[-1]
    try: img = base64.b64decode(raw)
    except Exception: return {"ok": False, "m": "Rasm noto'g'ri"}
    if not img or len(img) > 1_500_000: return {"ok": False, "m": "Rasm juda katta"}
    did = run("INSERT INTO cdeps(uid,amt,img,ts) VALUES(:u,:a,:m,:t) RETURNING id", u=u["id"], a=amt, m=raw, t=int(time.time()))[0]["id"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"cd:ok:{did}"),
                                                InlineKeyboardButton(text="❌ Rad", callback_data=f"cd:no:{did}")]])
    await notify_admins(f"🏆 Konkur to'lovi #{did}\n{u['name']} ({u['id']})\n{amt:,} so'm", kb, img)
    return {"ok": True, "m": "Chek yuborildi. Admin tasdiqlagach konkurga qo'shilasiz."}

# ---------------- Admin
@app.get("/api/admin/stats")
async def a_stats(a=Depends(adm)):
    r = one("SELECT COUNT(*) AS n, COALESCE(SUM(coins),0) AS c FROM users")
    return dict(users=r["n"], coins=r["c"], online=len(clients), games=len(duels) + len(snakes),
                dep=one("SELECT COUNT(*) AS n FROM deposits WHERE status='new'")["n"],
                wd=one("SELECT COUNT(*) AS n FROM withdraws WHERE status='new'")["n"])

@app.get("/api/admin/users")
async def a_users(q: str = "", a=Depends(adm)):
    if q.isdigit(): return run("SELECT * FROM users WHERE id=:i", i=int(q))
    return run("SELECT * FROM users WHERE name LIKE :q OR username LIKE :q ORDER BY joined DESC LIMIT 30", q=f"%{q}%")

@app.post("/api/admin/user")
async def a_user(b: dict, a=Depends(adm)):
    i = int(b["id"]); act = b["act"]
    if act == "zero": run("UPDATE users SET coins=0 WHERE id=:i", i=i)
    elif act == "give": add(i, int(b["n"]))
    elif act == "take": run("UPDATE users SET coins=GREATEST(0,coins-:n) WHERE id=:i" if "postgres" in DB_URL else "UPDATE users SET coins=MAX(0,coins-:n) WHERE id=:i", n=int(b["n"]), i=i)
    return {"ok": True}

TABLES = {"channels": ("kind", "chat", "title", "reward"), "cards": ("info",), "shop": ("icon", "title", "price")}

@app.get("/api/admin/list/{tb}")
async def a_list(tb: str, a=Depends(adm)):
    if tb not in TABLES: raise HTTPException(404)
    return run(f"SELECT * FROM {tb} ORDER BY id")

@app.post("/api/admin/add/{tb}")
async def a_add(tb: str, b: dict, a=Depends(adm)):
    if tb not in TABLES: raise HTTPException(404)
    cols = TABLES[tb]; vals = {c: b.get(c, 0 if c in ("reward", "price") else "") for c in cols}
    run(f"INSERT INTO {tb}({','.join(cols)}) VALUES({','.join(':' + c for c in cols)})", **vals); return {"ok": True}

@app.post("/api/admin/del/{tb}")
async def a_del(tb: str, b: dict, a=Depends(adm)):
    if tb not in TABLES: raise HTTPException(404)
    run(f"DELETE FROM {tb} WHERE id=:i", i=int(b["id"])); return {"ok": True}

@app.get("/api/admin/deposits")
async def a_deps(a=Depends(adm)):
    return run("SELECT d.id,d.uid,d.som,d.coins,d.status,d.ts,u.name FROM deposits d LEFT JOIN users u ON u.id=d.uid ORDER BY (d.status='new') DESC, d.id DESC LIMIT 40")

@app.get("/api/admin/receipt/{i}")
async def a_receipt(i: int, init: str = ""):
    u = check_init(init)
    if not u or u["id"] not in ADMINS: raise HTTPException(403)
    r = one("SELECT img FROM deposits WHERE id=:i", i=i)
    return Response(base64.b64decode(r["img"]) if r and r["img"] else b"", media_type="image/jpeg")

@app.post("/api/admin/deposit")
async def a_dep_do(b: dict, a=Depends(adm)): return {"ok": await process_deposit(int(b["id"]), bool(b["ok"]))}

@app.get("/api/admin/withdraws")
async def a_wds(a=Depends(adm)):
    return run("SELECT w.*,u.name FROM withdraws w LEFT JOIN users u ON u.id=w.uid ORDER BY (w.status='new') DESC, w.id DESC LIMIT 40")

@app.post("/api/admin/withdraw")
async def a_wd_do(b: dict, a=Depends(adm)): return {"ok": await process_withdraw(int(b["id"]), bool(b["ok"]))}

@app.get("/api/admin/settings")
async def a_set(a=Depends(adm)): return {k: cfg(k) for k in DEFAULTS}

@app.post("/api/admin/settings")
async def a_set_post(b: dict, a=Depends(adm)):
    for k, v in b.items():
        if k in DEFAULTS:
            v = str(int(v)); run("DELETE FROM settings WHERE k=:k", k=k); run("INSERT INTO settings(k,v) VALUES(:k,:v)", k=k, v=v); SETTINGS[k] = v
    return {"ok": True}

@app.post("/api/admin/reset_rating")
async def a_reset(a=Depends(adm)): run("UPDATE users SET wins=0, losses=0"); return {"ok": True}

@app.post("/api/admin/broadcast")
async def a_cast(b: dict, a=Depends(adm)):
    txt = str(b.get("text", ""))[:3000]
    async def go():
        for r in run("SELECT id FROM users"):
            try: await bot.send_message(r["id"], txt, reply_markup=app_kb() if WEBAPP_URL else None)
            except Exception: pass
            await asyncio.sleep(0.05)
    asyncio.create_task(go()); return {"ok": True}
