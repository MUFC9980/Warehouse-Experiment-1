import os
import re
import sqlite3
from datetime import datetime, date
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Warehouse Efficiency System - Jurnal AI", page_icon="📦", layout="wide")

# --- 0. SUPERVISOR PASSCODE & 8 USER ACCOUNTS CONFIG ---
def get_supervisor_pin() -> str:
    try:
        if "SUPERVISOR_PIN" in st.secrets:
            return str(st.secrets["SUPERVISOR_PIN"]).strip()
    except Exception:
        pass
    return os.getenv("SUPERVISOR_PIN", "8899").strip()

SUPERVISOR_PIN = get_supervisor_pin()

# 8 Accounts (Username: {"pin": "...", "display": "...", "role": "..."})
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
        st.markdown("Silakan masukkan **Username** dan **PIN** untuk masuk ke sistem gudang.")
        
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

# --- 2. DATABASE SETUP, WAL & MIGRATIONS ---
conn = sqlite3.connect("jurnal_bins.db", timeout=15.0, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA temp_store=MEMORY;")

conn.execute("""
    CREATE TABLE IF NOT EXISTS bin_map (
        jurnal_sku TEXT PRIMARY KEY,
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
        picker_name TEXT
    )
""")

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

conn.execute("CREATE INDEX IF NOT EXISTS idx_bin_location ON bin_map(row_num, floor_num);")
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

def get_warehouse_stats() -> dict:
    row = conn.execute("""
        SELECT
            COUNT(*),
            COALESCE(SUM(box_qty), 0),
            COALESCE(SUM(CASE WHEN pcs_per_box IS NOT NULL AND pcs_per_box > 0 THEN box_qty * pcs_per_box ELSE 0 END), 0),
            COUNT(DISTINCT bin_location)
        FROM bin_map
    """).fetchone()
    return {
        "total_skus": int(row[0]),
        "total_boxes": int(row[1]),
        "total_pcs": int(row[2]),
        "occupied_bins": int(row[3]),
        "empty_bins": 48 - int(row[3])
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

def search_inventory_for_ai(query_text: str, limit: int = 25) -> str:
    stats = get_warehouse_stats()
    summary_header = (
        f"RINGKASAN TOTAL GUDANG: {stats['total_skus']:,} SKU | "
        f"{stats['total_boxes']:,} Dus | {stats['total_pcs']:,} Pcs | "
        f"{stats['occupied_bins']}/48 Slot Terisi ({stats['empty_bins']} Kosong)."
    ).replace(",", ".")

    if stats["total_skus"] == 0:
        return f"{summary_header}\nSemua 48 slot rak masih kosong."

    slot_summary_df = pd.read_sql_query("""
        SELECT bin_location, COUNT(*) AS sku_count, COALESCE(SUM(box_qty), 0) AS total_dus
        FROM bin_map
        GROUP BY bin_location
        ORDER BY row_num, floor_num
    """, conn)
    slot_lines = ", ".join([f"{r['bin_location']}({r['sku_count']} SKU/{int(r['total_dus'])} dus)" for _, r in slot_summary_df.iterrows()])

    stopwords = {
        "dimana", "mana", "lokasi", "rak", "barang", "stok", "ada", "berapa", "yang",
        "untuk", "dari", "dan", "atau", "tolong", "carikan", "cek", "rekomendasi",
        "prediksi", "slot", "kosong", "masuk", "baru", "jumlah", "dus", "pcs", "ini", "itu"
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
                "(LOWER(jurnal_sku) LIKE ? OR LOWER(product_name) LIKE ? OR LOWER(category) LIKE ? OR LOWER(bin_location) LIKE ? OR LOWER(notes) LIKE ?)"
            )
            params.extend([like_pat, like_pat, like_pat, like_pat, like_pat])
        where_clause = " OR ".join(conditions)
        matched_df = pd.read_sql_query(
            f"""SELECT bin_location AS "Rak", jurnal_sku AS "SKU", product_name AS "Produk",
                       category AS "Kategori", box_qty AS "Dus", expiry_date AS "Expired", turnover_speed AS "Perputaran"
                FROM bin_map WHERE {where_clause} ORDER BY row_num, floor_num LIMIT {limit}""",
            conn, params=params
        )
    else:
        matched_df = pd.read_sql_query(
            f"""SELECT bin_location AS "Rak", jurnal_sku AS "SKU", product_name AS "Produk",
                       category AS "Kategori", box_qty AS "Dus", expiry_date AS "Expired", turnover_speed AS "Perputaran"
                FROM bin_map ORDER BY row_num, floor_num LIMIT {limit}""",
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
    sku: str, name: str, cat: str, row_num: int, floor_num: int, speed: str, notes: str,
    box_qty: int, pcs_per_box: int | None, block_reason: str, override_reason: str
) -> tuple[bool, str]:
    target_bin = f"R{row_num:02d}-L{floor_num}"
    qty_desc = format_qty_summary(box_qty, pcs_per_box)
    slot_counts_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS cnt, COALESCE(SUM(box_qty), 0) AS boxes FROM bin_map GROUP BY row_num, floor_num",
        conn
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
            valid_info = f"Slot kosong valid: {', '.join(valid_empty_bins[:10])}" if valid_empty_bins else "Tidak ada slot kosong."
            audit_prompt = f"""Auditor Kepala Gudang. Nilai apakah alasan 15% override logis.
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
    sku: str, name: str, cat: str, speed: str, notes: str, box_qty: int, pcs_per_box: int | None
) -> tuple[int | None, int | None, str]:
    slot_counts_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS cnt, COALESCE(SUM(box_qty), 0) AS boxes FROM bin_map GROUP BY row_num, floor_num",
        conn
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
        return None, None, "Tidak ada slot yang memenuhi zonasi!"
    valid_slots.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    top_candidates = valid_slots[:12]
    candidate_labels = [f"{s[4]} ({s[0]} SKU, {s[1]} dus)" for s in top_candidates]
    candidate_codes = [s[4] for s in top_candidates]

    try:
        client, active_model, status = get_ai_client_and_model()
        if not status.startswith("Offline"):
            prompt = f"Pilih 1 kode rak terbaik untuk {sku} {name}. Kandidat: {', '.join(candidate_labels)}. Balas HANYA dengan kode rak (misal R01-L1)."
            res = client.chat.completions.create(model=active_model, temperature=0.1, max_tokens=50, messages=[{"role": "user", "content": prompt}])
            match = re.search(r"R0([1-8])-L([1-6])", res.choices[0].message.content.upper())
            if match:
                chosen = f"R0{match.group(1)}-L{match.group(2)}"
                if chosen in candidate_codes:
                    return int(match.group(1)), int(match.group(2)), f"AI memilih {chosen}"
    except Exception:
        pass

    _, _, best_r, best_f, best_code = top_candidates[0]
    return best_r, best_f, f"Otomatis ke slot paling longgar ({best_code})."

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

stats_now = get_warehouse_stats()
st.caption(f"Zonasi 85% • 15% Fleksibel • 10.000+ SKU Ready | Stok: **{stats_now['total_skus']:,} SKU** | **{stats_now['occupied_bins']}/48 Slot Terisi**")

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

# --- TAB 1: INPUT & PEMETAAN RAK ---
with tab1:
    st.subheader("📥 Input Barang Masuk")
    with st.form("add_sku_form", clear_on_submit=False):
        c1, c2, c3 = st.columns(3)
        sku = c1.text_input("Kode SKU Jurnal", placeholder="JRN-1042")
        name = c2.text_input("Nama Produk", placeholder="Raket Yonex")
        cat = c3.text_input("Kategori", placeholder="Olahraga")

        q1, q2, q3, q4 = st.columns([1, 1, 1, 1.5])
        box_qty = q1.number_input("Jumlah Dus", min_value=1, value=1, step=1)
        pcs_per_box = q2.number_input("Isi/Dus (Pcs)", min_value=0, value=0, step=1)
        min_thresh = q3.number_input("Min. Threshold Dus (Alert)", min_value=1, value=3, step=1)
        speed = q4.selectbox("Perputaran", ["Fast-Moving (Laku Cepat)", "Medium (Normal)", "Slow-Moving (Lambat)", "Berat / Besar (Bulky)"])

        auto_mode = st.checkbox("🤖 Auto-Assign Rak oleh AI", value=True)

        r_c1, r_c2, r_c3, r_c4 = st.columns([1, 1, 1.5, 2])
        row_num = r_c1.selectbox("Baris", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}")
        floor_num = r_c2.selectbox("Lantai", list(range(1, 7)), format_func=lambda x: f"Lantai {x}")
        expiry_val = r_c3.date_input("Expired Date (Opsional)", value=None)
        notes = r_c4.text_input("Catatan Fisik", placeholder="Dus besar")

        st.divider()
        flex_expander = st.expander("🔐 15% Fleksibel: Override Supervisor + Audit AI")
        with flex_expander:
            allow_override = st.checkbox("Izinkan Penempatan di Luar Aturan Zonasi")
            ov1, ov2 = st.columns([2, 1])
            override_reason = ov1.text_input("Alasan Pengecualian:", placeholder="Kardus terlalu besar untuk lantai bawah")
            override_pin = ov2.text_input("Passcode Supervisor:", type="password", placeholder="PIN")

        if st.form_submit_button("Simpan Lokasi Rak"):
            st.session_state.pending_force_override = None
            if not sku.strip():
                st.warning("SKU wajib diisi.")
            else:
                pcs_val = int(pcs_per_box) if int(pcs_per_box) > 0 else None
                exp_str = expiry_val.strftime("%Y-%m-%d") if expiry_val else None

                if auto_mode:
                    with st.spinner("AI memilih slot terbaik..."):
                        ar, af, msg = auto_assign_bin(sku.strip(), name.strip(), cat.strip(), speed, notes.strip(), int(box_qty), pcs_val)
                    if ar is None:
                        st.error(f"Gagal: {msg}")
                    else:
                        b_code = f"R{ar:02d}-L{af}"
                        conn.execute("""INSERT OR REPLACE INTO bin_map 
                            (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sku.strip(), name.strip(), cat.strip(), ar, af, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                        conn.commit()
                        log_activity("AUTO_ASSIGN", f"{sku.strip()} -> {b_code}")
                        st.success(f"Berhasil Auto-Assign `{sku.strip()}` → **{b_code}** ({msg})")
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
                            r_ok, ai_fb = evaluate_override_reason_with_ai(sku.strip(), name.strip(), cat.strip(), row_num, floor_num, speed, notes.strip(), int(box_qty), pcs_val, blk_msg, override_reason.strip())
                        if r_ok:
                            conn.execute("""INSERT OR REPLACE INTO bin_map 
                                (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (sku.strip(), name.strip(), cat.strip(), row_num, floor_num, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                            conn.commit()
                            log_activity("OVERRIDE_APPROVED", f"{sku.strip()} ke {b_code}")
                            st.success(f"Override Disetujui AI: `{sku.strip()}` → **{b_code}**\n\n**Audit AI:** {ai_fb}")
                        else:
                            st.session_state.pending_force_override = {
                                "sku": sku.strip(), "name": name.strip(), "cat": cat.strip(),
                                "row_num": row_num, "floor_num": floor_num, "bin_code": b_code,
                                "speed": speed, "notes": notes.strip(), "box_qty": int(box_qty),
                                "pcs_val": pcs_val, "min_thresh": int(min_thresh), "exp_str": exp_str,
                                "override_reason": override_reason.strip(), "ai_feedback": ai_fb
                            }
                    else:
                        conn.execute("""INSERT OR REPLACE INTO bin_map 
                            (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sku.strip(), name.strip(), cat.strip(), row_num, floor_num, b_code, speed, notes.strip(), int(box_qty), pcs_val, int(min_thresh), exp_str))
                        conn.commit()
                        log_activity("SIMPAN_RAK", f"{sku.strip()} -> {b_code}")
                        st.success(f"Berhasil menyimpan: `{sku.strip()}` → **{b_code}**")

    if st.session_state.pending_force_override:
        pfo = st.session_state.pending_force_override
        st.error(f"⚠️ **Audit AI: Kurang Masuk Akal**\n\n{pfo['ai_feedback']}")
        fc1, fc2 = st.columns(2)
        if fc1.button("🔓 Tetap Paksa Simpan (Supervisor)", type="primary"):
            conn.execute("""INSERT OR REPLACE INTO bin_map 
                (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (pfo["sku"], pfo["name"], pfo["cat"], pfo["row_num"], pfo["floor_num"], pfo["bin_code"], pfo["speed"], pfo["notes"], pfo["box_qty"], pfo["pcs_val"], pfo["min_thresh"], pfo["exp_str"]))
            conn.commit()
            log_activity("FORCE_OVERRIDE", f"Force save {pfo['sku']} to {pfo['bin_code']}")
            st.session_state.pending_force_override = None
            st.success("Tersimpan dengan Force Override.")
            st.rerun()
        if fc2.button("❌ Batalkan"):
            st.session_state.pending_force_override = None
            st.rerun()

# --- TAB 2: PETA RAK & CYCLE COUNTING AUDIT MODE ---
with tab2:
    st.subheader("Denah Rak & Siklus Hitung Stok (Cycle Counting Audit)")
    agg_df = pd.read_sql_query("SELECT row_num, floor_num, COUNT(*) AS sku_cnt, COALESCE(SUM(box_qty), 0) AS total_dus FROM bin_map GROUP BY row_num, floor_num", conn)
    slot_lookup = {(int(r["row_num"]), int(r["floor_num"])): (int(r["sku_cnt"]), int(r["total_dus"])) for _, r in agg_df.iterrows()}
    
    grid_data = {}
    for r in range(1, 9):
        col_lbl = f"Baris {r:02d}"
        grid_data[col_lbl] = []
        for f in range(6, 0, -1):
            s_cnt, d_cnt = slot_lookup.get((r, f), (0, 0))
            grid_data[col_lbl].append(f"[Kosong] R{r:02d}-L{f}" if s_cnt == 0 else f"📦 {s_cnt} SKU ({d_cnt} dus)")
    st.dataframe(pd.DataFrame(grid_data, index=[f"Lantai {f}" for f in range(6, 0, -1)]), width="stretch", height=240)

    st.divider()
    st.markdown("#### 🔄 Cycle Count Audit Mode (Spot-Check Baris)")
    ac1, ac2 = st.columns(2)
    audit_row = ac1.selectbox("Pilih Baris untuk Audit Fisik", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}")
    audit_items = pd.read_sql_query("SELECT jurnal_sku, product_name, bin_location, box_qty FROM bin_map WHERE row_num = ? ORDER BY floor_num", conn, params=[audit_row])
    
    if audit_items.empty:
        st.info(f"Baris {audit_row:02d} kosong.")
    else:
        st.write(f"Ditemukan **{len(audit_items)}** SKU di Baris {audit_row:02d}. Masukkan hasil hitung fisik:")
        discrepancies = []
        for _, r in audit_items.iterrows():
            col_a, col_b, col_c = st.columns([2, 1, 1])
            col_a.text(f"{r['bin_location']} | {r['jurnal_sku']} — {r['product_name']}")
            system_qty = int(r['box_qty'])
            physical_qty = col_b.number_input("Hitung Fisik (Dus)", min_value=0, value=system_qty, key=f"aud_{r['jurnal_sku']}")
            if physical_qty != system_qty:
                col_c.error(f"Selisih: {physical_qty - system_qty:+d} dus")
                discrepancies.append((r['jurnal_sku'], system_qty, physical_qty, r['bin_location']))
            else:
                col_c.success("Match ✓")

        if discrepancies and st.button("📝 Koreksi & Sinkronkan Stok Opname"):
            for sku_id, sys_q, phys_q, b_loc in discrepancies:
                if phys_q == 0:
                    conn.execute("DELETE FROM bin_map WHERE jurnal_sku = ?", (sku_id,))
                else:
                    conn.execute("UPDATE bin_map SET box_qty = ? WHERE jurnal_sku = ?", (phys_q, sku_id))
            conn.commit()
            log_activity("CYCLE_COUNT_AUDIT", f"Audit Baris {audit_row:02d} disinkronkan. Koreksi {len(discrepancies)} item.")
            st.success("Stok opname berhasil disinkronkan dengan database!")
            st.rerun()

# --- TAB 3: ASISTEN AI ---
with tab3:
    st.subheader("🤖 Asisten Gudang & Prediksi Penempatan Rak")
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    
    chat_input = st.chat_input("Tanya lokasi barang, stok, atau rekomendasi rak...")
    if chat_input:
        st.session_state.chat_history.append({"role": "user", "content": chat_input})
        rel_inv = search_inventory_for_ai(chat_input)
        mem_block = get_memory_context()
        sys_prompt = f"Asisten Kepala Gudang (8 Baris x 6 Lantai). Zonasi: Fast-Moving (L1-L2), Medium (L3-L4), Slow/Bulky (L5-L6).\n{mem_block}\n\n{rel_inv}"
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
    pick_df = pd.read_sql_query("SELECT jurnal_sku, product_name, bin_location, box_qty, pcs_per_box, expiry_date FROM bin_map ORDER BY row_num, floor_num LIMIT 100", conn)
    if pick_df.empty:
        st.info("Belum ada stok di rak.")
    else:
        pick_df["label"] = pick_df.apply(lambda r: f"[{r['bin_location']}] {r['jurnal_sku']} — {r['product_name']} (Stok: {format_qty_summary(r['box_qty'], r['pcs_per_box'])})", axis=1)
        sel = st.multiselect("Pilih Item untuk Dipick:", pick_df["label"].tolist())
        if sel:
            sel_rows = pick_df[pick_df["label"].isin(sel)]
            picked_items = []
            inv_no = st.text_input("No. Surat Jalan", value=f"SJ-{datetime.now().strftime('%Y%m%d-%H%M')}")
            picker = st.text_input("Nama Picker", value=st.session_state.current_user)
            
            for _, r in sel_rows.iterrows():
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.text(f"{r['bin_location']} | {r['jurnal_sku']} ({r['product_name']})")
                q_ambil = c2.number_input("Ambil Dus", min_value=1, value=1, max_value=int(r['box_qty']), key=f"pk_{r['jurnal_sku']}")
                price = c3.number_input("Harga / Dus", min_value=0, value=0, step=1000, key=f"pr_{r['jurnal_sku']}")
                picked_items.append({"sku": r['jurnal_sku'], "name": r['product_name'], "bin": r['bin_location'], "ambil": q_ambil, "avail": int(r['box_qty']), "price": price})

            if st.button("🧾 Buat Surat Jalan & Potong Stok"):
                for it in picked_items:
                    rem = it["avail"] - it["ambil"]
                    if rem <= 0:
                        conn.execute("DELETE FROM bin_map WHERE jurnal_sku = ?", (it["sku"],))
                    else:
                        conn.execute("UPDATE bin_map SET box_qty = ? WHERE jurnal_sku = ?", (rem, it["sku"]))
                conn.commit()
                log_activity("PICKING", f"Picking {inv_no} oleh {picker} untuk {len(picked_items)} SKU", actor=picker)
                st.success(f"Surat Jalan **{inv_no}** berhasil diterbitkan dan stok dipotong otomatis!")

# --- TAB 5: REORDER ALERTS ---
with tab5:
    st.subheader("⚠️ Low Stock & Reorder Alerts")
    low_stock_df = pd.read_sql_query("SELECT bin_location AS 'Rak', jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus Tersisa', min_box_threshold AS 'Batas Minimum' FROM bin_map WHERE box_qty <= min_box_threshold ORDER BY box_qty ASC", conn)
    if low_stock_df.empty:
        st.success("✨ Semua stok aman! Tidak ada SKU yang berada di bawah batas minimum.")
    else:
        st.warning(f"Ditemukan **{len(low_stock_df)} SKU** yang menipis dan perlu segera direorder:")
        st.dataframe(low_stock_df, width="stretch")
        csv_reorder = low_stock_df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download Daftar Reorder (CSV)", data=csv_reorder, file_name="reorder_list.csv", mime="text/csv")

# --- TAB 6: FIFO & EXPIRY TRACKING ---
with tab6:
    st.subheader("⏳ FIFO & Expiry Date Management")
    expiry_df = pd.read_sql_query("SELECT bin_location AS 'Rak', jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus', expiry_date AS 'Expired Date' FROM bin_map WHERE expiry_date IS NOT NULL ORDER BY expiry_date ASC", conn)
    if expiry_df.empty:
        st.info("Belum ada data kadaluarsa (expiry date) yang dimasukkan.")
    else:
        st.markdown("Daftar produk diurutkan berdasarkan kedaluwarsa terdekat (**FIFO Priority**):")
        st.dataframe(expiry_df, width="stretch")

# --- TAB 7: IMPORT EXCEL / CSV ---
with tab7:
    st.subheader("Import Massal WMS")
    up_file = st.file_uploader("Upload CSV/Excel", type=["csv", "xlsx"])
    if up_file:
        df_up = pd.read_csv(up_file) if up_file.name.endswith(".csv") else pd.read_excel(up_file)
        st.dataframe(df_up.head(5))
        if st.button("Proses Import Batch"):
            records = []
            for _, row in df_up.iterrows():
                records.append((str(row["jurnal_sku"]), str(row["product_name"]), str(row.get("category", "")), int(row["row_num"]), int(row["floor_num"]), f"R{int(row['row_num']):02d}-L{int(row['floor_num'])}", str(row.get("turnover_speed", "Medium (Normal)")), str(row.get("notes", "")), int(row.get("box_qty", 1)), int(row.get("pcs_per_box", 0)) if pd.notna(row.get("pcs_per_box")) else None, 3, None))
            conn.executemany("INSERT OR REPLACE INTO bin_map (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", records)
            conn.commit()
            log_activity("IMPORT_MASSAL", f"Import {len(records)} SKU")
            st.success(f"Berhasil mengimpor {len(records)} SKU!")

# --- TAB 8: MEMORI & LEADERBOARD (PASSCODE PROTECTED) ---
with tab8:
    st.subheader("🧠 Memori Aturan & Staff Activity Leaderboard")
    if not st.session_state.tab6_unlocked:
        with st.form("unlock_t8"):
            p_in = st.text_input("Passcode Supervisor:", type="password")
            if st.form_submit_button("Buka Kunci"):
                if p_in.strip() == SUPERVISOR_PIN:
                    st.session_state.tab6_unlocked = True
                    st.rerun()
                else:
                    st.error("PIN Salah!")
    else:
        if st.button("🔒 Kunci Kembali"):
            st.session_state.tab6_unlocked = False
            st.rerun()

        st.markdown("#### 🏆 Leaderboard Aktivitas Staf Gudang")
        leader_df = pd.read_sql_query("SELECT actor AS 'Staf / Aktor', COUNT(*) AS 'Total Aktivitas Terbuka' FROM activity_log GROUP BY actor ORDER BY COUNT(*) DESC", conn)
        st.dataframe(leader_df, width="stretch")

        st.divider()
        st.markdown("#### 📜 Riwayat Aktivitas Lengkap")
        log_df = pd.read_sql_query("SELECT timestamp AS 'Waktu', actor AS 'Aktor', action AS 'Aksi', details AS 'Detail' FROM activity_log ORDER BY id DESC LIMIT 50", conn)
        st.dataframe(log_df, width="stretch")