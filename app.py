from __future__ import annotations

import io
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from PIL import Image

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
BIAS_ICON = {"LONG": "▲", "SHORT": "▼", "NEUTRAL": "•"}

st.set_page_config(page_title="Signal Radar V1", page_icon="📡", layout="wide")


def get_conn():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        );
        """
    )
    count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    if count == 0 and SEED_CSV.exists():
        sig = pd.read_csv(SEED_CSV).fillna("")
        for _, r in sig.iterrows():
            conn.execute(
                """INSERT INTO signals
                (setup_key,instrument,market_name,period,timeframe,bias,signal_type,entry_zone,stop_level,target,status,next_action,validity_end,confidence,notes,created_at,last_update)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(r[c] for c in [
                    "setup_key","instrument","market_name","period","timeframe","bias","signal_type","entry_zone","stop_level","target","status","next_action","validity_end","confidence","notes","created_at","last_update"
                ])
            )
        conn.commit()
        if SEED_UPDATES_CSV.exists():
            upd = pd.read_csv(SEED_UPDATES_CSV).fillna("")
            id_map = {r["setup_key"]: r["id"] for r in conn.execute("SELECT id, setup_key FROM signals").fetchall()}
            for _, r in upd.iterrows():
                signal_id = id_map.get(r["setup_key"]) if r["setup_key"] else None
                conn.execute(
                    "INSERT INTO updates (signal_id,received_at,category,summary,image_path,raw_text) VALUES (?,?,?,?,?,?)",
                    (signal_id, r["received_at"], r["category"], r["summary"], r["image_path"], r["raw_text"]),
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


def parse_received_at(filename: str) -> datetime:
    # WhatsApp Image 2026-08-16 at 21.44.05.jpeg
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2}).*?(\d{2})[.:](\d{2})[.:](\d{2})", filename)
    if m:
        y, mo, d, hh, mm, ss = map(int, m.groups())
        return datetime(y, mo, d, hh, mm, ss)
    return datetime.now()


def safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:150]


def save_uploaded_file(uploaded_file) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = UPLOAD_DIR / f"{ts}_{safe_name(uploaded_file.name)}"
    dest.write_bytes(uploaded_file.getbuffer())
    return str(dest.relative_to(APP_DIR))


def add_signal(data: dict) -> int:
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO signals
        (setup_key,instrument,market_name,period,timeframe,bias,signal_type,entry_zone,stop_level,target,status,next_action,validity_end,confidence,notes,created_at,last_update)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(data[k] for k in [
            "setup_key","instrument","market_name","period","timeframe","bias","signal_type","entry_zone","stop_level","target","status","next_action","validity_end","confidence","notes","created_at","last_update"
        ])
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
            data["instrument"], data["market_name"], data["period"], data["timeframe"], data["bias"], data["signal_type"],
            data["entry_zone"], data["stop_level"], data["target"], data["status"], data["next_action"], data["validity_end"],
            data["confidence"], data["notes"], data["last_update"], signal_id,
        ),
    )
    conn.commit()
    conn.close()


def add_update(signal_id: int | None, received_at: str, category: str, summary: str, image_path: str, raw_text: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO updates (signal_id,received_at,category,summary,image_path,raw_text) VALUES (?,?,?,?,?,?)",
        (signal_id, received_at, category, summary, image_path, raw_text),
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


init_db()
auto_expire()

st.title("📡 Signal Radar V1")
st.caption("WhatsApp → setup strutturati → prossima azione. Una riga per setup, tutte le immagini in cronologia.")

signals = load_signals()

with st.sidebar:
    st.header("Filtri")
    all_status = [s for s in STATUS_ORDER if s in signals["status"].unique().tolist()]
    selected_status = st.multiselect("Stato", all_status, default=[s for s in ["WATCH","READY","TRIGGERED"] if s in all_status])
    instruments = sorted(signals["instrument"].dropna().unique().tolist())
    selected_instruments = st.multiselect("Asset", instruments)
    selected_bias = st.multiselect("Bias", ["LONG", "SHORT", "NEUTRAL"])
    st.divider()
    st.caption("V1: i dati sono salvati in SQLite locale. Su Streamlit Community Cloud il filesystem può essere temporaneo: usa l'export/backup o, in V2, un database persistente.")

filtered = signals.copy()
if selected_status:
    filtered = filtered[filtered["status"].isin(selected_status)]
if selected_instruments:
    filtered = filtered[filtered["instrument"].isin(selected_instruments)]
if selected_bias:
    filtered = filtered[filtered["bias"].isin(selected_bias)]

active = signals[signals["status"].isin(["WATCH","READY","TRIGGERED"])]
ready = signals[signals["status"] == "READY"]
triggered = signals[signals["status"] == "TRIGGERED"]
expired = signals[signals["status"].isin(["EXPIRED","INVALIDATED"])]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Setup attivi", len(active))
m2.metric("Ready", len(ready))
m3.metric("Triggered", len(triggered))
m4.metric("Scaduti / invalidati", len(expired))

# Priority: TRIGGERED, READY, WATCH, then recent
priority_map = {"TRIGGERED": 0, "READY": 1, "WATCH": 2, "CLOSED": 3, "INVALIDATED": 4, "EXPIRED": 5, "INFO": 6}
filtered = filtered.assign(_priority=filtered["status"].map(priority_map).fillna(99)).sort_values(["_priority", "last_update"], ascending=[True, False])

tab_dash, tab_detail, tab_inbox, tab_archive = st.tabs(["🎯 Active Signals", "🧭 Dettaglio setup", "📥 Inbox / Nuova immagine", "🗂 Archivio"])

with tab_dash:
    st.subheader("Active Signals")
    if filtered.empty:
        st.info("Nessun setup con i filtri correnti.")
    else:
        view = filtered.copy()
        view["Stato"] = view["status"].map(lambda x: f"{STATUS_ICON.get(x,'')} {x}")
        view["Bias"] = view["bias"].map(lambda x: f"{BIAS_ICON.get(x,'')} {x}")
        view["Ultimo agg."] = pd.to_datetime(view["last_update"]).dt.strftime("%d/%m %H:%M")
        view["Validità"] = view["validity_end"].replace("", "—")
        view = view[["Stato","instrument","Bias","period","timeframe","entry_zone","target","next_action","Validità","Ultimo agg."]]
        view.columns = ["Stato","Asset","Bias","Periodo","TF","Entry / zona","Target","PROSSIMA AZIONE","Validità","Ultimo agg."]
        st.dataframe(view, use_container_width=True, hide_index=True, height=min(520, 72 + 35*len(view)))

        st.markdown("#### Focus operativo")
        for _, r in filtered[filtered["status"].isin(["TRIGGERED","READY","WATCH"])].head(8).iterrows():
            icon = STATUS_ICON.get(r["status"], "")
            with st.container(border=True):
                c1, c2, c3 = st.columns([1.2, 1, 3])
                c1.markdown(f"### {icon} {r['instrument']}")
                c1.caption(f"{BIAS_ICON.get(r['bias'],'')} {r['bias']} · {r['period']} · {r['timeframe']}")
                c2.markdown(f"**{r['status']}**")
                c2.caption(f"Validità: {r['validity_end'] or '—'}")
                c3.markdown(f"**Prossima azione:** {r['next_action'] or '—'}")
                if r["entry_zone"]:
                    c3.caption(f"Entry/Zona: {r['entry_zone']} · Target: {r['target'] or '—'}")

with tab_detail:
    st.subheader("Cronologia di un setup")
    all_rows = signals.to_dict("records")
    if not all_rows:
        st.info("Nessun setup salvato.")
    else:
        labels = {signal_label(r): r for r in all_rows}
        chosen = st.selectbox("Seleziona setup", list(labels.keys()))
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
            dt_txt = pd.to_datetime(u["received_at"]).strftime("%d/%m/%Y %H:%M")
            with st.expander(f"{dt_txt} · {u['category']} · {u['summary'][:90]}", expanded=False):
                if u["summary"]:
                    st.write(u["summary"])
                if u["raw_text"]:
                    st.code(u["raw_text"], language=None)
                display_image(u["image_path"], caption=dt_txt)

        st.markdown("### Aggiorna stato / operatività")
        with st.form(f"edit_{r['id']}"):
            ec1, ec2, ec3 = st.columns(3)
            status_new = ec1.selectbox("Stato", STATUS_ORDER, index=STATUS_ORDER.index(r["status"]) if r["status"] in STATUS_ORDER else 0)
            bias_new = ec2.selectbox("Bias", ["LONG","SHORT","NEUTRAL"], index=["LONG","SHORT","NEUTRAL"].index(r["bias"]) if r["bias"] in ["LONG","SHORT","NEUTRAL"] else 2)
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
                    "validity_end": validity_new, "last_update": datetime.now().isoformat(timespec="seconds")
                })
                update_signal(int(r["id"]), data)
                st.success("Setup aggiornato.")
                st.rerun()

with tab_inbox:
    st.subheader("Aggiungi uno screenshot WhatsApp")
    st.caption("V1: caricamento + conferma manuale. La logica è già pronta per aggiungere un estrattore AI in V2 senza cambiare database o dashboard.")
    uploaded = st.file_uploader("Screenshot", type=["png","jpg","jpeg","webp"])
    if uploaded is not None:
        try:
            img = Image.open(uploaded)
            st.image(img, use_container_width=True)
        except Exception:
            st.warning("Immagine non leggibile in anteprima, ma può comunque essere salvata.")
        parsed_dt = parse_received_at(uploaded.name)
        mode = st.radio("Questa immagine è…", ["Aggiornamento di un setup esistente", "Nuovo setup", "Solo INFO / didattica"], horizontal=True)

        current_signals = load_signals()
        if mode == "Aggiornamento di un setup esistente":
            choices = {signal_label(r): r for r in current_signals.to_dict("records")}
            selected = st.selectbox("Setup da aggiornare", list(choices.keys()))
            base = choices[selected]
            with st.form("update_upload_form"):
                received_date = st.date_input("Data ricezione", parsed_dt.date())
                received_time = st.time_input("Ora ricezione", parsed_dt.time().replace(microsecond=0))
                category = st.selectbox("Categoria", ["ANALISI", "WATCH", "ENTRY", "UPDATE", "RESULT", "RULE", "INFO"])
                summary = st.text_area("Riassunto", "")
                new_status = st.selectbox("Nuovo stato", STATUS_ORDER, index=STATUS_ORDER.index(base["status"]) if base["status"] in STATUS_ORDER else 0)
                new_action = st.text_area("Prossima azione", base["next_action"])
                if st.form_submit_button("Collega e salva", type="primary"):
                    received_at = datetime.combine(received_date, received_time).isoformat(timespec="seconds")
                    image_path = save_uploaded_file(uploaded)
                    add_update(int(base["id"]), received_at, category, summary, image_path)
                    data = dict(base)
                    data["status"] = new_status
                    data["next_action"] = new_action
                    data["last_update"] = received_at
                    update_signal(int(base["id"]), data)
                    st.success("Aggiornamento collegato allo stesso setup.")
                    st.rerun()

        elif mode == "Nuovo setup":
            with st.form("new_setup_form"):
                c1, c2, c3 = st.columns(3)
                instrument = c1.text_input("Asset / ticker", "")
                bias = c2.selectbox("Bias", ["LONG","SHORT","NEUTRAL"])
                status = c3.selectbox("Stato iniziale", ["WATCH","READY","TRIGGERED"])
                market_name = st.text_input("Nome mercato", "")
                period = st.text_input("Periodo / setup", "es. W3 Agosto 2026")
                timeframe = st.text_input("Timeframe", "")
                signal_type = st.selectbox("Tipo", ["WATCH","SETUP","ENTRY","MACRO","RULE"])
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
                            image_path = save_uploaded_file(uploaded)
                            add_update(sid, received_at, signal_type, summary, image_path)
                            st.success("Nuovo setup creato.")
                            st.rerun()
                        except sqlite3.IntegrityError:
                            st.error("Setup duplicato. Usa 'Aggiornamento di un setup esistente'.")

        else:
            with st.form("info_upload_form"):
                received_date = st.date_input("Data ricezione", parsed_dt.date())
                received_time = st.time_input("Ora ricezione", parsed_dt.time().replace(microsecond=0))
                summary = st.text_area("Riassunto / testo", "")
                if st.form_submit_button("Archivia come INFO"):
                    received_at = datetime.combine(received_date, received_time).isoformat(timespec="seconds")
                    image_path = save_uploaded_file(uploaded)
                    add_update(None, received_at, "INFO", summary, image_path)
                    st.success("Archiviato come INFO, senza intasare gli Active Signals.")
                    st.rerun()

with tab_archive:
    st.subheader("Archivio completo")
    archive = load_signals()
    if not archive.empty:
        exp = archive.copy()
        exp["status"] = exp["status"].map(lambda x: f"{STATUS_ICON.get(x,'')} {x}")
        st.dataframe(exp.drop(columns=["_priority"], errors="ignore"), use_container_width=True, hide_index=True)
        csv_bytes = archive.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Esporta setup CSV", csv_bytes, "signal_radar_export.csv", "text/csv")
    all_updates = load_updates()
    with st.expander("Aggiornamenti / immagini non assegnate"):
        unassigned = all_updates[all_updates["signal_id"].isna()]
        if unassigned.empty:
            st.caption("Nessuna INFO non assegnata.")
        else:
            for _, u in unassigned.sort_values("received_at", ascending=False).iterrows():
                st.markdown(f"**{u['received_at']} · {u['category']}** — {u['summary']}")
                display_image(u["image_path"])
                st.divider()

st.divider()
st.caption("Signal Radar V1 · Obiettivo: non generare segnali, ma non perderli e sapere sempre qual è la prossima azione.")
