import os
import re
import sqlite3
from datetime import datetime, date, timedelta
import pandas as pd
import streamlit as st
from openai import OpenAI

# --- PAGE CONFIGURATION ---
st.set_page_config(page_title="Warehouse Efficiency System - Surabaya, Jakarta & Toko Aneka", page_icon="📦", layout="wide")

# Main 8x6 Warehouses vs. Lightweight Retail Store Destinations
WAREHOUSES = ["Surabaya", "Jakarta"]
TRANSFER_LOCATIONS = ["Surabaya", "Jakarta", "Toko Aneka"]

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
        st.markdown("Silakan masukkan **Username** dan **PIN** untuk masuk ke sistem gudang (Surabaya, Jakarta & Toko Aneka).")
        
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

# Sales / Outbound Velocity Table (Tracks sales per warehouse so AI learns demand)
conn.execute("""
    CREATE TABLE IF NOT EXISTS outbound_sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        warehouse TEXT,
        jurnal_sku TEXT,
        product_name TEXT,
        boxes_sold INTEGER,
        sale_date TEXT,
        invoice_no TEXT,
        actor TEXT
    )
""")
conn.execute("CREATE INDEX IF NOT EXISTS idx_sales_wh_sku ON outbound_sales(warehouse, jurnal_sku, sale_date);")

# Inter-Location Stock Transfer Tracker Table (Surabaya <-> Jakarta <-> Toko Aneka)
conn.execute("""
    CREATE TABLE IF NOT EXISTS stock_transfers (
        transfer_id TEXT PRIMARY KEY,
        from_warehouse TEXT,
        to_warehouse TEXT,
        jurnal_sku TEXT,
        product_name TEXT,
        category TEXT,
        turnover_speed TEXT,
        box_qty INTEGER,
        pcs_per_box INTEGER,
        status TEXT,
        created_at TEXT,
        received_at TEXT,
        sender TEXT,
        receiver TEXT,
        courier_notes TEXT
    )
""")

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
# Safe migration for legacy databases missing the 'actor' column in activity_log
act_cols = [row[1] for row in conn.execute("PRAGMA table_info(activity_log)").fetchall()]
if "actor" not in act_cols:
    conn.execute("ALTER TABLE activity_log ADD COLUMN actor TEXT DEFAULT 'System'")

conn.commit()

def log_activity(action: str, details: str, actor: str | None = None):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    active_actor = actor if actor else st.session_state.get("current_user", "System")
    conn.execute(
        "INSERT INTO activity_log (timestamp, action, details, actor) VALUES (?, ?, ?, ?)",
        (now, action, details, active_actor)
    )
    conn.commit()

def get_loc_code(loc_name: str) -> str:
    codes = {
        "Surabaya": "SBY",
        "Jakarta": "JKT",
        "Toko Aneka": "ANK",
    }
    return codes.get(loc_name, loc_name[:3].upper())

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

# --- DEMAND & TRANSFER INTELLIGENCE ENGINE ---
def compute_jakarta_transfer_recommendations() -> pd.DataFrame:
    """
    Learns Jakarta's 30-day sales velocity from outbound_sales and compares against
    Jakarta's current stock + Surabaya's available surplus + active in-transit transfers.
    """
    cutoff_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    
    sales_df = pd.read_sql_query("""
        SELECT
            jurnal_sku,
            MAX(product_name) AS product_name,
            COALESCE(SUM(boxes_sold), 0) AS sold_30d,
            COUNT(DISTINCT substr(sale_date, 1, 10)) AS active_days
        FROM outbound_sales
        WHERE warehouse = 'Jakarta' AND sale_date >= ?
        GROUP BY jurnal_sku
    """, conn, params=[cutoff_date])

    jkt_df = pd.read_sql_query("""
        SELECT jurnal_sku, product_name, box_qty AS jkt_boxes, min_box_threshold
        FROM bin_map WHERE warehouse = 'Jakarta'
    """, conn)
    sby_df = pd.read_sql_query("""
        SELECT jurnal_sku, product_name, box_qty AS sby_boxes, min_box_threshold AS sby_min
        FROM bin_map WHERE warehouse = 'Surabaya'
    """, conn)

    transit_df = pd.read_sql_query("""
        SELECT jurnal_sku, COALESCE(SUM(box_qty), 0) AS in_transit_boxes
        FROM stock_transfers
        WHERE to_warehouse = 'Jakarta' AND status = 'In-Transit'
        GROUP BY jurnal_sku
    """, conn)

    all_skus = set(jkt_df["jurnal_sku"]).union(set(sales_df["jurnal_sku"]))
    if not all_skus:
        return pd.DataFrame()

    jkt_map = jkt_df.set_index("jurnal_sku").to_dict("index")
    sby_map = sby_df.set_index("jurnal_sku").to_dict("index")
    sales_map = sales_df.set_index("jurnal_sku").to_dict("index")
    transit_map = transit_df.set_index("jurnal_sku")["in_transit_boxes"].to_dict() if not transit_df.empty else {}

    rows = []
    for sku in sorted(all_skus):
        j_info = jkt_map.get(sku, {})
        s_info = sby_map.get(sku, {})
        sl_info = sales_map.get(sku, {})

        prod_name = j_info.get("product_name") or sl_info.get("product_name") or s_info.get("product_name") or sku
        jkt_boxes = int(j_info.get("jkt_boxes", 0))
        min_thresh = int(j_info.get("min_box_threshold", 3))
        in_transit = int(transit_map.get(sku, 0))
        effective_jkt = jkt_boxes + in_transit

        sold_30d = int(sl_info.get("sold_30d", 0))
        daily_rate = round(sold_30d / 30.0, 2) if sold_30d > 0 else 0.0
        days_left = round(effective_jkt / daily_rate, 1) if daily_rate > 0 else (999.0 if effective_jkt > min_thresh else 0.0)

        sby_boxes = int(s_info.get("sby_boxes", 0))
        sby_min = int(s_info.get("sby_min", 3))
        sby_surplus = max(0, sby_boxes - sby_min)

        target_jkt_stock = max(min_thresh * 2, int(round(daily_rate * 21)))
        needed_boxes = max(0, target_jkt_stock - effective_jkt)

        needs_transfer = (effective_jkt <= min_thresh) or (daily_rate > 0 and days_left < 14.0)
        if needs_transfer and needed_boxes > 0:
            suggested_transfer = min(needed_boxes, sby_surplus)
            if suggested_transfer > 0:
                action_status = f"🚚 Transfer {suggested_transfer} dus dari Surabaya"
            elif sby_boxes > 0:
                action_status = "⚠️ Stok Surabaya mepet (PO Supplier)"
            else:
                action_status = "🛒 Kosong di Surabaya (Wajib PO Supplier)"

            rows.append({
                "SKU": sku,
                "Produk": prod_name,
                "Stok JKT": jkt_boxes,
                "In-Transit": in_transit,
                "Terjual JKT (30h)": sold_30d,
                "Sales/Hari": daily_rate,
                "Estimasi Habis": f"{days_left} hari" if daily_rate > 0 else "Di bawah min.",
                "Surplus SBY": sby_surplus,
                "Saran Transfer": suggested_transfer,
                "Rekomendasi": action_status
            })

    return pd.DataFrame(rows)

def get_memory_context() -> str:
    mem_df = pd.read_sql_query(
        "SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC LIMIT 30",
        conn
    )
    log_df = pd.read_sql_query(
        "SELECT timestamp, actor, action, details FROM activity_log ORDER BY id DESC LIMIT 10",
        conn
    )
    parts = []
    if not mem_df.empty:
        rules = "\n".join([f"- [{r['category']}] {r['memory_text']}" for _, r in mem_df.iterrows()])
        parts.append(f"ATURAN & MEMORI TETAP GUDANG:\n{rules}")
    if not log_df.empty:
        logs = "\n".join([f"- {r['timestamp']} | {r['actor']} | {r['action']}: {r['details']}" for _, r in log_df.iterrows()])
        parts.append(f"RIWAYAT AKTIVITAS TERAKHIR:\n{logs}")
    return "\n\n".join(parts) if parts else "Belum ada memori khusus tambahan."

def search_inventory_for_ai(query_text: str, limit: int = 30) -> str:
    sby = get_warehouse_stats("Surabaya")
    jkt = get_warehouse_stats("Jakarta")
    in_transit_cnt = conn.execute("SELECT COALESCE(SUM(box_qty), 0) FROM stock_transfers WHERE status = 'In-Transit'").fetchone()[0]
    aneka_sent_cnt = conn.execute("SELECT COALESCE(SUM(box_qty), 0) FROM stock_transfers WHERE to_warehouse = 'Toko Aneka'").fetchone()[0]
    
    summary_header = (
        f"RINGKASAN GUDANG & TOKO:\n"
        f"- SURABAYA: {sby['total_skus']:,} SKU | {sby['total_boxes']:,} Dus | {sby['occupied_bins']}/48 Slot Terisi.\n"
        f"- JAKARTA: {jkt['total_skus']:,} SKU | {jkt['total_boxes']:,} Dus | {jkt['occupied_bins']}/48 Slot Terisi.\n"
        f"- TOKO ANEKA (Store Transfer Log): Total {int(aneka_sent_cnt):,} Dus tercatat pernah ditransfer ke Toko Aneka.\n"
        f"- IN-TRANSIT (SBY / JKT / Toko Aneka): {int(in_transit_cnt):,} Dus sedang dalam perjalanan."
    ).replace(",", ".")

    rec_df = compute_jakarta_transfer_recommendations()
    rec_text = ""
    if not rec_df.empty:
        rec_text = "\n\nANALISIS PENJUALAN JAKARTA & REKOMENDASI TRANSFER:\n" + rec_df.to_csv(index=False)

    if (sby["total_skus"] + jkt["total_skus"]) == 0:
        return f"{summary_header}\nSemua slot di Surabaya dan Jakarta masih kosong.{rec_text}"

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
        "prediksi", "slot", "kosong", "masuk", "baru", "jumlah", "dus", "pcs", "ini", "itu", "gudang", "toko"
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
    return f"{summary_header}\nKETERISIAN: {slot_lines}{rec_text}\n\nSAMPEL SKU:\n{matched_csv}"

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
in_transit_total = conn.execute("SELECT COALESCE(SUM(box_qty), 0) FROM stock_transfers WHERE status = 'In-Transit'").fetchone()[0]

st.caption(
    f"Zonasi 85% • 15% Fleksibel | "
    f"**Surabaya:** {sby_stats['total_skus']:,} SKU ({sby_stats['occupied_bins']}/48 Slot) • "
    f"**Jakarta:** {jkt_stats['total_skus']:,} SKU ({jkt_stats['occupied_bins']}/48 Slot) • "
    f"**In-Transit:** {int(in_transit_total):,} Dus"
)

if "tab6_unlocked" not in st.session_state:
    st.session_state.tab6_unlocked = False
if "pending_force_override" not in st.session_state:
    st.session_state.pending_force_override = None

tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10 = st.tabs([
    "1. Input & Zonasi",
    "2. Peta Rak & Audit",
    "3. Asisten AI",
    "4. Picking & Invoice",
    "5. 🚚 Transfer & Toko Aneka",
    "6. Reorder & Demand ⚠️",
    "7. FIFO & Expiry ⏳",
    "8. Import Excel",
    "9. 🔒 Memori & Leaderboard",
    "10. 🔔 Notifikasi & Log Staf"
])

# --- TAB 1: INPUT & PEMETAAN RAK (SURABAYA & JAKARTA ONLY) ---
with tab1:
    sub_sby, sub_jkt = st.tabs(["🏭 Gudang Surabaya (8×6)", "🏭 Gudang Jakarta (8×6)"])
    with sub_sby:
        render_warehouse_input_form("Surabaya")
    with sub_jkt:
        render_warehouse_input_form("Jakarta")

# --- TAB 2: PETA RAK & CYCLE COUNTING AUDIT MODE (SURABAYA & JAKARTA ONLY) ---
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
    st.subheader("🤖 Asisten Gudang & Prediksi Penjualan Jakarta / Transfer Antar Cabang")
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []
    
    chat_input = st.chat_input("Tanya lokasi barang, kecepatan jual Jakarta, atau transfer ke Toko Aneka...")
    if chat_input:
        st.session_state.chat_history.append({"role": "user", "content": chat_input})
        rel_inv = search_inventory_for_ai(chat_input)
        mem_block = get_memory_context()
        sys_prompt = (
            f"Asisten Kepala Gudang untuk cabang Surabaya dan Jakarta (masing-masing 8 Baris x 6 Lantai) serta pencatatan transfer ke Toko Aneka (toko retail tanpa sistem rak 8x6). "
            f"Zonasi gudang: Fast-Moving (L1-L2), Medium (L3-L4), Slow/Bulky (L5-L6). "
            f"Anda juga memantau kecepatan penjualan Jakarta dan merekomendasikan transfer stok dari Surabaya ke Jakarta jika stok Jakarta menipis (<14 hari).\n"
            f"{mem_block}\n\n{rel_inv}"
        )
        try:
            client, active_model, _ = get_ai_client_and_model()
            res = client.chat.completions.create(model=active_model, temperature=0.2, max_tokens=350, messages=[{"role": "system", "content": sys_prompt}] + st.session_state.chat_history[-6:])
            reply = res.choices[0].message.content
        except Exception as e:
            reply = f"Error AI: {e}"
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# --- TAB 4: PICKING & INVOICE (LOGS SALES VELOCITY) ---
with tab4:
    st.subheader("🛒 Picking & Cetak Surat Jalan (Pencatat Kecepatan Penjualan)")
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
            wh_prefix = get_loc_code(pick_wh)
            inv_no = st.text_input("No. Surat Jalan", value=f"SJ-{wh_prefix}-{datetime.now().strftime('%Y%m%d-%H%M')}")
            picker = st.text_input("Nama Picker", value=st.session_state.current_user)
            
            for _, r in sel_rows.iterrows():
                c1, c2, c3 = st.columns([2, 1, 1])
                c1.text(f"{r['bin_location']} | {r['jurnal_sku']} ({r['product_name']})")
                q_ambil = c2.number_input("Ambil Dus", min_value=1, value=1, max_value=int(r['box_qty']), key=f"pk_{pick_wh}_{r['jurnal_sku']}")
                price = c3.number_input("Harga / Dus", min_value=0, value=0, step=1000, key=f"pr_{pick_wh}_{r['jurnal_sku']}")
                picked_items.append({"sku": r['jurnal_sku'], "name": r['product_name'], "bin": r['bin_location'], "ambil": q_ambil, "avail": int(r['box_qty']), "price": price})

            if st.button(f"🧾 Buat Surat Jalan & Potong Stok ({pick_wh})"):
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                for it in picked_items:
                    rem = it["avail"] - it["ambil"]
                    if rem <= 0:
                        conn.execute("DELETE FROM bin_map WHERE warehouse = ? AND jurnal_sku = ?", (pick_wh, it["sku"]))
                    else:
                        conn.execute("UPDATE bin_map SET box_qty = ? WHERE warehouse = ? AND jurnal_sku = ?", (rem, pick_wh, it["sku"]))
                    
                    conn.execute("""
                        INSERT INTO outbound_sales (warehouse, jurnal_sku, product_name, boxes_sold, sale_date, invoice_no, actor)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (pick_wh, it["sku"], it["name"], int(it["ambil"]), now_str, inv_no, picker))
                
                conn.commit()
                log_activity("PICKING", f"[{pick_wh}] Picking {inv_no} oleh {picker} untuk {len(picked_items)} SKU", actor=picker)
                st.success(f"Surat Jalan **{inv_no}** ({pick_wh}) berhasil diterbitkan! Data penjualan otomatis direkam untuk analisis AI.")

# --- TAB 5: DEDICATED INTER-LOCATION TRANSFER TRACKER (SURABAYA <-> JAKARTA <-> TOKO ANEKA) ---
with tab5:
    st.subheader("🚚 Tracker Pengiriman Antar Gudang & Toko (Surabaya ↔ Jakarta ↔ Toko Aneka)")
    st.caption("Catatan: **Toko Aneka** beroperasi sebagai toko retail ringan (hanya mencatat pengiriman keluar/masuk tanpa sistem peta rak 8×6).")

    smart_recs = compute_jakarta_transfer_recommendations()
    if not smart_recs.empty:
        transferable = smart_recs[smart_recs["Saran Transfer"] > 0]
        if not transferable.empty:
            st.warning(f"🤖 **Notifikasi AI Demand Jakarta:** Terdapat **{len(transferable)} SKU** di Jakarta yang menipis berdasarkan kecepatan penjualan dan siap ditransfer dari Surabaya!")
            st.dataframe(transferable[["SKU", "Produk", "Stok JKT", "In-Transit", "Terjual JKT (30h)", "Estimasi Habis", "Surplus SBY", "Saran Transfer"]], width="stretch")

    st.divider()
    col_tr1, col_tr2 = st.columns([1.2, 1.8])

    # LEFT: Create New Transfer Shipment
    with col_tr1:
        st.markdown("#### 📤 Buat Pengiriman / Transfer Baru")
        origin_loc = st.selectbox("Lokasi Pengirim", TRANSFER_LOCATIONS, key="tr_origin")
        dest_options = [loc for loc in TRANSFER_LOCATIONS if loc != origin_loc]
        dest_loc = st.selectbox("Lokasi Tujuan", dest_options, key="tr_dest")

        # Case A: Origin is Surabaya or Jakarta
        if origin_loc in WAREHOUSES:
            avail_df = pd.read_sql_query(
                "SELECT jurnal_sku, product_name, category, turnover_speed, bin_location, box_qty, pcs_per_box FROM bin_map WHERE warehouse = ? AND box_qty > 0 ORDER BY jurnal_sku",
                conn, params=[origin_loc]
            )

            if avail_df.empty:
                st.warning(f"Tidak ada stok tersedia di {origin_loc} untuk dikirim.")
            else:
                avail_df["opt"] = avail_df.apply(lambda r: f"{r['jurnal_sku']} — {r['product_name']} (Rak {r['bin_location']} | Tersedia: {r['box_qty']} dus)", axis=1)
                chosen_opt = st.selectbox("Pilih Barang dari Rak", avail_df["opt"].tolist())
                chosen_row = avail_df[avail_df["opt"] == chosen_opt].iloc[0]

                with st.form("create_transfer_wh_form"):
                    tr_qty = st.number_input("Jumlah Dus Dikirim", min_value=1, max_value=int(chosen_row["box_qty"]), value=1, step=1)
                    courier_info = st.text_input("Ekspedisi / No. Resi / Driver", placeholder="Contoh: Kurir Toko / Kalog / Resi #12345")
                    if st.form_submit_button(f"🚚 Kirim ke {dest_loc} (Set In-Transit)", use_container_width=True):
                        tr_id = f"TRF-{get_loc_code(origin_loc)}-{get_loc_code(dest_loc)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                        
                        rem_origin = int(chosen_row["box_qty"]) - int(tr_qty)
                        if rem_origin <= 0:
                            conn.execute("DELETE FROM bin_map WHERE warehouse = ? AND jurnal_sku = ?", (origin_loc, chosen_row["jurnal_sku"]))
                        else:
                            conn.execute("UPDATE bin_map SET box_qty = ? WHERE warehouse = ? AND jurnal_sku = ?", (rem_origin, origin_loc, chosen_row["jurnal_sku"]))
                        
                        pcs_box_val = int(chosen_row["pcs_per_box"]) if pd.notna(chosen_row["pcs_per_box"]) else None
                        conn.execute("""
                            INSERT INTO stock_transfers (
                                transfer_id, from_warehouse, to_warehouse, jurnal_sku, product_name,
                                category, turnover_speed, box_qty, pcs_per_box, status, created_at, sender, courier_notes
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'In-Transit', ?, ?, ?)
                        """, (
                            tr_id, origin_loc, dest_loc, chosen_row["jurnal_sku"], chosen_row["product_name"],
                            chosen_row["category"], chosen_row["turnover_speed"], int(tr_qty), pcs_box_val,
                            now_ts, st.session_state.current_user, courier_info.strip()
                        ))
                        conn.commit()
                        log_activity("TRANSFER_SENT", f"{tr_id}: {tr_qty} dus {chosen_row['jurnal_sku']} ({origin_loc} -> {dest_loc})")
                        st.success(f"Pengiriman **{tr_id}** tercatat In-Transit ke **{dest_loc}**!")
                        st.rerun()

        # Case B: Origin is Toko Aneka
        else:
            st.caption("Catat pengiriman/retur barang dari **Toko Aneka** menuju gudang utama:")
            with st.form("create_transfer_aneka_form"):
                ank_sku = st.text_input("Kode SKU Jurnal", placeholder="JRN-1042")
                ank_name = st.text_input("Nama Produk", placeholder="Raket Yonex")
                ank_speed = st.selectbox("Perputaran (untuk penempatan rak tujuan)", ["Medium (Normal)", "Fast-Moving (Laku Cepat)", "Slow-Moving (Lambat)", "Berat / Besar (Bulky)"])
                ank_qty = st.number_input("Jumlah Dus Dikirim", min_value=1, value=1, step=1)
                courier_info = st.text_input("Ekspedisi / Driver / Catatan", placeholder="Contoh: Driver Toko Aneka")
                if st.form_submit_button(f"🚚 Kirim dari Toko Aneka ke {dest_loc}", use_container_width=True):
                    if not ank_sku.strip():
                        st.warning("Kode SKU wajib diisi.")
                    else:
                        tr_id = f"TRF-ANK-{get_loc_code(dest_loc)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                        conn.execute("""
                            INSERT INTO stock_transfers (
                                transfer_id, from_warehouse, to_warehouse, jurnal_sku, product_name,
                                category, turnover_speed, box_qty, pcs_per_box, status, created_at, sender, courier_notes
                            ) VALUES (?, 'Toko Aneka', ?, ?, ?, 'Toko Aneka', ?, ?, NULL, 'In-Transit', ?, ?, ?)
                        """, (
                            tr_id, dest_loc, ank_sku.strip(), ank_name.strip() or ank_sku.strip(),
                            ank_speed, int(ank_qty), now_ts, st.session_state.current_user, courier_info.strip()
                        ))
                        conn.commit()
                        log_activity("TRANSFER_SENT", f"{tr_id}: {ank_qty} dus {ank_sku.strip()} (Toko Aneka -> {dest_loc})")
                        st.success(f"Pengiriman **{tr_id}** dari Toko Aneka tercatat In-Transit ke **{dest_loc}**!")
                        st.rerun()

    # RIGHT: Live In-Transit Tracker & Receive Confirmation
    with col_tr2:
        st.markdown("#### 🛰️ Status Barang Dalam Perjalanan (In-Transit)")
        transit_list = pd.read_sql_query(
            "SELECT * FROM stock_transfers WHERE status = 'In-Transit' ORDER BY created_at DESC",
            conn
        )
        if transit_list.empty:
            st.success("✅ Tidak ada barang yang tertahan di perjalanan. Semua transfer sudah diterima.")
        else:
            for _, tr in transit_list.iterrows():
                with st.container(border=True):
                    t_c1, t_c2 = st.columns([2.5, 1])
                    with t_c1:
                        st.markdown(f"**{tr['transfer_id']}** | `{tr['from_warehouse']}` ➡️ **`{tr['to_warehouse']}`**")
                        st.write(f"📦 **{tr['jurnal_sku']}** — {tr['product_name']} (**{tr['box_qty']} dus**)")
                        st.caption(f"Dikirim: {tr['created_at']} oleh {tr['sender']} | Ekspedisi: {tr['courier_notes'] or '-'}")
                    with t_c2:
                        if st.button(f"✅ Terima di {tr['to_warehouse']}", key=f"rcv_{tr['transfer_id']}", type="primary", use_container_width=True):
                            dest = tr["to_warehouse"]
                            sku_t = tr["jurnal_sku"]
                            add_boxes = int(tr["box_qty"])
                            now_rcv = datetime.now().strftime("%Y-%m-%d %H:%M")

                            if dest == "Toko Aneka":
                                conn.execute("""
                                    UPDATE stock_transfers
                                    SET status = 'Completed', received_at = ?, receiver = ?
                                    WHERE transfer_id = ?
                                """, (now_rcv, st.session_state.current_user, tr["transfer_id"]))
                                conn.commit()
                                log_activity("TRANSFER_RECEIVED", f"{tr['transfer_id']} diterima di Toko Aneka ({add_boxes} dus {sku_t})")
                                st.success(f"Diterima di **Toko Aneka** dan tercatat di log transfer!")
                                st.rerun()
                            else:
                                existing_dest = conn.execute(
                                    "SELECT row_num, floor_num, bin_location, box_qty FROM bin_map WHERE warehouse = ? AND jurnal_sku = ?",
                                    (dest, sku_t)
                                ).fetchone()

                                if existing_dest:
                                    new_total = int(existing_dest[3]) + add_boxes
                                    conn.execute(
                                        "UPDATE bin_map SET box_qty = ? WHERE warehouse = ? AND jurnal_sku = ?",
                                        (new_total, dest, sku_t)
                                    )
                                    assigned_bin = existing_dest[2]
                                else:
                                    pcs_v = int(tr["pcs_per_box"]) if pd.notna(tr["pcs_per_box"]) else None
                                    ar, af, _ = auto_assign_bin(
                                        dest, sku_t, tr["product_name"], tr["category"] or "",
                                        tr["turnover_speed"] or "Medium (Normal)", "", add_boxes, pcs_v
                                    )
                                    ar = ar or 1
                                    af = af or 3
                                    assigned_bin = f"R{ar:02d}-L{af}"
                                    conn.execute("""
                                        INSERT INTO bin_map (
                                            warehouse, jurnal_sku, product_name, category, row_num, floor_num,
                                            bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 3)
                                    """, (
                                        dest, sku_t, tr["product_name"], tr["category"] or "", ar, af,
                                        assigned_bin, tr["turnover_speed"] or "Medium (Normal)",
                                        f"Transfer dari {tr['from_warehouse']}", add_boxes, pcs_v
                                    ))

                                conn.execute("""
                                    UPDATE stock_transfers
                                    SET status = 'Completed', received_at = ?, receiver = ?
                                    WHERE transfer_id = ?
                                """, (now_rcv, st.session_state.current_user, tr["transfer_id"]))
                                conn.commit()
                                log_activity("TRANSFER_RECEIVED", f"{tr['transfer_id']} diterima di {dest} -> Rak {assigned_bin}")
                                st.success(f"Diterima! Masuk ke Rak **{dest} | {assigned_bin}**")
                                st.rerun()

        st.divider()
        st.markdown("#### 📜 Riwayat Transfer Selesai (Surabaya ↔ Jakarta ↔ Toko Aneka)")
        filter_loc = st.selectbox("Filter Riwayat Lokasi", ["Semua Lokasi", "Toko Aneka", "Surabaya", "Jakarta"], key="hist_filter")
        if filter_loc == "Semua Lokasi":
            completed_df = pd.read_sql_query("""
                SELECT transfer_id AS 'No. Transfer', from_warehouse AS 'Dari', to_warehouse AS 'Ke',
                       jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus',
                       created_at AS 'Tgl Kirim', received_at AS 'Tgl Terima', receiver AS 'Penerima'
                FROM stock_transfers WHERE status = 'Completed' ORDER BY received_at DESC LIMIT 50
            """, conn)
        else:
            completed_df = pd.read_sql_query("""
                SELECT transfer_id AS 'No. Transfer', from_warehouse AS 'Dari', to_warehouse AS 'Ke',
                       jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus',
                       created_at AS 'Tgl Kirim', received_at AS 'Tgl Terima', receiver AS 'Penerima'
                FROM stock_transfers
                WHERE status = 'Completed' AND (from_warehouse = ? OR to_warehouse = ?)
                ORDER BY received_at DESC LIMIT 50
            """, conn, params=[filter_loc, filter_loc])

        if completed_df.empty:
            st.caption("Belum ada riwayat transfer yang selesai.")
        else:
            st.dataframe(completed_df, width="stretch")

# --- TAB 6: REORDER ALERTS & JAKARTA DEMAND INTELLIGENCE ---
with tab6:
    st.subheader("⚠️ AI Demand Learning (Jakarta) & Reorder Alerts")
    
    st.markdown("#### 🧠 Analisis Kecepatan Jual Jakarta & Saran Transfer Surabaya → Jakarta")
    rec_table = compute_jakarta_transfer_recommendations()
    if rec_table.empty:
        st.info("Belum ada lonjakan penjualan di Jakarta yang membutuhkan transfer stok dari Surabaya saat ini.")
    else:
        st.dataframe(rec_table, width="stretch")
        if st.button("🤖 Minta Ringkasan Keputusan Pembelian & Transfer dari AI"):
            with st.spinner("AI menganalisis kecepatan penjualan Jakarta vs stok Surabaya..."):
                try:
                    client, active_model, _ = get_ai_client_and_model()
                    prompt = (
                        "Anda adalah Supply Chain Manager. Berdasarkan data penjualan 30 hari terakhir di Jakarta dan stok Surabaya berikut, "
                        "berikan rekomendasi singkat (maksimal 5 poin) barang mana yang harus segera ditransfer dari Surabaya ke Jakarta dan mana yang harus di-PO ke supplier:\n\n"
                        + rec_table.to_csv(index=False)
                    )
                    res = client.chat.completions.create(model=active_model, temperature=0.2, max_tokens=350, messages=[{"role": "user", "content": prompt}])
                    st.info(res.choices[0].message.content)
                except Exception as e:
                    st.error(f"AI sedang offline: {e}")

    st.divider()
    st.markdown("#### 📉 Daftar Stok Di Bawah Batas Minimum (Surabaya & Jakarta)")
    low_stock_df = pd.read_sql_query(
        "SELECT warehouse AS 'Gudang', bin_location AS 'Rak', jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus Tersisa', min_box_threshold AS 'Batas Minimum' FROM bin_map WHERE box_qty <= min_box_threshold ORDER BY warehouse, box_qty ASC",
        conn
    )
    if low_stock_df.empty:
        st.success("✨ Semua stok di Surabaya & Jakarta berada di atas batas minimum.")
    else:
        st.dataframe(low_stock_df, width="stretch")
        csv_reorder = low_stock_df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download Daftar Reorder (CSV)", data=csv_reorder, file_name="reorder_list_sby_jkt.csv", mime="text/csv")

# --- TAB 7: FIFO & EXPIRY TRACKING ---
with tab7:
    st.subheader("⏳ FIFO & Expiry Date Management")
    expiry_df = pd.read_sql_query(
        "SELECT warehouse AS 'Gudang', bin_location AS 'Rak', jurnal_sku AS 'SKU', product_name AS 'Produk', box_qty AS 'Dus', expiry_date AS 'Expired Date' FROM bin_map WHERE expiry_date IS NOT NULL ORDER BY expiry_date ASC",
        conn
    )
    if expiry_df.empty:
        st.info("Belum ada data kadaluarsa (expiry date) yang dimasukkan.")
    else:
        st.markdown("Daftar produk diurutkan berdasarkan kedaluwarsa terdekat (**FIFO Priority**):")
        st.dataframe(expiry_df, width="stretch")

# --- TAB 8: IMPORT EXCEL / CSV ---
with tab8:
    st.subheader("Import Massal WMS (Surabaya & Jakarta)")
    default_import_wh = st.selectbox("Gudang Default (jika kolom 'warehouse' tidak ada di file)", WAREHOUSES)
    up_file = st.file_uploader("Upload CSV/Excel", type=["csv", "xlsx"])
    if up_file:
        df_up = pd.read_csv(up_file) if up_file.name.endswith(".csv") else pd.read_excel(up_file)
        st.dataframe(df_up.head(5))
        if st.button("Proses Import Batch"):
            records = []
            for _, row in df_up.iterrows():
                wh_val = str(row.get("warehouse", default_import_wh)).strip().title()
                if wh_val not in WAREHOUSES:
                    wh_val = default_import_wh
                records.append((
                    wh_val,
                    str(row["jurnal_sku"]),
                    str(row["product_name"]),
                    str(row.get("category", "")),
                    int(row["row_num"]),
                    int(row["floor_num"]),
                    f"R{int(row['row_num']):02d}-L{int(row['floor_num'])}",
                    str(row.get("turnover_speed", "Medium (Normal)")),
                    str(row.get("notes", "")),
                    int(row.get("box_qty", 1)),
                    int(row.get("pcs_per_box", 0)) if pd.notna(row.get("pcs_per_box")) else None,
                    3,
                    None
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO bin_map (warehouse, jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box, min_box_threshold, expiry_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                records
            )
            conn.commit()
            log_activity("IMPORT_MASSAL", f"Import {len(records)} SKU")
            st.success(f"Berhasil mengimpor {len(records)} SKU!")

# --- TAB 9: MEMORI & LEADERBOARD (PASSCODE PROTECTED) ---
with tab9:
    st.subheader("🧠 Memori Aturan & Staff Activity Leaderboard")
    if not st.session_state.tab6_unlocked:
        with st.form("unlock_t9"):
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

# --- TAB 10: REAL-TIME NOTIFICATIONS & STAFF ACTIVITY FEED ---
with tab10:
    st.subheader("🔔 Pusat Notifikasi & Log Aktivitas Akun Staf")
    st.caption("Pantau setiap aktivitas real-time yang dilakukan oleh masing-masing akun di Surabaya, Jakarta, dan Toko Aneka.")

    all_logs_df = pd.read_sql_query(
        "SELECT id, timestamp, actor, action, details FROM activity_log ORDER BY id DESC LIMIT 200",
        conn
    )

    if all_logs_df.empty:
        st.info("Belum ada aktivitas atau notifikasi yang tercatat di sistem.")
    else:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        today_logs = all_logs_df[all_logs_df["timestamp"].str.startswith(today_prefix, na=False)]
        override_logs = all_logs_df[all_logs_df["action"].isin(["OVERRIDE_APPROVED", "FORCE_OVERRIDE"])]
        transfer_logs = all_logs_df[all_logs_df["action"].isin(["TRANSFER_SENT", "TRANSFER_RECEIVED"])]

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Aktivitas Hari Ini", f"{len(today_logs)} Aksi")
        m2.metric("Total Log Tercatat", f"{len(all_logs_df)} Log")
        m3.metric("Aktivitas Transfer", f"{len(transfer_logs)} Log")
        m4.metric("Pengecualian / Override", f"{len(override_logs)} Log")

        st.divider()

        # Filters for Account, Action Type, and Keyword
        f_col1, f_col2, f_col3 = st.columns([1.2, 1.2, 1.6])
        known_actors = ["Semua Akun"] + sorted(all_logs_df["actor"].dropna().unique().tolist())
        selected_actor = f_col1.selectbox("Filter Akun Staf", known_actors, key="notif_actor_filter")

        action_labels = {
            "AUTO_ASSIGN": "🤖 Auto-Assign Rak",
            "SIMPAN_RAK": "📥 Input Manual Rak",
            "PICKING": "🛒 Picking / Surat Jalan",
            "TRANSFER_SENT": "🚚 Kirim Transfer",
            "TRANSFER_RECEIVED": "✅ Terima Transfer",
            "CYCLE_COUNT_AUDIT": "🔄 Cycle Count Audit",
            "OVERRIDE_APPROVED": "🔐 Override Disetujui",
            "FORCE_OVERRIDE": "⚠️ Force Override Supervisor",
            "IMPORT_MASSAL": "📊 Import Batch Excel"
        }
        available_actions = ["Semua Aktivitas"] + sorted(all_logs_df["action"].dropna().unique().tolist())
        selected_action = f_col2.selectbox(
            "Filter Jenis Aktivitas",
            available_actions,
            format_func=lambda x: action_labels.get(x, x),
            key="notif_action_filter"
        )
        search_kw = f_col3.text_input("Cari SKU / Lokasi / Kata Kunci", placeholder="Contoh: JRN-1042, Toko Aneka, Jakarta...", key="notif_kw")

        filtered_df = all_logs_df.copy()
        if selected_actor != "Semua Akun":
            filtered_df = filtered_df[filtered_df["actor"] == selected_actor]
        if selected_action != "Semua Aktivitas":
            filtered_df = filtered_df[filtered_df["action"] == selected_action]
        if search_kw.strip():
            kw_low = search_kw.strip().lower()
            filtered_df = filtered_df[
                filtered_df["details"].str.lower().str.contains(kw_low, na=False) |
                filtered_df["actor"].str.lower().str.contains(kw_low, na=False)
            ]

        st.markdown(f"Menampilkan **{len(filtered_df)}** notifikasi terbaru:")

        for _, row in filtered_df.head(50).iterrows():
            act = row["action"]
            actor_name = row["actor"] or "System"
            ts = row["timestamp"]
            det = row["details"]

            if act in ("FORCE_OVERRIDE", "OVERRIDE_APPROVED"):
                badge = "⚠️"
            elif act in ("TRANSFER_SENT", "TRANSFER_RECEIVED"):
                badge = "🚚"
            elif act == "PICKING":
                badge = "🛒"
            elif act == "CYCLE_COUNT_AUDIT":
                badge = "🔄"
            else:
                badge = "📦"

            with st.container(border=True):
                c_left, c_right = st.columns([4, 1])
                c_left.markdown(f"{badge} **{actor_name}** — `{action_labels.get(act, act)}`\n\n{det}")
                c_right.caption(f"🕒 {ts}")