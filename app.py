from __future__ import annotations

import hashlib
import io
import math
import re
import shutil
import sqlite3
import unicodedata
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

try:
    import pytesseract
except Exception:
    pytesseract = None

try:
    import yfinance as yf
except Exception:
    yf = None

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = APP_DIR / "assets"
DB_PATH = DATA_DIR / "signal_radar.db"
WHITELIST_PATH = APP_DIR / "asset_whitelist.txt"
SEED_CSV = DATA_DIR / "seed_signals.csv"
SEED_UPDATES_CSV = DATA_DIR / "seed_updates.csv"

STATUS_ORDER = ["WATCH", "READY", "TRIGGERED", "CLOSED", "INVALIDATED", "EXPIRED", "INFO"]
STATUS_ICON = {
    "WATCH": "🔵",
    "READY": "🟡",
    "TRIGGERED": "🟢",
    "CLOSED": "⚪",
    "INVALIDATED": "🔴",
    "EXPIRED": "🟤",
    "INFO": "⚫",
}
BIAS_OPTIONS = ["LONG", "SHORT", "NEUTRAL"]
BIAS_ICON = {"LONG": "▲", "SHORT": "▼", "NEUTRAL": "•"}
CATEGORIES = ["ANALISI", "MACRO", "WATCH", "SETUP", "ENTRY", "UPDATE", "RESULT", "RULE", "INFO"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]

# Yahoo è solo un aiuto gratuito/ritardato. Non usiamo proxy spot per DAX/FESX:
# se non abbiamo un future ragionevolmente equivalente, il prezzo resta manuale.
YAHOO_TICKERS = {
    "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
    "CL": "CL=F", "NG": "NG=F", "GC": "GC=F", "SI": "SI=F", "HG": "HG=F",
    "ZB": "ZB=F", "ZN": "ZN=F", "ZF": "ZF=F", "ZT": "ZT=F",
    "6E": "6E=F", "6B": "6B=F", "6A": "6A=F", "6C": "6C=F", "6J": "6J=F", "6N": "6N=F", "6S": "6S=F",
    "ZC": "ZC=F", "ZS": "ZS=F", "ZW": "ZW=F", "HE": "HE=F", "LE": "LE=F",
}

st.set_page_config(page_title="Signal Radar V2.2", page_icon="📡", layout="wide")


def get_conn():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table: str, column: str, ddl: str):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_key TEXT UNIQUE NOT NULL,
            instrument TEXT NOT NULL,
            market_name TEXT,
            period TEXT,
            timeframe TEXT,
            bias TEXT NOT NULL,
            signal_type TEXT,
            entry_zone TEXT,
            stop_level TEXT,
            target TEXT,
            status TEXT NOT NULL,
            next_action TEXT,
            validity_end TEXT,
            confidence INTEGER DEFAULT 100,
            notes TEXT,
            created_at TEXT NOT NULL,
            last_update TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER,
            received_at TEXT NOT NULL,
            category TEXT NOT NULL,
            summary TEXT,
            image_path TEXT,
            raw_text TEXT,
            file_hash TEXT,
            source_filename TEXT,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        );

        CREATE TABLE IF NOT EXISTS prices (
            instrument TEXT PRIMARY KEY,
            current_price REAL NOT NULL,
            price_time TEXT NOT NULL,
            source TEXT NOT NULL,
            trusted INTEGER DEFAULT 1,
            source_symbol TEXT,
            note TEXT
        );
        """
    )
    ensure_column(conn, "updates", "file_hash", "TEXT")
    ensure_column(conn, "updates", "source_filename", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_updates_hash ON updates(file_hash)")

    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if count == 0 and SEED_CSV.exists():
        sig = pd.read_csv(SEED_CSV).fillna("")
        cols = [
            "setup_key", "instrument", "market_name", "period", "timeframe", "bias", "signal_type",
            "entry_zone", "stop_level", "target", "status", "next_action", "validity_end", "confidence",
            "notes", "created_at", "last_update",
        ]
        for _, r in sig.iterrows():
            conn.execute(
                """INSERT OR IGNORE INTO signals
                (setup_key,instrument,market_name,period,timeframe,bias,signal_type,entry_zone,stop_level,target,status,next_action,validity_end,confidence,notes,created_at,last_update)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(r[c] for c in cols),
            )
        conn.commit()
        if SEED_UPDATES_CSV.exists():
            upd = pd.read_csv(SEED_UPDATES_CSV).fillna("")
            id_map = {r["setup_key"]: r["id"] for r in conn.execute("SELECT id, setup_key FROM signals").fetchall()}
            for _, r in upd.iterrows():
                signal_id = id_map.get(r["setup_key"]) if r["setup_key"] else None
                conn.execute(
                    """INSERT INTO updates
                    (signal_id,received_at,category,summary,image_path,raw_text,file_hash,source_filename)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (signal_id, r["received_at"], r["category"], r["summary"], r["image_path"], r["raw_text"], "", Path(r["image_path"]).name),
                )
            conn.commit()

    # V2.1 migration: seed rows from older versions had no hash, so uploading the
    # same screenshots created duplicate setups. Backfill hashes from any local image.
    for r in conn.execute("SELECT id,image_path,file_hash FROM updates WHERE COALESCE(file_hash,'')='' ").fetchall():
        rel = str(r["image_path"] or "")
        img_path = APP_DIR / rel if rel else None
        if img_path and img_path.exists() and img_path.is_file():
            try:
                h = hashlib.sha256(img_path.read_bytes()).hexdigest()
                conn.execute("UPDATE updates SET file_hash=? WHERE id=?", (h, int(r["id"])))
            except Exception:
                pass
    conn.commit()
    conn.close()


def reset_to_seed():
    """Delete test/imported DB rows and restore the curated baseline CSVs."""
    conn = get_conn()
    conn.execute("DELETE FROM updates")
    conn.execute("DELETE FROM signals")
    try:
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('signals','updates')")
    except Exception:
        pass
    conn.commit()
    conn.close()
    init_db()


def auto_expire():
    conn = get_conn()
    today = date.today().isoformat()
    conn.execute(
        """UPDATE signals SET status='EXPIRED'
           WHERE validity_end <> '' AND validity_end < ?
           AND status IN ('WATCH','READY')""",
        (today,),
    )
    conn.commit()
    conn.close()


def load_signals() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM signals ORDER BY datetime(last_update) DESC", conn)
    conn.close()
    return df


def load_updates(signal_id: int | None = None) -> pd.DataFrame:
    conn = get_conn()
    if signal_id is None:
        df = pd.read_sql_query("SELECT * FROM updates ORDER BY datetime(received_at) ASC", conn)
    else:
        df = pd.read_sql_query(
            "SELECT * FROM updates WHERE signal_id=? ORDER BY datetime(received_at) ASC", conn, params=(signal_id,)
        )
    conn.close()
    return df


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_exists(file_hash: str) -> bool:
    if not file_hash:
        return False
    conn = get_conn()
    found = conn.execute("SELECT 1 FROM updates WHERE file_hash=? LIMIT 1", (file_hash,)).fetchone() is not None
    conn.close()
    return found


def parse_received_at(filename: str) -> datetime:
    # WhatsApp Image 2026-08-16 at 21.44.05.jpeg
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2}).*?(\d{2})[.:](\d{2})[.:](\d{2})", filename)
    if m:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            pass
    return datetime.now()


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:150]


def save_file_bytes(filename: str, data: bytes) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = UPLOAD_DIR / f"{ts}_{safe_name(filename)}"
    dest.write_bytes(data)
    return str(dest.relative_to(APP_DIR))


def add_signal(data: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO signals
        (setup_key,instrument,market_name,period,timeframe,bias,signal_type,entry_zone,stop_level,target,status,next_action,validity_end,confidence,notes,created_at,last_update)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(data[k] for k in [
            "setup_key", "instrument", "market_name", "period", "timeframe", "bias", "signal_type", "entry_zone",
            "stop_level", "target", "status", "next_action", "validity_end", "confidence", "notes", "created_at", "last_update",
        ]),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_signal(signal_id: int, data: dict):
    conn = get_conn()
    conn.execute(
        """UPDATE signals SET instrument=?, market_name=?, period=?, timeframe=?, bias=?, signal_type=?, entry_zone=?, stop_level=?, target=?, status=?, next_action=?, validity_end=?, confidence=?, notes=?, last_update=? WHERE id=?""",
        (
            data["instrument"], data.get("market_name", ""), data.get("period", ""), data.get("timeframe", ""), data["bias"],
            data.get("signal_type", ""), data.get("entry_zone", ""), data.get("stop_level", ""), data.get("target", ""),
            data["status"], data.get("next_action", ""), data.get("validity_end", ""), int(data.get("confidence", 100) or 100),
            data.get("notes", ""), data["last_update"], signal_id,
        ),
    )
    conn.commit()
    conn.close()


def add_update(
    signal_id: int | None,
    received_at: str,
    category: str,
    summary: str,
    image_path: str,
    raw_text: str = "",
    file_hash: str = "",
    source_filename: str = "",
):
    conn = get_conn()
    conn.execute(
        """INSERT INTO updates
        (signal_id,received_at,category,summary,image_path,raw_text,file_hash,source_filename)
        VALUES (?,?,?,?,?,?,?,?)""",
        (signal_id, received_at, category, summary, image_path, raw_text, file_hash, source_filename),
    )
    if signal_id is not None:
        conn.execute("UPDATE signals SET last_update=? WHERE id=?", (received_at, signal_id))
    conn.commit()
    conn.close()


def display_image(rel_path: str, caption: str = ""):
    if not rel_path:
        return
    p = APP_DIR / rel_path
    if p.exists():
        st.image(str(p), caption=caption, use_container_width=True)
    else:
        st.warning(f"Immagine non trovata: {rel_path}")


def signal_label(row) -> str:
    return f"{STATUS_ICON.get(row['status'],'')} {row['instrument']} · {BIAS_ICON.get(row['bias'],'')} {row['bias']} · {row['period']} · {row['status']}"


def destination_label(row) -> str:
    return f"#{int(row['id'])} · {row['instrument']} · {row['bias']} · {row['period']}"


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip().lower()


def _prep_ocr(img: Image.Image, scale: float = 1.8) -> Image.Image:
    from PIL import ImageEnhance, ImageOps
    if scale != 1:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    img = ImageOps.grayscale(img)
    img = ImageEnhance.Contrast(img).enhance(1.7)
    return img


def ocr_image_regions(data: bytes) -> dict[str, str]:
    """Two-pass OCR: header for titles/tickers + full image for context.

    This is intentionally conservative: OCR is used to organize screenshots,
    never to accept numeric trading levels automatically.
    """
    if pytesseract is None or shutil.which("tesseract") is None:
        return {"header": "", "full": "", "combined": ""}
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        # Header carries ticker/title/bias much more reliably than the full screenshot.
        header = img.crop((0, 0, img.width, max(1, int(img.height * 0.42))))
        header_txt = pytesseract.image_to_string(_prep_ocr(header, 1.8), config="--psm 11")
        # Full pass keeps annotations/messages. Limit width for Streamlit Cloud speed.
        if img.width > 1800:
            ratio = 1800 / img.width
            img = img.resize((1800, int(img.height * ratio)))
        full_txt = pytesseract.image_to_string(_prep_ocr(img, 1.25), config="--psm 6")
        combined = (header_txt + "\n" + full_txt).strip()
        return {"header": header_txt, "full": full_txt, "combined": combined}
    except Exception:
        return {"header": "", "full": "", "combined": ""}


def load_asset_whitelist() -> list[dict]:
    out = []
    if WHITELIST_PATH.exists():
        for raw in WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|", 2)]
            if len(parts) != 3:
                continue
            code, name, aliases = parts
            out.append({"code": code.upper(), "name": name, "aliases": [a.strip().lower() for a in aliases.split(";") if a.strip()]})
    return out


ASSET_RULES = load_asset_whitelist()


def _alias_found(alias: str, text: str) -> bool:
    if not alias:
        return False
    a = re.escape(alias.lower())
    # Symbol-like aliases must appear as their own token. This avoids CL/HG false positives
    # from random OCR syllables.
    if re.fullmatch(r"[a-z0-9!&. -]+", alias.lower()) and len(alias.replace("!", "").replace(" ", "")) <= 5:
        return re.search(rf"(?<![a-z0-9]){a}(?![a-z0-9])", text, flags=re.I) is not None
    return alias.lower() in text


def detect_instrument(header_text: str, full_text: str) -> tuple[str, str, int]:
    h = normalize_text(header_text)
    f = normalize_text(full_text)
    best = ("", "", 0)
    for rule in ASSET_RULES:
        score = 0
        for alias in rule["aliases"]:
            if _alias_found(alias, h):
                score = max(score, 100 if len(alias) >= 6 else 90)
            elif _alias_found(alias, f):
                score = max(score, 85 if len(alias) >= 6 else 75)
        if score > best[2]:
            best = (rule["code"], rule["name"], score)
    # Conservative threshold: unknown is better than a false asset.
    return best if best[2] >= 75 else ("", "", best[2])


def _count_terms(text: str, weighted_terms: list[tuple[str, int]]) -> int:
    return sum(text.count(term) * weight for term, weight in weighted_terms)


def detect_bias(header_text: str, full_text: str) -> tuple[str, int]:
    h = normalize_text(header_text)
    f = normalize_text(full_text)
    # Strong operational language carries the decision. Side labels such as "Emo Long"
    # are deliberately ignored because both long and short levels can coexist on one chart.
    short_strong = [("entry short", 8), ("strategia short", 8), ("setup short", 7), ("scenario short", 6), ("short w3", 5), ("short w2", 5), ("short w4", 5)]
    long_strong = [("entry long", 8), ("strategia long", 8), ("setup long", 7), ("scenario long", 6), ("long w3", 5), ("long w2", 5), ("long w4", 5)]
    short_weak = [("target mensile short", 1), ("target short", 1)]
    long_weak = [("target mensile long", 1), ("target long", 1)]
    s = _count_terms(h, short_strong) * 2 + _count_terms(f, short_strong) + _count_terms(f, short_weak)
    l = _count_terms(h, long_strong) * 2 + _count_terms(f, long_strong) + _count_terms(f, long_weak)
    if s >= 6 and s >= l + 3:
        return "SHORT", min(100, 60 + s * 2)
    if l >= 6 and l >= s + 3:
        return "LONG", min(100, 60 + l * 2)
    return "NEUTRAL", max(s, l)


def detect_period(text: str, fallback_dt: datetime) -> str:
    t = normalize_text(text)
    # WhatsApp receive year is authoritative unless OCR clearly contains the same year.
    # This prevents errors such as 2008 instead of 2026 from chart text/noise.
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", t)]
    year = fallback_dt.year
    if fallback_dt.year in years:
        year = fallback_dt.year
    else:
        plausible = [y for y in years if abs(y - fallback_dt.year) <= 1]
        if plausible:
            year = min(plausible, key=lambda y: abs(y - fallback_dt.year))
    month = ""
    for m in MONTHS:
        if m in t:
            month = m.capitalize()
            break
    w_match = re.search(r"\bw\s*([1-5])\b", t)
    if w_match and month:
        return f"W{w_match.group(1)} {month} {year}"
    if w_match:
        return f"W{w_match.group(1)} {year}"
    if month:
        return f"{month} {year}"
    return ""


def detect_timeframe(text: str) -> str:
    t = normalize_text(text)
    vals = []
    if re.search(r"\b30\s*['’°]?\b|\b30m\b|30 min", t):
        vals.append("30m")
    if "daily" in t or re.search(r"\b1d\b", t):
        vals.append("D")
    if "weekly" in t or "settimanale" in t or re.search(r"\b1w\b", t):
        vals.append("W")
    if "mensile" in t or "monthly" in t:
        vals.append("M")
    out = []
    for x in vals:
        if x not in out:
            out.append(x)
    return "/".join(out)


def detect_category(text: str, instrument: str) -> str:
    t = normalize_text(text)
    # General teaching/chat messages must never become active signals even if OCR sees a ticker-like fragment.
    if any(x in t for x in ["didatticamente", "lezioni", "complimenti", "moltissime opportunita", "attaccare i grafici alla parete", "lanciare una freccetta"]):
        return "INFO"
    if any(x in t for x in ["risultato come da ipotesi", "target raggiunto"]):
        return "RESULT"
    if any(x in t for x in ["concetto di frattalita", "attesa della rottura", "puo optare", "regola alternativa"]):
        return "RULE"
    if any(x in t for x in ["livelli di entry", "entry short", "entry long", "potenziali livelli di entry"]):
        return "ENTRY"
    if "area da attenzionare" in t or "zona da attenzionare" in t or ("attenzion" in t and ("area" in t or "zona" in t)):
        return "WATCH"
    # Some screenshots render the title poorly; "Area ... W3 Agosto" is still a watch-type
    # message, but without a recognized asset it remains blocked for manual confirmation.
    if "area" in t and re.search(r"\bw\s*[1-5]\b", t):
        return "WATCH"
    if "strategia" in t:
        return "SETUP"
    if any(x in t for x in ["dinamica mensile", "dinamica delle medie", "pavimento importante", "verifica macro", "settimanale"]):
        return "MACRO" if instrument else "ANALISI"
    return "ANALISI" if instrument else "INFO"


def propose_status(category: str) -> str:
    return {"RESULT": "CLOSED", "ENTRY": "READY", "SETUP": "READY", "WATCH": "WATCH", "RULE": "WATCH", "MACRO": "WATCH", "ANALISI": "WATCH", "UPDATE": "WATCH", "INFO": "INFO"}.get(category, "WATCH")


def propose_next_action(category: str, bias: str) -> str:
    side = "long" if bias == "LONG" else "short" if bias == "SHORT" else ""
    if category == "ENTRY":
        return f"Attendere il prezzo nella zona di entry {side}; confermare manualmente livelli, stop e target.".strip()
    if category == "WATCH":
        return "Monitorare l'area indicata; nessuna entrata finché non compare la conferma prevista."
    if category == "SETUP":
        return f"Monitorare lo scenario {side} e attendere l'arrivo nella zona operativa indicata.".strip()
    if category == "RULE":
        return "Applicare la regola indicata solo quando si verifica la condizione; poi attendere il pullback/conferma."
    if category == "RESULT":
        return "Setup concluso: archiviare il risultato e non trattarlo come segnale ancora operativo."
    if category in ["MACRO", "ANALISI"]:
        return "Usare come contesto; attendere un successivo messaggio operativo di WATCH/ENTRY."
    return ""


def make_summary(instrument: str, category: str, bias: str, period: str, text: str) -> str:
    pieces = []
    if instrument:
        pieces.append(instrument)
    if bias != "NEUTRAL":
        pieces.append(bias)
    pieces.append(category)
    if period:
        pieces.append(period)
    base = " · ".join(pieces)
    t = re.sub(r"\s+", " ", text or "").strip()
    return f"{base}. OCR: {t[:220]}" if t else base


def confidence_score(instrument: str, asset_conf: int, bias: str, bias_conf: int, period: str, timeframe: str, category: str, ocr_text: str) -> int:
    score = 20
    if instrument:
        score += min(35, int(asset_conf * 0.35))
    if bias != "NEUTRAL":
        score += min(20, int(bias_conf * 0.2))
    if period:
        score += 10
    if timeframe:
        score += 5
    if category not in ["ANALISI", "INFO"]:
        score += 5
    if len((ocr_text or "").strip()) > 80:
        score += 5
    if not instrument and category != "INFO":
        score = min(score, 45)
    return min(score, 95)


def group_key(instrument: str, bias: str, period: str, category: str, received: datetime) -> str:
    if category == "INFO" or not instrument:
        return "INFO"
    p = period or received.strftime("%Y-%m")
    b = bias if bias != "NEUTRAL" else "DA_VERIFICARE"
    return f"{instrument}|{b}|{p}"


def _period_tokens(value: str) -> set[str]:
    t = normalize_text(value)
    return set(re.findall(r"w[1-5]|20\d{2}|" + "|".join(MONTHS), t))


def recommend_destination(proposal: dict, signals_df: pd.DataFrame) -> str:
    if proposal["Categoria"] == "INFO" or not proposal["Asset"]:
        return "INFO" if proposal["Categoria"] == "INFO" else "NUOVO"
    if signals_df.empty:
        return "NUOVO"
    best_label, best_score = "NUOVO", -999
    p_tokens = _period_tokens(proposal.get("Periodo", ""))
    for _, r in signals_df.iterrows():
        if str(r["instrument"]).upper() != str(proposal["Asset"]).upper():
            continue
        score = 8
        r_tokens = _period_tokens(str(r["period"]))
        overlap = len(p_tokens & r_tokens)
        score += overlap * 2
        if proposal.get("Periodo") and str(r["period"]) == proposal["Periodo"]:
            score += 4
        pb = proposal.get("Bias", "NEUTRAL")
        rb = str(r["bias"])
        if pb != "NEUTRAL":
            score += 4 if pb == rb else -5
        # Prefer active setups for new ENTRY/WATCH/SETUP; prefer closed for RESULT.
        cat = proposal.get("Categoria", "")
        if cat == "RESULT":
            score += 3 if r["status"] == "CLOSED" else 0
        else:
            score += 3 if r["status"] in ["WATCH", "READY", "TRIGGERED"] else -1
        try:
            last_dt = pd.to_datetime(r["last_update"], errors="coerce")
            recv_dt = pd.to_datetime(proposal["Ricevuto"], errors="coerce")
            if pd.notna(last_dt) and pd.notna(recv_dt) and abs((recv_dt - last_dt).total_seconds()) <= 14 * 24 * 3600:
                score += 1
        except Exception:
            pass
        if score > best_score:
            best_score = score
            best_label = destination_label(r)
    return best_label if best_score >= 10 else "NUOVO"


def analyze_item(item: dict, signals_df: pd.DataFrame) -> dict:
    dt = parse_received_at(item["name"])
    ocr = ocr_image_regions(item["bytes"])
    instrument, market_name, asset_conf = detect_instrument(ocr["header"], ocr["full"])
    bias, bias_conf = detect_bias(ocr["header"], ocr["full"])
    period = detect_period(ocr["combined"], dt)
    tf = detect_timeframe(ocr["combined"])
    category = detect_category(ocr["combined"], instrument)
    status = propose_status(category)
    action = propose_next_action(category, bias)
    conf = confidence_score(instrument, asset_conf, bias, bias_conf, period, tf, category, ocr["combined"])
    warning = ""
    if category != "INFO" and not instrument:
        warning = "⚠ VERIFICA ASSET"
    elif category in ["ENTRY", "SETUP", "WATCH"] and bias == "NEUTRAL":
        warning = "⚠ BIAS DA CONFERMARE"
    proposal = {
        "Importa": (not hash_exists(item["hash"])) and (category == "INFO" or bool(instrument)),
        "Ricevuto": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "File": item["name"],
        "Asset": instrument,
        "Mercato": market_name,
        "Bias": bias,
        "Periodo": period,
        "TF": tf,
        "Categoria": category,
        "Stato": status,
        "Gruppo": group_key(instrument, bias, period, category, dt),
        "Destinazione": "NUOVO",
        "Verifica": warning,
        "Entry": "", "Stop": "", "Target": "",
        "Prossima azione": action,
        "Sintesi": make_summary(instrument, category, bias, period, ocr["combined"]),
        "Confidence": conf,
        "OCR": ocr["combined"],
        "OCR_Header": ocr["header"],
        "Hash": item["hash"],
    }
    proposal["Destinazione"] = recommend_destination(proposal, signals_df)
    if proposal["Destinazione"] not in ["NUOVO", "INFO"]:
        # Existing validated setup can safely provide missing context, but never overwrite a
        # directional OCR decision that conflicts with it.
        m = signals_df[signals_df.apply(lambda r: destination_label(r) == proposal["Destinazione"], axis=1)]
        if not m.empty:
            r = m.iloc[0]
            if proposal["Bias"] == "NEUTRAL":
                proposal["Bias"] = str(r["bias"])
                proposal["Verifica"] = "" if proposal["Asset"] else proposal["Verifica"]
            if not proposal["Periodo"]:
                proposal["Periodo"] = str(r["period"])
            if not proposal["TF"]:
                proposal["TF"] = str(r["timeframe"])
            proposal["Gruppo"] = f"EXISTING|{int(r['id'])}"
            proposal["Prossima azione"] = str(r["next_action"] or proposal["Prossima azione"])
    if hash_exists(item["hash"]):
        proposal["Importa"] = False
        proposal["Verifica"] = "✓ GIÀ PRESENTE"
        proposal["Sintesi"] = "DUPLICATO già presente nel database. " + proposal["Sintesi"]
    return proposal


def refine_batch_proposals(proposals: list[dict], signals_df: pd.DataFrame) -> list[dict]:
    """Use chronology only to fill missing context, never to invent a new directional call."""
    if not proposals:
        return proposals
    props = sorted(proposals, key=lambda x: x["Ricevuto"])
    dts = [pd.to_datetime(p["Ricevuto"]) for p in props]

    # 1) Asset propagation inside a tight chronological cluster.
    for i, p in enumerate(props):
        if p["Categoria"] == "INFO" or p["Asset"]:
            continue
        prev = props[i - 1] if i > 0 else None
        nxt = props[i + 1] if i + 1 < len(props) else None
        prev_gap = (dts[i] - dts[i - 1]).total_seconds() / 60 if prev is not None else 999
        next_gap = (dts[i + 1] - dts[i]).total_seconds() / 60 if nxt is not None else 999
        inferred = ""
        if prev and nxt and prev["Asset"] and prev["Asset"] == nxt["Asset"] and prev_gap <= 10 and next_gap <= 10:
            inferred = prev["Asset"]
        elif prev and prev["Asset"] and prev_gap <= 7 and prev["Categoria"] != "INFO":
            inferred = prev["Asset"]
        if inferred:
            p["Asset"] = inferred
            same = next((r for r in ASSET_RULES if r["code"] == inferred), None)
            if same:
                p["Mercato"] = same["name"]
            p["Verifica"] = "⚠ ASSET INFERITO DALLA SEQUENZA"
            p["Confidence"] = min(int(p["Confidence"]), 70)

    # 2) Propagate period/timeframe context within the same asset and <=10 minutes.
    for i, p in enumerate(props):
        if p["Categoria"] == "INFO" or not p["Asset"]:
            continue
        candidates = []
        for j, q in enumerate(props):
            if i == j or q["Asset"] != p["Asset"] or q["Categoria"] == "INFO":
                continue
            gap = abs((dts[i] - dts[j]).total_seconds()) / 60
            if gap <= 10:
                candidates.append((gap, q))
        candidates.sort(key=lambda x: x[0])
        if not p["Periodo"]:
            p["Periodo"] = next((q["Periodo"] for _, q in candidates if q["Periodo"]), "")
        if not p["TF"]:
            p["TF"] = next((q["TF"] for _, q in candidates if q["TF"]), "")
        if p["Bias"] == "NEUTRAL":
            directional = [(gap, q) for gap, q in candidates if q["Bias"] in ["LONG", "SHORT"]]
            if directional:
                dirs = {q["Bias"] for _, q in directional}
                if len(dirs) == 1:
                    p["Bias"] = directional[0][1]["Bias"]

    # 3) Re-run destination matching after contextual refinement.
    for p in props:
        p["Destinazione"] = recommend_destination(p, signals_df)
        if p["Destinazione"] not in ["NUOVO", "INFO"]:
            match = [r for _, r in signals_df.iterrows() if destination_label(r) == p["Destinazione"]]
            if match:
                r = match[0]
                if p["Bias"] == "NEUTRAL":
                    p["Bias"] = str(r["bias"])
                if not p["Periodo"]:
                    p["Periodo"] = str(r["period"])
                if not p["TF"]:
                    p["TF"] = str(r["timeframe"])
                p["Gruppo"] = f"EXISTING|{int(r['id'])}"
                if p["Verifica"] == "⚠ BIAS DA CONFERMARE":
                    p["Verifica"] = ""
        else:
            p["Gruppo"] = group_key(p["Asset"], p["Bias"], p["Periodo"], p["Categoria"], pd.to_datetime(p["Ricevuto"]).to_pydatetime())
        if p["Categoria"] != "INFO" and not p["Asset"]:
            p["Importa"] = False
            p["Verifica"] = "⚠ VERIFICA ASSET"
        elif p["Destinazione"] == "NUOVO" and p["Categoria"] in ["ENTRY", "SETUP", "WATCH"] and p["Bias"] == "NEUTRAL":
            p["Verifica"] = "⚠ BIAS DA CONFERMARE"
    return props


def setup_preview(editor_df: pd.DataFrame) -> pd.DataFrame:
    rows = editor_df[editor_df["Importa"] == True].copy()  # noqa: E712
    if rows.empty:
        return pd.DataFrame(columns=["Destinazione finale", "Asset", "Bias", "Periodo", "Immagini", "Controllo"])
    keys = []
    for _, r in rows.iterrows():
        dest = str(r["Destinazione"])
        key = dest if dest not in ["NUOVO", "INFO"] else ("INFO" if dest == "INFO" else f"NUOVO · {r['Gruppo']}")
        keys.append(key)
    rows["_final"] = keys
    out = []
    for key, g in rows.groupby("_final"):
        if key == "INFO":
            continue
        warnings = sorted({str(x) for x in g["Verifica"].fillna("") if str(x).strip()})
        out.append({
            "Destinazione finale": key,
            "Asset": first_nonempty(g, "Asset"),
            "Bias": first_nonempty(g, "Bias"),
            "Periodo": first_nonempty(g, "Periodo"),
            "Immagini": len(g),
            "Controllo": " | ".join(warnings) if warnings else "✓ OK",
        })
    return pd.DataFrame(out)


def merged_timeframes(rows: pd.DataFrame) -> str:
    vals = []
    for v in rows["TF"].fillna("").tolist():
        for x in str(v).split("/"):
            x = x.strip()
            if x and x not in vals:
                vals.append(x)
    return "/".join(vals)


def first_nonempty(rows: pd.DataFrame, col: str) -> str:
    for v in rows[col].fillna("").tolist():
        if str(v).strip():
            return str(v).strip()
    return ""


def import_batch(editor_df: pd.DataFrame, file_map: dict[str, dict], signals_df: pd.DataFrame) -> tuple[int, int, list[str]]:
    selected = editor_df[editor_df["Importa"] == True].copy()  # noqa: E712
    if selected.empty:
        return 0, 0, ["Nessuna riga selezionata."]
    selected = selected.sort_values("Ricevuto")
    dest_map = {destination_label(r): int(r["id"]) for _, r in signals_df.iterrows()}
    imported = 0
    skipped = 0
    messages = []
    created_group_ids: dict[str, int] = {}

    # Create NEW groups first so all images in the same group share one setup.
    new_rows = selected[selected["Destinazione"] == "NUOVO"]
    for grp, rows in new_rows.groupby("Gruppo", dropna=False):
        grp = str(grp or "").strip()
        if not grp or grp == "INFO":
            continue
        rows = rows.sort_values("Ricevuto")
        last = rows.iloc[-1]
        instrument = first_nonempty(rows, "Asset").upper()
        if not instrument:
            continue
        bias = first_nonempty(rows, "Bias") or "NEUTRAL"
        period = first_nonempty(rows, "Periodo")
        market = first_nonempty(rows, "Mercato")
        tf = merged_timeframes(rows)
        category = first_nonempty(rows.iloc[::-1], "Categoria") or "SETUP"
        status = str(last.get("Stato", "WATCH") or "WATCH")
        received_first = str(rows.iloc[0]["Ricevuto"]).replace(" ", "T")
        received_last = str(rows.iloc[-1]["Ricevuto"]).replace(" ", "T")
        key_base = re.sub(r"[^A-Za-z0-9|_-]+", "_", grp)[:110]
        setup_key = f"AUTO|{key_base}|{received_first}"
        data = {
            "setup_key": setup_key,
            "instrument": instrument,
            "market_name": market,
            "period": period,
            "timeframe": tf,
            "bias": bias,
            "signal_type": category,
            "entry_zone": first_nonempty(rows, "Entry"),
            "stop_level": first_nonempty(rows, "Stop"),
            "target": first_nonempty(rows, "Target"),
            "status": status if status in STATUS_ORDER else "WATCH",
            "next_action": str(last.get("Prossima azione", "") or ""),
            "validity_end": "",
            "confidence": int(pd.to_numeric(rows["Confidence"], errors="coerce").fillna(50).mean()),
            "notes": "Creato da Import multiplo V2. Entry/Stop/Target vanno confermati manualmente quando presenti nelle immagini.",
            "created_at": received_first,
            "last_update": received_last,
        }
        try:
            created_group_ids[grp] = add_signal(data)
        except sqlite3.IntegrityError:
            # rare collision: append micro timestamp
            data["setup_key"] += "|" + datetime.now().strftime("%f")
            created_group_ids[grp] = add_signal(data)

    for _, row in selected.iterrows():
        file_name = str(row["File"])
        item = file_map.get(file_name)
        if item is None:
            skipped += 1
            messages.append(f"File non trovato in sessione: {file_name}")
            continue
        file_hash = str(row.get("Hash", item["hash"]))
        if hash_exists(file_hash):
            skipped += 1
            continue
        dest = str(row["Destinazione"])
        signal_id = None
        if dest == "INFO":
            signal_id = None
        elif dest == "NUOVO":
            signal_id = created_group_ids.get(str(row["Gruppo"]))
            if signal_id is None:
                skipped += 1
                messages.append(f"{file_name}: gruppo NUOVO non valido; controlla Asset/Gruppo.")
                continue
        elif dest in dest_map:
            signal_id = dest_map[dest]
        else:
            skipped += 1
            messages.append(f"{file_name}: destinazione non riconosciuta.")
            continue

        image_path = save_file_bytes(file_name, item["bytes"])
        received_at = str(row["Ricevuto"]).replace(" ", "T")
        category = str(row["Categoria"] or "INFO")
        add_update(
            signal_id,
            received_at,
            category,
            str(row.get("Sintesi", "") or ""),
            image_path,
            str(row.get("OCR", "") or ""),
            file_hash,
            file_name,
        )
        imported += 1

        # Existing setup: only update operational fields that the user reviewed.
        if signal_id is not None and dest != "NUOVO":
            cur_df = load_signals()
            cur_match = cur_df[cur_df["id"] == signal_id]
            if not cur_match.empty:
                cur = cur_match.iloc[0].to_dict()
                cur["status"] = str(row.get("Stato", cur["status"]) or cur["status"])
                action = str(row.get("Prossima azione", "") or "").strip()
                if action:
                    cur["next_action"] = action
                for src, dst in [("Entry", "entry_zone"), ("Stop", "stop_level"), ("Target", "target")]:
                    val = str(row.get(src, "") or "").strip()
                    if val:
                        cur[dst] = val
                cur["last_update"] = received_at
                update_signal(signal_id, cur)

    return imported, skipped, messages

def load_prices() -> pd.DataFrame:
    conn = get_conn()
    df = pd.read_sql_query("SELECT * FROM prices ORDER BY instrument", conn)
    conn.close()
    return df


def upsert_price(instrument: str, current_price: float, price_time: str, source: str, trusted: bool = True, source_symbol: str = "", note: str = ""):
    instrument = str(instrument or "").strip().upper()
    if not instrument or current_price is None or not math.isfinite(float(current_price)):
        return
    conn = get_conn()
    conn.execute(
        """INSERT INTO prices (instrument,current_price,price_time,source,trusted,source_symbol,note)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(instrument) DO UPDATE SET
             current_price=excluded.current_price, price_time=excluded.price_time, source=excluded.source,
             trusted=excluded.trusted, source_symbol=excluded.source_symbol, note=excluded.note""",
        (instrument, float(current_price), price_time, source, 1 if trusted else 0, source_symbol, note),
    )
    conn.commit()
    conn.close()


def delete_price(instrument: str):
    conn = get_conn()
    conn.execute("DELETE FROM prices WHERE instrument=?", (str(instrument).upper(),))
    conn.commit()
    conn.close()


def fetch_yahoo_price(instrument: str) -> tuple[float | None, str, str]:
    """Return (price, timestamp, message). Yahoo data is intentionally treated as indicative."""
    code = str(instrument or "").upper()
    symbol = YAHOO_TICKERS.get(code, "")
    if not symbol:
        return None, "", "Nessun ticker Yahoo affidabile configurato per questo asset."
    if yf is None:
        return None, "", "Modulo yfinance non disponibile."
    try:
        hist = yf.Ticker(symbol).history(period="1d", interval="1m", auto_adjust=False, prepost=False)
        if hist is None or hist.empty or "Close" not in hist.columns:
            hist = yf.Ticker(symbol).history(period="5d", interval="5m", auto_adjust=False, prepost=False)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None, "", f"Nessun prezzo restituito da Yahoo per {symbol}."
        close = hist["Close"].dropna()
        if close.empty:
            return None, "", f"Nessun close valido per {symbol}."
        price = float(close.iloc[-1])
        idx = close.index[-1]
        try:
            ts = pd.Timestamp(idx).tz_convert("Europe/Rome").strftime("%Y-%m-%d %H:%M:%S %Z") if getattr(idx, "tzinfo", None) else pd.Timestamp(idx).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            ts = datetime.now().isoformat(timespec="seconds")
        return price, ts, f"{symbol} · dati gratuiti Yahoo, possibili ritardi/differenze di contratto"
    except Exception as exc:
        return None, "", f"Errore Yahoo {symbol}: {exc}"


def _numeric_candidates(token: str) -> list[float]:
    t = str(token or "").strip().replace(" ", "")
    if not t:
        return []
    sign = -1.0 if t.startswith("-") else 1.0
    t = t.lstrip("+-")
    out: list[float] = []
    def add(x):
        try:
            v = sign * float(x)
            if math.isfinite(v):
                out.append(v)
        except Exception:
            pass
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            add(t.replace(".", "").replace(",", "."))
        else:
            add(t.replace(",", ""))
    elif "," in t:
        parts = t.split(",")
        if len(parts) == 2:
            add(parts[0] + "." + parts[1])
            if len(parts[1]) == 3 and parts[0] not in ("0", "00"):
                add(parts[0] + parts[1])
        else:
            add("".join(parts[:-1]) + "." + parts[-1])
            add("".join(parts))
    elif "." in t:
        parts = t.split(".")
        add(t)
        if len(parts) == 2 and len(parts[1]) == 3 and parts[0] not in ("0", "00"):
            add(parts[0] + parts[1])
        elif len(parts) > 2:
            add("".join(parts))
    else:
        add(t)
    seen, unique = set(), []
    for v in out:
        key = round(v, 10)
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return unique


def parse_level_values(text: str, current_price: float | None = None) -> list[float]:
    tokens = re.findall(r"\d[\d.,]*", str(text or ""))
    vals = []
    for token in tokens:
        cands = _numeric_candidates(token)
        if not cands:
            continue
        if current_price and current_price > 0:
            def dist(v):
                if v == 0:
                    return 999.0
                return abs(math.log(abs(v) / abs(current_price)))
            chosen = min(cands, key=dist)
        else:
            chosen = cands[0]
        vals.append(float(chosen))
    return vals


def parse_entry_range(text: str, current_price: float | None = None) -> tuple[float | None, float | None]:
    vals = parse_level_values(text, current_price)
    if not vals:
        return None, None
    if len(vals) == 1:
        return vals[0], vals[0]
    return min(vals[0], vals[1]), max(vals[0], vals[1])


def parse_single_level(text: str, current_price: float | None = None) -> float | None:
    vals = parse_level_values(text, current_price)
    return vals[0] if vals else None


def assess_signal(row: dict | pd.Series, price_row: dict | pd.Series | None) -> dict:
    if price_row is None:
        return {"label": "⚪ PREZZO N/D", "distance": None, "detail": "Inserisci il prezzo attuale.", "trusted": False}
    try:
        cp = float(price_row["current_price"])
    except Exception:
        return {"label": "⚪ PREZZO N/D", "distance": None, "detail": "Prezzo attuale non valido.", "trusted": False}
    trusted = bool(int(price_row.get("trusted", 0))) if hasattr(price_row, "get") else bool(price_row["trusted"])
    bias = str(row.get("bias", "NEUTRAL") if hasattr(row, "get") else row["bias"]).upper()
    entry_text = str(row.get("entry_zone", "") if hasattr(row, "get") else row["entry_zone"])
    stop_text = str(row.get("stop_level", "") if hasattr(row, "get") else row["stop_level"])
    target_text = str(row.get("target", "") if hasattr(row, "get") else row["target"])
    low, high = parse_entry_range(entry_text, cp)
    stop = parse_single_level(stop_text, cp)
    target = parse_single_level(target_text, cp)
    prefix = "" if trusted else "≈ "

    if trusted and stop is not None:
        if bias == "LONG" and cp <= stop:
            return {"label": "🔴 INVALIDATO", "distance": None, "detail": f"Prezzo {cp:g} ≤ stop {stop:g}.", "trusted": trusted}
        if bias == "SHORT" and cp >= stop:
            return {"label": "🔴 INVALIDATO", "distance": None, "detail": f"Prezzo {cp:g} ≥ stop {stop:g}.", "trusted": trusted}

    if target is not None:
        reached = (bias == "LONG" and cp >= target) or (bias == "SHORT" and cp <= target)
        if reached:
            return {"label": prefix + "⚪ TARGET SUPERATO", "distance": None, "detail": "Il movimento indicato è già arrivato oltre il target: non considerarlo una nuova entry senza revisione.", "trusted": trusted}

    if low is None or high is None:
        return {"label": prefix + "⚠ LIVELLI N/D", "distance": None, "detail": "Prezzo disponibile, ma Entry/Zona non è numerica o manca.", "trusted": trusted}

    if low <= cp <= high:
        return {"label": prefix + "🟢 IN ZONA ENTRY", "distance": 0.0, "detail": f"Prezzo {cp:g} dentro zona {low:g}–{high:g}.", "trusted": trusted}

    nearest = low if cp < low else high
    dist_pct = abs(cp - nearest) / abs(cp) * 100 if cp else None
    if bias == "LONG":
        if cp > high:
            label = "🟡 ATTESA PULLBACK"
            detail = f"Prezzo sopra la zona long ({low:g}–{high:g}); attesa di ritorno verso l'entry."
        else:
            label = "🟠 SOTTO ENTRY"
            detail = f"Prezzo sotto la zona long ({low:g}–{high:g}); setup da ricontrollare prima di entrare."
    elif bias == "SHORT":
        if cp < low:
            label = "🟡 ATTESA RIMBALZO"
            detail = f"Prezzo sotto la zona short ({low:g}–{high:g}); attesa di risalita verso l'entry."
        else:
            label = "🟠 SOPRA ENTRY"
            detail = f"Prezzo sopra la zona short ({low:g}–{high:g}); setup da ricontrollare prima di entrare."
    else:
        label = "⚪ BIAS N/D"
        detail = "Bias non definito: confronto prezzo/entry solo informativo."
    return {"label": prefix + label, "distance": dist_pct, "detail": detail, "trusted": trusted}


def build_price_map(prices_df: pd.DataFrame) -> dict[str, dict]:
    if prices_df.empty:
        return {}
    return {str(r["instrument"]).upper(): r.to_dict() for _, r in prices_df.iterrows()}


def format_price(v) -> str:
    try:
        x = float(v)
        if abs(x) >= 1000:
            return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if abs(x) >= 10:
            return f"{x:.3f}".rstrip("0").rstrip(".").replace(".", ",")
        return f"{x:.5f}".rstrip("0").rstrip(".").replace(".", ",")
    except Exception:
        return "—"


init_db()
auto_expire()

st.title("📡 Signal Radar V2.2")
st.caption("WhatsApp → OCR vincolato → deduplica → raggruppamento → revisione → una riga per setup.")

signals = load_signals()
prices = load_prices()
price_map = build_price_map(prices)

with st.sidebar:
    st.header("Filtri")
    all_status = [s for s in STATUS_ORDER if not signals.empty and s in signals["status"].unique().tolist()]
    selected_status = st.multiselect("Stato", all_status, default=[s for s in ["WATCH", "READY", "TRIGGERED"] if s in all_status])
    instruments = sorted(signals["instrument"].dropna().unique().tolist()) if not signals.empty else []
    selected_instruments = st.multiselect("Asset", instruments)
    selected_bias = st.multiselect("Bias", BIAS_OPTIONS)
    st.divider()
    ocr_ok = pytesseract is not None and shutil.which("tesseract") is not None
    st.caption(f"OCR locale gratuito: {'✅ disponibile' if ocr_ok else '⚠️ non disponibile'}")
    st.caption("V2.2: prezzi correnti + controllo validità. Entry/Stop/Target restano da confermare manualmente; Yahoo è solo indicativo.")

filtered = signals.copy()
if selected_status:
    filtered = filtered[filtered["status"].isin(selected_status)]
if selected_instruments:
    filtered = filtered[filtered["instrument"].isin(selected_instruments)]
if selected_bias:
    filtered = filtered[filtered["bias"].isin(selected_bias)]

active = signals[signals["status"].isin(["WATCH", "READY", "TRIGGERED"])] if not signals.empty else signals
ready = signals[signals["status"] == "READY"] if not signals.empty else signals
triggered = signals[signals["status"] == "TRIGGERED"] if not signals.empty else signals
expired = signals[signals["status"].isin(["EXPIRED", "INVALIDATED"])] if not signals.empty else signals

m1, m2, m3, m4 = st.columns(4)
m1.metric("Setup attivi", len(active))
m2.metric("Ready", len(ready))
m3.metric("Triggered", len(triggered))
m4.metric("Scaduti / invalidati", len(expired))

priority_map = {"TRIGGERED": 0, "READY": 1, "WATCH": 2, "CLOSED": 3, "INVALIDATED": 4, "EXPIRED": 5, "INFO": 6}
if not filtered.empty:
    filtered = filtered.assign(_priority=filtered["status"].map(priority_map).fillna(99)).sort_values(["_priority", "last_update"], ascending=[True, False])

tab_dash, tab_detail, tab_prices, tab_batch, tab_single, tab_archive = st.tabs([
    "🎯 Active Signals", "🧭 Dettaglio setup", "💹 Prezzi", "⚡ Import multiplo", "➕ Singola immagine", "🗂 Archivio"
])

with tab_dash:
    st.subheader("Active Signals")
    if filtered.empty:
        st.info("Nessun setup con i filtri correnti.")
    else:
        view = filtered.copy()
        view["Stato"] = view["status"].map(lambda x: f"{STATUS_ICON.get(x,'')} {x}")
        view["Bias"] = view["bias"].map(lambda x: f"{BIAS_ICON.get(x,'')} {x}")
        view["Ultimo agg."] = pd.to_datetime(view["last_update"], errors="coerce").dt.strftime("%d/%m %H:%M")
        view["Validità"] = view["validity_end"].replace("", "—")
        view["Prezzo attuale"] = view.apply(lambda r: format_price(price_map.get(str(r["instrument"]).upper(), {}).get("current_price")), axis=1)
        view["Check prezzo"] = view.apply(lambda r: assess_signal(r, price_map.get(str(r["instrument"]).upper()))["label"], axis=1)
        view["Dist. entry"] = view.apply(lambda r: (lambda d: "—" if d is None else f"{d:.2f}%")(assess_signal(r, price_map.get(str(r["instrument"]).upper()))["distance"]), axis=1)
        view = view[["Stato", "instrument", "Bias", "period", "timeframe", "Prezzo attuale", "entry_zone", "Dist. entry", "Check prezzo", "target", "next_action", "Validità", "Ultimo agg."]]
        view.columns = ["Stato", "Asset", "Bias", "Periodo", "TF", "Prezzo attuale", "Entry / zona", "Dist. Entry", "VALIDITÀ PREZZO", "Target", "PROSSIMA AZIONE", "Validità", "Ultimo agg."]
        st.dataframe(view, use_container_width=True, hide_index=True, height=min(560, 72 + 35 * len(view)))

        st.markdown("#### Focus operativo")
        for _, r in filtered[filtered["status"].isin(["TRIGGERED", "READY", "WATCH"])].head(8).iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([1.2, 1, 3])
                c1.markdown(f"### {STATUS_ICON.get(r['status'],'')} {r['instrument']}")
                c1.caption(f"{BIAS_ICON.get(r['bias'],'')} {r['bias']} · {r['period']} · {r['timeframe']}")
                c2.markdown(f"**{r['status']}**")
                c2.caption(f"Validità: {r['validity_end'] or '—'}")
                pinfo = price_map.get(str(r["instrument"]).upper())
                check = assess_signal(r, pinfo)
                c3.markdown(f"**Prossima azione:** {r['next_action'] or '—'}")
                if pinfo:
                    c3.markdown(f"**Prezzo attuale:** {format_price(pinfo.get('current_price'))} · **{check['label']}**")
                    c3.caption(f"{check['detail']} · Fonte: {pinfo.get('source','—')} · {pinfo.get('price_time','—')}")
                else:
                    c3.markdown("**Prezzo attuale:** — · ⚪ PREZZO N/D")
                if r["entry_zone"]:
                    c3.caption(f"Entry/Zona: {r['entry_zone']} · Stop: {r['stop_level'] or '—'} · Target: {r['target'] or '—'}")

with tab_detail:
    st.subheader("Cronologia di un setup")
    all_rows = signals.to_dict("records") if not signals.empty else []
    if not all_rows:
        st.info("Nessun setup salvato.")
    else:
        labels = {signal_label(r): r for r in all_rows}
        chosen = st.selectbox("Seleziona setup", list(labels.keys()), key="detail_select")
        r = labels[chosen]
        c1, c2, c3 = st.columns([1, 1, 2])
        c1.metric("Asset", r["instrument"])
        c2.metric("Stato", f"{STATUS_ICON.get(r['status'],'')} {r['status']}")
        c3.markdown(f"**Prossima azione**  \n{r['next_action'] or '—'}")

        info1, info2 = st.columns(2)
        with info1:
            st.markdown(f"**Bias:** {r['bias']}  ")
            st.markdown(f"**Periodo:** {r['period'] or '—'}  ")
            st.markdown(f"**Timeframe:** {r['timeframe'] or '—'}  ")
            st.markdown(f"**Tipo:** {r['signal_type'] or '—'}")
        with info2:
            st.markdown(f"**Entry/Zona:** {r['entry_zone'] or '—'}  ")
            st.markdown(f"**Stop:** {r['stop_level'] or '—'}  ")
            st.markdown(f"**Target:** {r['target'] or '—'}  ")
            st.markdown(f"**Validità:** {r['validity_end'] or '—'}")
        pinfo = price_map.get(str(r["instrument"]).upper())
        price_check = assess_signal(r, pinfo)
        with st.container(border=True):
            pc1, pc2, pc3 = st.columns([1, 1.3, 2.7])
            pc1.metric("Prezzo attuale", format_price(pinfo.get("current_price")) if pinfo else "—")
            pc2.markdown(f"**{price_check['label']}**")
            if pinfo:
                pc2.caption(f"Fonte: {pinfo.get('source','—')} · {pinfo.get('price_time','—')}")
            pc3.write(price_check["detail"])
            if price_check["distance"] is not None:
                pc3.caption(f"Distanza dalla zona entry: {price_check['distance']:.2f}%")
        if r["notes"]:
            st.info(r["notes"])

        st.markdown("### Timeline")
        updates = load_updates(int(r["id"]))
        if updates.empty:
            st.caption("Nessun aggiornamento associato.")
        for _, u in updates.iterrows():
            dt_txt = pd.to_datetime(u["received_at"], errors="coerce").strftime("%d/%m/%Y %H:%M")
            summ = str(u["summary"] or "")
            with st.expander(f"{dt_txt} · {u['category']} · {summ[:90]}", expanded=False):
                if summ:
                    st.write(summ)
                if u["raw_text"]:
                    with st.expander("Testo OCR"):
                        st.text(u["raw_text"])
                display_image(u["image_path"], caption=dt_txt)

        st.markdown("### Aggiorna stato / operatività")
        with st.form(f"edit_{r['id']}"):
            ec1, ec2, ec3 = st.columns(3)
            status_new = ec1.selectbox("Stato", STATUS_ORDER, index=STATUS_ORDER.index(r["status"]) if r["status"] in STATUS_ORDER else 0)
            bias_new = ec2.selectbox("Bias", BIAS_OPTIONS, index=BIAS_OPTIONS.index(r["bias"]) if r["bias"] in BIAS_OPTIONS else 2)
            conf_new = ec3.slider("Confidence %", 0, 100, int(r["confidence"] or 100), 5)
            entry_new = st.text_input("Entry / zona", r["entry_zone"])
            stop_new = st.text_input("Stop", r["stop_level"])
            target_new = st.text_input("Target", r["target"])
            action_new = st.text_area("Prossima azione", r["next_action"])
            notes_new = st.text_area("Note", r["notes"])
            validity_new = st.text_input("Validità (YYYY-MM-DD, vuoto = nessuna)", r["validity_end"])
            st.markdown("**Prezzo attuale (facoltativo)**")
            existing_p = price_map.get(str(r["instrument"]).upper(), {})
            pcol1, pcol2 = st.columns([1, 2])
            current_price_new = pcol1.number_input("Prezzo", min_value=0.0, value=float(existing_p.get("current_price", 0.0) or 0.0), format="%.6f", key=f"detail_price_{r['id']}")
            current_price_note = pcol2.text_input("Nota prezzo / contratto", str(existing_p.get("note", "") or ""), key=f"detail_price_note_{r['id']}")
            if st.form_submit_button("Salva modifiche", type="primary"):
                data = dict(r)
                data.update({
                    "status": status_new, "bias": bias_new, "confidence": conf_new, "entry_zone": entry_new,
                    "stop_level": stop_new, "target": target_new, "next_action": action_new, "notes": notes_new,
                    "validity_end": validity_new, "last_update": datetime.now().isoformat(timespec="seconds"),
                })
                update_signal(int(r["id"]), data)
                if current_price_new > 0:
                    upsert_price(r["instrument"], current_price_new, datetime.now().isoformat(timespec="seconds"), "TradingView / broker (manuale)", True, "", current_price_note)
                st.success("Setup aggiornato.")
                st.rerun()

with tab_prices:
    st.subheader("💹 Prezzi correnti")
    st.caption("Il prezzo serve a capire se un setup è ancora vicino all'entry, è già passato oltre il target o ha violato lo stop. Per decisioni operative usa preferibilmente il prezzo manuale preso dallo stesso future/continuous del segnale.")

    active_assets = sorted(set(signals[signals["status"].isin(["WATCH", "READY", "TRIGGERED"])]["instrument"].dropna().astype(str).str.upper().tolist())) if not signals.empty else []
    known_assets = sorted(set(active_assets + list(YAHOO_TICKERS.keys()) + (prices["instrument"].astype(str).str.upper().tolist() if not prices.empty else [])))

    with st.form("manual_price_form"):
        c1, c2, c3 = st.columns([1, 1, 2])
        asset_price = c1.selectbox("Asset", known_assets if known_assets else ["ES"], index=0)
        existing = price_map.get(str(asset_price).upper(), {})
        price_val = c2.number_input("Prezzo attuale", min_value=0.0, value=float(existing.get("current_price", 0.0) or 0.0), format="%.6f")
        price_note = c3.text_input("Nota / contratto", str(existing.get("note", "") or ""), placeholder="es. TradingView ES1! / contratto settembre")
        trusted = st.checkbox("Usa questo prezzo per il controllo di validità", value=True, help="Attivalo solo se il prezzo è dello stesso strumento/base del segnale. I prezzi Yahoo vengono salvati come indicativi e non invalidano automaticamente uno stop.")
        if st.form_submit_button("💾 Salva prezzo manuale", type="primary"):
            if price_val <= 0:
                st.warning("Inserisci un prezzo maggiore di zero.")
            else:
                upsert_price(asset_price, price_val, datetime.now().isoformat(timespec="seconds"), "TradingView / broker (manuale)", trusted, "", price_note)
                st.success(f"Prezzo {asset_price} aggiornato.")
                st.rerun()

    st.markdown("### Aggiornamento gratuito indicativo")
    st.caption("Yahoo può essere utile per un colpo d'occhio su alcuni futures CME/CBOT/NYMEX, ma può essere ritardato o riferirsi a un contratto diverso. Per questo NON viene considerato affidabile per invalidare automaticamente un setup.")
    yahoo_assets = [a for a in active_assets if a in YAHOO_TICKERS]
    if yahoo_assets:
        selected_yahoo = st.multiselect("Asset da aggiornare via Yahoo", yahoo_assets, default=yahoo_assets)
        if st.button("🌐 Aggiorna prezzi Yahoo", use_container_width=True, disabled=not selected_yahoo):
            prog = st.progress(0, text="Aggiornamento prezzi...")
            ok, errs = 0, []
            for i, asset in enumerate(selected_yahoo, 1):
                px, ts, msg = fetch_yahoo_price(asset)
                if px is not None:
                    upsert_price(asset, px, ts or datetime.now().isoformat(timespec="seconds"), "Yahoo Finance (indicativo)", False, YAHOO_TICKERS.get(asset, ""), msg)
                    ok += 1
                else:
                    errs.append(f"{asset}: {msg}")
                prog.progress(i / len(selected_yahoo), text=f"{asset} · {i}/{len(selected_yahoo)}")
            prog.empty()
            if ok:
                st.success(f"Aggiornati {ok} prezzi indicativi.")
            for e in errs:
                st.warning(e)
            st.rerun()
    else:
        st.info("Nessun setup attivo con ticker Yahoo configurato. DAX e FESX, per esempio, restano volutamente manuali per evitare proxy non confrontabili con i livelli futures.")

    st.markdown("### Prezzi salvati")
    prices_now = load_prices()
    if prices_now.empty:
        st.info("Nessun prezzo salvato.")
    else:
        pview = prices_now.copy()
        pview["Prezzo"] = pview["current_price"].map(format_price)
        pview["Affidabilità"] = pview["trusted"].map(lambda x: "✅ usa per validità" if int(x) else "⚠ indicativo")
        pview = pview[["instrument", "Prezzo", "price_time", "source", "Affidabilità", "note"]]
        pview.columns = ["Asset", "Prezzo", "Data/ora", "Fonte", "Uso", "Nota / contratto"]
        st.dataframe(pview, use_container_width=True, hide_index=True)
        del_asset = st.selectbox("Rimuovi un prezzo salvato", ["—"] + prices_now["instrument"].astype(str).tolist())
        if del_asset != "—" and st.button("🗑 Rimuovi prezzo"):
            delete_price(del_asset)
            st.rerun()

with tab_batch:
    st.subheader("⚡ Import multiplo WhatsApp")
    st.caption("Carica molte immagini insieme. V2.2 usa una whitelist asset, legge soprattutto il titolo, corregge anni OCR improbabili e tenta di collegare gli aggiornamenti allo stesso setup.")

    uploaded_files = st.file_uploader(
        "Trascina qui gli screenshot WhatsApp",
        type=["png", "jpg", "jpeg", "webp"],
        accept_multiple_files=True,
        key="batch_uploader",
    )

    if uploaded_files:
        items = []
        for uf in uploaded_files:
            data = uf.getvalue()
            items.append({"name": uf.name, "bytes": data, "hash": hash_bytes(data), "dt": parse_received_at(uf.name)})
        items = sorted(items, key=lambda x: (x["dt"], x["name"]))
        token = "|".join(x["hash"] for x in items)
        if st.session_state.get("batch_token") != token:
            st.session_state["batch_token"] = token
            st.session_state["batch_items"] = items
            st.session_state.pop("batch_df", None)
        file_map = {x["name"]: x for x in st.session_state.get("batch_items", items)}

        dup_count = sum(hash_exists(x["hash"]) for x in items)
        c1, c2, c3 = st.columns(3)
        c1.metric("Immagini caricate", len(items))
        c2.metric("Già presenti", dup_count)
        c3.metric("Da analizzare", len(items) - dup_count)

        if st.button("🔎 Analizza e proponi", type="primary", use_container_width=True):
            proposals = []
            progress = st.progress(0, text="Analisi immagini...")
            for i, item in enumerate(items, start=1):
                proposals.append(analyze_item(item, load_signals()))
                progress.progress(i / len(items), text=f"Analisi {i}/{len(items)} · {item['name']}")
            proposals = refine_batch_proposals(proposals, load_signals())
            progress.empty()
            st.session_state["batch_df"] = pd.DataFrame(proposals)

        if "batch_df" in st.session_state:
            batch_df = st.session_state["batch_df"].copy()
            current_signals = load_signals()
            existing_destinations = [destination_label(r) for _, r in current_signals.iterrows()]
            destination_options = ["NUOVO", "INFO"] + existing_destinations

            st.markdown("### 1. Controlla le proposte")
            st.info("V2.1 prova prima a collegare l’immagine a un setup esistente. Solo se **Destinazione = NUOVO**, la colonna **Gruppo** decide quali immagini diventano un unico setup. Le righe con ⚠ richiedono controllo.")

            edited = st.data_editor(
                batch_df,
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                key="batch_editor",
                column_order=[
                    "Importa", "Ricevuto", "File", "Asset", "Bias", "Periodo", "TF", "Categoria", "Stato",
                    "Destinazione", "Gruppo", "Verifica", "Prossima azione", "Entry", "Stop", "Target", "Confidence", "Sintesi",
                ],
                column_config={
                    "Importa": st.column_config.CheckboxColumn("Importa", width="small"),
                    "Ricevuto": st.column_config.TextColumn("Ricevuto", width="medium"),
                    "File": st.column_config.TextColumn("File", disabled=True, width="large"),
                    "Asset": st.column_config.TextColumn("Asset", width="small"),
                    "Bias": st.column_config.SelectboxColumn("Bias", options=BIAS_OPTIONS, width="small"),
                    "Periodo": st.column_config.TextColumn("Periodo", width="medium"),
                    "TF": st.column_config.TextColumn("TF", width="small"),
                    "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES, width="small"),
                    "Stato": st.column_config.SelectboxColumn("Stato", options=STATUS_ORDER, width="small"),
                    "Destinazione": st.column_config.SelectboxColumn("Destinazione", options=destination_options, width="large"),
                    "Gruppo": st.column_config.TextColumn("Gruppo", width="large"),
                    "Verifica": st.column_config.TextColumn("Verifica", width="medium"),
                    "Prossima azione": st.column_config.TextColumn("Prossima azione", width="large"),
                    "Entry": st.column_config.TextColumn("Entry", width="medium"),
                    "Stop": st.column_config.TextColumn("Stop", width="medium"),
                    "Target": st.column_config.TextColumn("Target", width="medium"),
                    "Confidence": st.column_config.NumberColumn("Conf.%", min_value=0, max_value=100, step=5, width="small"),
                    "Sintesi": st.column_config.TextColumn("Sintesi", width="large"),
                },
                disabled=["File", "OCR", "Hash"],
            )
            st.session_state["batch_df"] = edited.copy()

            if st.button("♻️ Ricalcola destinazioni dopo le correzioni", use_container_width=True):
                recalculated = refine_batch_proposals(edited.to_dict("records"), current_signals)
                st.session_state["batch_df"] = pd.DataFrame(recalculated)
                st.rerun()

            st.markdown("### 2. Setup finali proposti")
            preview_setups = setup_preview(edited)
            if not preview_setups.empty:
                pmap_now = build_price_map(load_prices())
                preview_setups["Prezzo attuale"] = preview_setups["Asset"].map(lambda a: format_price(pmap_now.get(str(a).upper(), {}).get("current_price")))
                def _preview_check(pr):
                    rows_g = edited[(edited["Importa"] == True) & (edited["Asset"].astype(str) == str(pr["Asset"]))]  # noqa: E712
                    rr = rows_g.iloc[0] if not rows_g.empty else {}
                    pseudo = {"bias": pr.get("Bias", "NEUTRAL"), "entry_zone": rr.get("Entry", "") if hasattr(rr, "get") else "", "stop_level": rr.get("Stop", "") if hasattr(rr, "get") else "", "target": rr.get("Target", "") if hasattr(rr, "get") else ""}
                    return assess_signal(pseudo, pmap_now.get(str(pr["Asset"]).upper()))["label"]
                preview_setups["Check prezzo"] = preview_setups.apply(_preview_check, axis=1)
            if preview_setups.empty:
                st.caption("Nessun setup operativo selezionato.")
            else:
                st.dataframe(preview_setups, use_container_width=True, hide_index=True)
                st.caption(f"Risultato previsto: **{len(preview_setups)} setup operativi**. Le immagini INFO restano fuori dagli Active Signals.")

            st.markdown("### 3. Anteprima immagini")
            preview_names = edited[edited["Importa"] == True]["File"].tolist()  # noqa: E712
            if preview_names:
                preview = st.selectbox("Scegli immagine da controllare", preview_names, key="batch_preview")
                pitem = file_map.get(preview)
                if pitem:
                    cimg, ctxt = st.columns([1.5, 1])
                    with cimg:
                        st.image(pitem["bytes"], caption=preview, use_container_width=True)
                    with ctxt:
                        prow = edited[edited["File"] == preview].iloc[0]
                        st.markdown(f"**Asset:** {prow['Asset'] or '—'}")
                        st.markdown(f"**Bias:** {prow['Bias']}")
                        st.markdown(f"**Categoria:** {prow['Categoria']}")
                        st.markdown(f"**Destinazione:** {prow['Destinazione']}")
                        st.markdown(f"**Gruppo:** {prow['Gruppo']}")
                        with st.expander("Testo OCR"):
                            st.text(str(prow.get("OCR", "")))

            st.markdown("### 4. Importa")
            selected_count = int((edited["Importa"] == True).sum())  # noqa: E712
            st.caption(f"Pronte per l'importazione: {selected_count} immagini. I duplicati sono normalmente deselezionati.")
            if st.button(f"✅ IMPORTA TUTTO ({selected_count})", type="primary", use_container_width=True, disabled=selected_count == 0):
                imported, skipped, messages = import_batch(edited, file_map, load_signals())
                if imported:
                    st.success(f"Importate {imported} immagini. Saltate {skipped}.")
                    st.session_state.pop("batch_df", None)
                    st.session_state.pop("batch_token", None)
                    st.session_state.pop("batch_items", None)
                    if messages:
                        with st.expander("Dettagli"):
                            for msg in messages:
                                st.write("•", msg)
                    st.rerun()
                else:
                    st.warning(f"Nessuna immagine importata. Saltate {skipped}.")
                    if messages:
                        for msg in messages:
                            st.write("•", msg)
    else:
        st.info("Carica 2 o più screenshot per usare l'import multiplo. Puoi anche caricarne uno solo per testare l'OCR.")

with tab_single:
    st.subheader("Aggiunta manuale singola")
    st.caption("Fallback rapido quando vuoi inserire una sola immagine senza passare dall'analisi batch.")
    uploaded = st.file_uploader("Screenshot", type=["png", "jpg", "jpeg", "webp"], key="single_upload")
    if uploaded is not None:
        data_bytes = uploaded.getvalue()
        st.image(data_bytes, use_container_width=True)
        parsed_dt = parse_received_at(uploaded.name)
        current_signals = load_signals()
        has_signals = not current_signals.empty
        mode_options = ["Aggiornamento di un setup esistente", "Nuovo setup", "Solo INFO / didattica"]
        mode = st.radio("Questa immagine è…", mode_options, index=0 if has_signals else 1, horizontal=True, key="single_mode")

        if mode == "Aggiornamento di un setup esistente":
            if not has_signals:
                st.warning("Non esistono ancora setup da aggiornare.")
            else:
                choices = {signal_label(r): r for r in current_signals.to_dict("records")}
                selected = st.selectbox("Setup da aggiornare", list(choices.keys()), key="single_existing")
                base = choices[selected]
                with st.form("single_update_form"):
                    received_date = st.date_input("Data ricezione", parsed_dt.date())
                    received_time = st.time_input("Ora ricezione", parsed_dt.time().replace(microsecond=0))
                    category = st.selectbox("Categoria", CATEGORIES)
                    summary = st.text_area("Riassunto", "")
                    new_status = st.selectbox("Nuovo stato", STATUS_ORDER, index=STATUS_ORDER.index(base["status"]) if base["status"] in STATUS_ORDER else 0)
                    new_action = st.text_area("Prossima azione", base["next_action"])
                    if st.form_submit_button("Collega e salva", type="primary"):
                        h = hash_bytes(data_bytes)
                        if hash_exists(h):
                            st.error("Questa immagine risulta già importata.")
                        else:
                            received_at = datetime.combine(received_date, received_time).isoformat(timespec="seconds")
                            image_path = save_file_bytes(uploaded.name, data_bytes)
                            add_update(int(base["id"]), received_at, category, summary, image_path, "", h, uploaded.name)
                            data = dict(base)
                            data["status"] = new_status
                            data["next_action"] = new_action
                            data["last_update"] = received_at
                            update_signal(int(base["id"]), data)
                            st.success("Aggiornamento collegato.")
                            st.rerun()

        elif mode == "Nuovo setup":
            with st.form("single_new_form"):
                c1, c2, c3 = st.columns(3)
                instrument = c1.text_input("Asset / ticker", "")
                bias = c2.selectbox("Bias", BIAS_OPTIONS)
                status = c3.selectbox("Stato iniziale", ["WATCH", "READY", "TRIGGERED"])
                market_name = st.text_input("Nome mercato", "")
                period = st.text_input("Periodo / setup", "es. W3 Agosto 2026")
                timeframe = st.text_input("Timeframe", "")
                signal_type = st.selectbox("Tipo", ["WATCH", "SETUP", "ENTRY", "MACRO", "RULE"])
                entry_zone = st.text_input("Entry / zona", "")
                stop_level = st.text_input("Stop", "")
                target = st.text_input("Target", "")
                next_action = st.text_area("Prossima azione", "")
                validity_end = st.text_input("Validità (YYYY-MM-DD)", "")
                confidence = st.slider("Confidence %", 0, 100, 100, 5)
                summary = st.text_area("Riassunto dell'immagine", "")
                notes = st.text_area("Note", "")
                received_date = st.date_input("Data ricezione", parsed_dt.date())
                received_time = st.time_input("Ora ricezione", parsed_dt.time().replace(microsecond=0))
                if st.form_submit_button("Crea setup", type="primary"):
                    if not instrument.strip():
                        st.error("Inserisci almeno l'asset/ticker.")
                    else:
                        received_at = datetime.combine(received_date, received_time).isoformat(timespec="seconds")
                        setup_key = f"{instrument.strip().upper()}|{bias}|{period.strip()}|{received_at}"
                        data = {
                            "setup_key": setup_key, "instrument": instrument.strip().upper(), "market_name": market_name,
                            "period": period, "timeframe": timeframe, "bias": bias, "signal_type": signal_type,
                            "entry_zone": entry_zone, "stop_level": stop_level, "target": target, "status": status,
                            "next_action": next_action, "validity_end": validity_end, "confidence": confidence,
                            "notes": notes, "created_at": received_at, "last_update": received_at,
                        }
                        try:
                            sid = add_signal(data)
                            h = hash_bytes(data_bytes)
                            image_path = save_file_bytes(uploaded.name, data_bytes)
                            add_update(sid, received_at, signal_type, summary, image_path, "", h, uploaded.name)
                            st.success("Nuovo setup creato.")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("Setup duplicato. Usa 'Aggiornamento di un setup esistente'.")
        else:
            with st.form("single_info_form"):
                received_date = st.date_input("Data ricezione", parsed_dt.date())
                received_time = st.time_input("Ora ricezione", parsed_dt.time().replace(microsecond=0))
                summary = st.text_area("Riassunto / testo", "")
                if st.form_submit_button("Archivia come INFO"):
                    h = hash_bytes(data_bytes)
                    if hash_exists(h):
                        st.error("Questa immagine risulta già importata.")
                    else:
                        received_at = datetime.combine(received_date, received_time).isoformat(timespec="seconds")
                        image_path = save_file_bytes(uploaded.name, data_bytes)
                        add_update(None, received_at, "INFO", summary, image_path, "", h, uploaded.name)
                        st.success("Archiviato come INFO.")
                        st.rerun()

with tab_archive:
    st.subheader("Archivio e backup")
    archive = load_signals()
    if not archive.empty:
        exp = archive.copy()
        exp["status"] = exp["status"].map(lambda x: f"{STATUS_ICON.get(x,'')} {x}")
        st.dataframe(exp, use_container_width=True, hide_index=True)
        st.download_button("⬇️ Esporta setup CSV", archive.to_csv(index=False).encode("utf-8-sig"), "signal_radar_setup.csv", "text/csv")
    all_updates = load_updates()
    if not all_updates.empty:
        st.download_button("⬇️ Esporta timeline CSV", all_updates.to_csv(index=False).encode("utf-8-sig"), "signal_radar_timeline.csv", "text/csv")
    if DB_PATH.exists():
        st.download_button("💾 Backup database SQLite", DB_PATH.read_bytes(), "signal_radar.db", "application/octet-stream")
        st.warning("Su Streamlit Community Cloud il filesystem può essere temporaneo. Fai periodicamente il backup del database; una V3 potrà usare un database persistente esterno.")

    with st.expander("🧹 Manutenzione V2.1 — ripristino test iniziale"):
        st.write("Usa questa funzione **solo adesso**, se la V2 ha creato i duplicati/falsi setup visibili nel test. Cancella le righe del database e ricrea il baseline corretto dei 17 screenshot: 6N, DAX, 6J, FESX ed ES.")
        confirm_reset = st.checkbox("Confermo: voglio eliminare i setup di test attuali e ripristinare il baseline iniziale", key="confirm_seed_reset")
        if st.button("🔄 RIPRISTINA BASELINE TEST", disabled=not confirm_reset, type="secondary"):
            reset_to_seed()
            st.success("Baseline ripristinato. I duplicati del test V2 sono stati rimossi.")
            st.rerun()

    with st.expander("INFO / immagini non assegnate"):
        if all_updates.empty:
            st.caption("Archivio vuoto.")
        else:
            unassigned = all_updates[all_updates["signal_id"].isna()]
            if unassigned.empty:
                st.caption("Nessuna INFO non assegnata.")
            else:
                for _, u in unassigned.sort_values("received_at", ascending=False).iterrows():
                    st.markdown(f"**{u['received_at']} · {u['category']}** — {u['summary']}")
                    display_image(u["image_path"])
                    st.divider()

st.divider()
st.caption("Signal Radar V2.2 · OCR conservativo, whitelist asset, deduplica e raggruppamento. Nessun livello numerico viene accettato automaticamente come segnale di trading.")
