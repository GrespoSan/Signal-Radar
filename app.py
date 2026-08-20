from __future__ import annotations

import hashlib
import io
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

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
ASSET_DIR = APP_DIR / "assets"
DB_PATH = DATA_DIR / "signal_radar.db"
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

st.set_page_config(page_title="Signal Radar V2", page_icon="📡", layout="wide")


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
    conn.close()


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


def ocr_image(data: bytes) -> str:
    if pytesseract is None or shutil.which("tesseract") is None:
        return ""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        max_w = 1800
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)))
        return pytesseract.image_to_string(img, config="--psm 6")
    except Exception:
        return ""


MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]


def detect_instrument(text: str) -> tuple[str, str]:
    t = normalize_text(text)
    rules = [
        (r"\b6n1?\b|new zealand dollar", "6N", "New Zealand Dollar Futures"),
        (r"\b6j1?\b|japanese yen", "6J", "Japanese Yen Futures"),
        (r"\bfdax1?\b|\bdax\b|pax strategia", "DAX", "DAX Futures"),
        (r"\bfesx1?\b|euro ?stoxx|eurostoxx|burostoxx", "FESX", "Euro Stoxx 50 Futures"),
        (r"\bes1?\b|s&p ?500|s&p500|sp ?500|e-mini s&p", "ES", "S&P 500 E-mini Futures"),
        (r"\bnq1?\b|nasdaq", "NQ", "Nasdaq 100 Futures"),
        (r"\brty1?\b|russell", "RTY", "Russell 2000 Futures"),
        (r"\bym1?\b|dow jones", "YM", "Dow Jones Futures"),
        (r"\bgc1?\b|gold futures|oro", "GC", "Gold Futures"),
        (r"\bcl1?\b|wti|crude oil", "CL", "WTI Crude Oil Futures"),
        (r"\bhg1?\b|copper|rame", "HG", "Copper Futures"),
        (r"\b6e1?\b|euro fx", "6E", "Euro FX Futures"),
        (r"\b6b1?\b|british pound", "6B", "British Pound Futures"),
    ]
    for pattern, code, name in rules:
        if re.search(pattern, t, flags=re.I):
            return code, name
    return "", ""


def detect_bias(text: str) -> str:
    t = normalize_text(text)
    short_terms = ["entry short", "strategia short", "target mensile short", "emo short", "short w", "scenario short"]
    long_terms = ["entry long", "strategia long", "target mensile long", "emo long", "scenario long"]
    s = sum(t.count(x) for x in short_terms)
    l = sum(t.count(x) for x in long_terms)
    if s > l and s > 0:
        return "SHORT"
    if l > s and l > 0:
        return "LONG"
    return "NEUTRAL"


def detect_period(text: str, fallback_dt: datetime) -> str:
    t = normalize_text(text)
    year_match = re.search(r"\b(20\d{2})\b", t)
    year = year_match.group(1) if year_match else str(fallback_dt.year)
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
    if re.search(r"\b30\s*['’]?\b|\b30m\b|30 min", t):
        vals.append("30m")
    if "daily" in t or re.search(r"\b1d\b", t):
        vals.append("D")
    if "weekly" in t or "settimanale" in t or re.search(r"\b1w\b", t):
        vals.append("W")
    if "mensile" in t or "monthly" in t or re.search(r"\b1m\b", t):
        vals.append("M")
    # keep useful multi-TF context but avoid duplicates
    out = []
    for x in vals:
        if x not in out:
            out.append(x)
    return "/".join(out)


def detect_category(text: str, instrument: str) -> str:
    t = normalize_text(text)
    if any(x in t for x in ["risultato come da ipotesi", "risultato", "target raggiunto"]):
        return "RESULT"
    if any(x in t for x in ["concetto di frattalita", "attesa della rottura", "puo optare", "regola alternativa"]):
        return "RULE"
    if any(x in t for x in ["livelli di entry", "entry short", "entry long", "potenziali livelli di entry"]):
        return "ENTRY"
    if "area da attenzionare" in t or "zona da attenzionare" in t:
        return "WATCH"
    if "strategia" in t:
        return "SETUP"
    if any(x in t for x in ["dinamica mensile", "dinamica delle medie", "pavimento importante", "verifica macro"]):
        return "MACRO"
    if not instrument and any(x in t for x in ["didattic", "lezioni", "complimenti", "moltissime opportunita"]):
        return "INFO"
    return "ANALISI" if instrument else "INFO"


def propose_status(category: str) -> str:
    return {
        "RESULT": "CLOSED",
        "ENTRY": "READY",
        "SETUP": "READY",
        "WATCH": "WATCH",
        "RULE": "WATCH",
        "MACRO": "WATCH",
        "ANALISI": "WATCH",
        "UPDATE": "WATCH",
        "INFO": "INFO",
    }.get(category, "WATCH")


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
    if t:
        snippet = t[:220]
        return f"{base}. OCR: {snippet}"
    return base


def confidence_score(instrument: str, bias: str, period: str, timeframe: str, category: str, ocr_text: str) -> int:
    score = 35
    if instrument:
        score += 25
    if bias != "NEUTRAL":
        score += 15
    if period:
        score += 10
    if timeframe:
        score += 5
    if category not in ["ANALISI", "INFO"]:
        score += 5
    if len((ocr_text or "").strip()) > 80:
        score += 5
    return min(score, 95)


def group_key(instrument: str, bias: str, period: str, category: str, received: datetime) -> str:
    if not instrument:
        return "INFO"
    p = period or received.strftime("%Y-%m")
    b = bias if bias != "NEUTRAL" else "DA_VERIFICARE"
    return f"{instrument}|{b}|{p}"


def recommend_destination(proposal: dict, signals_df: pd.DataFrame) -> str:
    if proposal["Categoria"] == "INFO" or not proposal["Asset"]:
        return "INFO"
    if signals_df.empty:
        return "NUOVO"
    best_label = "NUOVO"
    best_score = 0
    for _, r in signals_df.iterrows():
        score = 0
        if r["instrument"] == proposal["Asset"]:
            score += 4
        if r["bias"] == proposal["Bias"] and proposal["Bias"] != "NEUTRAL":
            score += 2
        p1 = normalize_text(str(r["period"]))
        p2 = normalize_text(proposal["Periodo"])
        if p1 and p2 and (p1 in p2 or p2 in p1):
            score += 3
        else:
            # softer month/week overlap
            toks1 = set(re.findall(r"w[1-5]|20\d{2}|" + "|".join(MONTHS), p1))
            toks2 = set(re.findall(r"w[1-5]|20\d{2}|" + "|".join(MONTHS), p2))
            score += len(toks1 & toks2)
        if score > best_score:
            best_score = score
            best_label = destination_label(r)
    return best_label if best_score >= 6 else "NUOVO"


def analyze_item(item: dict, signals_df: pd.DataFrame) -> dict:
    dt = parse_received_at(item["name"])
    text = ocr_image(item["bytes"])
    instrument, market_name = detect_instrument(text)
    bias = detect_bias(text)
    period = detect_period(text, dt)
    tf = detect_timeframe(text)
    category = detect_category(text, instrument)
    status = propose_status(category)
    action = propose_next_action(category, bias)
    conf = confidence_score(instrument, bias, period, tf, category, text)
    proposal = {
        "Importa": not hash_exists(item["hash"]),
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
        "Entry": "",
        "Stop": "",
        "Target": "",
        "Prossima azione": action,
        "Sintesi": make_summary(instrument, category, bias, period, text),
        "Confidence": conf,
        "OCR": text,
        "Hash": item["hash"],
    }
    proposal["Destinazione"] = recommend_destination(proposal, signals_df)
    if hash_exists(item["hash"]):
        proposal["Sintesi"] = "DUPLICATO già presente nel database. " + proposal["Sintesi"]
    return proposal


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


init_db()
auto_expire()

st.title("📡 Signal Radar V2")
st.caption("WhatsApp → import multiplo → proposta automatica → conferma → una riga per setup, tutte le immagini in cronologia.")

signals = load_signals()

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
    st.caption("V2 non importa automaticamente prezzi numerici come affidabili: Entry/Stop/Target restano da confermare.")

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

tab_dash, tab_detail, tab_batch, tab_single, tab_archive = st.tabs([
    "🎯 Active Signals", "🧭 Dettaglio setup", "⚡ Import multiplo", "➕ Singola immagine", "🗂 Archivio"
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
        view = view[["Stato", "instrument", "Bias", "period", "timeframe", "entry_zone", "target", "next_action", "Validità", "Ultimo agg."]]
        view.columns = ["Stato", "Asset", "Bias", "Periodo", "TF", "Entry / zona", "Target", "PROSSIMA AZIONE", "Validità", "Ultimo agg."]
        st.dataframe(view, use_container_width=True, hide_index=True, height=min(560, 72 + 35 * len(view)))

        st.markdown("#### Focus operativo")
        for _, r in filtered[filtered["status"].isin(["TRIGGERED", "READY", "WATCH"])].head(8).iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([1.2, 1, 3])
                c1.markdown(f"### {STATUS_ICON.get(r['status'],'')} {r['instrument']}")
                c1.caption(f"{BIAS_ICON.get(r['bias'],'')} {r['bias']} · {r['period']} · {r['timeframe']}")
                c2.markdown(f"**{r['status']}**")
                c2.caption(f"Validità: {r['validity_end'] or '—'}")
                c3.markdown(f"**Prossima azione:** {r['next_action'] or '—'}")
                if r["entry_zone"]:
                    c3.caption(f"Entry/Zona: {r['entry_zone']} · Target: {r['target'] or '—'}")

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
            if st.form_submit_button("Salva modifiche", type="primary"):
                data = dict(r)
                data.update({
                    "status": status_new, "bias": bias_new, "confidence": conf_new, "entry_zone": entry_new,
                    "stop_level": stop_new, "target": target_new, "next_action": action_new, "notes": notes_new,
                    "validity_end": validity_new, "last_update": datetime.now().isoformat(timespec="seconds"),
                })
                update_signal(int(r["id"]), data)
                st.success("Setup aggiornato.")
                st.rerun()

with tab_batch:
    st.subheader("⚡ Import multiplo WhatsApp")
    st.caption("Carica molte immagini insieme. V2 le ordina dal nome file, usa OCR locale gratuito e propone Asset/Bias/TF/Categoria/Gruppo. Prima dell'importazione puoi correggere tutto.")

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
            progress.empty()
            st.session_state["batch_df"] = pd.DataFrame(proposals)

        if "batch_df" in st.session_state:
            batch_df = st.session_state["batch_df"].copy()
            current_signals = load_signals()
            existing_destinations = [destination_label(r) for _, r in current_signals.iterrows()]
            destination_options = ["NUOVO", "INFO"] + existing_destinations

            st.markdown("### 1. Controlla le proposte")
            st.info("La colonna **Gruppo** decide quali immagini diventano un unico setup quando Destinazione = NUOVO. Se due immagini appartengono allo stesso setup, lascia lo stesso Gruppo. Se sono setup diversi, cambia il Gruppo.")

            edited = st.data_editor(
                batch_df,
                use_container_width=True,
                hide_index=True,
                num_rows="fixed",
                key="batch_editor",
                column_order=[
                    "Importa", "Ricevuto", "File", "Asset", "Bias", "Periodo", "TF", "Categoria", "Stato",
                    "Destinazione", "Gruppo", "Prossima azione", "Entry", "Stop", "Target", "Confidence", "Sintesi",
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

            st.markdown("### 2. Anteprima rapida")
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

            st.markdown("### 3. Importa")
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
st.caption("Signal Radar V2 · Obiettivo: organizzare e non perdere i segnali. Le proposte OCR sono assistenza, non conferma automatica di un trade.")
