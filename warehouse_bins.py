import sqlite3
from datetime import datetime
import pandas as pd
import streamlit as st
from openai import OpenAI

# --- 1. LOCAL AI CONNECTION SETUP (LM Studio Port 1234 -> Jan AI Port 1337 Fallback) ---
def get_ai_client_and_model() -> tuple[OpenAI, str]:
    """Connects to LM Studio (1234) first, falling back to Jan AI (1337)."""
    for port in (1234, 1337):
        client = OpenAI(base_url=f"http://127.0.0.1:{port}/v1", api_key="not-needed")
        try:
            models = client.models.list()
            if models.data:
                return client, models.data[0].id
        except Exception:
            continue
    return OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="not-needed"), "local-model"

# --- 2. DATABASE SETUP (SQLite + Persistent AI Memory) ---
conn = sqlite3.connect("jurnal_bins.db", check_same_thread=False)
conn.execute("""
    CREATE TABLE IF NOT EXISTS bin_map (
        jurnal_sku TEXT PRIMARY KEY,
        product_name TEXT,
        category TEXT,
        row_num INTEGER,
        floor_num INTEGER,
        bin_location TEXT,
        turnover_speed TEXT,
        notes TEXT
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

def get_memory_context() -> str:
    """Fetches saved warehouse rules & recent logs to feed into the AI."""
    mem_df = pd.read_sql_query(
        "SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC",
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

def validate_placement_against_rules(
    sku: str, name: str, cat: str, row_num: int, floor_num: int, speed: str, notes: str
) -> tuple[bool, str]:
    """Enforces 85% strict floor turnover rules and custom row memory rules."""
    speed_low = speed.lower()

    # 1. Strict Floor-Turnover Rules (85% Enforcement)
    if "fast-moving" in speed_low:
        if floor_num not in (1, 2):
            return False, f"Barang Fast-Moving wajib ditaruh di Lantai 1 atau 2 (Anda memilih Lantai {floor_num})."
    elif "medium" in speed_low:
        if floor_num not in (3, 4):
            return False, f"Barang Medium wajib ditaruh di Lantai 3 atau 4 (Anda memilih Lantai {floor_num})."
    elif "slow-moving" in speed_low or "berat" in speed_low or "bulky" in speed_low:
        if floor_num not in (5, 6):
            return False, f"Barang Slow-Moving / Bulky wajib ditaruh di Lantai 5 atau 6 (Anda memilih Lantai {floor_num})."

    # 2. Check custom row rules in warehouse_memory
    mem_df = pd.read_sql_query(
        "SELECT category, memory_text FROM warehouse_memory ORDER BY id DESC",
        conn
    )
    if mem_df.empty:
        return True, ""

    item_blob = f"{sku} {name} {cat} {notes}".lower()
    bin_code = f"R{row_num:02d}-L{floor_num}"

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

# --- 3. PAGE HEADER & TABS ---
st.set_page_config(page_title="Sistem Rak Gudang 8x6 - Jurnal AI", layout="wide")
st.title("📦 Sistem Lokasi Rak Gudang (8 Baris × 6 Lantai)")
st.caption("Zonasi 85% Ketat • 15% Fleksibel dengan Supervisor Override")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "1. Input Lokasi Rak SKU",
    "2. Peta Visual Rak (8×6)",
    "3. Tanya AI (Dengan Memori)",
    "4. Prediksi Rak Barang Masuk",
    "5. Import Excel / CSV",
    "6. Memori & Aturan AI"
])

# --- TAB 1: INPUT & PEMETAAN RAK ---
with tab1:
    st.subheader("Hubungkan Kode Produk Mekari Jurnal ke Rak (Baris 1–8, Lantai 1–6)")
    st.info("🔒 **Sistem 85% Ketat (Zonasi Aktif):** Pelanggaran zonasi diblokir otomatis. Gunakan *15% Fleksibel (Override)* jika ada kebutuhan operasional khusus.")
    
    with st.form("add_sku_form", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        sku = col1.text_input("Kode Produk Jurnal (SKU)", placeholder="Contoh: JRN-1042")
        name = col2.text_input("Nama Produk (sesuai Jurnal)", placeholder="Contoh: Raket Badminton Yonex")
        cat = col3.text_input("Kategori Barang", placeholder="Contoh: Olahraga / Raket")

        col4, col5, col6, col7 = st.columns([1, 1, 1.5, 2])
        row_num = col4.selectbox("Baris (Row)", list(range(1, 9)), format_func=lambda x: f"Baris {x:02d}")
        floor_num = col5.selectbox(
            "Lantai / Tingkat",
            list(range(1, 7)),
            format_func=lambda x: f"Lantai {x} {'(Bawah)' if x == 1 else '(Atas)' if x == 6 else ''}"
        )
        speed = col6.selectbox("Kecepatan Perputaran", [
            "Fast-Moving (Laku Cepat)",
            "Medium (Normal)",
            "Slow-Moving (Lambat)",
            "Berat / Besar (Bulky)"
        ])
        notes = col7.text_input("Catatan Fisik", placeholder="Contoh: Dus besar, butuh tangga jika lantai 5-6")

        st.divider()
        flex_expander = st.expander("🔓 15% Fleksibel: Opsi Pengecualian / Override Supervisor")
        with flex_expander:
            allow_override = st.checkbox("Izinkan Penempatan di Luar Aturan Zonasi (Pengecualian)")
            override_reason = st.text_input("Alasan Pengecualian (Wajib diisi jika dicentang):", placeholder="Contoh: Rak bawah penuh, titip sementara 1 hari")

        if st.form_submit_button("Simpan Lokasi Rak"):
            if not sku.strip():
                st.warning("Harap isi Kode Produk Jurnal (SKU).")
            else:
                bin_code = f"R{row_num:02d}-L{floor_num}"
                is_allowed, block_reason = validate_placement_against_rules(
                    sku.strip(), name.strip(), cat.strip(), row_num, floor_num, speed, notes.strip()
                )

                if not is_allowed and not allow_override:
                    log_activity("DITOLAK_ZONASI", f"{sku.strip()} ({name.strip()}) ke {bin_code}: {block_reason}")
                    st.error(
                        f"⛔ **Penempatan Ditolak (85% Aturan Ketat):** {block_reason}\n\n"
                        "💡 *Tip fleksibilitas (15%):* Jika ini adalah kasus darurat/pengecualian, buka expander **15% Fleksibel** di atas dan isi alasan override."
                    )
                else:
                    if not is_allowed and allow_override:
                        if not override_reason.strip():
                            st.warning("⚠️ Anda mencentang override, tetapi **Alasan Pengecualian** belum diisi!")
                            st.stop()
                        log_activity("OVERRIDE_ZONASI", f"OVERRIDE disetujui untuk {sku.strip()} ke {bin_code}. Alasan: {override_reason}")
                        st.warning(f"⚠️ **Pengecualian 15% Diterapkan (Override):** Menyimpan ke {bin_code} meskipun melanggar zonasi. Alasan tercatat: *{override_reason}*")

                    conn.execute(
                        "INSERT OR REPLACE INTO bin_map VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (sku.strip(), name.strip(), cat.strip(), row_num, floor_num, bin_code, speed, notes.strip())
                    )
                    conn.commit()
                    if is_allowed:
                        log_activity("SIMPAN_RAK", f"{sku.strip()} ({name.strip()}) -> {bin_code}")
                    st.success(f"Berhasil menyimpan: {sku.strip()} → **{bin_code}** (Baris {row_num}, Lantai {floor_num})")

    df_bins = pd.read_sql_query("""
        SELECT 
            bin_location AS "Kode Rak",
            jurnal_sku AS "Kode SKU Jurnal",
            product_name AS "Nama Produk",
            category AS "Kategori",
            turnover_speed AS "Perputaran",
            notes AS "Catatan"
        FROM bin_map 
        ORDER BY row_num, floor_num
    """, conn)
    st.dataframe(df_bins, width="stretch")

    if not df_bins.empty:
        del_col1, del_col2 = st.columns([2, 1])
        sku_to_delete = del_col1.selectbox(
            "Hapus SKU dari Rak (jika salah input atau stok habis):",
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
    raw_df = pd.read_sql_query("SELECT row_num, floor_num, jurnal_sku, product_name FROM bin_map", conn)
    
    grid_data = {}
    for r in range(1, 9):
        col_label = f"Baris {r:02d}"
        grid_data[col_label] = []
        for f in range(6, 0, -1):
            matches = raw_df[(raw_df["row_num"] == r) & (raw_df["floor_num"] == f)]
            if matches.empty:
                grid_data[col_label].append(f"[Kosong] R{r:02d}-L{f}")
            else:
                items = ", ".join(matches["jurnal_sku"].tolist())
                grid_data[col_label].append(f"📦 {items}")

    grid_df = pd.DataFrame(grid_data, index=[f"Lantai {f}" for f in range(6, 0, -1)])
    st.dataframe(grid_df, width="stretch", height=260)

# --- TAB 3: TANYA AI (CHAT + MEMORI PERSISTEN) ---
with tab3:
    st.subheader("Asisten Gudang AI (Mengingat Aturan, Riwayat & Percakapan)")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    col_reset, _ = st.columns([1, 4])
    if col_reset.button("🧹 Reset Obrolan Sesi Ini"):
        st.session_state.chat_history = []
        st.rerun()

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    user_prompt = st.chat_input("Tanya lokasi barang, riwayat pindah rak, atau ketik 'Ingat: ...'")
    if user_prompt:
        st.session_state.chat_history.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.markdown(user_prompt)

        lower_prompt = user_prompt.strip().lower()
        if lower_prompt.startswith(("ingat:", "ingat bahwa", "catat:")):
            clean_note = user_prompt.split(":", 1)[-1].strip() if ":" in user_prompt else user_prompt.strip()
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
            inventory_table = df_bins.to_csv(index=False) if not df_bins.empty else "Belum ada data barang."
            memory_block = get_memory_context()

            system_msg = f"""Kamu adalah asisten kepala gudang untuk rak 8 Baris (R01-R08) dan 6 Lantai (L1-L6).
Zonasi wajib: Fast-Moving (Lantai 1-2), Medium (Lantai 3-4), Slow-Moving/Bulky (Lantai 5-6).
Jawab singkat dan akurat dalam Bahasa Indonesia berdasarkan data rak, aturan zonasi, dan memori gudang.

{memory_block}

DATA RAK SAAT INI:
{inventory_table}"""

            recent_messages = [{"role": "system", "content": system_msg}] + st.session_state.chat_history[-6:]

            with st.chat_message("assistant"):
                with st.spinner("Memeriksa database & memori gudang..."):
                    try:
                        client, active_model = get_ai_client_and_model()
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
                        st.error(f"Gagal terhubung ke Local AI Server: {e}")

# --- TAB 4: PREDIKSI PENEMPATAN RAK BARANG MASUK ---
with tab4:
    st.subheader("Prediksi Rak Terbaik untuk Barang Baru Masuk")
    new_item = st.text_input(
        "Deskripsi & Jumlah Barang Masuk:",
        placeholder="Contoh: 15 dus meja ping pong lipat, berat total 300kg"
    )
    if st.button("Prediksi Lokasi Rak"):
        if not new_item.strip():
            st.warning("Isi deskripsi barang masuk terlebih dahulu.")
        else:
            inventory_table = (
                df_bins.to_csv(index=False)
                if not df_bins.empty
                else "Semua 48 slot rak masih kosong."
            )
            memory_block = get_memory_context()

            slotting_prompt = f"""Kamu adalah perencana rak gudang (8 Baris R01-R08, 6 Lantai L1-L6).
Zonasi wajib:
- Fast-Moving: Lantai 1-2 (L1-L2)
- Medium: Lantai 3-4 (L3-L4)
- Slow-Moving / Bulky / Berat: Lantai 5-6 (L5-L6)

Patuhi zonasi ini dan ATURAN GUDANG di bawah ini. Pilih 1 Kode Rak kosong terbaik (format R0X-LX) sesuai perputarannya dan beri 2 alasan singkat dalam Bahasa Indonesia.

{memory_block}

Rak terisi saat ini:
{inventory_table}"""

            with st.spinner("Menghitung slot terbaik berdasarkan zonasi..."):
                try:
                    client, active_model = get_ai_client_and_model()
                    res = client.chat.completions.create(
                        model=active_model,
                        temperature=0.2,
                        max_tokens=300,
                        messages=[
                            {"role": "system", "content": slotting_prompt},
                            {"role": "user", "content": new_item}
                        ]
                    )
                    st.success(res.choices[0].message.content)
                except Exception as e:
                    st.error(f"Gagal terhubung ke Local AI Server: {e}")

# --- TAB 5: IMPORT DATA LAMA (EXCEL / CSV) ---
with tab5:
    st.subheader("Import Data Massal dari WMS Lama / Excel / Mekari Jurnal")
    st.markdown(
        "Unggah file **.xlsx** atau **.csv** dengan kolom wajib: "
        "`jurnal_sku`, `product_name`, `row_num` (1–8), dan `floor_num` (1–6). "
        "Kolom opsional: `category`, `turnover_speed`, `notes`."
    )

    template_df = pd.DataFrame([
        {
            "jurnal_sku": "JRN-1001",
            "product_name": "Contoh Produk A",
            "category": "Kategori Umum",
            "row_num": 1,
            "floor_num": 1,
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

            st.write("Pratinjau Data yang Akan Diimpor:")
            st.dataframe(upload_df.head(10), width="stretch")

            if st.button("Proses Import ke Database"):
                required_cols = {"jurnal_sku", "product_name", "row_num", "floor_num"}
                if not required_cols.issubset(set(upload_df.columns)):
                    st.error(f"Kolom wajib tidak lengkap. Pastikan ada: {', '.join(required_cols)}")
                else:
                    imported_count = 0
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
                        bin_code = f"R{r_val:02d}-L{f_val}"

                        conn.execute(
                            "INSERT OR REPLACE INTO bin_map VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (sku_val, name_val, cat_val, r_val, f_val, bin_code, speed_val, notes_val)
                        )
                        imported_count += 1

                    conn.commit()
                    log_activity("IMPORT_MASSAL", f"Import {imported_count} SKU dari {uploaded_file.name}")
                    st.success(
                        f"Selesai! **{imported_count}** SKU berhasil diimpor ke database "
                        f"({skipped_count} baris dilewati)."
                    )
        except Exception as e:
            st.error(f"Gagal membaca file: {e}")

# --- TAB 6: MEMORI & ATURAN AI ---
with tab6:
    st.subheader("🧠 Memori Permanen & Aturan Khusus Gudang")
    st.caption("Semua catatan di sini disimpan di SQLite dan otomatis diperiksa saat input barang maupun tanya AI.")

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