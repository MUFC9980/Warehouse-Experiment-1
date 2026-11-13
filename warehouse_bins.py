import os
import re
import sqlite3
from datetime import datetime
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

# --- 0. SUPERVISOR PASSCODE CONFIG (For 15% Flexibility Override & Tab 6 Memory) ---
def get_supervisor_pin() -> str:
    """Reads SUPERVISOR_PIN from Streamlit secrets or env var, defaulting to '8899'."""
    try:
        if "SUPERVISOR_PIN" in st.secrets:
            return str(st.secrets["SUPERVISOR_PIN"]).strip()
    except Exception:
        pass
    return os.getenv("SUPERVISOR_PIN", "8899").strip()

SUPERVISOR_PIN = get_supervisor_pin()

# --- 1. LOCAL AI CONNECTION SETUP (LM Studio 1234 -> Jan AI 1337 -> Optional Cloud Fallback) ---
def get_ai_client_and_model() -> tuple[OpenAI, str, str]:
    """Connects directly to Local AI (LM Studio 1234 or Jan AI 1337), prioritizing Qwen/Chat models."""
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

# --- 2. DATABASE SETUP, WAL CONCURRENCY & 10K-SKU INDEXES ---
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
        pcs_per_box INTEGER
    )
""")

# Safe migration for existing databases
existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(bin_map)").fetchall()]
if "box_qty" not in existing_cols:
    conn.execute("ALTER TABLE bin_map ADD COLUMN box_qty INTEGER DEFAULT 1")
if "pcs_per_box" not in existing_cols:
    conn.execute("ALTER TABLE bin_map ADD COLUMN pcs_per_box INTEGER")

# Indexes for fast 10,000+ SKU queries
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
        details TEXT
    )
""")
conn.commit()

def log_activity(action: str, details: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO activity_log (timestamp, action, details) VALUES (?, ?, ?)",
        (now, action, details)
    )
    conn.commit()

def format_qty_summary(box_qty: int, pcs_per_box: int | None) -> str:
    b_qty = int(box_qty) if pd.notna(box_qty) and box_qty else 1
    if pd.notna(pcs_per_box) and pcs_per_box and int(pcs_per_box) > 0:
        p_qty = int(pcs_per_box)
        return f"{b_qty} dus × {p_qty} pcs ({b_qty * p_qty:,} pcs)".replace(",", ".")
    return f"{b_qty} dus"

def get_warehouse_stats() -> dict:
    """Fast aggregate query across 10k+ SKUs."""
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
    """Fetches saved warehouse rules & recent logs to feed into the AI."""
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
        rules = "\n".join(
            [f"- [{r['category']}] {r['memory_text']}" for _, r in mem_df.iterrows()]
        )
        parts.append(f"ATURAN & MEMORI TETAP GUDANG:\n{rules}")
    if not log_df.empty:
        logs = "\n".join(
            [f"- {r['timestamp']} | {r['action']}: {r['details']}" for _, r in log_df.iterrows()]
        )
        parts.append(f"RIWAYAT AKTIVITAS TERAKHIR:\n{logs}")

    return "\n\n".join(parts) if parts else "Belum ada memori khusus tambahan."

def search_inventory_for_ai(query_text: str, limit: int = 25) -> str:
    """Retrieves only relevant SKUs + global summary stats so 10,000+ SKUs never overflow AI context."""
    stats = get_warehouse_stats()
    summary_header = (
        f"RINGKASAN TOTAL GUDANG: {stats['total_skus']:,} SKU | "
        f"{stats['total_boxes']:,} Dus | {stats['total_pcs']:,} Pcs terhitung | "
        f"{stats['occupied_bins']}/48 Slot Terisi ({stats['empty_bins']} Slot Kosong)."
    ).replace(",", ".")

    if stats["total_skus"] == 0:
        return f"{summary_header}\nSemua 48 slot rak masih kosong."

    slot_summary_df = pd.read_sql_query("""
        SELECT bin_location, COUNT(*) AS sku_count, COALESCE(SUM(box_qty), 0) AS total_dus
        FROM bin_map
        GROUP BY bin_location
        ORDER BY row_num, floor_num
    """, conn)
    slot_lines = ", ".join([
        f"{r['bin_location']}({r['sku_count']} SKU/{int(r['total_dus'])} dus)"
        for _, r in slot_summary_df.iterrows()
    ])

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
                       category AS "Kategori", box_qty AS "Dus", pcs_per_box AS "Isi_Dus",
                       turnover_speed AS "Perputaran", notes AS "Catatan"
                FROM bin_map
                WHERE {where_clause}
                ORDER BY row_num, floor_num
                LIMIT {limit}""",
            conn,
            params=params
        )
    else:
        matched_df = pd.read_sql_query(
            f"""SELECT bin_location AS "Rak", jurnal_sku AS "SKU", product_name AS "Produk",
                       category AS "Kategori", box_qty AS "Dus", pcs_per_box AS "Isi_Dus",
                       turnover_speed AS "Perputaran", notes AS "Catatan"
                FROM bin_map
                ORDER BY row_num, floor_num
                LIMIT {limit}""",
            conn
        )

    matched_csv = (
        matched_df.to_csv(index=False)
        if not matched_df.empty
        else "Tidak ada SKU yang cocok langsung dengan kata kunci pertanyaan."
    )

    return (
        f"{summary_header}\n"
        f"KETERISIAN PER SLOT: {slot_lines}\n\n"
        f"SAMPEL DATA SKU RELEVAN (Maks {limit} hasil):\n{matched_csv}"
    )

def validate_placement_against_rules(
    sku: str, name: str, cat: str, row_num: int, floor_num: int, speed: str, notes: str
) -> tuple[bool, str]:
    """Enforces 85% strict floor turnover rules and custom row memory rules."""
    speed_low = speed.lower()

    if "fast-moving" in speed_low:
        if floor_num not in (1, 2):
            return False, f"Barang Fast-Moving wajib ditaruh di Lantai 1 atau 2 (Anda memilih Lantai {floor_num})."
    elif "medium" in speed_low:
        if floor_num not in (3, 4):
            return False, f"Barang Medium wajib ditaruh di Lantai 3 atau 4 (Anda memilih Lantai {floor_num})."
    elif "slow-moving" in speed_low or "berat" in speed_low or "bulky" in speed_low:
        if floor_num not in (5, 6):
            return False, f"Barang Slow-Moving / Bulky wajib ditaruh di Lantai 5 atau 6 (Anda memilih Lantai {floor_num})."

    mem_df = pd.read_sql_query(
        "SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC",
        conn
    )
    if mem_df.empty:
        return True, ""

    item_blob = f"{sku} {name} {cat} {notes}".lower()

    for _, r in mem_df.iterrows():
        rule_raw = str(r["memory_text"]).strip()
        rule_low = rule_raw.lower()

        row_tokens = [f"baris {row_num:02d}", f"baris {row_num}", f"r{row_num:02d}"]
        if any(tok in rule_low for tok in row_tokens) and "hanya boleh" in rule_low:
            allowed_part = (
                rule_low.split("oleh")[-1]
                if "oleh" in rule_low
                else rule_low.split("hanya boleh")[-1]
            )
            allowed_keywords = [
                w.strip(" .,-")
                for w in allowed_part.replace("diisi", "").split()
                if len(w.strip(" .,-")) > 2
            ]
            if allowed_keywords and not all(kw in item_blob for kw in allowed_keywords):
                return (
                    False,
                    f"Melanggar aturan tetap: '{rule_raw}' "
                    f"(Barang '{name}' tidak sesuai dengan aturan zona baris ini)."
                )

    return True, ""

def auto_assign_bin(
    sku: str, name: str, cat: str, speed: str, notes: str, box_qty: int, pcs_per_box: int | None
) -> tuple[int | None, int | None, str]:
    """Finds valid slots (prioritizing empty slots first, then least-crowded valid slots at 10k scale)."""
    slot_counts_df = pd.read_sql_query(
        "SELECT row_num, floor_num, COUNT(*) AS cnt, COALESCE(SUM(box_qty), 0) AS boxes FROM bin_map GROUP BY row_num, floor_num",
        conn
    )
    occupancy_map = {
        (int(r["row_num"]), int(r["floor_num"])): (int(r["cnt"]), int(r["boxes"]))
        for _, r in slot_counts_df.iterrows()
    }

    valid_slots = []
    for r in range(1, 9):
        for f in range(1, 7):
            ok, _ = validate_placement_against_rules(sku, name, cat, r, f, speed, notes)
            if ok:
                sku_cnt, box_cnt = occupancy_map.get((r, f), (0, 0))
                valid_slots.append((sku_cnt, box_cnt, r, f, f"R{r:02d}-L{f}"))

    if not valid_slots:
        return None, None, "Tidak ada slot yang memenuhi aturan zonasi & memori barang ini!"

    valid_slots.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    top_candidates = valid_slots[:12]
    candidate_labels = [
        f"{s[4]} ({s[0]} SKU, {s[1]} dus)" for s in top_candidates
    ]
    candidate_codes = [s[4] for s in top_candidates]

    memory_block = get_memory_context()
    qty_desc = format_qty_summary(box_qty, pcs_per_box)

    try:
        client, active_model, status = get_ai_client_and_model()
        if not status.startswith("Offline"):
            prompt = (
                f"Pilih TEPAT SATU kode rak terbaik untuk produk baru berikut dari daftar kandidat slot paling longgar.\n"
                f"Produk: {sku} - {name} (Kategori: {cat}, Jumlah: {qty_desc}, Perputaran: {speed}, Catatan: {notes})\n"
                f"Kandidat slot valid: {', '.join(candidate_labels)}\n"
                f"{memory_block}\n\n"
                f"Balas HANYA dengan kode rak (contoh: R01-L1) diikuti 1 kalimat alasan singkat."
            )
            res = client.chat.completions.create(
                model=active_model,
                temperature=0.1,
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}]
            )
            ai_reply = res.choices[0].message.content.strip()
            match = re.search(r"R0([1-8])-L([1-6])", ai_reply.upper())
            if match:
                chosen_code = f"R0{match.group(1)}-L{match.group(2)}"
                if chosen_code in candidate_codes:
                    return int(match.group(1)), int(match.group(2)), f"Dipilih oleh AI ({chosen_code}): {ai_reply}"
    except Exception:
        pass

    _, _, best_r, best_f, best_code = top_candidates[0]
    return best_r, best_f, f"Dipilih otomatis ke slot paling longgar sesuai zonasi 85% ({best_code})."

# --- 3. PAGE HEADER & TABS (6 STREAMLINED TABS) ---
st.set_page_config(page_title="Sistem Rak Gudang 8x6 - Jurnal AI", layout="wide")
st.title("📦 Sistem Lokasi Rak Gudang (8 Baris × 6 Lantai)")

stats_now = get_warehouse_stats()
st.caption(
    f"Zonasi 85% Ketat • 15% Fleksibel (PIN Supervisor) • Skala 10.000+ SKU Ready | "
    f"📊 Total Stok: **{stats_now['total_skus']:,} SKU** • **{stats_now['total_boxes']:,} Dus** • "
    f"**{stats_now['occupied_bins']}/48 Slot Terisi**"
)

if "tab6_unlocked" not in st.session_state:
    st.session_state.tab6_unlocked = False

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "1. Input Lokasi Rak SKU",
    "2. Peta Visual Rak (8×6)",
    "3. Asisten & Prediksi AI",
    "4. Ambil Barang & Invoice",
    "5. Import Excel / CSV",
    "6. 🔒 Memori & Aturan AI"
])

# --- TAB 1: INPUT & PEMETAAN RAK ---
with tab1:
    st.subheader("Hubungkan Kode Produk Mekari Jurnal ke Rak (Baris 1–8, Lantai 1–6)")
    st.info("🔒 **Sistem 85% Ketat (Zonasi Aktif):** Pelanggaran zonasi diblokir otomatis. Centang **Auto-Assign AI** agar AI memilihkan slot paling longgar sesuai zonasi.")
    
    with st.form("add_sku_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        sku = col1.text_input("Kode Produk Jurnal (SKU)", placeholder="Contoh: JRN-1042")
        name = col2.text_input("Nama Produk (sesuai Jurnal)", placeholder="Contoh: Raket Badminton Yonex")
        cat = col3.text_input("Kategori Barang", placeholder="Contoh: Olahraga / Raket")

        q_col1, q_col2, q_col3 = st.columns([1, 1, 2])
        box_qty = q_col1.number_input("Jumlah Dus (Box)", min_value=1, value=1, step=1)
        pcs_per_box_input = q_col2.number_input(
            "Isi per Dus / Pcs (Opsional)",
            min_value=0,
            value=0,
            step=1,
            help="Biarkan 0 jika tidak perlu menghitung jumlah isi di dalam dus."
        )
        speed = q_col3.selectbox("Kecepatan Perputaran", [
            "Fast-Moving (Laku Cepat)",
            "Medium (Normal)",
            "Slow-Moving (Lambat)",
            "Berat / Besar (Bulky)"
        ])

        auto_mode = st.checkbox(
            "🤖 Auto-Assign Lokasi Rak oleh AI (Otomatis pilih Baris & Lantai paling longgar sesuai zonasi & memori)",
            value=True
        )

        col4, col5, col6 = st.columns([1, 1, 2.5])
        row_num = col4.selectbox("Baris (Manual)", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}")
        floor_num = col5.selectbox(
            "Lantai (Manual)",
            list(range(1, 7)),
            format_func=lambda x: f"Lantai {x} {'(Bawah)' if x == 1 else '(Atas)' if x == 6 else ''}"
        )
        notes = col6.text_input("Catatan Fisik", placeholder="Contoh: Dus besar, butuh tangga jika lantai 5-6")

        st.divider()
        flex_expander = st.expander("🔐 15% Fleksibel: Opsi Pengecualian / Override Supervisor (Wajib Passcode)")
        with flex_expander:
            allow_override = st.checkbox("Izinkan Penempatan di Luar Aturan Zonasi (Pengecualian)")
            ov_col1, ov_col2 = st.columns([2, 1])
            override_reason = ov_col1.text_input(
                "Alasan Pengecualian (Wajib diisi jika dicentang):",
                placeholder="Contoh: Rak bawah penuh, titip sementara 1 hari"
            )
            override_pin_input = ov_col2.text_input(
                "Passcode Supervisor:",
                type="password",
                placeholder="Masukkan PIN"
            )

        if st.form_submit_button("Simpan Lokasi Rak"):
            if not sku.strip():
                st.warning("Harap isi Kode Produk Jurnal (SKU).")
            else:
                pcs_val = int(pcs_per_box_input) if int(pcs_per_box_input) > 0 else None
                qty_label = format_qty_summary(int(box_qty), pcs_val)

                if auto_mode:
                    with st.spinner("🤖 AI sedang mencari & menetapkan slot rak terbaik..."):
                        assigned_r, assigned_f, assign_msg = auto_assign_bin(
                            sku.strip(), name.strip(), cat.strip(), speed, notes.strip(), int(box_qty), pcs_val
                        )
                    if assigned_r is None:
                        st.error(f"⛔ **Gagal Auto-Assign:** {assign_msg}")
                    else:
                        bin_code = f"R{assigned_r:02d}-L{assigned_f}"
                        conn.execute(
                            """INSERT OR REPLACE INTO bin_map
                               (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sku.strip(), name.strip(), cat.strip(), assigned_r, assigned_f, bin_code, speed, notes.strip(), int(box_qty), pcs_val)
                        )
                        conn.commit()
                        log_activity("AUTO_ASSIGN_RAK", f"{sku.strip()} ({name.strip()}, {qty_label}) -> {bin_code} | {assign_msg}")
                        st.success(f"🤖 **Berhasil Auto-Assign:** `{sku.strip()}` ({qty_label}) → **{bin_code}** (Baris {assigned_r}, Lantai {assigned_f})\n\n_{assign_msg}_")
                else:
                    bin_code = f"R{row_num:02d}-L{floor_num}"
                    is_allowed, block_reason = validate_placement_against_rules(
                        sku.strip(), name.strip(), cat.strip(), row_num, floor_num, speed, notes.strip()
                    )

                    if not is_allowed and not allow_override:
                        log_activity("DITOLAK_ZONASI", f"{sku.strip()} ({name.strip()}) ke {bin_code}: {block_reason}")
                        st.error(
                            f"⛔ **Penempatan Ditolak (85% Aturan Ketat):** {block_reason}\n\n"
                            "💡 *Tip:* Centang **Auto-Assign Lokasi Rak oleh AI** di atas, atau buka **15% Fleksibel** dan masukkan Passcode Supervisor."
                        )
                    else:
                        if not is_allowed and allow_override:
                            if not override_reason.strip():
                                st.warning("⚠️ Anda mencentang override, tetapi **Alasan Pengecualian** belum diisi!")
                                st.stop()
                            if override_pin_input.strip() != SUPERVISOR_PIN:
                                log_activity("GAGAL_PIN_OVERRIDE", f"Percobaan override ditolak (PIN salah) untuk {sku.strip()} ke {bin_code}")
                                st.error("🔐 **Passcode Supervisor Salah!** Pengecualian 15% dibatalkan.")
                                st.stop()
                            log_activity("OVERRIDE_ZONASI", f"OVERRIDE disetujui (PIN valid) untuk {sku.strip()} ke {bin_code}. Alasan: {override_reason}")
                            st.warning(f"⚠️ **Pengecualian 15% Diterapkan (Override):** Menyimpan ke {bin_code} meskipun melanggar zonasi. Alasan tercatat: *{override_reason}*")

                        conn.execute(
                            """INSERT OR REPLACE INTO bin_map
                               (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sku.strip(), name.strip(), cat.strip(), row_num, floor_num, bin_code, speed, notes.strip(), int(box_qty), pcs_val)
                        )
                        conn.commit()
                        if is_allowed:
                            log_activity("SIMPAN_RAK", f"{sku.strip()} ({name.strip()}, {qty_label}) -> {bin_code}")
                        st.success(f"Berhasil menyimpan: {sku.strip()} ({qty_label}) → **{bin_code}** (Baris {row_num}, Lantai {floor_num})")

    st.divider()
    search_col1, search_col2 = st.columns([3, 1])
    sku_search_query = search_col1.text_input(
        "🔍 Cari di Database Rak (SKU, Nama Produk, Kategori, atau Kode Rak):",
        placeholder="Ketik kata kunci untuk menyaring dari ribuan SKU... (kosongkan untuk tampilkan 100 terbaru)"
    )
    row_limit = search_col2.selectbox("Batas Tampilan", [100, 250, 500], index=0)

    if sku_search_query.strip():
        pat = f"%{sku_search_query.strip().lower()}%"
        raw_bins_df = pd.read_sql_query(
            f"""SELECT bin_location, jurnal_sku, product_name, category, box_qty, pcs_per_box, turnover_speed, notes
                FROM bin_map
                WHERE LOWER(jurnal_sku) LIKE ? OR LOWER(product_name) LIKE ? OR LOWER(category) LIKE ? OR LOWER(bin_location) LIKE ?
                ORDER BY row_num, floor_num
                LIMIT {row_limit}""",
            conn,
            params=[pat, pat, pat, pat]
        )
    else:
        raw_bins_df = pd.read_sql_query(
            f"""SELECT bin_location, jurnal_sku, product_name, category, box_qty, pcs_per_box, turnover_speed, notes
                FROM bin_map
                ORDER BY rowid DESC
                LIMIT {row_limit}""",
            conn
        )

    if raw_bins_df.empty:
        df_bins = pd.DataFrame(columns=[
            "Kode Rak", "Kode SKU Jurnal", "Nama Produk", "Kategori",
            "Jumlah Dus", "Isi / Dus", "Total Pcs", "Perputaran", "Catatan"
        ])
    else:
        df_bins = pd.DataFrame({
            "Kode Rak": raw_bins_df["bin_location"],
            "Kode SKU Jurnal": raw_bins_df["jurnal_sku"],
            "Nama Produk": raw_bins_df["product_name"],
            "Kategori": raw_bins_df["category"],
            "Jumlah Dus": raw_bins_df["box_qty"].fillna(1).astype(int),
            "Isi / Dus": raw_bins_df["pcs_per_box"].apply(
                lambda x: f"{int(x)} pcs" if pd.notna(x) and int(x) > 0 else "-"
            ),
            "Total Pcs": raw_bins_df.apply(
                lambda r: f"{int(r['box_qty'] or 1) * int(r['pcs_per_box']):,} pcs".replace(",", ".")
                if pd.notna(r["pcs_per_box"]) and int(r["pcs_per_box"]) > 0 else "-",
                axis=1
            ),
            "Perputaran": raw_bins_df["turnover_speed"],
            "Catatan": raw_bins_df["notes"]
        })

    st.caption(f"Menampilkan **{len(df_bins)}** baris (dari total **{stats_now['total_skus']:,}** SKU di database).")
    st.dataframe(df_bins, width="stretch")

    if not df_bins.empty:
        del_col1, del_col2 = st.columns([2, 1])
        sku_to_delete = del_col1.selectbox(
            "Hapus SKU dari Rak (dari hasil pencarian di atas):",
            df_bins["Kode SKU Jurnal"].tolist()
        )
        if del_col2.button("🗑️ Hapus SKU Terpilih"):
            conn.execute("DELETE FROM bin_map WHERE jurnal_sku = ?", (sku_to_delete,))
            conn.commit()
            log_activity("HAPUS_RAK", f"Menghapus SKU {sku_to_delete} dari rak")
            st.success(f"SKU **{sku_to_delete}** berhasil dihapus dari rak.")
            st.rerun()

# --- TAB 2: PETA VISUAL RAK 8 BARIS x 6 LANTAI ---
with tab2:
    st.subheader("Denah Keterisian Rak (Lantai 6 Paling Atas → Lantai 1 Paling Bawah)")
    agg_df = pd.read_sql_query(
        """SELECT row_num, floor_num, COUNT(*) AS sku_cnt, COALESCE(SUM(box_qty), 0) AS total_dus
           FROM bin_map
           GROUP BY row_num, floor_num""",
        conn
    )
    slot_lookup = {
        (int(r["row_num"]), int(r["floor_num"])): (int(r["sku_cnt"]), int(r["total_dus"]))
        for _, r in agg_df.iterrows()
    }
    
    grid_data = {}
    for r in range(1, 9):
        col_label = f"Baris {r:02d}"
        grid_data[col_label] = []
        for f in range(6, 0, -1):
            if (r, f) not in slot_lookup:
                grid_data[col_label].append(f"[Kosong] R{r:02d}-L{f}")
            else:
                s_cnt, d_cnt = slot_lookup[(r, f)]
                grid_data[col_label].append(f"📦 {s_cnt} SKU ({d_cnt} dus)")

    grid_df = pd.DataFrame(grid_data, index=[f"Lantai {f}" for f in range(6, 0, -1)])
    st.dataframe(grid_df, width="stretch", height=260)

    st.divider()
    st.markdown("#### 🔎 Inspeksi Isi Detail per Slot Rak")
    insp_c1, insp_c2 = st.columns(2)
    insp_row = insp_c1.selectbox("Pilih Baris", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}", key="insp_r")
    insp_floor = insp_c2.selectbox("Pilih Lantai", list(range(1, 7)), format_func=lambda x: f"Lantai {x}", key="insp_f")
    target_bin = f"R{insp_row:02d}-L{insp_floor}"

    slot_items_df = pd.read_sql_query(
        """SELECT jurnal_sku AS "SKU", product_name AS "Nama Produk", category AS "Kategori",
                  box_qty AS "Jumlah Dus", pcs_per_box AS "Isi / Dus", turnover_speed AS "Perputaran", notes AS "Catatan"
           FROM bin_map
           WHERE row_num = ? AND floor_num = ?
           ORDER BY jurnal_sku""",
        conn,
        params=[insp_row, insp_floor]
    )
    if slot_items_df.empty:
        st.info(f"Slot **{target_bin}** saat ini kosong.")
    else:
        st.write(f"Isi Slot **{target_bin}** (**{len(slot_items_df)}** SKU):")
        st.dataframe(slot_items_df, width="stretch")

# --- TAB 3: GABUNGAN TANYA AI, MEMORI & PREDIKSI RAK BARANG MASUK ---
with tab3:
    st.subheader("🤖 Asisten Gudang & Prediksi Penempatan Rak (Local AI)")
    _, active_model_preview, ai_status = get_ai_client_and_model()
    st.caption(f"Status Koneksi AI: **{ai_status}** • Model: `{active_model_preview}` • Mode Hemat Konteks (10k SKU Ready)")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    with st.expander("📦 Prediksi Cepat Rak untuk Barang Baru Masuk (Slotting)", expanded=False):
        p_col1, p_col2 = st.columns([3, 1])
        new_item_input = p_col1.text_input(
            "Deskripsi & Jumlah Barang Masuk:",
            placeholder="Contoh: 15 dus sepatu roda inline skate (isi 6 pcs/dus), berat total 250kg",
            key="quick_slotting_input"
        )
        predict_clicked = p_col2.button("Prediksi Slot Terbaik", width="stretch")

    col_reset, _ = st.columns([1, 4])
    if col_reset.button("🧹 Reset Obrolan Sesi Ini"):
        st.session_state.chat_history = []
        st.rerun()

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    chat_input_val = st.chat_input("Tanya lokasi barang, jumlah dus/pcs, atau minta rekomendasi rak...")

    active_prompt = None
    if predict_clicked:
        if not new_item_input.strip():
            st.warning("Isi deskripsi barang masuk terlebih dahulu.")
        else:
            active_prompt = f"Rekomendasikan 1 slot rak terbaik untuk barang baru masuk berikut ini: {new_item_input.strip()}"
    elif chat_input_val:
        active_prompt = chat_input_val.strip()

    if active_prompt:
        st.session_state.chat_history.append({"role": "user", "content": active_prompt})
        with st.chat_message("user"):
            st.markdown(active_prompt)

        lower_prompt = active_prompt.lower()
        if lower_prompt.startswith(("ingat:", "ingat bahwa", "catat:")):
            if not st.session_state.tab6_unlocked:
                reply = "🔐 Penambahan aturan permanen (`Ingat: ...`) dilindungi Passcode Supervisor. Buka kunci **Tab 6 (Memori & Aturan AI)** terlebih dahulu untuk menambah aturan baru."
                st.session_state.chat_history.append({"role": "assistant", "content": reply})
                with st.chat_message("assistant"):
                    st.warning(reply)
            else:
                clean_note = active_prompt.split(":", 1)[-1].strip() if ":" in active_prompt else active_prompt
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                conn.execute(
                    "INSERT INTO warehouse_memory (category, memory_text, created_at) VALUES (?, ?, ?)",
                    ("Aturan Zona Rak", clean_note, now_str)
                )
                conn.commit()
                log_activity("TAMBAH_MEMORI", clean_note)
                reply = f"✅ Tersimpan ke memori permanen gudang: **\"{clean_note}\"**"
                st.session_state.chat_history.append({"role": "assistant", "content": reply})
                with st.chat_message("assistant"):
                    st.markdown(reply)
        else:
            relevant_inventory = search_inventory_for_ai(active_prompt, limit=25)
            memory_block = get_memory_context()

            system_msg = f"""Kamu adalah asisten kepala gudang sekaligus perencana penempatan rak (8 Baris R01-R08, 6 Lantai L1-L6).
Zonasi wajib:
- Fast-Moving (Laku Cepat): Lantai 1-2 (L1-L2)
- Medium (Normal): Lantai 3-4 (L3-L4)
- Slow-Moving / Bulky / Berat: Lantai 5-6 (L5-L6)

Tugasmu:
1. Jika pengguna menanyakan lokasi barang, stok (jumlah dus/pcs), atau riwayat gudang, jawab singkat dan akurat berdasarkan RINGKASAN & SAMPEL DATA SKU RELEVAN.
2. Jika pengguna meminta rekomendasi/prediksi rak untuk barang baru masuk, patuhi aturan zonasi dan ATURAN GUDANG di bawah, pilih 1 Kode Rak terbaik (format R0X-LX) yang paling kosong/longgar, dan berikan 2 alasan singkat dalam Bahasa Indonesia.

{memory_block}

{relevant_inventory}"""

            recent_messages = [{"role": "system", "content": system_msg}] + st.session_state.chat_history[-6:]

            with st.chat_message("assistant"):
                with st.spinner("Mencari di database & memproses dengan Local AI..."):
                    try:
                        client, active_model, _ = get_ai_client_and_model()
                        res = client.chat.completions.create(
                            model=active_model,
                            temperature=0.2,
                            max_tokens=350,
                            messages=recent_messages
                        )
                        reply = res.choices[0].message.content
                        st.markdown(reply)
                        st.session_state.chat_history.append({"role": "assistant", "content": reply})
                    except Exception as e:
                        st.error(
                            f"Gagal terhubung ke Local AI Server: {e}\n\n"
                            "Pastikan di **LM Studio** / **Jan AI**, model Qwen2.5 sudah dalam status **Started / Active**."
                        )

# --- TAB 4: AMBIL BARANG DARI RAK & PRINTABLE INVOICE ---
with tab4:
    st.subheader("🛒 Pengambilan Barang (Picking) & Cetak Invoice / Surat Jalan")
    if stats_now["total_skus"] == 0:
        st.info("Belum ada barang tersimpan di rak untuk diambil. Tambahkan barang terlebih dahulu di Tab 1.")
    else:
        inv_col1, inv_col2, inv_col3 = st.columns(3)
        default_inv_no = f"INV-{datetime.now().strftime('%Y%m%d-%H%M')}"
        invoice_no = inv_col1.text_input("Nomor Invoice / Surat Jalan", value=default_inv_no)
        customer_name = inv_col2.text_input("Nama Pelanggan / Tujuan Pengiriman", placeholder="Contoh: Toko Sport Jaya / Cabang Surabaya")
        picker_name = inv_col3.text_input("Nama Staf Pengambil / Picker", placeholder="Contoh: Budi")

        pick_search = st.text_input(
            "🔍 Filter / Cari Barang yang Akan Diambil (ketik SKU, Nama Produk, atau Kode Rak):",
            placeholder="Contoh: JRN-1042 atau Yonex atau R01 (menampilkan maks 50 hasil agar cepat di HP)"
        )

        if pick_search.strip():
            p_pat = f"%{pick_search.strip().lower()}%"
            pick_df = pd.read_sql_query(
                """SELECT jurnal_sku, product_name, category, row_num, floor_num, bin_location, notes, box_qty, pcs_per_box
                   FROM bin_map
                   WHERE LOWER(jurnal_sku) LIKE ? OR LOWER(product_name) LIKE ? OR LOWER(bin_location) LIKE ?
                   ORDER BY row_num, floor_num
                   LIMIT 50""",
                conn,
                params=[p_pat, p_pat, p_pat]
            )
        else:
            pick_df = pd.read_sql_query(
                """SELECT jurnal_sku, product_name, category, row_num, floor_num, bin_location, notes, box_qty, pcs_per_box
                   FROM bin_map
                   ORDER BY row_num, floor_num
                   LIMIT 50""",
                conn
            )

        pick_df["label"] = pick_df.apply(
            lambda r: f"[{r['bin_location']}] {r['jurnal_sku']} — {r['product_name']} ({format_qty_summary(r['box_qty'], r['pcs_per_box'])})",
            axis=1
        )
        selected_labels = st.multiselect(
            "Pilih Barang yang Akan Diambil dari Rak (Otomatis diurutkan sesuai jalur rak R01 → R08):",
            options=pick_df["label"].tolist()
        )

        if selected_labels:
            selected_rows = pick_df[pick_df["label"].isin(selected_labels)].sort_values(by=["row_num", "floor_num"])
            st.markdown("#### Detail Jumlah Dus & Harga per Item")

            picked_items = []
            for idx, r in selected_rows.iterrows():
                avail_boxes = int(r["box_qty"] or 1)
                pcs_box = int(r["pcs_per_box"]) if pd.notna(r["pcs_per_box"]) and int(r["pcs_per_box"]) > 0 else 0
                stock_hint = f"Stok: {format_qty_summary(avail_boxes, pcs_box)}"

                c1, c2, c3, c4 = st.columns([2.5, 1, 1.5, 2])
                c1.markdown(
                    f"**{r['bin_location']}** | `{r['jurnal_sku']}`<br>{r['product_name']}<br><small>{stock_hint}</small>",
                    unsafe_allow_html=True
                )
                qty_dus = c2.number_input("Ambil (Dus)", min_value=1, value=1, step=1, key=f"qty_{r['jurnal_sku']}")
                price = c3.number_input("Harga / Dus (Rp)", min_value=0, value=0, step=1000, key=f"price_{r['jurnal_sku']}")
                item_note = c4.text_input("Keterangan", value=r["notes"] if r["notes"] else "", key=f"note_{r['jurnal_sku']}")

                total_pcs_picked = qty_dus * pcs_box if pcs_box > 0 else 0

                picked_items.append({
                    "Kode Rak": r["bin_location"],
                    "Kode SKU": r["jurnal_sku"],
                    "Nama Produk": r["product_name"],
                    "Ambil (Dus)": qty_dus,
                    "Isi / Dus": f"{pcs_box} pcs" if pcs_box > 0 else "-",
                    "Total Pcs": f"{total_pcs_picked:,} pcs".replace(",", ".") if total_pcs_picked > 0 else "-",
                    "Harga Satuan": price,
                    "Subtotal": qty_dus * price,
                    "Keterangan": item_note,
                    "_avail_boxes": avail_boxes
                })

            st.divider()
            opt_col1, opt_col2 = st.columns([2, 1])
            update_stock_mode = opt_col1.checkbox(
                "Potong stok dus di rak otomatis setelah invoice dibuat (hapus slot jika sisa dus = 0)",
                value=True
            )

            if opt_col2.button("🧾 Buat & Tampilkan Invoice Siap Cetak", type="primary"):
                inv_items_df = pd.DataFrame(picked_items)
                grand_total = inv_items_df["Subtotal"].sum()
                now_display = datetime.now().strftime("%d %B %Y, %H:%M WIB")

                sku_summary = ", ".join([f"{x['Kode SKU']} ({x['Ambil (Dus)']} dus dari {x['Kode Rak']})" for x in picked_items])
                log_activity("AMBIL_BARANG", f"{invoice_no} untuk '{customer_name or 'Umum'}': {sku_summary}")

                if update_stock_mode:
                    for x in picked_items:
                        remaining = x["_avail_boxes"] - x["Ambil (Dus)"]
                        if remaining <= 0:
                            conn.execute("DELETE FROM bin_map WHERE jurnal_sku = ?", (x["Kode SKU"],))
                        else:
                            conn.execute("UPDATE bin_map SET box_qty = ? WHERE jurnal_sku = ?", (remaining, x["Kode SKU"]))
                    conn.commit()
                    log_activity("UPDATE_STOK_RAK", f"Stok rak diperbarui otomatis setelah picking {invoice_no}")

                rows_html = ""
                for i, item in enumerate(picked_items, start=1):
                    harga_str = f"Rp {item['Harga Satuan']:,.0f}".replace(",", ".") if item["Harga Satuan"] > 0 else "-"
                    sub_str = f"Rp {item['Subtotal']:,.0f}".replace(",", ".") if item["Subtotal"] > 0 else "-"
                    rows_html += f"""
                    <tr>
                        <td style="text-align:center;">{i}</td>
                        <td style="font-weight:bold; color:#0f172a;">{item['Kode Rak']}</td>
                        <td>{item['Kode SKU']}</td>
                        <td>{item['Nama Produk']}</td>
                        <td style="text-align:center; font-weight:bold;">{item['Ambil (Dus)']} dus</td>
                        <td style="text-align:center;">{item['Isi / Dus']}</td>
                        <td style="text-align:center;">{item['Total Pcs']}</td>
                        <td style="text-align:right;">{harga_str}</td>
                        <td style="text-align:right;">{sub_str}</td>
                        <td>{item['Keterangan']}</td>
                    </tr>
                    """

                total_str = f"Rp {grand_total:,.0f}".replace(",", ".") if grand_total > 0 else "-"

                printable_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                <meta charset="utf-8">
                <style>
                    body {{
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
                        color: #1e293b;
                        background: #ffffff;
                        padding: 24px;
                        margin: 0;
                    }}
                    .invoice-box {{
                        max-width: 880px;
                        margin: auto;
                        border: 1px solid #cbd5e1;
                        border-radius: 8px;
                        padding: 28px;
                        background: #fff;
                    }}
                    .header {{
                        display: flex;
                        justify-content: space-between;
                        align-items: flex-start;
                        border-bottom: 2px solid #0f172a;
                        padding-bottom: 14px;
                        margin-bottom: 18px;
                    }}
                    .title {{
                        font-size: 22px;
                        font-weight: 700;
                        color: #0f172a;
                        margin: 0;
                    }}
                    .subtitle {{
                        font-size: 13px;
                        color: #64748b;
                        margin-top: 4px;
                    }}
                    .meta {{
                        text-align: right;
                        font-size: 13px;
                        line-height: 1.6;
                    }}
                    .info-grid {{
                        display: flex;
                        justify-content: space-between;
                        margin-bottom: 20px;
                        font-size: 14px;
                        background: #f8fafc;
                        padding: 12px 16px;
                        border-radius: 6px;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        font-size: 12.5px;
                    }}
                    th {{
                        background: #0f172a;
                        color: #ffffff;
                        padding: 9px 7px;
                        text-align: left;
                    }}
                    td {{
                        border-bottom: 1px solid #e2e8f0;
                        padding: 8px 7px;
                    }}
                    .total-row td {{
                        font-weight: 700;
                        font-size: 13.5px;
                        border-top: 2px solid #0f172a;
                        background: #f8fafc;
                    }}
                    .signatures {{
                        display: flex;
                        justify-content: space-between;
                        margin-top: 42px;
                        text-align: center;
                        font-size: 13px;
                    }}
                    .sig-box {{
                        width: 28%;
                    }}
                    .sig-line {{
                        margin-top: 52px;
                        border-top: 1px solid #475569;
                        padding-top: 4px;
                    }}
                    .print-btn {{
                        background: #2563eb;
                        color: white;
                        border: none;
                        padding: 10px 20px;
                        font-size: 14px;
                        font-weight: 600;
                        border-radius: 6px;
                        cursor: pointer;
                        margin-bottom: 16px;
                    }}
                    .print-btn:hover {{
                        background: #1d4ed8;
                    }}
                    @media print {{
                        .no-print {{ display: none !important; }}
                        body {{ padding: 0; }}
                        .invoice-box {{ border: none; padding: 0; }}
                    }}
                </style>
                </head>
                <body>
                    <div class="no-print" style="text-align:right; max-width:880px; margin:auto;">
                        <button class="print-btn" onclick="window.print()">🖨️ Cetak Invoice / Simpan PDF</button>
                    </div>
                    <div class="invoice-box">
                        <div class="header">
                            <div>
                                <div class="title">INVOICE & SURAT JALAN PENGAMBILAN</div>
                                <div class="subtitle">Sistem Manajemen Rak Gudang (8 Baris × 6 Lantai) • Mekari Jurnal</div>
                            </div>
                            <div class="meta">
                                <strong>No. Dokumen:</strong> {invoice_no}<br>
                                <strong>Tanggal:</strong> {now_display}
                            </div>
                        </div>
                        <div class="info-grid">
                            <div>
                                <strong>Tujuan / Pelanggan:</strong><br>
                                {customer_name if customer_name.strip() else "Pengambilan Internal / Umum"}
                            </div>
                            <div style="text-align:right;">
                                <strong>Petugas Gudang (Picker):</strong><br>
                                {picker_name if picker_name.strip() else "-"}
                            </div>
                        </div>
                        <table>
                            <thead>
                                <tr>
                                    <th style="text-align:center; width:32px;">No</th>
                                    <th>Lokasi Rak</th>
                                    <th>Kode SKU</th>
                                    <th>Nama Produk</th>
                                    <th style="text-align:center;">Jumlah Dus</th>
                                    <th style="text-align:center;">Isi / Dus</th>
                                    <th style="text-align:center;">Total Pcs</th>
                                    <th style="text-align:right;">Harga / Dus</th>
                                    <th style="text-align:right;">Subtotal</th>
                                    <th>Keterangan</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rows_html}
                                <tr class="total-row">
                                    <td colspan="4" style="text-align:right;">TOTAL KESELURUHAN</td>
                                    <td style="text-align:center;">{inv_items_df['Ambil (Dus)'].sum()} dus</td>
                                    <td></td>
                                    <td></td>
                                    <td></td>
                                    <td style="text-align:right;">{total_str}</td>
                                    <td></td>
                                </tr>
                            </tbody>
                        </table>
                        <div class="signatures">
                            <div class="sig-box">
                                Dibuat Oleh,
                                <div class="sig-line">Admin Gudang</div>
                            </div>
                            <div class="sig-box">
                                Diambil Oleh,
                                <div class="sig-line">{picker_name if picker_name.strip() else "Staf Picker"}</div>
                            </div>
                            <div class="sig-box">
                                Diterima Oleh,
                                <div class="sig-line">Penerima / Kurir</div>
                            </div>
                        </div>
                    </div>
                </body>
                </html>
                """

                st.success(f"Invoice **{invoice_no}** berhasil dibuat! Klik tombol **🖨️ Cetak Invoice / Simpan PDF** di bawah ini.")
                components.html(printable_html, height=620, scrolling=True)

                export_df = inv_items_df.drop(columns=["_avail_boxes"])
                csv_invoice = export_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="⬇️ Download Data Invoice (CSV)",
                    data=csv_invoice,
                    file_name=f"{invoice_no}.csv",
                    mime="text/csv"
                )

# --- TAB 5: IMPORT DATA LAMA (EXCEL / CSV) ---
with tab5:
    st.subheader("Import Data Massal dari WMS Lama / Excel / Mekari Jurnal (Batch 10.000+ SKU)")
    st.markdown(
        "Unggah file **.xlsx** atau **.csv** dengan kolom wajib: "
        "`jurnal_sku`, `product_name`, `row_num` (1–8), dan `floor_num` (1–6). "
        "Kolom opsional: `box_qty`, `pcs_per_box`, `category`, `turnover_speed`, `notes`."
    )

    template_df = pd.DataFrame([
        {
            "jurnal_sku": "JRN-1001",
            "product_name": "Contoh Produk A",
            "category": "Kategori Umum",
            "row_num": 1,
            "floor_num": 1,
            "box_qty": 10,
            "pcs_per_box": 24,
            "turnover_speed": "Fast-Moving (Laku Cepat)",
            "notes": "Rak bawah"
        }
    ])
    csv_template = template_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="⬇️ Download Template CSV Contoh",
        data=csv_template,
        file_name="template_rak_gudang.csv",
        mime="text/csv"
    )

    uploaded_file = st.file_uploader("Pilih file Excel (.xlsx) atau CSV (.csv)", type=["csv", "xlsx"])
    if uploaded_file is not None:
        try:
            if uploaded_file.name.endswith(".csv"):
                upload_df = pd.read_csv(uploaded_file)
            else:
                upload_df = pd.read_excel(uploaded_file)

            st.write(f"Pratinjau Data (**{len(upload_df):,}** baris terdeteksi):")
            st.dataframe(upload_df.head(10), width="stretch")

            if st.button("Proses Import ke Database"):
                required_cols = {"jurnal_sku", "product_name", "row_num", "floor_num"}
                if not required_cols.issubset(set(upload_df.columns)):
                    st.error(f"Kolom wajib tidak lengkap. Pastikan ada: {', '.join(required_cols)}")
                else:
                    records_to_insert = []
                    skipped_count = 0
                    for _, row in upload_df.iterrows():
                        sku_val = str(row["jurnal_sku"]).strip()
                        if not sku_val or sku_val == "nan":
                            skipped_count += 1
                            continue
                        try:
                            r_val = int(row["row_num"])
                            f_val = int(row["floor_num"])
                            if not (1 <= r_val <= 8 and 1 <= f_val <= 6):
                                skipped_count += 1
                                continue
                        except (ValueError, TypeError):
                            skipped_count += 1
                            continue

                        name_val = str(row.get("product_name", "")).strip()
                        cat_val = str(row.get("category", "")).strip() if pd.notna(row.get("category")) else ""
                        speed_val = (
                            str(row.get("turnover_speed", "Medium (Normal)")).strip()
                            if pd.notna(row.get("turnover_speed"))
                            else "Medium (Normal)"
                        )
                        notes_val = str(row.get("notes", "")).strip() if pd.notna(row.get("notes")) else ""
                        box_val = int(row["box_qty"]) if pd.notna(row.get("box_qty")) and int(row["box_qty"]) > 0 else 1
                        pcs_val = int(row["pcs_per_box"]) if pd.notna(row.get("pcs_per_box")) and int(row["pcs_per_box"]) > 0 else None
                        bin_code = f"R{r_val:02d}-L{f_val}"

                        records_to_insert.append(
                            (sku_val, name_val, cat_val, r_val, f_val, bin_code, speed_val, notes_val, box_val, pcs_val)
                        )

                    if records_to_insert:
                        conn.executemany(
                            """INSERT OR REPLACE INTO bin_map
                               (jurnal_sku, product_name, category, row_num, floor_num, bin_location, turnover_speed, notes, box_qty, pcs_per_box)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            records_to_insert
                        )
                        conn.commit()

                    imported_count = len(records_to_insert)
                    log_activity("IMPORT_MASSAL", f"Import {imported_count} SKU dari {uploaded_file.name}")
                    st.success(
                        f"Selesai! **{imported_count:,}** SKU berhasil diimpor dalam 1 batch cepat "
                        f"({skipped_count} baris dilewati)."
                    )
        except Exception as e:
            st.error(f"Gagal membaca file: {e}")

# --- TAB 6: MEMORI & ATURAN AI (PASSCODE PROTECTED) ---
with tab6:
    st.subheader("🧠 Memori Permanen & Aturan Khusus Gudang")

    if not st.session_state.tab6_unlocked:
        st.warning("🔐 **Akses Terkunci (Supervisor Only):** Masukkan Passcode Supervisor untuk melihat atau mengubah aturan permanen & riwayat log gudang.")
        with st.form("unlock_tab6_form"):
            pin_col1, pin_col2 = st.columns([2, 1])
            tab6_pin = pin_col1.text_input("Passcode Supervisor:", type="password", placeholder="Masukkan PIN Supervisor")
            unlock_submitted = pin_col2.form_submit_button("🔓 Buka Kunci Tab 6", width="stretch")

        if unlock_submitted:
            if tab6_pin.strip() == SUPERVISOR_PIN:
                st.session_state.tab6_unlocked = True
                log_activity("BUKA_TAB6", "Supervisor membuka akses Tab 6 Memori & Aturan")
                st.rerun()
            else:
                log_activity("GAGAL_PIN_TAB6", "Percobaan membuka Tab 6 gagal (PIN salah)")
                st.error("⛔ Passcode salah! Akses ditolak.")
    else:
        top_c1, top_c2 = st.columns([3, 1])
        top_c1.caption("🔓 Terbuka untuk Supervisor • Semua catatan di sini otomatis diperiksa saat input barang maupun tanya AI.")
        if top_c2.button("🔒 Kunci Kembali Tab 6", width="stretch"):
            st.session_state.tab6_unlocked = False
            st.rerun()

        with st.form("add_memory_form", clear_on_submit=True):
            m_col1, m_col2 = st.columns([1, 3])
            mem_cat = m_col1.selectbox("Jenis Memori", [
                "Aturan Zona Rak",
                "Kondisi Fisik Rak",
                "Info Supplier / Barang",
                "SOP Staf Gudang"
            ])
            mem_text = m_col2.text_input(
                "Isi Memori / Aturan yang Harus Diingat AI:",
                placeholder="Contoh: Baris 01 hanya boleh diisi oleh raket badminton"
            )
            if st.form_submit_button("Simpan ke Memori AI"):
                if mem_text.strip():
                    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
                    conn.execute(
                        "INSERT INTO warehouse_memory (category, memory_text, created_at) VALUES (?, ?, ?)",
                        (mem_cat, mem_text.strip(), now_str)
                    )
                    conn.commit()
                    log_activity("TAMBAH_MEMORI", f"[{mem_cat}] {mem_text.strip()}")
                    st.success("Memori berhasil disimpan! Aturan zonasi 85% dan fleksibilitas 15% aktif.")
                else:
                    st.warning("Isi teks memori terlebih dahulu.")

        mem_table = pd.read_sql_query(
            "SELECT id AS 'ID', created_at AS 'Waktu', category AS 'Kategori', memory_text AS 'Isi Memori' FROM warehouse_memory ORDER BY id DESC",
            conn
        )
        st.dataframe(mem_table, width="stretch")

        if not mem_table.empty:
            del_id = st.number_input("Hapus Memori berdasarkan ID:", min_value=1, step=1)
            if st.button("Hapus Memori Ini"):
                conn.execute("DELETE FROM warehouse_memory WHERE id = ?", (int(del_id),))
                conn.commit()
                st.success(f"Memori ID #{int(del_id)} dihapus.")
                st.rerun()

        st.divider()
        st.subheader("📜 Riwayat Aktivitas Gudang (Otomatis Diingat AI)")
        log_table = pd.read_sql_query(
            "SELECT timestamp AS 'Waktu', action AS 'Aksi', details AS 'Detail' FROM activity_log ORDER BY id DESC LIMIT 30",
            conn
        )
        st.dataframe(log_table, width="stretch")