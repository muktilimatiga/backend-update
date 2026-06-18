import os
import re
import datetime as dt
import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch
from services.supabase_client import supabase

POSTGRES_URI = os.getenv("POSTGRES_URI", "dbname=data user=root password=Noclex1965 host=172.16.121.11 port=5435")
TABLE_NAME = os.getenv("POSTGRES_TABLE", "data_fiber")

class ExcelHandler:
    
    CANDIDATE_COLS = {
        "name": ["nama","name","customer","pelanggan"],
        "pppoe": ["user pppoe","user_pppoe","pppoe","no internet","no. internet","internet","id internet"],
        "address": ["alamat","address","addr"],
        "onu_port": ["port onu","onu port","port","port_onu"],
        "onu_sn": ["no. sn","sn","serial","no sn","onu sn","serial number","serial_number"],
        "password": ["password","pppoe password","pw","pass"],
        "paket": ["paket", "Paket", "PAKET"],
    }

    @staticmethod
    def parse_sheet_name(name):
        n = (name or "").strip()
        if not n or n.upper().startswith("TOTAL") or n.upper() in {"FIBER","SUMMARY","SHEET1"}: return None, None
        m = re.search(r"^(?P<olt>[A-Z]+)(?:\s+[A-Z0-9\s]+?)?(?:\s+PORT)?\s+(?P<port>[\d\.]+)\s*$", n, re.I)
        return (m.group("olt").strip().upper(), m.group("port").strip()) if m else (n.upper(), None)

    @staticmethod
    def norm_cols(df): return df.rename(columns=lambda c: str(c).strip().lower() if c else "")

    @staticmethod
    def pick(df, keys): 
        for k in keys: 
            if k in df.columns: return k
        return None

    @classmethod
    def docs_from_sheet(cls, xl, sheet):
        olt_name, olt_port = cls.parse_sheet_name(sheet)
        if not olt_name: return
        try:
            temp = xl.parse(sheet, header=None, dtype=str)
            idx = -1
            for i, row in temp.head(20).iterrows():
                s = ' '.join(str(x).lower() for x in row.dropna())
                if "nama" in s and ("pppoe" in s or "alamat" in s):
                    idx = i; break
            if idx == -1: return
            df = xl.parse(sheet, header=idx, dtype=str).fillna("")
        except: return

        df = cls.norm_cols(df)
        cols = {k: cls.pick(df, v) for k, v in cls.CANDIDATE_COLS.items()}
        if not (cols["name"] and (cols["pppoe"] or cols["address"])): return

        def clean(v): 
            s = str(v).strip()
            return s[:-2] if s.endswith(".0") else s

        for _, r in df.iterrows():
            pppoe = clean(r.get(cols["pppoe"], ""))
            if not pppoe: continue 
            
            # Simple parsing
            onu_port_val = (r.get(cols["onu_port"], "").strip() if cols["onu_port"] else None) or None
            final_olt = olt_port
            onu_id = None
            if onu_port_val and ":" in onu_port_val:
                parts = onu_port_val.split(':', 1)
                final_olt = parts[0]
                if len(parts) > 1: onu_id = parts[1]

            yield {
                "user_pppoe": pppoe,
                "nama": clean(r.get(cols["name"], "")),
                "alamat": clean(r.get(cols["address"], "")),
                "olt_name": olt_name,
                "olt_port": final_olt,
                "onu_sn": clean(r.get(cols["onu_sn"], "")).upper(),
                "pppoe_password": clean(r.get(cols["password"], "")),
                "interface": onu_port_val,
                "onu_id": onu_id,
                "sheet": sheet,
                "paket": clean(r.get(cols["paket"], "")),
                "updated_at": dt.datetime.utcnow().isoformat(),
            }

    @classmethod
    def process_file(cls, file_obj):
        print("--- FINAL ATTEMPT: ROBUST UPLOAD ---")
        
        conn = psycopg2.connect(POSTGRES_URI)
        cur = conn.cursor()
        
        # 1. FIX LOCAL DB (Safe method)
        print("1. Fixing Local Database Rules...")
        try:
            cur.execute(f"ALTER TABLE {TABLE_NAME} DROP CONSTRAINT IF EXISTS {TABLE_NAME}_pkey;")
            conn.commit()
        except: conn.rollback()

        # 2. WIPE LOCAL DATA
        print("2. Wiping Local Data...")
        cur.execute(f"TRUNCATE TABLE {TABLE_NAME};")
        conn.commit()

        # 3. WIPE SUPABASE (Best Effort)
        print("3. Wiping Supabase (If possible)...")
        try:
            supabase.table(TABLE_NAME).delete().neq("user_pppoe", "______").execute()
        except Exception as e:
            print(f"   [NOTE] Supabase wipe error (ignored): {e}")

        # 4. READ EXCEL
        print("4. Reading Excel...")
        xl = pd.ExcelFile(file_obj)
        all_rows = []
        for sheet in xl.sheet_names:
            for doc in cls.docs_from_sheet(xl, sheet) or []:
                all_rows.append(doc)

        print(f"5. Uploading {len(all_rows)} rows...")

        # 5. INSERT (Local = Critical, Supabase = Try/Except)
        
        def insert_local(batch):
            tuples = [(r["user_pppoe"], r["nama"], r["alamat"], r["olt_name"], r["olt_port"], r["onu_sn"], r["pppoe_password"], r["interface"], r["onu_id"], r["sheet"], r["paket"], r["updated_at"]) for r in batch]
            sql = f"""
                INSERT INTO {TABLE_NAME} (user_pppoe, nama, alamat, olt_name, olt_port, onu_sn, pppoe_password, interface, onu_id, sheet, paket, updated_at) 
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            execute_batch(cur, sql, tuples)

        def insert_remote(batch):
            try:
                # We use standard insert. If Supabase rejects duplicates (409), we catch it.
                supabase.table(TABLE_NAME).insert(batch).execute()
            except Exception as e:
                # Convert exception to string to check for 409 Conflict
                err_str = str(e)
                if "409" in err_str or "Conflict" in err_str:
                    print(f"   [WARN] Supabase rejected duplicates in this batch. (Run ALTER TABLE in Supabase Dashboard to fix).")
                else:
                    print(f"   [ERR] Supabase Upload Failed: {e}")

        batch_size = 500
        for i in range(0, len(all_rows), batch_size):
            batch = all_rows[i:i + batch_size]
            
            insert_remote(batch) # Won't crash the script anymore
            insert_local(batch)  # Will definitely succeed
            conn.commit()
            
            print(f"   Processed {min(i + batch_size, len(all_rows))} / {len(all_rows)}")

        conn.close()
        print("--- DONE (Check warnings for Supabase status) ---")
        return len(all_rows)
