"""
Sura Sushi — бот чеклистов (@sura_checklist_bot). v0.1

Чеклисты, расписание и люди — в Google-таблице «Сура — Чеклисты (бот)» (читает через Apps Script).
Бот по расписанию присылает чеклист в тему «Чеклисты» группы точки, проверяющий проходит блоки кнопками,
бот считает баллы и % и пишет каждую проверку в лист «Журнал».

Владелец (ADMIN_IDS):
  в личке:  /tablica URL — адрес Apps Script таблицы · /obnovit — перечитать таблицу · /status · /help
            прислать bot.py или .zip с ним — обновление с откатом при ошибке
  в группе: /tut 17 · /tut 353 — эта тема получает чеклисты точки · /tut_off
            /kontrol — этот чат получает итоги проверок · /test [17|353] — прислать чеклист сейчас
"""
import io
import json
import os
import py_compile
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone

import requests

VERSION = "checklist 0.1 (12.11.2026)"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data.json")
LOCK = threading.RLock()

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


def load_env():
    env = {}
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
TOKEN = ENV["TG_BOT_TOKEN"]
ADMINS = {x.strip() for x in ENV.get("ADMIN_IDS", "").split(",") if x.strip()}
TG_BASE = ENV.get("TG_API_URL", "https://api.telegram.org").rstrip("/")
API = f"{TG_BASE}/bot{TOKEN}"
POINTS = {"17": "17 мкр", "353": "3 мкр"}
POINT_ALIAS = {"17": "17", "17мкр": "17", "353": "353", "3": "353", "3мкр": "353", "353мкр": "353"}
DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ───────────────────────── утилиты ─────────────────────────
def msk_now():
    return datetime.now(timezone.utc) + timedelta(hours=3)


def esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def num(s):
    return float(str(s).replace(",", ".").replace(" ", ""))


def fmt(x):
    return (f"{x:.1f}".rstrip("0").rstrip(".")).replace(".", ",")


_S = threading.local()


def tg(method, **kw):
    s = getattr(_S, "s", None) or requests.Session()
    _S.s = s
    timeout = 70 if method == "getUpdates" else (10, 30)
    for attempt in range(2):
        try:
            r = s.post(f"{API}/{method}", timeout=timeout, **kw).json()
        except Exception as e:
            print(f"tg error {method}:", e)
            _S.s = None
            s = requests.Session()
            if attempt == 0 and method != "getUpdates":
                continue
            return {}
        wait = (r.get("parameters") or {}).get("retry_after")
        if r.get("error_code") == 429 and wait and wait <= 10 and attempt == 0:
            time.sleep(wait)
            continue
        if not r.get("ok") and method != "getUpdates":
            print(f"tg {method}: {str(r)[:200]}")
        return r
    return {}


def send(chat, text, markup=None, thread=None):
    body = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup:
        body["reply_markup"] = markup
    if thread:
        body["message_thread_id"] = thread
    return (tg("sendMessage", json=body).get("result") or {}).get("message_id")


def edit(chat, mid, text, markup=None):
    r = tg("editMessageText", json={"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML",
                                    "disable_web_page_preview": True,
                                    "reply_markup": markup or {"inline_keyboard": []}})
    return r.get("ok") or "not modified" in str(r.get("description", ""))


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows if row]}


def dm_admins(text):
    for a in ADMINS:
        send(a, text)


# ───────────────────────── хранение ─────────────────────────
def load():
    with LOCK:
        try:
            with open(DATA, encoding="utf-8") as fh:
                d = json.load(fh)
        except FileNotFoundError:
            d = {}
        except Exception as e:
            bad = f"{DATA}.broken-{int(time.time())}"
            try:
                os.replace(DATA, bad)
            except OSError:
                pass
            print(f"data.json не читается ({e}), отложил в {bad}")
            d = {}
        for k, v in (("binds", {}), ("control", []), ("sessions", {}), ("sent", {}), ("sheet", {}),
                     ("sheet_url", ""), ("pending_rows", []), ("warned", {})):
            d.setdefault(k, v)
        return d


def save(d):
    with LOCK:
        tmp = DATA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, DATA)


def update(fn):
    with LOCK:
        d = load()
        out = fn(d)
        save(d)
        return out


# ───────────────────────── Google-таблица ─────────────────────────
def sheet_url():
    return load().get("sheet_url") or ENV.get("SHEET_URL", "")


def fetch_sheet():
    """Скачать листы Чеклисты/Расписание/Люди. → (ok, текст ошибки)."""
    url = sheet_url()
    if not url:
        return False, "нет адреса таблицы — пришли /tablica URL"
    try:
        r = requests.get(url, timeout=60)
        data = r.json()
        if "Чеклисты" not in data:
            return False, f"таблица ответила без листа «Чеклисты»: {r.text[:120]}"
    except Exception as e:
        return False, f"таблица не ответила: {e}"
    update(lambda d: d.update({"sheet": data, "sheet_ts": msk_now().isoformat()}))
    return True, ""


def rows_of(name):
    """Лист как список словарей по заголовкам первой строки."""
    rows = load()["sheet"].get(name) or []
    if not rows:
        return []
    head = [str(h).strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue
        out.append({head[i]: str(r[i]).strip() if i < len(r) else "" for i in range(len(head))})
    return out


def col(row, *names):
    for n in names:
        for k, v in row.items():
            if k.lower().startswith(n.lower()):
                return v
    return ""


def parse_options(s):
    """«0–2=5; 3–4=4» → [(«0–2», 5), …]."""
    out = []
    for part in re.split(r"[;\n]", s or ""):
        if "=" in part:
            label, pts = part.rsplit("=", 1)
            try:
                out.append((label.strip(), num(pts)))
            except ValueError:
                continue
    return out


def parse_norm(s):
    """«30 ±10%» → (30, 0.10, True); «30 ±2» → (30, 2, False)."""
    ns = re.findall(r"\d+(?:[.,]\d+)?", s or "")
    if not ns:
        return None, 0.10, True
    norm = num(ns[0])
    if len(ns) > 1:
        return norm, (num(ns[1]) / 100 if "%" in s else num(ns[1])), "%" in s
    return norm, 0.10, True


def checklist_items(name):
    items = []
    for r in rows_of("Чеклисты"):
        if col(r, "Чеклист") != name or col(r, "Активен").lower() in ("нет", "no", "0"):
            continue
        kind = col(r, "Тип").lower()
        it = {"no": col(r, "№"), "title": col(r, "Пункт"), "hint": col(r, "Что проверить"),
              "photo": col(r, "Фото").lower() in ("да", "yes", "1")}
        if kind.startswith("вес"):
            norm, tol, rel = parse_norm(col(r, "Норма"))
            if norm is None:
                continue
            it.update(kind="weight", norm=norm, tol=tol, rel=rel, max=0)
        else:
            opts = parse_options(col(r, "Варианты"))
            if not opts:
                opts = [("Да", 0), ("Нет", 0)] if kind.startswith("да") else []
            if not opts:
                continue
            it.update(kind="yesno" if kind.startswith("да") else "scale", opts=opts, max=max(p for _, p in opts))
        items.append(it)
    return items


def checklist_names():
    seen = []
    for r in rows_of("Чеклисты"):
        n = col(r, "Чеклист")
        if n and n not in seen:
            seen.append(n)
    return seen


def schedule():
    out = []
    for r in rows_of("Расписание"):
        p = POINT_ALIAS.get(col(r, "Точка").lower().replace(" ", ""))
        tm = re.match(r"(\d{1,2})[:.](\d\d)", col(r, "Время отправки"))
        if not p or not tm or not col(r, "Чеклист"):
            continue
        days_s = col(r, "День").lower()
        days = set(range(7)) if ("еже" in days_s or "кажд" in days_s or not days_s) else \
            {DAYS[x] for x in re.findall(r"пн|вт|ср|чт|пт|сб|вс", days_s)}
        thr = re.findall(r"\d+", col(r, "Порог"))
        out.append({"checklist": col(r, "Чеклист"), "point": p, "who": col(r, "Кто"),
                    "days": days, "time": f"{int(tm[1]):02d}:{tm[2]}",
                    "deadline": col(r, "Сдать"), "threshold": int(thr[0]) if thr else 80,
                    "escalate": col(r, "Если")})
    return out


def push_rows(rows):
    """Строки в лист «Журнал». Не дошло — копим и досылаем позже."""
    url = sheet_url()
    pend = load()["pending_rows"] + rows
    if not url:
        update(lambda d: d.update(pending_rows=pend))
        return "нет адреса таблицы"
    try:
        r = requests.post(url, json={"rows": pend}, timeout=60, allow_redirects=True)
        ok = r.ok and "ok" in r.text[:100].lower()
    except Exception as e:
        ok, r = False, e
    update(lambda d: d.update(pending_rows=[] if ok else pend))
    return "ok" if ok else f"ошибка записи: {str(getattr(r, 'text', r))[:120]}"


# ───────────────────────── сессия проверки ─────────────────────────
def new_session(checklist, point, chat, thread, sch=None):
    items = checklist_items(checklist)
    if not items:
        return None, f"в таблице нет активных пунктов чеклиста «{checklist}»"
    sid = secrets.token_hex(3)
    s = {"sid": sid, "checklist": checklist, "point": point, "chat": chat, "thread": thread,
         "date": msk_now().date().isoformat(), "deadline": (sch or {}).get("deadline", ""),
         "who": (sch or {}).get("who", ""), "threshold": (sch or {}).get("threshold", 80),
         "items": items, "answers": {}, "idx": 0, "status": "new", "checker": None, "checker_name": ""}
    mid = send(chat, render(s), markup(s), thread)
    if not mid:
        return None, "не смог отправить сообщение в чат"
    s["mid"] = mid
    update(lambda d: d["sessions"].__setitem__(sid, s))
    return s, ""


def score(s):
    got = mx = 0.0
    for i, it in enumerate(s["items"]):
        if it["max"] > 0:
            mx += it["max"]
            a = s["answers"].get(str(i))
            if a:
                got += a["points"]
    return got, mx


def weight_eval(it, vals):
    bad = []
    for v in vals:
        dev = v - it["norm"]
        lim = it["norm"] * it["tol"] if it["rel"] else it["tol"]
        if abs(dev) > lim + 1e-9:
            bad.append(f"{fmt(v)} ({'+' if dev > 0 else '−'}{fmt(abs(dev))} г)")
    return bad


def head(s):
    d = datetime.fromisoformat(s["date"])
    t = f"🧾 <b>{esc(s['checklist'])}</b> · {POINTS[s['point']]} · {DAY_NAMES[d.weekday()]} {d:%d.%m}"
    if s.get("deadline"):
        t += f"\nСдать до {esc(s['deadline'])}"
    if s.get("who") and not s.get("checker"):
        t += f" · проверяет: {esc(s['who'])}"
    if s.get("checker_name"):
        t += f"\nПроверяет: <b>{esc(s['checker_name'])}</b>"
    return t


def answer_line(it, a):
    if it["kind"] == "weight":
        vals = "/".join(fmt(v) for v in a["vals"])
        return f"{esc(it['title'])}: {vals} " + ("✅" if not a["bad"] else "⚠️ " + ", ".join(a["bad"]))
    pts = f" → {fmt(a['points'])}/{fmt(it['max'])}" if it["max"] > 0 else ""
    if it["kind"] == "yesno":
        mark = " ⚠️" if a["label"].lower().startswith("нет") else ""
    else:
        mark = "" if it["max"] == 0 or a["points"] >= it["max"] else " ⚠️"
    return f"{esc(it['title'])}: {esc(a['label'])}{pts}{mark}"


def summary_lines(s):
    got, mx = score(s)
    pct = round(got / mx * 100) if mx else 0
    ok = pct >= s.get("threshold", 80)
    L = [f"Итог: <b>{fmt(got)} из {fmt(mx)} · {pct}%</b> " + ("✅ пройдено" if ok else f"❌ ниже {s.get('threshold', 80)}%")]
    for i, it in enumerate(s["items"]):
        a = s["answers"].get(str(i))
        if a:
            L.append(("• " if it["kind"] != "weight" else "⚖️ ") + answer_line(it, a))
    return L, pct, ok


def render(s):
    t = head(s)
    if s["status"] == "new":
        n = sum(1 for it in s["items"] if it["max"] > 0)
        return t + f"\n\n{n} блоков с баллами + журналы и вес нарезки.\nНажми «Начать», когда будешь на кухне."
    if s["status"] == "done":
        L, pct, ok = summary_lines(s)
        return t + "\n\n" + "\n".join(L) + f"\n\n✅ Сдано {esc(s.get('done_at', ''))}"
    if s["status"] == "cancel":
        return t + "\n\n✖️ Проверка отменена"
    i = s["idx"]
    if i >= len(s["items"]):
        L, pct, ok = summary_lines(s)
        return t + "\n\n" + "\n".join(L) + "\n\nВсё верно? Жми «Сдать». Ошибся — «Назад»."
    it = s["items"][i]
    done = [answer_line(s["items"][j], s["answers"][str(j)]) for j in range(i) if str(j) in s["answers"]]
    body = ("\n".join("✔️ " + x for x in done) + "\n\n") if done else ""
    body += f"<b>{i + 1}/{len(s['items'])} · {esc(it['title'])}</b>"
    if it.get("hint"):
        body += f"\n<i>{esc(it['hint'])}</i>"
    if it["kind"] == "weight":
        lim = f"±{fmt(it['tol'] * 100)}%" if it["rel"] else f"±{fmt(it['tol'])} г"
        body += f"\nНорма {fmt(it['norm'])} г ({lim}). Напиши в эту тему 4 замера через пробел, например: <code>33 30 31 32</code>"
    elif it["kind"] == "scale" and re.match(r"\d", it["opts"][0][0]):
        body += "\nСколько несоответствий?"
    return t + "\n\n" + body


def markup(s):
    sid = s["sid"]
    if s["status"] == "new":
        return kb([[("▶️ Начать проверку", f"c:{sid}:go:0")]])
    if s["status"] != "run":
        return None
    i = s["idx"]
    back = ("⬅️ Назад", f"c:{sid}:back:0") if i > 0 else None
    if i >= len(s["items"]):
        return kb([[("✅ Сдать", f"c:{sid}:done:0")], [back] if back else []])
    it = s["items"][i]
    rows = []
    if it["kind"] == "weight":
        rows.append([("Пропустить", f"c:{sid}:skip:0")])
    else:
        btn = [(f"{lab} → {fmt(p)}" if it["max"] > 0 and re.match(r"\d", lab) else lab, f"c:{sid}:a:{k}")
               for k, (lab, p) in enumerate(it["opts"])]
        per = 2 if all(len(b[0]) <= 14 for b in btn) else 1
        rows += [btn[k:k + per] for k in range(0, len(btn), per)]
    if back:
        rows.append([back])
    return kb(rows)


def refresh(s):
    edit(s["chat"], s["mid"], render(s), markup(s))


def save_session(s):
    update(lambda d: d["sessions"].__setitem__(s["sid"], s))


def finish(s, user_name):
    s["status"] = "done"
    s["done_at"] = msk_now().strftime("%d.%m %H:%M")
    save_session(s)
    refresh(s)
    L, pct, ok = summary_lines(s)
    now = msk_now()
    d = datetime.fromisoformat(s["date"]).strftime("%d.%m.%Y")
    rows = []
    for i, it in enumerate(s["items"]):
        a = s["answers"].get(str(i))
        if not a:
            continue
        if it["kind"] == "weight":
            ans, pts, com = " ".join(fmt(v) for v in a["vals"]), "", ", ".join(a["bad"]) or "в норме"
        else:
            ans, pts, com = a["label"], (a["points"] if it["max"] > 0 else ""), ""
        rows.append([d, now.strftime("%H:%M"), POINTS[s["point"]], s["checklist"], s["checker_name"],
                     it["no"], it["title"], ans, pts, "", com, ""])
    got, mx = score(s)
    rows.append([d, now.strftime("%H:%M"), POINTS[s["point"]], s["checklist"], s["checker_name"],
                 "ИТОГ", "Итог проверки", f"{fmt(got)} из {fmt(mx)}", got, "", "пройдено" if ok else "ниже порога",
                 f"{pct}%"])
    res = push_rows(rows)
    note = "" if res == "ok" else f"\n<i>Журнал: {esc(res)} — допишу, когда таблица ответит</i>"
    text = f"{'✅' if ok else '❌'} <b>{esc(s['checklist'])} · {POINTS[s['point']]} · {d}</b>\n" \
           f"Проверял: {esc(s['checker_name'])}\n" + "\n".join(L) + note
    targets = set(load()["control"]) | set(ADMINS)
    for c in targets:
        send(c, text)


# ───────────────────────── кнопки ─────────────────────────
_RECENT = {}


def user_name(u):
    return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or u.get("username") or str(u.get("id"))


def on_callback(cq):
    data = cq.get("data") or ""
    uid = str((cq.get("from") or {}).get("id"))
    m = re.fullmatch(r"c:([0-9a-f]{6}):(\w+):(\d+)", data)
    now = time.time()
    key = (data, uid)
    if now - _RECENT.get(key, 0) < 1.5:  # двойное нажатие
        return tg("answerCallbackQuery", json={"callback_query_id": cq["id"]})
    _RECENT[key] = now
    s = load()["sessions"].get(m[1]) if m else None
    if not s:
        return tg("answerCallbackQuery", json={"callback_query_id": cq["id"], "text": "Эта проверка устарела"})
    act, arg = m[2], int(m[3])
    if s["status"] in ("done", "cancel"):
        return tg("answerCallbackQuery", json={"callback_query_id": cq["id"], "text": "Проверка уже закрыта"})
    if act == "go":
        if s["status"] == "new":
            s.update(status="run", checker=uid, checker_name=user_name(cq["from"]), started=msk_now().isoformat())
            save_session(s)
            refresh(s)
        return tg("answerCallbackQuery", json={"callback_query_id": cq["id"]})
    if uid != s.get("checker") and uid not in ADMINS:
        return tg("answerCallbackQuery", json={"callback_query_id": cq["id"],
                                               "text": f"Проверку ведёт {s.get('checker_name')}", "show_alert": False})
    tg("answerCallbackQuery", json={"callback_query_id": cq["id"]})
    i = s["idx"]
    if act == "back":
        s["idx"] = max(0, i - 1)
    elif act == "a" and i < len(s["items"]):
        it = s["items"][i]
        if arg < len(it.get("opts", [])):
            lab, p = it["opts"][arg]
            s["answers"][str(i)] = {"label": lab, "points": p}
            s["idx"] = i + 1
    elif act == "skip" and i < len(s["items"]):
        s["answers"].pop(str(i), None)
        s["idx"] = i + 1
    elif act == "done" and i >= len(s["items"]):
        return finish(s, user_name(cq["from"]))
    save_session(s)
    refresh(s)


def on_weight_text(msg):
    """Замеры веса текстом в теме. True — сообщение забрали."""
    uid = str((msg.get("from") or {}).get("id"))
    chat = msg["chat"]["id"]
    th = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    text = msg.get("text") or ""
    for s in sorted(load()["sessions"].values(), key=lambda x: x.get("started", ""), reverse=True):
        if s["status"] != "run" or s["chat"] != chat or s.get("thread") != th or s.get("checker") != uid:
            continue
        if s["idx"] >= len(s["items"]) or s["items"][s["idx"]]["kind"] != "weight":
            continue
        vals = [num(x) for x in re.findall(r"\d+(?:[.,]\d+)?", text)]
        if not vals:
            return False
        it = s["items"][s["idx"]]
        s["answers"][str(s["idx"])] = {"vals": vals[:10], "bad": weight_eval(it, vals[:10])}
        s["idx"] += 1
        save_session(s)
        tg("deleteMessage", json={"chat_id": chat, "message_id": msg["message_id"]})
        refresh(s)
        return True
    return False


# ───────────────────────── расписание ─────────────────────────
def scheduler():
    last_fetch = 0
    while True:
        try:
            if time.time() - last_fetch > 600:
                last_fetch = time.time()
                fetch_sheet()
            now = msk_now()
            today = now.date().isoformat()
            d = load()
            for sch in schedule():
                if now.weekday() not in sch["days"]:
                    continue
                hh, mm = map(int, sch["time"].split(":"))
                due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                key = f"{today}|{sch['checklist']}|{sch['point']}|{sch['time']}"
                if now < due or key in d["sent"] or now - due > timedelta(hours=3):
                    continue
                b = d["binds"].get(sch["point"])
                if not b:
                    wk = f"{today}|{sch['point']}"
                    if wk not in d["warned"]:
                        update(lambda x: x["warned"].__setitem__(wk, 1))
                        dm_admins(f"⚠️ Пора слать «{esc(sch['checklist'])}» на {POINTS[sch['point']]}, но тема не привязана. "
                                  f"Напиши в нужной теме: <code>/tut {sch['point']}</code>")
                    continue
                s, err = new_session(sch["checklist"], sch["point"], b["chat"], b.get("thread"), sch)
                update(lambda x: x["sent"].__setitem__(key, s["sid"] if s else "err"))
                if err:
                    dm_admins(f"❌ Не отправил чеклист: {esc(err)}")
            # чистка: сессии и отметки старше 60 дней
            cut = (now - timedelta(days=60)).date().isoformat()

            def _clean(x):
                x["sessions"] = {k: v for k, v in x["sessions"].items() if v.get("date", "") >= cut}
                x["sent"] = {k: v for k, v in x["sent"].items() if k[:10] >= cut}
                x["warned"] = {k: v for k, v in x["warned"].items() if k[:10] >= cut}
            update(_clean)
            if load()["pending_rows"]:
                push_rows([])
        except Exception:
            print("scheduler error:", traceback.format_exc()[-1500:])
        time.sleep(30)


# ───────────────────────── команды ─────────────────────────
HELP = f"""<b>Бот чеклистов Сура</b> · {VERSION}

<b>В личке</b>
/tablica URL — адрес Apps Script таблицы
/obnovit — перечитать таблицу сейчас (сама — раз в 10 мин)
/status — привязки, расписание, журнал
/test [17|353] — прислать чеклист сюда, в личку (для пробы)

<b>В группе (тема «Чеклисты»)</b>
/tut 17 · /tut 353 — эта тема получает чеклисты точки
/tut_off — отвязать тему
/kontrol — этот чат получает итоги проверок (/kontrol_off — убрать)
/test [17|353] — прислать чеклист сюда прямо сейчас

<b>Обновление</b>: пришли сюда bot.py или .zip с ним."""


def status_text():
    d = load()
    b = "\n".join(f"  {POINTS[p]} → чат {v['chat']}" + (f", тема {v['thread']}" if v.get("thread") else "")
                  for p, v in d["binds"].items()) or "  нет"
    sc = "\n".join(f"  {esc(x['checklist'])} · {POINTS[x['point']]} · "
                   f"{','.join(DAY_NAMES[i] for i in sorted(x['days']))} {x['time']}" for x in schedule()) or "  пусто"
    lst = "\n".join(f"  {esc(n)}: {len(checklist_items(n))} пунктов" for n in checklist_names()) or "  нет"
    return (f"<b>Статус</b> · {VERSION}\nТаблица: {'есть' if sheet_url() else 'нет'}, прочитана {d.get('sheet_ts', '—')[:16]}\n"
            f"Чеклисты:\n{lst}\nРасписание:\n{sc}\nТемы:\n{b}\nЧатов «Контроль»: {len(d['control'])}\n"
            f"Не дописано в журнал: {len(d['pending_rows'])} строк")


def install_update(chat, doc):
    f = tg("getFile", json={"file_id": doc["file_id"]}).get("result") or {}
    if not f:
        return send(chat, "Не смог скачать файл")
    data = requests.get(f"{TG_BASE}/file/bot{TOKEN}/{f['file_path']}", timeout=60).content
    name = doc.get("file_name", "")
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            src = next((z.read(n) for n in z.namelist() if os.path.basename(n) == "bot.py"), None)
    else:
        src = data if re.sub(r"\s*\(\d+\)", "", name) == "bot.py" else None
    if not src:
        return send(chat, "Принимаю только bot.py или .zip с ним")
    tmp = os.path.join(HERE, ".tmp_bot.py")
    open(tmp, "wb").write(src)
    try:
        py_compile.compile(tmp, doraise=True)
    except Exception as e:
        os.remove(tmp)
        return send(chat, f"❌ Файл с ошибкой, ничего не менял:\n<pre>{esc(str(e)[-600:])}</pre>")
    os.makedirs(os.path.join(HERE, "backups"), exist_ok=True)
    shutil.copy2(os.path.join(HERE, "bot.py"), os.path.join(HERE, "backups", f"bot.py.{msk_now():%Y%m%d-%H%M%S}"))
    os.replace(tmp, os.path.join(HERE, "bot.py"))
    send(chat, "✅ bot.py обновлён, перезапускаюсь…")
    raise SystemExit(0)


def on_message(msg):
    chat = msg["chat"]["id"]
    ctype = msg["chat"].get("type")
    uid = str((msg.get("from") or {}).get("id"))
    th = msg.get("message_thread_id") if msg.get("is_topic_message") else None
    text = (msg.get("text") or "").strip()
    word = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if not word:
        if text:
            on_weight_text(msg)
        elif ctype == "private" and uid in ADMINS and msg.get("document"):
            install_update(chat, msg["document"])
        return
    args = text.split()[1:]
    if uid not in ADMINS:
        if ctype == "private" and word == "/start":
            send(chat, "Привет! Я присылаю чеклисты проверок в рабочие чаты Сура. Здесь мне писать ничего не нужно.")
        return
    if ctype == "private":
        if word in ("/start", "/help"):
            return send(chat, HELP)
        if word == "/tablica":
            if not args or not args[0].startswith("https://script.google.com/"):
                return send(chat, "Формат: <code>/tablica https://script.google.com/macros/s/…/exec</code>")
            update(lambda d: d.update(sheet_url=args[0]))
            ok, err = fetch_sheet()
            return send(chat, ("✅ Таблица подключена.\n\n" + status_text()) if ok else f"❌ {esc(err)}")
        if word == "/obnovit":
            ok, err = fetch_sheet()
            return send(chat, ("✅ Перечитал таблицу.\n\n" + status_text()) if ok else f"❌ {esc(err)}")
        if word == "/status":
            return send(chat, status_text())
        if word != "/test":
            return send(chat, HELP)
    # группа (и /test в личке — проверка прямо сюда)
    # группа
    if word == "/tut":
        p = POINT_ALIAS.get(args[0].lower()) if args else None
        if not p:
            return send(chat, "Формат: <code>/tut 17</code> или <code>/tut 353</code>", thread=th)
        update(lambda d: d["binds"].__setitem__(p, {"chat": chat, "thread": th}))
        return send(chat, f"✅ Сюда будут приходить чеклисты {POINTS[p]}.", thread=th)
    if word == "/tut_off":
        update(lambda d: d.update(binds={k: v for k, v in d["binds"].items()
                                         if not (v["chat"] == chat and v.get("thread") == th)}))
        return send(chat, "✅ Тема отвязана.", thread=th)
    if word == "/kontrol":
        update(lambda d: d["control"].append(chat) if chat not in d["control"] else None)
        return send(chat, "✅ Сюда будут приходить итоги проверок.", thread=th)
    if word == "/kontrol_off":
        update(lambda d: d.update(control=[c for c in d["control"] if c != chat]))
        return send(chat, "✅ Итоги сюда больше не приходят.", thread=th)
    if word == "/test":
        if not load()["sheet"]:
            fetch_sheet()
        bound = next((p for p, v in load()["binds"].items() if v["chat"] == chat and v.get("thread") == th), None)
        p = POINT_ALIAS.get(args[0].lower()) if args else bound or "17"
        names = checklist_names()
        if not names:
            return send(chat, "В таблице нет чеклистов — проверь /status в личке", thread=th)
        sch = next((x for x in schedule() if x["point"] == p and x["checklist"] == names[0]), None)
        s, err = new_session(names[0], p, chat, th, sch)
        if err:
            send(chat, f"❌ {esc(err)}", thread=th)


# ───────────────────────── запуск ─────────────────────────
def main():
    tg("deleteMyCommands", json={})
    for a in ADMINS:
        tg("setMyCommands", json={"commands": [
            {"command": "test", "description": "Прислать чеклист сюда для пробы"},
            {"command": "status", "description": "Привязки, расписание, журнал"},
            {"command": "obnovit", "description": "Перечитать таблицу"},
            {"command": "tablica", "description": "Адрес Apps Script таблицы"},
            {"command": "help", "description": "Все команды"}], "scope": {"type": "chat", "chat_id": int(a)}})
    tg("deleteWebhook", json={"drop_pending_updates": False})
    threading.Thread(target=scheduler, daemon=True).start()
    print("checklist bot started", VERSION)
    offset = None
    while True:
        r = tg("getUpdates", json={"timeout": 50, "offset": offset, "allowed_updates": ["message", "callback_query"]})
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    on_callback(u["callback_query"])
                elif "message" in u:
                    on_message(u["message"])
            except SystemExit:
                tg("getUpdates", json={"timeout": 0, "offset": offset})
                raise
            except Exception:
                print("handle error:", traceback.format_exc()[-1500:])
        if not r.get("ok"):
            time.sleep(5)


if __name__ == "__main__":
    main()
