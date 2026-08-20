from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import sqlite3
import unicodedata
from datetime import date, datetime
from pathlib import Path

import numpy as np
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

try:
    import cv2
except Exception:
    cv2 = None

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

# Prezzi online: la V2.3 prova automaticamente a recuperare il future corrispondente.
# DAX/FESX hanno anche un controllo di coerenza contro l'indice spot: se il ticker
# Yahoo del future è palesemente obsoleto/sbagliato, viene scartato e si passa al
# fallback manuale. Nessun proxy spot viene usato come prezzo operativo del future.
ONLINE_PRICE_CONFIG = {
    "ES": {"symbol": "ES=F"}, "NQ": {"symbol": "NQ=F"}, "YM": {"symbol": "YM=F"}, "RTY": {"symbol": "RTY=F"},
    "CL": {"symbol": "CL=F"}, "NG": {"symbol": "NG=F"}, "GC": {"symbol": "GC=F"}, "SI": {"symbol": "SI=F"}, "HG": {"symbol": "HG=F"},
    "ZB": {"symbol": "ZB=F"}, "ZN": {"symbol": "ZN=F"}, "ZF": {"symbol": "ZF=F"}, "ZT": {"symbol": "ZT=F"},
    "6E": {"symbol": "6E=F"}, "6B": {"symbol": "6B=F"}, "6A": {"symbol": "6A=F"}, "6C": {"symbol": "6C=F"},
    "6J": {"symbol": "6J=F"}, "6N": {"symbol": "6N=F"}, "6S": {"symbol": "6S=F"},
    "ZC": {"symbol": "ZC=F"}, "ZS": {"symbol": "ZS=F"}, "ZW": {"symbol": "ZW=F"}, "HE": {"symbol": "HE=F"}, "LE": {"symbol": "LE=F"},
    # Ticker Eurex presenti su Yahoo, accettati solo se coerenti con lo spot.
    "DAX": {"symbol": "FDAX.EX", "anchor": "^GDAXI", "max_diff_pct": 8.0},
    "FESX": {"symbol": "FESX.EX", "anchor": "^STOXX50E", "max_diff_pct": 8.0},
}
YAHOO_TICKERS = {k: v["symbol"] for k, v in ONLINE_PRICE_CONFIG.items()}

st.set_page_config(page_title="Signal Radar V2.4", page_icon="📡", layout="wide")


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


def ocr_image_regions(data: bytes) -> dict:
    """OCR a due passaggi con coordinate.

    Il passaggio header privilegia ticker/titolo. Il passaggio full usa image_to_data:
    oltre al testo conserva bbox e confidence delle parole, indispensabili in V2.4 per
    collegare una scritta ENTRY/STOP/TARGET alla quota sull'asse prezzi.
    """
    empty = {"header": "", "full": "", "combined": "", "tokens": [], "ocr_w": 0, "ocr_h": 0}
    if pytesseract is None or shutil.which("tesseract") is None:
        return empty
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        header = img.crop((0, 0, img.width, max(1, int(img.height * 0.42))))
        header_txt = pytesseract.image_to_string(_prep_ocr(header, 1.8), config="--psm 11")

        # Larghezza limitata per Streamlit Cloud; il full OCR viene poi ingrandito 1.25x.
        if img.width > 1800:
            ratio = 1800 / img.width
            img = img.resize((1800, max(1, int(img.height * ratio))))
        full_img = _prep_ocr(img, 1.25)
        raw = pytesseract.image_to_data(full_img, config="--psm 11", output_type=pytesseract.Output.DICT)
        tokens = []
        line_buckets = {}
        n = len(raw.get("text", []))
        for i in range(n):
            txt = str(raw["text"][i] or "").strip()
            if not txt or txt.lower() == "nan":
                continue
            try:
                conf = float(raw.get("conf", [0] * n)[i])
            except Exception:
                conf = 0.0
            if conf < 0:
                continue
            tok = {
                "text": txt,
                "left": int(raw["left"][i]), "top": int(raw["top"][i]),
                "width": int(raw["width"][i]), "height": int(raw["height"][i]),
                "conf": conf,
                "block": int(raw.get("block_num", [0] * n)[i]),
                "par": int(raw.get("par_num", [0] * n)[i]),
                "line": int(raw.get("line_num", [0] * n)[i]),
            }
            tokens.append(tok)
            key = (tok["block"], tok["par"], tok["line"])
            line_buckets.setdefault(key, []).append(tok)
        full_lines = []
        for toks in line_buckets.values():
            toks = sorted(toks, key=lambda z: z["left"])
            full_lines.append((min(z["top"] for z in toks), " ".join(z["text"] for z in toks)))
        full_txt = "\n".join(x[1] for x in sorted(full_lines, key=lambda z: z[0]))
        combined = (header_txt + "\n" + full_txt).strip()
        return {
            "header": header_txt, "full": full_txt, "combined": combined,
            "tokens": tokens, "ocr_w": int(full_img.width), "ocr_h": int(full_img.height),
        }
    except Exception:
        return empty


def ocr_axis_tokens(data: bytes, ocr_w: int, ocr_h: int, instrument: str = "") -> list[dict]:
    """OCR mirato alla colonna dei prezzi, evitando quasi tutta la toolbar TradingView."""
    if pytesseract is None or shutil.which("tesseract") is None or ocr_w <= 0 or ocr_h <= 0:
        return []
    try:
        from PIL import ImageFilter, ImageOps
        img = Image.open(io.BytesIO(data)).convert("RGB")
        ow, oh = img.size
        # Negli screenshot TradingView allegati le quote sono fra ~90% e 98.5% della larghezza.
        x0, x1 = int(ow * 0.90), int(ow * 0.985)
        y0, y1 = int(oh * 0.035), int(oh * 0.84)
        crop = img.crop((x0, y0, x1, y1))
        scale = 6.0
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
        crop = ImageOps.autocontrast(ImageOps.grayscale(crop)).filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))
        raw = pytesseract.image_to_data(crop, config="--psm 11", output_type=pytesseract.Output.DICT)
        out = []
        n = len(raw.get("text", []))
        for i in range(n):
            txt = str(raw["text"][i] or "").strip()
            if not txt or not re.search(r"\d", txt):
                continue
            try:
                conf = float(raw.get("conf", [0] * n)[i])
            except Exception:
                conf = 0.0
            lx_orig = x0 + float(raw["left"][i]) / scale
            ty_orig = y0 + float(raw["top"][i]) / scale
            ww_orig = float(raw["width"][i]) / scale
            hh_orig = float(raw["height"][i]) / scale
            out.append({
                "text": txt,
                "left": int(lx_orig / ow * ocr_w), "top": int(ty_orig / oh * ocr_h),
                "width": max(1, int(ww_orig / ow * ocr_w)), "height": max(1, int(hh_orig / oh * ocr_h)),
                "conf": conf, "block": 990, "par": 0, "line": i + 1, "axis": True, "variant": "gray",
            })

        # Secondo passaggio solo per strumenti con scala >= centinaia: l'adaptive threshold
        # recupera spesso le cifre bianche dentro i box verdi/rossi di TradingView.
        code = str(instrument or "").upper()
        if cv2 is not None and code not in ["6N", "6J", "6E", "6B", "6A", "6C", "6S"]:
            try:
                import numpy as _np
                arr = _np.array(img)[:, :, ::-1].copy()  # RGB -> BGR
                crop2 = arr[y0:y1, x0:x1]
                scale2 = 7.0
                crop2 = cv2.resize(crop2, None, fx=scale2, fy=scale2, interpolation=cv2.INTER_CUBIC)
                gray2 = cv2.cvtColor(crop2, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray2)
                adapt = cv2.adaptiveThreshold(clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9)
                raw2 = pytesseract.image_to_data(adapt, config="--psm 11", output_type=pytesseract.Output.DICT)
                n2 = len(raw2.get("text", []))
                for i in range(n2):
                    txt = str(raw2["text"][i] or "").strip()
                    if not txt or not re.search(r"\d", txt):
                        continue
                    try: conf = float(raw2.get("conf", [0] * n2)[i])
                    except Exception: conf = 0.0
                    lx_orig = x0 + float(raw2["left"][i]) / scale2
                    ty_orig = y0 + float(raw2["top"][i]) / scale2
                    ww_orig = float(raw2["width"][i]) / scale2
                    hh_orig = float(raw2["height"][i]) / scale2
                    out.append({
                        "text": txt, "left": int(lx_orig / ow * ocr_w), "top": int(ty_orig / oh * ocr_h),
                        "width": max(1, int(ww_orig / ow * ocr_w)), "height": max(1, int(hh_orig / oh * ocr_h)),
                        "conf": conf, "block": 991, "par": 0, "line": i + 1, "axis": True, "variant": "adaptive",
                    })
            except Exception:
                pass
        return out
    except Exception:
        return []

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
    if any(x in t for x in ["livelli di entry", "entry short", "entry long", "potenziali livelli di entry"]) or re.search(r"\bentry\b", t):
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
    if category in ["ENTRY", "SETUP", "WATCH", "RULE", "MACRO", "ANALISI"] and instrument:
        ocr["axis_tokens"] = ocr_axis_tokens(item["bytes"], int(ocr.get("ocr_w", 0) or 0), int(ocr.get("ocr_h", 0) or 0), instrument)
    else:
        ocr["axis_tokens"] = []
    status = propose_status(category)
    action = propose_next_action(category, bias)
    conf = confidence_score(instrument, asset_conf, bias, bias_conf, period, tf, category, ocr["combined"])
    warning = ""
    if category != "INFO" and not instrument:
        warning = "⚠ VERIFICA ASSET"
    elif category in ["ENTRY", "SETUP", "WATCH"] and bias == "NEUTRAL":
        warning = "⚠ BIAS DA CONFERMARE"

    # V2.4: prova a ricavare anche i livelli dal grafico. Usa il prezzo online quando
    # disponibile solo per interpretare correttamente i numeri OCR; la geometria resta
    # ancorata alla scala prezzi visibile nello screenshot.
    cp = None
    if instrument:
        online = fetch_online_price(instrument)
        cp = online.get("price")
        if cp is None:
            pmap = build_price_map(load_prices())
            cp = pmap.get(instrument, {}).get("current_price")
    levels = extract_trading_levels(item["bytes"], ocr, instrument, bias, category, cp)
    if levels["level_check"] not in ["🟢 AUTO HIGH", "⚪ LIVELLI N/D"]:
        warning = (warning + " | " if warning else "") + levels["level_check"]
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
        "Entry": levels["entry"], "Stop": levels["stop"], "Target": levels["target"],
        "Entry conf": levels["entry_conf"], "Stop conf": levels["stop_conf"], "Target conf": levels["target_conf"],
        "Livelli conf": levels["level_conf"], "Livelli OK": levels["levels_ok"], "Check livelli": levels["level_check"],
        "Livelli debug": levels["level_debug"],
        "Prossima azione": action,
        "Sintesi": make_summary(instrument, category, bias, period, ocr["combined"]),
        "Confidence": conf,
        "OCR": ocr["combined"],
        "OCR_Header": ocr["header"],
        "OCR_Tokens": json.dumps(ocr.get("tokens", []), ensure_ascii=False),
        "OCR_AxisTokens": json.dumps(ocr.get("axis_tokens", []), ensure_ascii=False),
        "OCR_W": int(ocr.get("ocr_w", 0) or 0), "OCR_H": int(ocr.get("ocr_h", 0) or 0),
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
        return pd.DataFrame(columns=["Destinazione finale", "Asset", "Bias", "Periodo", "Entry / zona", "Stop", "Target", "Livelli", "Immagini", "Controllo"])
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
        entry = accepted_level(g, "Entry")
        stop = accepted_level(g, "Stop")
        target = accepted_level(g, "Target")
        level_checks = []
        if "Check livelli" in g.columns:
            level_checks = [str(x) for x in g["Check livelli"].fillna("") if str(x).strip() and str(x) != "⚪ LIVELLI N/D"]
        out.append({
            "Destinazione finale": key,
            "Asset": first_nonempty(g, "Asset"),
            "Bias": first_nonempty(g, "Bias"),
            "Periodo": first_nonempty(g, "Periodo"),
            "Entry / zona": entry or "—",
            "Stop": stop or "—",
            "Target": target or "—",
            "Livelli": max(level_checks, key=lambda x: ("HIGH" in x, "VERIFY" not in x)) if level_checks else "⚪ N/D",
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


def accepted_level(rows: pd.DataFrame, col: str) -> str:
    """Restituisce il livello migliore tra quelli confermati/auto-HIGH."""
    if rows.empty or col not in rows.columns:
        return ""
    work = rows.copy()
    if "Livelli OK" in work.columns:
        work = work[work["Livelli OK"].fillna(False).astype(bool)]
    if work.empty:
        return ""
    if "Livelli conf" in work.columns:
        work["_lc"] = pd.to_numeric(work["Livelli conf"], errors="coerce").fillna(0)
        work = work.sort_values(["_lc", "Ricevuto"], ascending=[False, False])
    else:
        work = work.sort_values("Ricevuto", ascending=False)
    for v in work[col].fillna("").tolist():
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
            "entry_zone": accepted_level(rows, "Entry"),
            "stop_level": accepted_level(rows, "Stop"),
            "target": accepted_level(rows, "Target"),
            "status": status if status in STATUS_ORDER else "WATCH",
            "next_action": str(last.get("Prossima azione", "") or ""),
            "validity_end": "",
            "confidence": int(pd.to_numeric(rows["Confidence"], errors="coerce").fillna(50).mean()),
            "notes": "Creato da Import multiplo V2.4. Livelli HIGH vengono accettati automaticamente; MEDIUM solo se confermati nella tabella di revisione.",
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
                levels_ok = bool(row.get("Livelli OK", False))
                if levels_ok:
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


def _series_last_price(hist: pd.DataFrame) -> tuple[float | None, str]:
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None, ""
    close = hist["Close"].dropna()
    if close.empty:
        return None, ""
    price = float(close.iloc[-1])
    idx = close.index[-1]
    try:
        ts_obj = pd.Timestamp(idx)
        if getattr(ts_obj, "tzinfo", None) is not None:
            ts = ts_obj.tz_convert("Europe/Rome").strftime("%Y-%m-%d %H:%M:%S %Z")
        else:
            ts = ts_obj.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts = datetime.now().isoformat(timespec="seconds")
    return price, ts


def _fetch_one_yahoo_symbol(symbol: str) -> tuple[float | None, str, str]:
    if yf is None:
        return None, "", "Modulo yfinance non disponibile."
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False, prepost=True)
        px, ts = _series_last_price(hist)
        if px is None:
            hist = ticker.history(period="5d", interval="5m", auto_adjust=False, prepost=True)
            px, ts = _series_last_price(hist)
        if px is None:
            return None, "", f"Nessun prezzo online per {symbol}."
        return px, ts, ""
    except Exception as exc:
        return None, "", f"Errore Yahoo {symbol}: {exc}"


@st.cache_data(ttl=90, show_spinner=False)
def fetch_online_price(instrument: str) -> dict:
    """Prezzo online automatico. Cache 90 s per non martellare la fonte ad ogni rerun."""
    code = str(instrument or "").strip().upper()
    cfg = ONLINE_PRICE_CONFIG.get(code)
    if not cfg:
        return {"asset": code, "price": None, "time": "", "source_symbol": "", "message": "Nessuna fonte online configurata."}
    symbol = cfg["symbol"]
    px, ts, err = _fetch_one_yahoo_symbol(symbol)
    if px is None:
        return {"asset": code, "price": None, "time": "", "source_symbol": symbol, "message": err or "Prezzo non trovato."}

    # Per i ticker Eurex di Yahoo controlliamo che il valore non sia palesemente
    # obsoleto rispetto all'indice spot. Lo spot serve SOLO come controllo, non
    # viene mai sostituito al future.
    anchor_symbol = str(cfg.get("anchor", "") or "")
    if anchor_symbol:
        anchor_px, _, anchor_err = _fetch_one_yahoo_symbol(anchor_symbol)
        if anchor_px is not None and anchor_px > 0:
            diff = abs(px - anchor_px) / abs(anchor_px) * 100.0
            max_diff = float(cfg.get("max_diff_pct", 8.0))
            if diff > max_diff:
                return {
                    "asset": code, "price": None, "time": "", "source_symbol": symbol,
                    "message": f"{symbol} scartato: differenza {diff:.1f}% rispetto a {anchor_symbol}; probabile dato obsoleto/non confrontabile."
                }
        elif anchor_err:
            # Se il controllo spot non è disponibile non consideriamo affidabile
            # un ticker Eurex storicamente problematico: meglio fallback manuale.
            return {"asset": code, "price": None, "time": "", "source_symbol": symbol, "message": f"Controllo coerenza non disponibile: {anchor_err}"}

    return {
        "asset": code, "price": float(px), "time": ts or datetime.now().isoformat(timespec="seconds"),
        "source_symbol": symbol, "message": f"{symbol} · Yahoo Finance automatico (può essere ritardato)"
    }


def refresh_online_prices(instruments: list[str] | tuple[str, ...], force: bool = False) -> dict[str, dict]:
    """Aggiorna automaticamente i prezzi trovabili online e li salva nel DB.
    Restituisce anche gli asset non trovati, che saranno gli unici a richiedere input manuale.
    """
    codes = sorted({str(x or "").strip().upper() for x in instruments if str(x or "").strip()})
    if force:
        try:
            fetch_online_price.clear()
        except Exception:
            pass
    out: dict[str, dict] = {}
    for code in codes:
        res = fetch_online_price(code)
        out[code] = res
        if res.get("price") is not None:
            upsert_price(
                code, float(res["price"]), str(res.get("time") or datetime.now().isoformat(timespec="seconds")),
                "Online automatico · Yahoo Finance", True, str(res.get("source_symbol", "")), str(res.get("message", ""))
            )
        else:
            # Non usare un vecchio prezzo online come se fosse ancora corrente.
            # Un eventuale fallback manuale, invece, viene conservato.
            conn = get_conn()
            old = conn.execute("SELECT source FROM prices WHERE instrument=?", (code,)).fetchone()
            if old and str(old["source"] or "").startswith("Online automatico"):
                conn.execute("DELETE FROM prices WHERE instrument=?", (code,))
                conn.commit()
            conn.close()
    return out


def fetch_yahoo_price(instrument: str) -> tuple[float | None, str, str]:
    """Compatibilità con eventuali chiamate V2.2: ora usa il motore automatico V2.3."""
    res = fetch_online_price(str(instrument or "").upper())
    return res.get("price"), str(res.get("time", "")), str(res.get("message", ""))

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


# Range plausibili usati SOLO per interpretare l'OCR numerico (es. 26.650 -> 26.650 e non 26,65).
# Sono volutamente larghi: non servono a decidere il trade, ma a scartare date/percentuali/rumore.
PRICE_SCALE_HINTS = {
    "6N": (0.30, 1.50), "6J": (0.003, 0.020), "6E": (0.50, 2.00), "6B": (0.50, 2.50),
    "DAX": (5000, 50000), "FESX": (1000, 10000), "ES": (1000, 15000), "NQ": (5000, 50000),
    "YM": (10000, 100000), "RTY": (500, 10000), "GC": (500, 10000), "SI": (5, 200), "HG": (0.5, 20),
    "CL": (10, 300), "NG": (0.5, 30), "ZB": (50, 250), "ZN": (50, 200), "ZF": (50, 200), "ZT": (50, 200),
    "ZC": (100, 2000), "ZS": (100, 3000), "ZW": (100, 3000), "HE": (20, 300), "LE": (20, 300),
}


def _price_bounds(instrument: str, current_price: float | None) -> tuple[float, float]:
    if current_price is not None and current_price > 0:
        # Abbastanza ampio da includere livelli HTF, ma stretto da eliminare percentuali/date.
        return float(current_price) * 0.70, float(current_price) * 1.30
    return PRICE_SCALE_HINTS.get(str(instrument or "").upper(), (0.0001, 1_000_000.0))


def _enhanced_numeric_candidates(token: str, instrument: str, current_price: float | None) -> list[float]:
    raw = str(token or "").strip().replace(" ", "")
    parts = [x for x in re.split(r"[_;/|]+", raw) if x]
    if not parts:
        parts = [raw]
    vals: list[float] = []
    for part in parts:
        vals.extend(_numeric_candidates(part))
        # OCR tipo 0,591.20 -> 0.59120
        if part.startswith("0") and sum(part.count(sep) for sep in [".", ","]) >= 2:
            m = re.match(r"0[.,]([0-9.,]+)", part)
            if m:
                digits = re.sub(r"\D", "", m.group(1))
                if digits:
                    try: vals.append(float("0." + digits))
                    except Exception: pass
        # Se l'asset quota in migliaia, OCR spesso legge 26.59 o 6.50 al posto di 26.590/6.590.
        hint = PRICE_SCALE_HINTS.get(str(instrument or "").upper())
        if hint and hint[0] >= 500 and re.search(r"[.,]", part):
            for base in _numeric_candidates(part):
                # Solo token con vera forma decimale (6.50 / 26.59), non singole cifre OCR.
                dec_digits = len(re.sub(r"\D", "", re.split(r"[.,]", part)[-1]))
                if dec_digits >= 2 and 1 <= abs(base) < 100:
                    vals.append(float(base) * 1000.0)
        # FX sotto 1: Tesseract a volte perde lo zero/virgola e restituisce 58880.
        if hint and hint[1] < 5 and not re.search(r"[.,]", part):
            digs = re.sub(r"\D", "", part)
            if 4 <= len(digs) <= 6:
                try:
                    vals.append(float(int(digs)) / (10 ** len(digs)))
                except Exception:
                    pass
    # Riparazione conservativa di un singolo carattere numerico extra (es. 246.590 -> 26.590).
    digits = re.sub(r"\D", "", raw)
    if current_price is not None and current_price > 0:
        expected_digits = len(str(int(abs(current_price))))
    else:
        hint = PRICE_SCALE_HINTS.get(str(instrument or "").upper())
        expected_digits = len(str(int(math.sqrt(hint[0] * hint[1])))) if hint and hint[0] >= 1 else 0
    if expected_digits and len(digits) == expected_digits + 1 and expected_digits >= 3:
        for i in range(len(digits)):
            d = digits[:i] + digits[i+1:]
            try: vals.append(float(int(d)))
            except Exception: pass
    # Numero a 4 cifre con asset a 5 cifre: usa il prefisso del prezzo corrente (6490 -> 26490).
    if current_price is not None and current_price >= 10000 and digits.isdigit():
        exp = str(int(abs(current_price)))
        if len(digits) == len(exp) - 1:
            for k in range(1, min(2, len(exp)-len(digits)) + 1):
                try: vals.append(float(int(exp[:k] + digits)))
                except Exception: pass
    # unique
    out=[]; seen=set()
    for v in vals:
        try:
            key=round(float(v),10)
            if math.isfinite(float(v)) and key not in seen:
                seen.add(key); out.append(float(v))
        except Exception: pass
    return out


def _pick_price_candidate(token: str, instrument: str, current_price: float | None) -> float | None:
    vals = _enhanced_numeric_candidates(token, instrument, current_price)
    if not vals:
        return None
    lo, hi = _price_bounds(instrument, current_price)
    valid = []
    for v in vals:
        av = abs(float(v))
        if 1990 <= av <= 2040 and abs(av - round(av)) < 1e-9:  # anno
            continue
        if av <= 0 or not (lo <= av <= hi):
            continue
        valid.append(float(v))
    if not valid:
        return None
    if current_price is not None and current_price > 0:
        return min(valid, key=lambda v: abs(math.log(abs(v) / abs(current_price))))
    # Con il solo hint, preferisci il candidato più centrale nel range logaritmico.
    mid = math.sqrt(max(lo, 1e-12) * max(hi, 1e-12))
    return min(valid, key=lambda v: abs(math.log(abs(v) / mid)))


def _ocr_lines(ocr: dict) -> list[dict]:
    buckets: dict[tuple[int, int, int], list[dict]] = {}
    for tok in ocr.get("tokens", []) or []:
        key = (int(tok.get("block", 0)), int(tok.get("par", 0)), int(tok.get("line", 0)))
        buckets.setdefault(key, []).append(tok)
    lines = []
    for toks in buckets.values():
        toks = sorted(toks, key=lambda z: int(z.get("left", 0)))
        x1 = min(int(z.get("left", 0)) for z in toks)
        y1 = min(int(z.get("top", 0)) for z in toks)
        x2 = max(int(z.get("left", 0)) + int(z.get("width", 0)) for z in toks)
        y2 = max(int(z.get("top", 0)) + int(z.get("height", 0)) for z in toks)
        text = " ".join(str(z.get("text", "")) for z in toks).strip()
        if text:
            lines.append({"text": text, "norm": normalize_text(text), "x1": x1, "x2": x2, "y1": y1, "y2": y2, "yc": (y1+y2)/2, "tokens": toks})
    return sorted(lines, key=lambda z: (z["y1"], z["x1"]))


def _axis_price_calibration(ocr: dict, instrument: str, current_price: float | None) -> dict:
    """Stima price=f(y) dai numeri OCR sul lato destro del grafico."""
    w, h = int(ocr.get("ocr_w", 0) or 0), int(ocr.get("ocr_h", 0) or 0)
    if w <= 0 or h <= 0:
        return {"ok": False, "conf": 0, "n": 0}
    pts = []
    for tok in (ocr.get("tokens", []) or []) + (ocr.get("axis_tokens", []) or []):
        x = int(tok.get("left", 0)) + int(tok.get("width", 0)) / 2
        y = int(tok.get("top", 0)) + int(tok.get("height", 0)) / 2
        # Asse prezzi/etichette livello: nella parte destra; evita fascia date in basso.
        if x < w * 0.73 or y < h * 0.035 or y > h * 0.88:
            continue
        v = _pick_price_candidate(str(tok.get("text", "")), instrument, current_price)
        if v is None:
            continue
        try:
            conf = float(tok.get("conf", 0) or 0)
        except Exception:
            conf = 0
        if conf < 20:
            continue
        pts.append((float(y), float(v), conf))
    if len(pts) < 2:
        return {"ok": False, "conf": 0, "n": len(pts)}

    # Unisci duplicati quasi sulla stessa quota usando la mediana.
    pts.sort(key=lambda z: z[0])
    groups = []
    for y, v, c in pts:
        if groups and abs(y - np.mean([q[0] for q in groups[-1]])) <= 4:
            groups[-1].append((y, v, c))
        else:
            groups.append([(y, v, c)])
    merged = [(float(np.median([q[0] for q in g])), float(np.median([q[1] for q in g])), float(np.mean([q[2] for q in g]))) for g in groups]
    if len(merged) < 2:
        return {"ok": False, "conf": 0, "n": len(merged)}

    arr = np.array([[x[0], x[1]] for x in merged], dtype=float)

    # RANSAC leggero: con OCR di screenshot compressi possono esserci molti numeri sbagliati,
    # ma bastano 3-4 quote coerenti per ricostruire correttamente la scala lineare TradingView.
    keep = np.ones(len(arr), dtype=bool)
    base_px = abs(float(current_price or np.median(arr[:, 1])))
    ransac_tol = max(base_px * 0.0030, 1e-8)
    best_mask = None
    best_score = (-1, float("inf"))
    if len(arr) >= 3:
        for i in range(len(arr) - 1):
            for j in range(i + 1, len(arr)):
                dy = arr[j, 0] - arr[i, 0]
                if abs(dy) < 5:
                    continue
                a0 = (arr[j, 1] - arr[i, 1]) / dy
                if a0 >= 0:
                    continue
                b0 = arr[i, 1] - a0 * arr[i, 0]
                resid = np.abs(arr[:, 1] - (a0 * arr[:, 0] + b0))
                mask = resid <= ransac_tol
                count = int(mask.sum())
                err = float(np.mean(resid[mask])) if count else float("inf")
                score = (count, -err)
                if score > (best_score[0], -best_score[1]):
                    best_score = (count, err)
                    best_mask = mask
    if best_mask is not None and int(best_mask.sum()) >= 3:
        keep = best_mask
    else:
        # Fallback iterativo per casi con soli due riferimenti puliti.
        for _ in range(3):
            cur0 = arr[keep]
            if len(cur0) < 2:
                break
            a0, b0 = np.polyfit(cur0[:, 0], cur0[:, 1], 1)
            pred0 = a0 * cur0[:, 0] + b0
            resid0 = np.abs(cur0[:, 1] - pred0)
            span = max(np.ptp(cur0[:, 1]), base_px * 0.01, 1e-8)
            tol = max(span * 0.08, base_px * 0.0025)
            local_keep = resid0 <= tol
            if local_keep.all() or local_keep.sum() < 2:
                break
            idx = np.where(keep)[0]
            keep[idx[~local_keep]] = False
    cur = arr[keep]
    if len(cur) < 2:
        return {"ok": False, "conf": 0, "n": len(cur)}
    a, b = np.polyfit(cur[:, 0], cur[:, 1], 1)
    if not math.isfinite(a) or not math.isfinite(b) or a >= 0:
        return {"ok": False, "conf": 0, "n": len(cur)}
    pred = a * cur[:, 0] + b
    ss_res = float(np.sum((cur[:, 1] - pred) ** 2))
    ss_tot = float(np.sum((cur[:, 1] - np.mean(cur[:, 1])) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else 1.0
    if len(cur) >= 5 and r2 >= 0.985:
        conf = 96
    elif len(cur) >= 4 and r2 >= 0.98:
        conf = 92
    elif len(cur) >= 3 and r2 >= 0.975:
        conf = 88
    elif len(cur) >= 3 and r2 >= 0.94:
        conf = 78
    elif len(cur) >= 2 and r2 >= 0.95:
        conf = 65
    else:
        return {"ok": False, "conf": 0, "n": len(cur), "r2": r2}
    return {"ok": True, "a": float(a), "b": float(b), "conf": conf, "n": int(len(cur)), "r2": float(r2)}


def _price_from_y(y: float, calib: dict) -> float | None:
    if not calib.get("ok"):
        return None
    try:
        v = float(calib["a"]) * float(y) + float(calib["b"])
        return v if math.isfinite(v) and v > 0 else None
    except Exception:
        return None


def _price_token_quality(text: str, instrument: str) -> int:
    raw = str(text or "").strip()
    digits = re.sub(r"\D", "", raw)
    code = str(instrument or "").upper()
    if not digits:
        return 0
    if code in ["6N", "6J", "6E", "6B", "6A", "6C", "6S"]:
        # Per FX una lettura come 0,58 è troppo grossolana per essere un livello operativo.
        if re.search(r"0[.,]\d{4,}", raw) or (not re.search(r"[.,]", raw) and len(digits) >= 5):
            return 95
        if re.search(r"0[.,]\d{3}", raw):
            return 78
        return 55
    hint = PRICE_SCALE_HINTS.get(code)
    if hint and hint[0] >= 500:
        if len(digits) >= 4:
            return 92
        return 55
    return 85 if len(digits) >= 3 else 60


def _direct_price_near_line(line: dict, ocr: dict, instrument: str, current_price: float | None) -> tuple[float | None, int]:
    w = int(ocr.get("ocr_w", 0) or 0)
    if w <= 0:
        return None, 0
    best = None
    for tok in (ocr.get("tokens", []) or []) + (ocr.get("axis_tokens", []) or []):
        yc = int(tok.get("top", 0)) + int(tok.get("height", 0)) / 2
        dy = abs(yc - float(line["yc"]))
        if dy > max(16, (float(line["y2"]) - float(line["y1"])) * 0.9):
            continue
        v = _pick_price_candidate(str(tok.get("text", "")), instrument, current_price)
        if v is None:
            continue
        xc = int(tok.get("left", 0)) + int(tok.get("width", 0)) / 2
        same_line = dy <= max(10, (float(line["y2"]) - float(line["y1"])) * 0.8)
        right_side = xc >= w * 0.68
        if not same_line and not right_side:
            continue
        score = (40 if same_line else 0) + (35 if right_side else 0) + float(tok.get("conf", 0) or 0) * 0.25 - dy * 0.5
        if best is None or score > best[0]:
            base_conf = 94 if same_line and right_side else 86 if right_side else 68
            base_conf = min(base_conf, _price_token_quality(str(tok.get("text", "")), instrument))
            best = (score, float(v), base_conf)
    return (best[1], best[2]) if best else (None, 0)


def _band_near_y(data: bytes, ocr: dict, y_target: float, allow_green: bool = True) -> tuple[float | None, float | None, str]:
    """Cerca una banda gialla/verde orizzontale vicina alla label operativa."""
    w, h = int(ocr.get("ocr_w", 0) or 0), int(ocr.get("ocr_h", 0) or 0)
    if w <= 0 or h <= 0:
        return None, None, ""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((w, h))
        hsv = np.asarray(img.convert("HSV"), dtype=np.uint8)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        yellow = (H >= 24) & (H <= 55) & (S >= 28) & (V >= 135)
        green = (H >= 52) & (H <= 105) & (S >= 28) & (V >= 105) if allow_green else np.zeros_like(yellow)
        mask = yellow | green
        x1, x2 = int(w * 0.04), int(w * 0.94)
        dens = mask[:, x1:x2].mean(axis=1)
        y0 = max(0, int(y_target - h * 0.12)); y1 = min(h, int(y_target + h * 0.12))
        rows = np.where(dens[y0:y1] >= 0.055)[0] + y0
        if len(rows) == 0:
            return None, None, ""
        groups = []
        cur = [int(rows[0])]
        for yy in rows[1:]:
            yy = int(yy)
            if yy <= cur[-1] + 2:
                cur.append(yy)
            else:
                groups.append(cur); cur = [yy]
        groups.append(cur)
        scored = []
        for g in groups:
            top, bot = min(g), max(g)
            center = (top + bot) / 2
            thickness = bot - top + 1
            # Le bande vere battono le linee sottili; vicinanza alla label resta importante.
            score = -abs(center - y_target) + min(thickness, 45) * 2.2 + float(np.max(dens[top:bot+1])) * 25
            scored.append((score, top, bot, center, thickness))
        best = max(scored, key=lambda z: z[0])
        _, top, bot, center, thickness = best
        if abs(center - y_target) > h * 0.07:
            return None, None, ""
        # Per una linea sottile non fingiamo una zona: verrà usato il livello centrale.
        if thickness < max(4, int(h * 0.004)):
            return None, None, "line"
        midx = int((x1+x2)/2)
        color = "yellow" if yellow[int(center), max(0, min(w-1, midx))] else "green"
        return float(top), float(bot), color
    except Exception:
        return None, None, ""


def _global_yellow_bands(data: bytes, ocr: dict) -> list[tuple[float, float, float]]:
    """Restituisce bande gialle orizzontali (top,bottom,density), ordinate dall'alto."""
    w, h = int(ocr.get("ocr_w", 0) or 0), int(ocr.get("ocr_h", 0) or 0)
    if w <= 0 or h <= 0:
        return []
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB").resize((w, h))
        hsv = np.asarray(img.convert("HSV"), dtype=np.uint8)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        mask = (H >= 24) & (H <= 55) & (S >= 24) & (V >= 140)
        x1, x2 = int(w * 0.04), int(w * 0.91)
        dens = mask[:, x1:x2].mean(axis=1)
        valid_y0, valid_y1 = int(h * 0.10), int(h * 0.70)
        rows = np.where(dens[valid_y0:valid_y1] >= 0.055)[0] + valid_y0
        if len(rows) == 0:
            return []
        groups=[]; cur=[int(rows[0])]
        for yy in rows[1:]:
            yy=int(yy)
            if yy <= cur[-1]+2: cur.append(yy)
            else: groups.append(cur); cur=[yy]
        groups.append(cur)
        out=[]
        for g in groups:
            top,bot=min(g),max(g); thick=bot-top+1
            if thick < max(4,int(h*.004)):
                continue
            density=float(np.max(dens[top:bot+1]))
            out.append((float(top),float(bot),density))
        return sorted(out,key=lambda z:z[0])
    except Exception:
        return []


def _line_score(line: dict, kind: str, h: int) -> int:
    t = line["norm"]
    y = float(line["yc"])
    # Evita titoli nella primissima fascia, salvo che contengano proprio ENTRY SHORT/LONG.
    top_penalty = 18 if h and y < h * 0.12 and not ("entry short" in t or "entry long" in t) else 0
    if kind == "entry":
        if "entry short" in t or "entry long" in t:
            score = 120
        elif re.search(r"\bentry\s*[123]\b", t):
            score = 116
        elif "zona da attenzionare" in t:
            score = 106
        elif "area da attenzionare" in t:
            score = 104
        elif "potenziali livelli di entry" in t:
            score = 68
        elif "livelli di entry" in t:
            score = 72
        elif re.search(r"\bentry\b", t):
            score = 98
        else:
            score = 0
        if h and y > h * 0.78:
            score -= 65
        return score - top_penalty
    if kind == "stop":
        return (105 if re.search(r"\bstop\b", t) else 0) - top_penalty
    if kind == "target":
        score = 0
        if "t1 tecnico" in t or "t1.tecnico" in t:
            score = 115
        elif "1 target" in t or "1° target" in t or "1°target" in t or "1 target mensile" in t:
            score = 112
        elif "target mensile" in t:
            score = 100
        elif "target" in t:
            score = 88
        return score - top_penalty
    return 0


def _fmt_level(v: float | None, instrument: str) -> str:
    if v is None or not math.isfinite(float(v)):
        return ""
    code = str(instrument or "").upper()
    dec = 5 if code in ["6N", "6E", "6B", "6A", "6C", "6S"] else 6 if code == "6J" else 3 if code in ["CL", "NG"] else 2
    x = float(v)
    if abs(x) >= 1000:
        # Convenzione italiana, ma sempre parsabile dal motore interno grazie all'hint asset.
        txt = f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
        if dec:
            txt = txt.rstrip("0").rstrip(",")
        return txt
    return f"{x:.{dec}f}".rstrip("0").rstrip(",").replace(".", ",")


def extract_trading_levels(data: bytes, ocr: dict, instrument: str, bias: str, category: str, current_price: float | None = None) -> dict:
    """Estrae Entry/Zona, Stop e Target combinando testo, geometria e scala prezzi.

    HIGH (>=85): compilazione automatica e checkbox confermata.
    MEDIUM (65-84): proposta visibile ma da confermare.
    LOW: campo lasciato vuoto.
    """
    lines = _ocr_lines(ocr)
    h = int(ocr.get("ocr_h", 0) or 0)
    calib = _axis_price_calibration(ocr, instrument, current_price)

    def value_for_line(line: dict, kind: str) -> tuple[float | None, int, str]:
        direct, dconf = _direct_price_near_line(line, ocr, instrument, current_price)
        mapped = _price_from_y(float(line["yc"]), calib)
        if direct is not None and dconf >= 86:
            # Se mapping e OCR diretto concordano, confidence massima.
            if mapped is not None and abs(direct - mapped) / max(abs(direct), 1e-9) <= 0.015:
                return direct, min(98, max(dconf, int(calib.get("conf", 0)) + 2)), "OCR+asse"
            return direct, dconf, "OCR vicino label"
        if mapped is not None:
            return mapped, int(calib.get("conf", 0)), "asse prezzi"
        return direct, dconf, "OCR vicino label" if direct is not None else ""

    # ENTRY / ZONA
    entry_text, entry_conf, entry_method = "", 0, ""
    entry_candidates = [(_line_score(line, "entry", h), line) for line in lines]
    entry_candidates = [(sc, li) for sc, li in entry_candidates if sc >= 50]
    entry_candidates.sort(key=lambda z: z[0], reverse=True)
    if category in ["ENTRY", "SETUP", "WATCH", "RULE", "ANALISI", "MACRO"]:
        if entry_candidates:
            _, line = entry_candidates[0]
            center_val, center_conf, method = value_for_line(line, "entry")
            top, bot, color = _band_near_y(data, ocr, float(line["yc"]), allow_green=True)
            if top is not None and bot is not None and calib.get("ok"):
                p1, p2 = _price_from_y(top, calib), _price_from_y(bot, calib)
                if p1 is not None and p2 is not None:
                    lo, hi = min(p1, p2), max(p1, p2)
                    width_pct = abs(hi-lo) / max(abs((hi+lo)/2), 1e-9) * 100
                    if width_pct <= 4.0:
                        entry_text = f"{_fmt_level(lo, instrument)} – {_fmt_level(hi, instrument)}" if abs(hi-lo) > max(abs(hi)*0.00008, 1e-8) else _fmt_level((lo+hi)/2, instrument)
                        entry_conf = min(97, int(calib.get("conf", 0)) + 5)
                        entry_method = f"label+banda {color}+asse"
            if not entry_text and center_val is not None:
                entry_text = _fmt_level(center_val, instrument)
                entry_conf = min(94, center_conf)
                entry_method = method
        # Se OCR non vede la scritta dentro la banda ma la categoria è chiaramente ENTRY/WATCH,
        # usa la prima banda gialla visibile SOLO se la scala prezzi è calibrata.
        if not entry_text and category in ["ENTRY", "WATCH", "SETUP"] and calib.get("ok"):
            bands = _global_yellow_bands(data, ocr)
            if bands:
                top, bot, _ = bands[0]
                p1, p2 = _price_from_y(top, calib), _price_from_y(bot, calib)
                if p1 is not None and p2 is not None:
                    lo,hi=min(p1,p2),max(p1,p2)
                    width_pct=abs(hi-lo)/max(abs((hi+lo)/2),1e-9)*100
                    if width_pct <= 4.0:
                        entry_text=f"{_fmt_level(lo,instrument)} – {_fmt_level(hi,instrument)}"
                        entry_conf=min(82,int(calib.get("conf",0))+3)  # fallback mai HIGH
                        entry_method="banda gialla globale+asse"

    # STOP
    stop_text, stop_conf, stop_method = "", 0, ""
    stop_candidates = [(_line_score(line, "stop", h), line) for line in lines]
    stop_candidates = [(sc, li) for sc, li in stop_candidates if sc > 0]
    stop_candidates.sort(key=lambda z: z[0], reverse=True)
    if stop_candidates:
        v, c, method = value_for_line(stop_candidates[0][1], "stop")
        if v is not None:
            stop_text, stop_conf, stop_method = _fmt_level(v, instrument), min(96, c), method

    # TARGET: raccogli più livelli, poi ordina T1 coerentemente con il bias/entry.
    target_vals = []
    for sc, line in sorted([(_line_score(line, "target", h), line) for line in lines if _line_score(line, "target", h) > 0], key=lambda z: z[0], reverse=True):
        v, c, method = value_for_line(line, "target")
        if v is not None:
            target_vals.append((float(v), min(96, c), sc, method))
    uniq = []
    for item in target_vals:
        if not any(abs(item[0]-q[0]) / max(abs(item[0]), 1e-9) < 0.00015 for q in uniq):
            uniq.append(item)
    entry_mid = None
    if entry_text:
        lo, hi = parse_entry_range(entry_text, current_price)
        if lo is not None and hi is not None:
            entry_mid = (lo+hi)/2
    if entry_mid is not None and uniq:
        if str(bias).upper() == "SHORT":
            profitable = [q for q in uniq if q[0] < entry_mid]
            if profitable:
                profitable.sort(key=lambda q: entry_mid-q[0])
                uniq = profitable + [q for q in uniq if q not in profitable]
        elif str(bias).upper() == "LONG":
            profitable = [q for q in uniq if q[0] > entry_mid]
            if profitable:
                profitable.sort(key=lambda q: q[0]-entry_mid)
                uniq = profitable + [q for q in uniq if q not in profitable]
    target_text, target_conf, target_method = "", 0, ""
    if uniq:
        chosen = uniq[:2]
        target_text = " / ".join(_fmt_level(q[0], instrument) for q in chosen)
        target_conf = min(q[1] for q in chosen)
        target_method = "+".join(sorted({q[3] for q in chosen if q[3]}))

    # Safeguard: un livello ricavato dalla geometria dello screenshot deve essere
    # ragionevolmente vicino al prezzo corrente. Se è troppo lontano, è quasi sempre
    # un numero OCR mal interpretato (data, ATR, altra scala) e viene scartato.
    if current_price is not None and current_price > 0 and entry_text:
        elo, ehi = parse_entry_range(entry_text, current_price)
        if elo is not None and ehi is not None:
            emid = (elo + ehi) / 2
            if abs(emid - current_price) / abs(current_price) * 100 > 5.0:
                entry_text, entry_conf, entry_method = "", 0, "scartata: >5% dal prezzo"
    if current_price is not None and current_price > 0 and target_text:
        tv = parse_single_level(target_text, current_price)
        if tv is not None and abs(tv-current_price)/abs(current_price)*100 > 5.0:
            target_text, target_conf, target_method = "", 0, "scartato: >5% dal prezzo"
    if current_price is not None and current_price > 0 and stop_text:
        sv = parse_single_level(stop_text, current_price)
        if sv is not None and abs(sv-current_price)/abs(current_price)*100 > 5.0:
            stop_text, stop_conf, stop_method = "", 0, "scartato: >5% dal prezzo"

    # Non mostrare una proposta numerica sotto MEDIUM: meglio N/D che falsa precisione.
    if entry_conf < 65:
        entry_text = ""
    if stop_conf < 65:
        stop_text = ""
    if target_conf < 65:
        target_text = ""
    present_confs = [c for txt, c in [(entry_text, entry_conf), (stop_text, stop_conf), (target_text, target_conf)] if txt]
    level_conf = int(round(np.mean(present_confs))) if present_confs else 0
    # Per l'operatività l'ENTRY è il campo decisivo: HIGH solo se l'entry è HIGH.
    auto_high = bool(entry_text and entry_conf >= 85)
    if auto_high:
        check = "🟢 AUTO HIGH"
    elif entry_text and entry_conf >= 65:
        check = "🟡 VERIFY ENTRY"
    elif (stop_text or target_text) and level_conf >= 65:
        check = "🟡 PARZIALE"
    else:
        check = "⚪ LIVELLI N/D"
    debug = f"asse n={calib.get('n',0)} r2={calib.get('r2',0):.3f} conf={calib.get('conf',0)}; entry={entry_method}; stop={stop_method}; target={target_method}"
    return {
        "entry": entry_text, "stop": stop_text, "target": target_text,
        "entry_conf": int(entry_conf), "stop_conf": int(stop_conf), "target_conf": int(target_conf),
        "level_conf": int(level_conf), "levels_ok": auto_high, "level_check": check, "level_debug": debug,
    }


def recalc_levels_for_records(records: list[dict], file_map: dict[str, dict]) -> list[dict]:
    """Ricalcola i livelli dopo correzioni manuali di Asset/Bias/Categoria."""
    out = []
    pmap = build_price_map(load_prices())
    for row in records:
        r = dict(row)
        item = file_map.get(str(r.get("File", "")))
        if item is None:
            out.append(r); continue
        try:
            ocr = {
                "tokens": json.loads(str(r.get("OCR_Tokens", "[]") or "[]")),
                "axis_tokens": json.loads(str(r.get("OCR_AxisTokens", "[]") or "[]")),
                "ocr_w": int(float(r.get("OCR_W", 0) or 0)), "ocr_h": int(float(r.get("OCR_H", 0) or 0)),
                "header": str(r.get("OCR_Header", "") or ""), "full": str(r.get("OCR", "") or ""), "combined": str(r.get("OCR", "") or ""),
            }
            if asset and not ocr.get("axis_tokens"):
                ocr["axis_tokens"] = ocr_axis_tokens(item["bytes"], int(ocr.get("ocr_w",0) or 0), int(ocr.get("ocr_h",0) or 0), asset)
        except Exception:
            ocr = ocr_image_regions(item["bytes"])
        asset = str(r.get("Asset", "") or "").upper()
        cp = pmap.get(asset, {}).get("current_price") if asset else None
        if asset and cp is None:
            online = fetch_online_price(asset)
            cp = online.get("price")
        levels = extract_trading_levels(item["bytes"], ocr, asset, str(r.get("Bias", "NEUTRAL")), str(r.get("Categoria", "ANALISI")), cp)
        r.update({
            "Entry": levels["entry"], "Stop": levels["stop"], "Target": levels["target"],
            "Entry conf": levels["entry_conf"], "Stop conf": levels["stop_conf"], "Target conf": levels["target_conf"],
            "Livelli conf": levels["level_conf"], "Livelli OK": levels["levels_ok"], "Check livelli": levels["level_check"],
            "Livelli debug": levels["level_debug"],
        })
        out.append(r)
    return out


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

st.title("📡 Signal Radar V2.4")
st.caption("WhatsApp → OCR vincolato → deduplica → raggruppamento → revisione → una riga per setup.")

signals = load_signals()
_active_assets_boot = sorted(set(signals[signals["status"].isin(["WATCH", "READY", "TRIGGERED"])]["instrument"].dropna().astype(str).str.upper().tolist())) if not signals.empty else []
online_status = refresh_online_prices(_active_assets_boot) if _active_assets_boot else {}
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
    st.caption("V2.4: prezzo online automatico + estrazione Entry/Stop/Target da testo, geometria e scala prezzi. HIGH=auto, MEDIUM=verifica.")

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
            online_found_here = bool(online_status.get(str(r["instrument"]).upper(), {}).get("price") is not None)
            if online_found_here:
                st.caption("💹 Prezzo trovato automaticamente online: nessun inserimento manuale richiesto.")
                current_price_new = 0.0
                current_price_note = ""
            else:
                st.markdown("**Fallback prezzo manuale (solo perché online non trovato)**")
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
                if (not online_found_here) and current_price_new > 0:
                    upsert_price(r["instrument"], current_price_new, datetime.now().isoformat(timespec="seconds"), "Fallback manuale", True, "", current_price_note)
                st.success("Setup aggiornato.")
                st.rerun()

with tab_prices:
    st.subheader("💹 Prezzi correnti automatici")
    st.caption("Signal Radar prova automaticamente la fonte online ogni ~90 secondi. Il prezzo manuale compare SOLO per gli asset che non vengono trovati o che falliscono i controlli di coerenza.")

    active_assets = sorted(set(signals[signals["status"].isin(["WATCH", "READY", "TRIGGERED"])]["instrument"].dropna().astype(str).str.upper().tolist())) if not signals.empty else []
    if active_assets:
        if st.button("🔄 Aggiorna ora i prezzi online", use_container_width=True):
            online_status = refresh_online_prices(active_assets, force=True)
            st.success("Tentativo di aggiornamento online completato.")
            st.rerun()

        # Ricalcolo dalla cache/DB corrente per mostrare lo stato effettivo.
        current_online = refresh_online_prices(active_assets)
        prices_now = load_prices()
        pmap_now = build_price_map(prices_now)
        status_rows = []
        missing_assets = []
        for asset in active_assets:
            res = current_online.get(asset, {})
            online_ok = res.get("price") is not None
            pinfo = pmap_now.get(asset, {})
            if not online_ok:
                missing_assets.append(asset)
            status_rows.append({
                "Asset": asset,
                "Prezzo": format_price(pinfo.get("current_price")) if pinfo else "—",
                "Stato fonte": "✅ ONLINE" if online_ok else "⚠ FALLBACK MANUALE",
                "Ticker/Fonte": res.get("source_symbol", "") or pinfo.get("source_symbol", "") or "—",
                "Ora prezzo": pinfo.get("price_time", "—") if pinfo else "—",
                "Nota": res.get("message", "") if not online_ok else res.get("message", ""),
            })
        st.table(pd.DataFrame(status_rows))

        if missing_assets:
            st.markdown("### ✍️ Solo prezzi non trovati online")
            st.warning("Inserisci manualmente soltanto questi asset. Quando una fonte online valida tornerà disponibile, il prezzo automatico sostituirà il fallback manuale.")
            with st.form("manual_price_fallback_form"):
                c1, c2, c3 = st.columns([1, 1, 2])
                asset_price = c1.selectbox("Asset non trovato", missing_assets)
                existing = pmap_now.get(str(asset_price).upper(), {})
                price_val = c2.number_input("Prezzo attuale", min_value=0.0, value=float(existing.get("current_price", 0.0) or 0.0), format="%.6f")
                price_note = c3.text_input("Nota / contratto", str(existing.get("note", "") or ""), placeholder="es. TradingView FDAX1! / contratto settembre")
                if st.form_submit_button("💾 Salva fallback manuale", type="primary"):
                    if price_val <= 0:
                        st.warning("Inserisci un prezzo maggiore di zero.")
                    else:
                        upsert_price(asset_price, price_val, datetime.now().isoformat(timespec="seconds"), "Fallback manuale", True, "", price_note)
                        st.success(f"Fallback {asset_price} aggiornato.")
                        st.rerun()
        else:
            st.success("✅ Tutti gli asset attivi hanno un prezzo online disponibile: non serve inserire nulla a mano.")
    else:
        st.info("Nessun setup attivo: i prezzi verranno cercati automaticamente quando compariranno asset WATCH/READY/TRIGGERED.")

    st.caption("Nota: il controllo di validità confronta il prezzo con Entry/Stop/Target confermati. Un prezzo online può essere ritardato; per DAX/FESX un dato Yahoo anomalo viene scartato invece di usare lo spot come sostituto del future.")

with tab_batch:
    st.subheader("⚡ Import multiplo WhatsApp")
    st.caption("Carica molte immagini insieme. V2.4 riconosce setup e prova anche a ricavare Entry/Zona, Stop e Target dalla scala prezzi del grafico. I livelli HIGH sono accettati automaticamente; i MEDIUM richiedono conferma.")

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
            # Dopo grouping/inferenze ricalcola i livelli: è essenziale per immagini in cui
            # l'asset era leggibile solo grazie alla sequenza cronologica.
            proposals = recalc_levels_for_records(proposals, file_map)
            progress.empty()
            st.session_state["batch_df"] = pd.DataFrame(proposals)

        if "batch_df" in st.session_state:
            batch_df = st.session_state["batch_df"].copy()
            current_signals = load_signals()
            existing_destinations = [destination_label(r) for _, r in current_signals.iterrows()]
            destination_options = ["NUOVO", "INFO"] + existing_destinations

            st.markdown("### 1. Controlla le proposte")
            st.info("V2.4 prova prima a collegare l’immagine a un setup esistente. Per i livelli: **🟢 AUTO HIGH** viene confermato automaticamente; **🟡 VERIFY** è una proposta da controllare. Puoi correggere Asset/Bias e poi ricalcolare anche i livelli.")

            edited = st.data_editor(
                batch_df,
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                key="batch_editor",
                column_order=[
                    "Importa", "Ricevuto", "File", "Asset", "Bias", "Periodo", "TF", "Categoria", "Stato",
                    "Destinazione", "Gruppo", "Verifica", "Livelli OK", "Check livelli", "Entry", "Stop", "Target", "Livelli conf",
                    "Prossima azione", "Confidence", "Sintesi",
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
                    "Livelli OK": st.column_config.CheckboxColumn("Livelli OK", width="small", help="HIGH è già selezionato. Per MEDIUM spunta solo dopo aver controllato il grafico."),
                    "Check livelli": st.column_config.TextColumn("Check livelli", width="medium", disabled=True),
                    "Entry": st.column_config.TextColumn("Entry / zona", width="medium"),
                    "Stop": st.column_config.TextColumn("Stop", width="medium"),
                    "Target": st.column_config.TextColumn("Target", width="medium"),
                    "Livelli conf": st.column_config.NumberColumn("Livelli %", min_value=0, max_value=100, step=1, width="small", disabled=True),
                    "Prossima azione": st.column_config.TextColumn("Prossima azione", width="large"),
                    "Confidence": st.column_config.NumberColumn("Conf.%", min_value=0, max_value=100, step=5, width="small"),
                    "Sintesi": st.column_config.TextColumn("Sintesi", width="large"),
                },
                disabled=["File", "OCR", "Hash"],
            )
            st.session_state["batch_df"] = edited.copy()
            st.caption("🟢 AUTO HIGH: il livello può essere salvato automaticamente. 🟡 VERIFY: controlla/correggi Entry-Stop-Target e poi spunta **Livelli OK**. Se non lo spunti, il setup viene importato ma i numeri non vengono salvati.")

            if st.button("♻️ Ricalcola destinazioni + livelli dopo le correzioni", use_container_width=True):
                recalculated = refine_batch_proposals(edited.to_dict("records"), current_signals)
                recalculated = recalc_levels_for_records(recalculated, file_map)
                st.session_state["batch_df"] = pd.DataFrame(recalculated)
                st.rerun()

            st.markdown("### 2. Setup finali proposti")
            preview_setups = setup_preview(edited)
            if not preview_setups.empty:
                preview_assets = sorted(set(preview_setups["Asset"].dropna().astype(str).str.upper().tolist()))
                refresh_online_prices(preview_assets)
                pmap_now = build_price_map(load_prices())
                preview_setups["Prezzo attuale"] = preview_setups["Asset"].map(lambda a: format_price(pmap_now.get(str(a).upper(), {}).get("current_price")))
                def _preview_check(pr):
                    rows_g = edited[(edited["Importa"] == True) & (edited["Asset"].astype(str) == str(pr["Asset"]))]  # noqa: E712
                    rr = rows_g.iloc[0] if not rows_g.empty else {}
                    pseudo = {
                        "bias": pr.get("Bias", "NEUTRAL"),
                        "entry_zone": "" if pr.get("Entry / zona", "—") == "—" else pr.get("Entry / zona", ""),
                        "stop_level": "" if pr.get("Stop", "—") == "—" else pr.get("Stop", ""),
                        "target": "" if pr.get("Target", "—") == "—" else pr.get("Target", ""),
                    }
                    return assess_signal(pseudo, pmap_now.get(str(pr["Asset"]).upper()))["label"]
                preview_setups["Check prezzo"] = preview_setups.apply(_preview_check, axis=1)
            if preview_setups.empty:
                st.caption("Nessun setup operativo selezionato.")
            else:
                st.table(preview_setups)
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
                        try:
                            _im = Image.open(io.BytesIO(pitem["bytes"])).convert("RGB")
                            _w, _h = _im.size
                            _zoom = _im.crop((int(_w*0.70), int(_h*0.06), int(_w*0.985), int(_h*0.76)))
                            st.caption("Zoom automatico area livelli / asse prezzi")
                            st.image(_zoom, use_container_width=True)
                        except Exception:
                            pass
                    with ctxt:
                        prow = edited[edited["File"] == preview].iloc[0]
                        st.markdown(f"**Asset:** {prow['Asset'] or '—'}")
                        st.markdown(f"**Bias:** {prow['Bias']}")
                        st.markdown(f"**Categoria:** {prow['Categoria']}")
                        st.markdown(f"**Destinazione:** {prow['Destinazione']}")
                        st.markdown(f"**Gruppo:** {prow['Gruppo']}")
                        st.markdown(f"**Entry/Zona:** {prow.get('Entry','') or '—'} · **Stop:** {prow.get('Stop','') or '—'}")
                        st.markdown(f"**Target:** {prow.get('Target','') or '—'} · **Livelli:** {prow.get('Check livelli','⚪ N/D')} ({int(prow.get('Livelli conf',0) or 0)}%)")
                        with st.expander("Diagnostica livelli"):
                            st.text(str(prow.get("Livelli debug", "")))
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
st.caption("Signal Radar V2.4 · OCR + geometria + scala prezzi. Livelli HIGH auto; MEDIUM solo dopo conferma. Il Radar organizza i segnali, non sostituisce la verifica del grafico originale.")
