# services/database.py
# Database operations for fiber customer data

import os
import datetime as dt
import psycopg2
import logging
from typing import Optional, Dict, Any, List

# --- Configuration ---
POSTGRES_URI = os.getenv(
    "POSTGRES_URI",
    "dbname=data user=root password=Noclex1965 host=172.16.121.11 port=5435"
)
TABLE_NAME = os.getenv("POSTGRES_TABLE", "data_fiber")


def get_customer_by_sn(onu_sn: str) -> Optional[Dict[str, Any]]:
    """
    Lookup customer by ONU serial number.
    Returns customer dict or None if not found.
    """
    try:
        conn = psycopg2.connect(POSTGRES_URI)
        cur = conn.cursor()
        
        sql = f"""
        SELECT user_pppoe, nama, alamat, olt_name, olt_port, onu_sn,
               pppoe_password, interface, onu_id, paket
        FROM {TABLE_NAME}
        WHERE UPPER(onu_sn) = UPPER(%s)
        LIMIT 1;
        """
        
        cur.execute(sql, (onu_sn,))
        row = cur.fetchone()
        
        cur.close()
        conn.close()
        
        if not row:
            return None
            
        return {
            "user_pppoe": row[0],
            "nama": row[1],
            "alamat": row[2],
            "olt_name": row[3],
            "olt_port": row[4],
            "onu_sn": row[5],
            "pppoe_password": row[6],
            "interface": row[7],
            "onu_id": row[8],
            "paket": row[9],
        }
        
    except Exception as e:
        logging.error(f"[DB] Failed to lookup customer by SN {onu_sn}: {e}")
        return None


def get_customers_by_sns(onu_sns: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Bulk lookup customers by list of ONU serial numbers.
    Returns dict: { sn: customer_data }
    """
    if not onu_sns:
        return {}
        
    try:
        conn = psycopg2.connect(POSTGRES_URI)
        cur = conn.cursor()
        
        # Create placeholders for IN clause
        placeholders = ','.join(['%s'] * len(onu_sns))
        sql = f"""
        SELECT user_pppoe, nama, alamat, olt_name, olt_port, onu_sn,
               pppoe_password, interface, onu_id, paket
        FROM {TABLE_NAME}
        WHERE UPPER(onu_sn) IN ({placeholders});
        """
        
        cur.execute(sql, [sn.upper() for sn in onu_sns])
        rows = cur.fetchall()
        
        cur.close()
        conn.close()
        
        result = {}
        for row in rows:
            sn = row[5].upper() if row[5] else ""
            result[sn] = {
                "user_pppoe": row[0],
                "nama": row[1],
                "alamat": row[2],
                "olt_name": row[3],
                "olt_port": row[4],
                "onu_sn": row[5],
                "pppoe_password": row[6],
                "interface": row[7],
                "onu_id": row[8],
                "paket": row[9],
            }
        
        return result
        
    except Exception as e:
        logging.error(f"[DB] Failed to bulk lookup customers: {e}")
        return {}


def fetch_paket_from_billing(user_pppoe: str) -> Optional[str]:
    """
    Fetch paket from billing system by user_pppoe.
    Used when paket is missing from database.
    """
    try:
        from services.biling_scaper import BillingScraper
        from core import settings
        
        scraper = BillingScraper()
        customers = scraper.search(user_pppoe)
        
        if not customers:
            return None
        
        # Get the first matching customer
        customer = customers[0]
        cid = customer.get("id")
        if not cid:
            return None
        
        # Get invoice/detail data which contains paket
        detail_url = settings.DETAIL_URL_BILLING.format(cid)
        invoice_data = scraper.get_invoice_data(detail_url)
        
        paket = invoice_data.get("paket")
        if paket:
            logging.info(f"[BILLING] Found paket '{paket}' for {user_pppoe}")
        return paket
        
    except Exception as e:
        logging.error(f"[BILLING] Failed to fetch paket for {user_pppoe}: {e}")
        return None


def save_customer_config(
    user_pppoe: str,
    nama: str,
    alamat: str,
    olt_name: str,
    interface: str,
    onu_sn: str,
    pppoe_password: Optional[str] = None,
    paket: Optional[str] = None,
) -> bool:
    """
    Save or update customer configuration to data_fiber table.
    Called after successful OLT configuration.
    
    Returns True if successful, False otherwise.
    """
    try:
        conn = psycopg2.connect(POSTGRES_URI)
        cur = conn.cursor()
        
        # Parse interface to get olt_port and onu_id
        olt_port = None
        onu_id = None
        if interface and ":" in interface:
            parts = interface.split(":", 1)
            olt_port = parts[0]
            onu_id = parts[1] if len(parts) > 1 else None
        
        sql = f"""
        INSERT INTO {TABLE_NAME} (
            user_pppoe, nama, alamat, olt_name, olt_port, onu_sn,
            pppoe_password, interface, onu_id, paket, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_pppoe)
        DO UPDATE SET
            nama = EXCLUDED.nama,
            alamat = EXCLUDED.alamat,
            olt_name = EXCLUDED.olt_name,
            olt_port = EXCLUDED.olt_port,
            onu_sn = EXCLUDED.onu_sn,
            pppoe_password = EXCLUDED.pppoe_password,
            interface = EXCLUDED.interface,
            onu_id = EXCLUDED.onu_id,
            paket = EXCLUDED.paket,
            updated_at = EXCLUDED.updated_at;
        """
        
        cur.execute(sql, (
            user_pppoe,
            nama,
            alamat,
            olt_name,
            olt_port,
            onu_sn,
            pppoe_password,
            interface,
            onu_id,
            paket,
            dt.datetime.utcnow()
        ))
        
        conn.commit()
        cur.close()
        conn.close()
        
        logging.info(f"[DB] Saved customer config: {user_pppoe} to {TABLE_NAME}")
        return True
        
    except Exception as e:
        logging.error(f"[DB] Failed to save customer config: {e}")
        return False


async def save_customer_config_async(
    user_pppoe: str,
    nama: str,
    alamat: str,
    olt_name: str,
    interface: str,
    onu_sn: str,
    pppoe_password: Optional[str] = None,
    paket: Optional[str] = None,
) -> bool:
    """Async wrapper for save_customer_config."""
    import asyncio
    return await asyncio.to_thread(
        save_customer_config,
        user_pppoe, nama, alamat, olt_name, interface, onu_sn,
        pppoe_password, paket
    )
