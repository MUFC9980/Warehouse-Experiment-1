import os
import re
import sqlite3
from datetime import datetime, date
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Warehouse Efficiency System - Surabaya & Jakarta", page_icon="📦", layout="wide")

WAREHOUSES = ["Surabaya", "Jakarta"]

# --- 0. SUPERVISOR PASSCODE & 8 USER ACCOUNTS CONFIG ---
def get_supervisor_pin() -> str:
    try:
        if "SUPERVISOR_PIN" in st.secrets:
            return str(st.secrets["SUPERVISOR_PIN"]).strip()
    except Exception:
        pass
    return os.getenv("SUPERVISOR_PIN", "8899").strip()

SUPERVISOR_PIN = get_supervisor_pin()

USER_ACCOUNTS = {
    "david":   {"pin": SUPERVISOR_PIN, "display": "David (Supervisor)",   "role": "Supervisor"},
    "hartono": {"pin": SUPERVISOR_PIN, "display": "Hartono (Director)",   "role": "Supervisor"},
    "admin":   {"pin": "1111",         "display": "Admin Gudang",         "role": "Admin"},
    "budi":    {"pin": "2222",         "display": "Budi (Staff Gudang)",  "role": "Staff"},
    "andi":    {"pin": "3333",         "display": "Andi (Staff Gudang)",  "role": "Staff"},
    "siti":    {"pin": "4444",         "display": "Siti (Admin Stok)",    "role": "Staff"},
    "eko":     {"pin": "5555",         "display": "Eko (Picker)",         "role": "Staff"},
    "rudi":    {"pin": "6666",         "display": "Rudi (Checker)",       "role": "Staff"},
}

# --- AUTHENTICATION / SIGN-IN GATE ---
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "current_user" not in st.session_state:
    st.session_state.current_user = ""
if "current_role" not in st.session_state:
    st.session_state.current_role = ""

if not st.session_state.authenticated:
    col_l, col_mid, col_r = st.columns([1, 1.5, 1])
    with col_mid:
        st.title("🔐 Warehouse Efficiency System")
        st.markdown("Silakan masukkan **Username** dan **PIN** untuk masuk ke sistem gudang (Surabaya & Jakarta).")
        
        with st.form("login_form"):
            entered_user = st.text_input("Username", placeholder="Contoh: david, budi, admin...")
            entered_pin = st.text_input("Passcode / PIN", type="password", placeholder="Masukkan PIN...")
            submit_login = st.form_submit_button("Masuk ke Program", use_container_width=True)
            
            if submit_login:
                uname = entered_user.strip().lower()
                upin = entered_pin.strip()
                if uname in USER_ACCOUNTS and USER_ACCOUNTS[uname]["pin"] == upin:
                    st.session_state.authenticated = True
                    st.session_state.current_user = USER_ACCOUNTS[uname]["display"]
                    st.session_state.current_role = USER_ACCOUNTS[uname]["role"]
                    st.success(f"Selamat datang, {st.session_state.current_user}! Memuat sistem...")
                    st.rerun()
                else:
                    st.error("Username atau PIN salah! Silakan periksa kembali.")
    
    st.stop()

# --- 1. LOCAL AI CONNECTION SETUP ---
def get_ai_client_and_model() -> tuple[OpenAI, str, str]:
    for port, server_name in ((1234, "LM Studio"), (1337, "Jan AI")):
        client = OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="not-needed", timeout=15.0)
        try:
            models = client.models.list()
            if models.data:
                all_ids = [m.id for m in models.data]
                preferred = [
                    mid for mid in all_ids
                    if any(good in mid.lower() for good in ("qwen", "instruct", "chat", "gemma", "llama"))
                    and not any(bad in mid.lower() for bad in ("embed", "bge", "e5-", "rerank", "sentence-transformer"))
                ]
                if preferred:
                    return client, preferred[0], f"Local AI ({server_name} :{port})"

                chat_models = [
                    mid for mid in all_ids
                    if not any(bad in mid.lower() for bad in ("embed", "bge", "e5-", "rerank", "sentence-transformer"))
                ]
                if chat_models:
                    return client, chat_models[0], f"Local AI ({server_name} :{port})"
        except Exception:
            continue

    api_key = os.getenv("AI_API_KEY", "")
    base_url = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
    model_name = os.getenv("AI_MODEL", "gpt-4o-mini")
    try:
        if "AI_API_KEY" in st.secrets:
            api_key = st.secrets["AI_API_KEY"]
            base_url = st.secrets.get("AI_BASE_URL", base_url)
            model_name = st.secrets.get("AI_MODEL", model_name)
    except Exception:
        pass

    if api_key:
        return OpenAI(base_url=base_url, api_key=api_key), model_name, "Cloud AI Fallback"

    return OpenAI(base_url="http://127.0.0.1:1337/v1", api_key="not-needed"), "Qwen2.5-3B-Instruct-4bit", "Offline (Server Belum Aktif)"

# --- 2. DATABASE SETUP, WAL & MULTI-WAREHOUSE MIGRATION ---
conn = sqlite3.connect("jurnal_bins.db", timeout=15.0, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA temp_store=MEMORY;")

# Check if bin_map exists and needs migration to composite primary key (warehouse, jurnal_sku)
table_exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bin_map'").fetchone()

if not table_exists:
    conn.execute("""
        CREATE TABLE bin_map (
            warehouse TEXT DEFAULT 'Surabaya',
            jurnal_sku TEXT,
            product_name TEXT,
            category TEXT,
            row_num INTEGER,
            floor_num INTEGER,
            bin_location TEXT,
            turnover_speed TEXT,
            notes TEXT,
            box_qty INTEGER DEFAULT 1,
            pcs_per_box INTEGER,
            min_box_threshold INTEGER DEFAULT 3,
            expiry_date TEXT,
            picker_name TEXT,
            PRIMARY KEY (warehouse, jurnal_sku)
        )
    """)
else:
    existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(bin_map)").fetchall()]
    if "box_qty" not in existing_cols:
        conn.execute("ALTER TABLE bin_map ADD COLUMN box_qty INTEGER DEFAULT 1")
    if "pcs_per_box" not in existing_cols:
        conn.execute("ALTER TABLE bin_map ADD COLUMN pcs_per_box INTEGER")
    if "min_box_threshold" not in existing_cols:
        conn.execute("ALTER TABLE bin_map ADD COLUMN min_box_threshold INTEGER DEFAULT 3")
    if "expiry_date" not in existing_cols:
        conn.execute("ALTER TABLE bin_map ADD COLUMN expiry_date TEXT")
    if "picker_name" not in existing_cols:
        conn.execute("ALTER TABLE bin_map ADD COLUMN picker_name TEXT")
    if "warehouse" not in existing_cols:
        # Migrate legacy single-warehouse table safely to composite PK (warehouse, jurnal_sku)
        conn.execute("""
            CREATE TABLE bin_map_new (
                warehouse TEXT DEFAULT 'Surabaya',
                jurnal_sku TEXT,
                product_name TEXT,
                category TEXT,
                row_num INTEGER,
                floor_num INTEGER,
                bin_location TEXT,
                turnover_speed TEXT,
                notes TEXT,
                box_qty INTEGER DEFAULT 1,
                pcs_per_box INTEGER,
                min_box_threshold INTEGER DEFAULT 3,
                expiry_date TEXT,
                picker_name TEXT,
                PRIMARY KEY (warehouse, jurnal_sku)
            )
        """)
        conn.execute("""
            INSERT INTO bin_map_new (
                warehouse, jurnal_sku, product_name, category, row_num, floor_num,
                bin_location, turnover_speed, notes, box_qty, pcs_per_box,
                min_box_threshold, expiry_date, picker_name
            )
            SELECT
                'Surabaya', jurnal_sku, product_name, category, row_num, floor_num,
                bin_location, turnover_speed, notes, COALESCE(box_qty, 1), pcs_per_box,
                COALESCE(min_box_threshold, 3), expiry_date, picker_name
            FROM bin_map
        """)
        conn.execute("DROP TABLE bin_map")
        conn.execute("ALTER TABLE bin_map_new RENAME TO bin_map")

conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_bin_location ON bin_map(warehouse, row_num, floor_num);")
conn.execute("CREATE INDEX IF NOT EXISTS idx_product_name ON bin_map(product_name);")
conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON bin_map(category);")

conn.execute("""
    CREATE TABLE IF NOT EXISTS warehouse_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT,
        memory_text TEXT,
        created_at TEXT
    )
""")
conn.execute("""
    CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        action TEXT,
        details TEXT,
        actor TEXT
    )
""")
conn.commit()

def log_activity(action: str, details: str, actor: str | None = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    active_actor = actor if actor else st.session_state.get("current_user", "System")
    conn.execute(
        "INSERT INTO activity_log (timestamp, action, details, actor) VALUES (?, ?, ?, ?)",
        (now, action, details, active_actor)
    )
    conn.commit()

def format_qty_summary(box_qty: int, pcs_per_box: int | None) -> str:
    b_qty = int(box_qty) if pd.notna(box_qty) and box_qty else 1
    if pd.notna(pcs_per_box) and pcs_per_box and int(pcs_per_box) > 0:
        p_qty = int(pcs_per_box)
        return f"{b_qty} dus × {p_qty} pcs ({b_qty * p_qty:,} pcs)".replace(",", ".")
    return f"{b_qty} dus"

def get_warehouse_stats(warehouse: str | None = None) -> dict:
    if warehouse:
        row = conn.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(box_qty), 0),
                COALESCE(SUM(CASE WHEN pcs_per_box IS NOT NULL AND pcs_per_box > 0 THEN box_qty * pcs_per_box ELSE 0 END), 0),
                COUNT(DISTINCT bin_location)
            FROM bin_map
            WHERE warehouse = ?
        """, (warehouse,)).fetchone()
        total_slots = 48
    else:
        row = conn.execute("""
            SELECT
                COUNT(*),
                COALESCE(SUM(box_qty), 0),
                COALESCE(SUM(CASE WHEN pcs_per_box IS NOT NULL AND pcs_per_box > 0 THEN box_qty * pcs_per_box ELSE 0 END), 0),
                COUNT(DISTINCT warehouse || '-' || bin_location)
            FROM bin_map
        """).fetchone()
        total_slots = 96
    return {
        "total_skus": int(row[0]),
        "total_boxes": int(row[1]),
        "total_pcs": int(row[2]),
        "occupied_bins": int(row[3]),
        "empty_bins": total_slots - int(row[3]),
        "total_slots": total_slots
    }

def get_memory_context() -> str:
    mem_df = pd.read_sql_query(
        "SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC LIMIT 30",
        conn
    )
    log_df = pd.read_sql_query(
        "SELECT timestamp, action, details FROM activity_log ORDER BY id DESC LIMIT 10",
        conn
    )
    parts = []
    if not mem_df.empty:
        rules = "\n".join([f"- [{r['category']}] {r['memory_text']}" for _, r in mem_df.iterrows()])
        parts.append(f"ATURAN & MEMORI TETAP GUDANG:\n{rules}")
    if not log_df.empty:
        logs = "\n".join([f"- {r['timestamp']} | {r['action']}: {r['details']}" for _, r in log_df.iterrows()])
        parts.append(f"RIWAYAT AKTIVITAS TERAKHIR:\n{logs}")
    return "\n\n".join(parts) if parts else "Belum ada memori khusus tambahan."

def search_inventory_for_ai(query_text: str, limit: int = 30) -> str:
    sby = get_warehouse_stats("Surabaya")
    jkt = get_warehouse_stats("Jakarta")
    summary_header = (
        f"RINGKASAN GUDANG:\n"
        f"- SURABAYA: {sby['total_skus']:,} SKU | {sby['total_boxes']:,} Dus | {sby['occupied_bins']}/48 Slot Terisi.\n"
        f"- JAKARTA: {jkt['total_skus']:,} SKU | {jkt['total_boxes']:,} Dus | {jkt['occupied_bins']}/48 Slot Terisi."
    ).replace(",", ".")

    if (sby["total_skus"] + jkt["total_skus"]) == 0:
        return f"{summary_header}\nSemua slot di Surabaya dan Jakarta masih kosong."

    slot_summary_df = pd.read_sql_query("""
        SELECT warehouse, bin_location, COUNT(*) AS sku_count, COALESCE(SUM(box_qty), 0) AS total_dus
        FROM bin_map
        GROUP BY warehouse, bin_location
        ORDER BY warehouse, row_num, floor_num
    """, conn)
    slot_lines = ", ".join([
        f"[{r['warehouse'][:3].upper()}] {r['bin_location']}({r['sku_count']} SKU/{int(r['total_dus'])} dus)"
        for _, r in slot_summary_df.iterrows()
    ])

    stopwords = {
        "dimana", "mana", "lokasi", "rak", "barang", "stok", "ada", "berapa", "yang",
        "untuk", "dari", "dan", "atau", "tolong", "carikan", "cek", "rekomendasi",
        "prediksi", "slot", "kosong", "masuk", "baru", "jumlah", "dus", "pcs", "ini", "itu", "gudang"
    }
    tokens = [
        t.strip(".,?!:;()[]\"'")
        for t in query_text.lower().split()
        if len(t.strip(".,?!:;()[]\"'")) >= 2 and t.strip(".,?!:;()[]\"'") not in stopwords
    ]

    if tokens:
        conditions = []
        params = []
        for tok in tokens[:5]:
            like_pat = f"%{tok}%"
            conditions.append(
                "(LOWER(warehouse) LIKE ? OR LOWER(jurnal_sku) LIKE ? OR LOWER(product_name) LIKE ? OR LOWER(category) LIKE ? OR LOWER(bin_location) LIKE ? OR LOWER(notes) LIKE ?)"
            )
            params.extend([like_pat, like_pat, like_pat, like_pat, like_pat, like_pat])
        where_clause = " OR ".join(conditions)
        matched_df = pd.read_sql_query(
            f"""SELECT warehouse AS "Gudang", bin_location AS "Rak", jurnal_sku AS "SKU", product_name AS "Produk",
                       category AS "Kategori", box_qty AS "Dus", expiry_date AS "Expired", turnover_speed AS "Perputaran"
                FROM bin_map WHERE {where_clause} ORDER BY warehouse, row_num, floor_num LIMIT {limit}""",
            conn, params=params
        )
    else:
        matched_df = pd.read_sql_query(
            f"""SELECT warehouse AS "Gudang", bin_location AS "Rak", jurnal_sku AS "SKU", product_name AS "Produk",
                       category AS "Kategori", box_qty AS "Dus", expiry_date AS "Expired", turnover_speed AS "Perputaran"
                FROM bin_map ORDER BY warehouse, row_num, floor_num LIMIT {limit}""",
            conn
        )

    matched_csv = matched_df.to_csv(index=False) if not matched_df.empty else "Tidak ada SKU yang cocok."
    return f"{summary_header}\nKETERISIAN: {slot_lines}\n\nSAMPEL SKU:\n{matched_csv}"

def validate_placement_against_rules(
    sku: str, name: str, cat: str, row_num: int, floor_num: int, speed: str, notes: str
) -> tuple[bool, str]:
    speed_low = speed.lower()
    if "fast-moving" in speed_low:
        if floor_num not in (1, 2):
            return False, f"Barang Fast-Moving wajib di Lantai 1 atau 2 (Anda memilih Lantai {floor_num})."
    elif "medium" in speed_low:
        if floor_num not in (3, 4):
            return False, f"Barang Medium wajib di Lantai 3 atau 4 (Anda memilih Lantai {floor_num})."
    elif "slow-moving" in speed_low or "berat" in speed_low or "bulky" in speed_low:
        if floor_num not in (5, 6):
            return False, f"Barang Slow-Moving / Bulky wajib di Lantai 5 atau 6 (Anda memilih Lantai {floor_num})."

    mem_df = pd.read_sql_query("SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC", conn)
    if mem_df.empty:
        return True, ""
    item_blob = f"{sku} {name} {cat} {notes}".lower()
    for _, r in mem_df.iterrows():
        rule_raw = str(r["memory_text"]).strip()
        rule_low = rule_raw.lower()
        row_tokens = [f"baris {row_num:02d}", f"baris {row_num}", f"r{row_num:02d}"]
        if any(tok in rule_low for tok in row_tokens) and "hanya boleh" in rule_low:
            allowed_part = rule_low.split("oleh")[-1] if "oleh" in rule_low else rule_low.split("hanya boleh")[-1]
            allowed_keywords = [w.strip(" .,-") for w in allowed_part.replace("diisi", "").split() if len(w.strip(" .,-")) > 2]
            if allowed_keywords and not all(kw in item_blob for kw in allowed_keywords):
                return False, f"Melanggar aturan tetap: '{rule_raw}'."
    return True, ""

def evaluate_override_reason_with_ai(
    warehouse: str, sku: str, name: str, cat: str, row_num: int, floor_num: int, speed: str, notes: str,
    box_qty: int, pcs_per_box: int | None, block_reason: str, override_reason: str
) -> tuple[bool, str]:
    target_bin = f"R{row_num:02d}-L{floor_num}"
    qty_desc = format_qty_summary(box_qty, pcs_per_box)
    slot_counts_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS cnt, COALESCE(SUM(box_qty), 0) AS boxes FROM bin_map WHERE warehouse = ? GROUP BY row_num, floor_num",
        conn, params=[warehouse]
    )
    occupancy_map = {(int(r["row_num"]), int(r["floor_num"])): (int(r["cnt"]), int(r["boxes"])) for _, r in slot_counts_df.iterrows()}

    valid_empty_bins = []
    for r in range(1, 9):
        for f in range(1, 7):
            ok, _ = validate_placement_against_rules(sku, name, cat, r, f, speed, notes)
            if ok and occupancy_map.get((r, f), (0, 0))[0] == 0:
                valid_empty_bins.append(f"R{r:02d}-L{f}")

    cleaned = override_reason.strip()
    if len(cleaned) < 6 or len(set(cleaned.lower())) <= 3:
        return False, f"**Alasan Kurang Masuk Akal:** Penjelasan terlalu singkat (`\"{cleaned}\"`)."

    try:
        client, active_model, status = get_ai_client_and_model()
        if not status.startswith("Offline"):
            valid_info = f"Slot kosong valid di {warehouse}: {', '.join(valid_empty_bins[:10])}" if valid_empty_bins else f"Tidak ada slot kosong di {warehouse}."
            audit_prompt = f"""Auditor Kepala Gudang ({warehouse}). Nilai apakah alasan 15% override logis.
GUDANG: {warehouse}
PRODUK: {sku} — {name} ({qty_desc}, {speed})
TUJUAN: {target_bin} | PELANGGARAN: {block_reason}
FAKTA GUDANG: {valid_info}
ALASAN STAF: "{override_reason}"

Awali baris pertama dengan tepat:
STATUS: MASUK AKAL
atau
STATUS: KURANG MASUK AKAL
Di bawahnya berikan 2 kalimat alasan."""
            res = client.chat.completions.create(model=active_model, temperature=0.1, max_tokens=220, messages=[{"role": "user", "content": audit_prompt}])
            reply = res.choices[0].message.content.strip()
            first_line = reply.splitlines()[0].upper() if reply else ""
            is_logical = "MASUK AKAL" in first_line and "KURANG" not in first_line and "TIDAK" not in first_line
            cleaned_feedback = "\n".join(reply.splitlines()[1:]).strip() if len(reply.splitlines()) > 1 else reply
            return is_logical, cleaned_feedback
    except Exception:
        pass

    return True, f"Passcode valid. Alasan dicatat: *\"{override_reason}\"*"

def auto_assign_bin(
    warehouse: str, sku: str, name: str, cat: str, speed: str, notes: str, box_qty: int, pcs_per_box: int | None
) -> tuple[int | None, int | None, str]:
    slot_counts_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS cnt, COALESCE(SUM(box_qty), 0) AS boxes FROM bin_map WHERE warehouse = ? GROUP BY row_num, floor_num",
        conn, params=[warehouse]
    )
    occupancy_map = {(int(r["row_num"]), int(r["floor_num"])): (int(r["cnt"]), int(r["boxes"])) for _, r in slot_counts_df.iterrows()}

    valid_slots = []
    for r in range(1, 9):
        for f in range(1, 7):
            ok, _ = validate_placement_against_rules(sku, name, cat, r, f, speed, notes)
            if ok:
                sku_cnt, box_cnt = occupancy_map.get((r, f), (0, 0))
                valid_slots.append((sku_cnt, box_cnt, r, f, f"R{r:02d}-L{f}"))

    if not valid_slots:
        return None, None, f"Tidak ada slot di Gudang {warehouse} yang memenuhi zonasi!"
    valid_slots.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    top_candidates = valid_slots[:12]
    candidate_labels = [f"{s[4]} ({s[0]} SKU, {s[1]} dus)" for s in top_candidates]
    candidate_codes = [s[4] for s in top_candidates]

    try:
        client, active_model, status = get_ai_client_and_model()
        if not status.startswith("Offline"):
            prompt = f"Pilih 1 kode rak terbaik di Gudang {warehouse} untuk {sku} {name}. Kandidat: {', '.join(candidate_labels)}. Balas HANYA dengan kode rak (misal R01-L1)."
            res = client.chat.completions.create(model=active_model, temperature=0.1, max_tokens=50, messages=[{"role": "user", "content": prompt}])
            match = re.search(r"R0([1-8])-L([1-6])", res.choices[0].message.content.upper())
            if match:
                chosen = f"R0{match.group(1)}-L{match.group(2)}"
                if chosen in candidate_codes:
                    return int(match.group(1)), int(match.group(2)), f"AI memilih {chosen} ({warehouse})"
    except Exception:
        pass

    _, _, best_r, best_f, best_code = top_candidates[0]
    return best_r, best_f, f"Otomatis ke slot paling longgar di {warehouse} ({best_code})."

# --- HELPER RENDERER FOR TAB 1 WAREHOUSE INPUT ---
def render_warehouse_input_form(warehouse_name: str):
    wh_key = warehouse_name.lower()
    st.subheader(f"📥 Input Barang Masuk — Gudang {warehouse_name}")
    
    with st.form(f"add_sku_form_{wh_key}", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        sku = c1.text_input("Kode SKU Jurnal", placeholder="JRN-1042", key=f"sku_{wh_key}")
        name = c2.text_input("Nama Produk", placeholder="Raket Yonex", key=f"name_{wh_key}")
        cat = c3.text_input("Kategori", placeholder="Olahraga", key=f"cat_{wh_key}")

        q1, q2, q3, q4 = st.columns([1, 1, 1, 1.5])
        box_qty = q1.number_input("Jumlah Dus", min_value=1, value=1, step=1, key=f"bqty_{wh_key}")
        pcs_per_box = q2.number_input("Isi/Dus (Pcs)", min_value=0, value=0, step=1, key=f"pbox_{wh_key}")
        min_thresh = q3.number_input("Min. Threshold Dus (Alert)", min_value=1, value=3, step=1, key=f"mth_{wh_key}")
        speed = q4.selectbox("Perputaran", ["Fast-Moving (Laku Cepat)", "Medium (Normal)", "Slow-Moving (Lambat)", "Berat / Besar (Bulky)"], key=f"spd_{wh_key}")

        auto_mode = st.checkbox(f"🤖 Auto-Assign Rak {warehouse_name} oleh AI", value=True, key=f"auto_{wh_key}")

        r_c1, r_c2, r_c3, r_c4 = st.columns([1, 1, 1.5, 2])
        row_num = r_c1.selectbox("Baris", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}", key=f"row_{wh_key}")
        floor_num = r_c2.selectbox("Lantai", list(range(1, 7)), format_func=lambda x: f"Lantai {x}", key=f"flr_{wh_key}")
        expiry_val = r_c3.date_input("Expired Date (Opsional)", value=None, key=f"exp_{wh_key}")
        notes = r_c4.text_input("Catatan Fisik", placeholder="Dus besar", key=f"nts_{wh_key}")

        st.divider()
        with st.expander(f"🔐 15% Fleksibel ({warehouse_name}): Override Supervisor + Audit AI"):
            allow_override = st.checkbox("Izinkan Penempatan di Luar Aturan Zonasi", key=f"ov_chk_{wh_key}")
            ov1, ov2 = st.columns([2, 1])
            override_reason = ov1.text_input("Alasan Pengecualian:", placeholder="Kardus terlalu besar untuk lantai bawah", key=f"ov_rsn_{wh_key}")
            override_pin = ov2.text_input("Passcode Supervisor:", type="password", placeholder="PIN", key=f"ov_pin_{wh_key}")

        if st.form_submit_button(f"Simpan Lokasi Rak ({warehouse_name})"):
            st.session_state.pending_force_override = None
            if not sku.strip():
                st.warning("SKU wajib diisi.")
            else:
                pcs_val = int(pcs_per_box) if int(pcs_per_box) > 0 else None
                exp_str = expiry_val.strftime("%Y-%m-%d") if expiry_val else None

                if auto_mode:
                    with st.spinner(f"AI memilih slot terbaik di {warehouse_name}..."):
                        ar, af, msg = auto_assign_bin(warehouse_name, sku.strip(), name.strip(), cat.strip(), speed, notes.strip(), int(box_qty), pcs_val)
                    if ar is None:
                        st.error(f"Gagal: {msg}")
                    else:
                        b_code = f"R{ar:02d}-L{af}"
                        conn.execute("""INSERT OR REPLACE INTO bin_map 
                            (warehouse, jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (warehouse_name, sku.strip(), name.strip(), cat.strip(), ar, af, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                        conn.commit()
                        log_activity("AUTO_ASSIGN", f"[{warehouse_name}] {sku.strip()} -> {b_code}")
                        st.success(f"Berhasil Auto-Assign `{sku.strip()}` → **{warehouse_name} | {b_code}** ({msg})")
                else:
                    b_code = f"R{row_num:02d}-L{floor_num}"
                    is_ok, blk_msg = validate_placement_against_rules(sku.strip(), name.strip(), cat.strip(), row_num, floor_num, speed, notes.strip())
                    if not is_ok and not allow_override:
                        st.error(f"Ditolak Zonasi: {blk_msg}")
                    elif not is_ok and allow_override:
                        if not override_reason.strip():
                            st.warning("Alasan override belum diisi.")
                            st.stop()
                        if override_pin.strip() != SUPERVISOR_PIN:
                            st.error("Passcode Supervisor salah!")
                            st.stop()
                        with st.spinner("AI menganalisis alasan override..."):
                            r_ok, ai_fb = evaluate_override_reason_with_ai(warehouse_name, sku.strip(), name.strip(), cat.strip(), row_num, floor_num, speed, notes.strip(), int(box_qty), pcs_val, blk_msg, override_reason.strip())
                        if r_ok:
                            conn.execute("""INSERT OR REPLACE INTO bin_map 
                                (warehouse, jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (warehouse_name, sku.strip(), name.strip(), cat.strip(), row_num, floor_num, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                            conn.commit()
                            log_activity("OVERRIDE_APPROVED", f"[{warehouse_name}] {sku.strip()} ke {b_code}")
                            st.success(f"Override Disetujui AI: `{sku.strip()}` → **{warehouse_name} | {b_code}**\n\n**Audit AI:** {ai_fb}")
                        else:
                            st.session_state.pending_force_override = {
                                "warehouse": warehouse_name,
                                "sku": sku.strip(), "name": name.strip(), "cat": cat.strip(),
                                "row_num": row_num, "floor_num": floor_num, "bin_code": b_code,
                                "speed": speed, "notes": notes.strip(), "box_qty": int(box_qty),
                                "pcs_val": pcs_val, "min_thresh": int(min_thresh), "exp_str": exp_str,
                                "override_reason": override_reason.strip(), "ai_feedback": ai_fb
                            }
                    else:
                        conn.execute("""INSERT OR REPLACE INTO bin_map 
                            (warehouse, jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (warehouse_name, sku.strip(), name.strip(), cat.strip(), row_num, floor_num, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                        conn.commit()
                        log_activity("SIMPAN_RAK", f"[{warehouse_name}] {sku.strip()} -> {b_code}")
                        st.success(f"Berhasil menyimpan: `{sku.strip()}` → **{warehouse_name} | {b_code}**")

    if st.session_state.pending_force_override and st.session_state.pending_force_override.get("warehouse") == warehouse_name:
        pfo = st.session_state.pending_force_override
        st.error(f"⚠️ **Audit AI ({warehouse_name}): Kurang Masuk Akal**\n\n{pfo['ai_feedback']}")
        fc1, fc2 = st.columns(2)
        if fc1.button(f"🔓 Tetap Paksa Simpan ({warehouse_name})", type="primary", key=f"force_btn_{wh_key}"):
            conn.execute("""INSERT OR REPLACE INTO bin_map 
                (warehouse, jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pfo["warehouse"], pfo["sku"], pfo["name"], pfo["cat"], pfo["row_num"], pfo["floor_num"], pfo["bin_code"], pfo["speed"], pfo["notes"], pfo["box_qty"], pfo["pcs_val"], pfo["min_thresh"], pfo["exp_str"]))
            conn.commit()
            log_activity("FORCE_OVERRIDE", f"[{pfo['warehouse']}] Force save {pfo['sku']} to {pfo['bin_code']}")
            st.session_state.pending_force_override = None
            st.success("Tersimpan dengan Force Override.")
            st.rerun()
        if fc2.button("❌ Batalkan", key=f"cancel_btn_{wh_key}"):
            st.session_state.pending_force_override = None
            st.rerun()

# --- HELPER RENDERER FOR 8x6 WAREHOUSE MAP ---
def render_warehouse_grid(warehouse_name: str):
    agg_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS sku_cnt, COALESCE(SUM(box_qty), 0) AS total_dus FROM bin_map WHERE warehouse = ? GROUP BY row_num, floor_num",
        conn, params=[warehouse_name]
    )
    slot_lookup = {(int(r["row_num"]), int(r["floor_num"])): (int(r["sku_cnt"]), int(r["total_dus"])) for _, r in agg_df.iterrows()}
    
    grid_data = {}
    for r in range(1, 9):
        col_lbl = f"Baris {r:02d}"
        grid_data[col_lbl] = []
        for f in range(6, 0, -1):
            s_cnt, d_cnt = slot_lookup.get((r, f), (0, 0))
            grid_data[col_lbl].append(f"[Kosong] R{r:02d}-L{f}" if s_cnt == 0 else f"📦 {s_cnt} SKU ({d_cnt} dus)")
    st.dataframe(pd.DataFrame(grid_data, index=[f"Lantai {f}" for f in range(6, 0, -1)]), width="stretch", height=240)

# --- 3. STREAMLIT APP & UI ---
head_col1, head_col2 = st.columns([4, 1])
with head_col1:
    st.title("📦 Warehouse Efficiency System")
with head_col2:
    st.markdown(f"**👤 {st.session_state.current_user}**")
    if st.button("🚪 Keluar / Logout", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state.current_user = ""
        st.session_state.current_role = ""
        st.rerun()

sby_stats = get_warehouse_stats("Surabaya")
jkt_stats = get_warehouse_stats("Jakarta")
st.caption(
    f"Zonasi 85% • 15% Fleksibel | "
    f"**Surabaya:** {sby_stats['total_skus']:,} SKU ({sby_stats['occupied_bins']}/48 Slot) • "
    f"**Jakarta:** {jkt_stats['total_skus']:,} SKU ({jkt_stats['occupied_bins']}/48 Slot)"
)

if "tab6_unlocked" not in st.session_state:
    st.session_state.tab6_unlocked = False
if "pending_force_override" not in st.session_state:
    st.session_state.pending_force_override = None

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
    "1. Input & Zonasi",
    "2. Peta Rak & Audit",
    "3. Asisten AI",
    "4. Picking & Invoice",
    "5. Reorder Alerts ⚠️",
    "6. FIFO & Expiry ⏳",
    "7. Import Excel",
    "8. 🔒 Memori & Leaderboard"
])

# --- TAB 1: INPUT & PEMETAAN RAK (SURABAYA & JAKARTA) ---
with tab1:
    sub_sby, sub_jkt = st.tabs(["🏭 Gudang Surabaya (8×6)", "🏭 Gudang Jakarta (8×6)"])
    with sub_sby:
        render_warehouse_input_form("Surabaya")
    with sub_jkt:
        render_warehouse_input_form("Jakarta")

# --- TAB 2: PETA RAK & CYCLE COUNTING AUDIT MODE ---
with tab2:
    st.subheader("Denah Rak (Surabaya & Jakarta) & Cycle Counting Audit")
    
    map_sby, map_jkt = st.tabs(["🗺️ Peta Rak Surabaya", "🗺️ Peta Rak Jakarta"])
    with map_sby:
        st.markdown(f"**Status Surabaya:** {sby_stats['total_skus']} SKU | {sby_stats['total_boxes']} Dus | {sby_stats['occupied_bins']}/48 Slot Terisi")
        render_warehouse_grid("Surabaya")
    with map_jkt:
        st.markdown(f"**Status Jakarta:** {jkt_stats['total_skus']} SKU | {jkt_stats['total_boxes']} Dus | {jkt_stats['occupied_bins']}/48 Slot Terisi")
        render_warehouse_grid("Jakarta")

    st.divider()
    st.markdown("#### 🔄 Cycle Count Audit Mode (Spot-Check Baris)")
    ac0, ac1 = st.columns(2)
    audit_wh = ac0.selectbox("Pilih Gudang Audit", WAREHOUSES)
    audit_row = ac1.selectbox("Pilih Baris untuk Audit Fisik", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}")
    
    audit_items = pd.read_sql_query(
        "SELECT jurnal_sku, product_name, bin_location, box_qty FROM bin_map WHERE warehouse = ? AND row_num = ? ORDER BY floor_num",
        conn, params=[audit_wh, audit_row]
    )
    
    if audit_items.empty:
        st.info(f"Gudang **{audit_wh}** — Baris {audit_row:02d} kosong.")
    else:
        st.write(f"Ditemukan **{len(audit_items)}** SKU di **{audit_wh}** (Baris {audit_row:02d}). Masukkan hasil hitung fisik:")
        discrepancies = []
        for _, r in audit_items.iterrows():
            col_a, col_b, col_c = st.columns([2, 1, 1])
            col_a.text(f"{r['bin_location']} | {r['jurnal_sku']} — {r['product_name']}")
            system_qty = int(r['box_qty'])
            physical_qty = col_b.number_input("Hitung Fisik (Dus)", min_value=0, value=system_qty, key=f"aud_{audit_wh}_{r['jurnal_sku']}")
            if physical_qty != system_qty:
                col_c.error(f"Selisih: {physical_qty - system_qty:+d} dus")
                discrepancies.append((r['jurnal_sku'], system_qty, physical_qty, r['bin_location']))
            else:
                col_c.success("Match ✓")

        if discrepancies and st.button(f"📝 Koreksi & Sinkronkan Stok Opname ({audit_wh})"):
            for sku_id, sys_q, phys_q, b_loc in discrepancies:
                if phys_q == 0:
                    conn.execute("DELETE FROM bin_map WHERE warehouse = ? AND jurnal_sku = ?", (audit_wh, sku_id))
                else:
                    conn.execute("UPDATE bin_map SET box_qty = ? WHERE warehouse = ? AND jurnal_sku = ?", (phys_q, audit_wh, sku_id))
            conn.commit()
            log_activity("CYCLE_COUNT_AUDIT", f"[{audit_wh}] Audit Baris {audit_row:02d} disinkronkan. Koreksi {len(discrepancies)} item.")
            st.success(f"Stok opname Gudang {audit_wh} berhasil disinkronkan!")
            st.rerun()

# --- TAB 3: ASISTEN AI ---
with tab3:
    st.subheader("🤖 Asisten Gudang & Prediksi Penempatan Rak (Surabaya & Jakarta)")
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    
    chat_input = st.chat_input("Tanya lokasi barang di Surabaya/Jakarta, stok, atau rekomendasi rak...")
    if chat_input:
        st.session_state.chat_history.append({"role": "user", "content": chat_input})
        rel_inv = search_inventory_for_ai(chat_input)
        mem_block = get_memory_context()
        sys_prompt = f"Asisten Kepala Gudang untuk cabang Surabaya dan Jakarta (masing-masing 8 Baris x 6 Lantai). Zonasi: Fast-Moving (L1-L2), Medium (L3-L4), Slow/Bulky (L5-L6).\n{mem_block}\n\n{rel_inv}"
        try:
            client, active_model, _ = get_ai_client_and_model()
            res = client.chat.completions.create(model=active_model, temperature=0.2, max_tokens=300, messages=[{"role": "system", "content": sys_prompt}] + st.session_state.chat_history[-6:])
            reply = res.choices[0].message.content
        except Exception as e:
            reply = f"Error AI: {e}"
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# --- TAB 4: PICKING & INVOICE ---
with tab4:
    st.subheader("🛒 Picking & Cetak Surat Jalan")
    pick_wh = st.selectbox("Pilih Cabang Gudang Asal", WAREHOUSES, key="pick_wh_select")
    pick_df = pd.read_sql_query(
        "SELECT jurnal_sku, product_name, bin_location, box_qty, pcs_per_box, expiry_date FROM bin_map WHERE warehouse = ? ORDER BY row_num, floor_num LIMIT 150",
        conn, params=[pick_wh]
    )
    if pick_df.empty:
        st.info(f"Belum ada stok di rak Gudang {pick_wh}.")
    else:
        pick_df["label"] = pick_df.apply(lambda r: f"[{r['bin_location']}] {r['jurnal_sku']} — {r['product_name']} (Stok: {format_qty_summary(r['box_qty'], r['pcs_per_box'])})", axis=1)
        sel = st.multiselect(f"Pilih Item untuk Dipick ({pick_wh}):", pick_df["label"].tolist())
        if sel:
            sel_rows = pick_df[pick_df["label"].isin(sel)]
            picked_items = []
            wh_prefix = "SBY" if pick_wh == "Surabaya" else "JKT"
            inv_no = st.text_input("No. Surat Jalan", value=f"SJ-{wh_prefix}-{datetime.now().strftime('%Y%m%d-%H%M')}")
            picker = st.text_input("Nama Picker", value=st.session_state.current_user)
            
            for _, r in sel_rows.iterrows():
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.text(f"{r['bin_location']} | {r['jurnal_sku']} ({r['product_name']})")
                q_ambil = c2.number_input("Ambil Dus", min_value=1, value=1, max_value=int(r['box_qty']), key=f"pk_{pick_wh}_{r['jurnal_sku']}")
                price = c3.number_input("Harga / Dus", min_value=0, value=0, step=1000, key=f"pr_{pick_wh}_{r['jurnal_sku']}")
                picked_items.append({"sku": r['jurnal_sku'], "name": r['product_name'], "bin": r['bin_location'], "ambil": q_ambil, "avail": int(r['box_qty']), "price": price})

            if st.button(f"🧾 Buat Surat Jalan & Potong Stok ({pick_wh})"):
                for it in picked_items:
                    rem = it["avail"] - it["ambil"]
                    if rem <= 0:
                        conn.execute("DELETE FROM bin_map WHERE warehouse = ? AND jurnal_sku = ?", (pick_wh