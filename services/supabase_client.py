import sys
from pathlib import Path

# Add parent directory to sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
import asyncio
from supabase import create_client
from core import settings
import logging
import datetime as dt

# Initialize Supabase client
supabase = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)

def normalize_user_pppoe(pppoe: str) -> str:
    """Normalize user_pppoe by removing '3700' from billing format.
    
    Billing system uses format like: 101037006813
    Local system uses format like: 101006813
    This function removes '3700' to match local format.
    """
    # Remove '370' if present (billing format -> local format)
    if "370" in pppoe:
        return pppoe.replace("370", "")
    return pppoe


def search_customers(search_term: str, limit: int = 20):
    """Search customers by name, alamat, user_pppoe, or onu_sn.

    Splits the search term by spaces and matches ALL words (AND logic).
    For user_pppoe searches, also tries normalized format (removes '3700').
    """
    # Split search term into individual words and convert to uppercase
    words = search_term.strip().upper().split()

    # Start building the query
    query = supabase.table("data_fiber").select("*")

    for word in words:
        # Normalize word if it looks like a pppoe (numeric)
        normalized_word = normalize_user_pppoe(word) if word.isdigit() else word

        # Build OR conditions including onu_sn
        if word.isdigit() and normalized_word != word:
            query = query.or_(
                f"nama.ilike.%{word}%,alamat.ilike.%{word}%,"
                f"user_pppoe.ilike.%{word}%,user_pppoe.ilike.%{normalized_word}%,"
                f"onu_sn.ilike.%{word}%"
            )
        else:
            query = query.or_(
                f"nama.ilike.%{word}%,alamat.ilike.%{word}%,"
                f"user_pppoe.ilike.%{word}%,onu_sn.ilike.%{word}%"
            )

    response = query.limit(limit).execute()
    return response.data

def search_mitra(search_term: str, limit: int = 10):
    """Search Mitra """
    words = search_term.strip().upper().split()
    query = supabase.table().select("*")
    for word in words:
        query = query.or_()
    
    response = query.limit(limit).execute()
    return response.data

def search_monitoring(search_term: str, limit: int = 20):
    """Search Monitoring"""
    words = search_term.strip().upper().split()
    query = supabase.table("monitoring").select("*")
    for word in words:
        query = query.or_(f"nama.ilike.%{word}%,alamat.ilike.%{word}%,user_pppoe.ilike.%{word}%")
    response = query.limit(limit).execute()
    return response.data


async def save_customer_config(
    user_pppoe: str,
    nama: str,
    alamat: str,
    olt_name: str,
    interface: str,
    onu_sn: str,
    pppoe_password: str = None,
    paket: str = None,
) -> bool:
    """
    Save or update customer configuration to Supabase data_fiber table.
    Uses upsert on user_pppoe as the unique key.
    Returns True if successful, False otherwise.
    """
    
    try:
        # Parse interface to get olt_port and onu_id
        olt_port = None
        onu_id = None
        if interface and ":" in interface:
            parts = interface.split(":", 1)
            port_str = parts[0]
            if "_" in port_str:
                olt_port = port_str.split("_")[-1]
            elif "-" in port_str:
                olt_port = port_str.split("-")[-1]
            else:
                olt_port = port_str
            onu_id = parts[1] if len(parts) > 1 else None
        
        data = {
            "user_pppoe": user_pppoe,
            "nama": nama,
            "alamat": alamat,
            "olt_name": olt_name,
            "olt_port": olt_port,
            "onu_sn": onu_sn,
            "pppoe_password": pppoe_password,
            "interface": interface,
            "onu_id": onu_id,
            "paket": paket,
            "updated_at": dt.datetime.utcnow().isoformat(),
        }
        
        def _upsert():
            return supabase.table("data_fiber").upsert(
                data, 
                on_conflict="user_pppoe"
            ).execute()
        
        response = await asyncio.to_thread(_upsert)
        logging.info(f"[SUPABASE] Saved customer config: {user_pppoe}")
        return True
        
    except Exception as e:
        logging.error(f"[SUPABASE] Failed to save customer config: {e}")
        return False

def update_noc_data_sync(data: dict):
    """
    Sync update NOC data (like coords, etc) for a customer in data_fiber.
    Uses upsert on user_pppoe.
    """
    
    try:
        data["updated_at"] = dt.datetime.utcnow().isoformat()
        user_pppoe = data.get("user_pppoe")
        if not user_pppoe:
            raise ValueError("user_pppoe is required for update_noc_data_sync")
            
        existing = supabase.table("data_fiber").select("user_pppoe").eq("user_pppoe", user_pppoe).execute()
        if existing.data and len(existing.data) > 0:
            response = supabase.table("data_fiber").update(data).eq("user_pppoe", user_pppoe).execute()
        else:
            response = supabase.table("data_fiber").insert(data).execute()
            
        return response
    except Exception as e:
        logging.error(f"[SUPABASE] Exception in update_noc_data_sync: {e}")
        raise e

def save_billing_data_sync(data: dict):
    """
    Upsert billing customer data into data_fiber table.
    Uses upsert on user_pppoe as the unique key.
    Only writes non-None fields to avoid overwriting existing data.
    """
    try:
        data["updated_at"] = dt.datetime.utcnow().isoformat()
        user_pppoe = data.get("user_pppoe")
        if not user_pppoe:
            raise ValueError("user_pppoe is required for save_billing_data_sync")

        upsert_data = {k: v for k, v in data.items() if v is not None}

        existing = supabase.table("data_fiber").select("user_pppoe").eq("user_pppoe", user_pppoe).execute()
        if existing.data and len(existing.data) > 0:
            response = supabase.table("data_fiber").update(upsert_data).eq("user_pppoe", user_pppoe).execute()
        else:
            response = supabase.table("data_fiber").insert(upsert_data).execute()

        logging.info(f"[SUPABASE] Saved billing data for {user_pppoe}")
        return response
    except Exception as e:
        logging.error(f"[SUPABASE] Exception in save_billing_data_sync: {e}")
        raise e

# --- LIBRENMS ---
async def update_data(data: list[dict]) -> bool:
    """
    Update data from LibreNMS API into the 'librenms' table in Supabase.
    """
    
    if not data:
        return False
        
    try:
        def _upsert():
            # Uses 'port_id' as unique key for upserting port data
            return supabase.table("librenms").upsert(data, on_conflict="port_id").execute()
            
        response = await asyncio.to_thread(_upsert)
        logging.info(f"[SUPABASE] Successfully updated {len(data)} records in librenms table")
        return True
    except Exception as e:
        logging.error(f"[SUPABASE] Failed to update librenms data: {e}")
        return False

async def search_libre_data(search_term: str, limit: int = 20):
    """
    Search LibreNMS port data.
    """
    words = search_term.strip().upper().split()
    if not words:
        return []
        
    query = supabase.table("librenms").select("*")
    for word in words:
        or_conditions = [
            f"device_name.ilike.%{word}%",
            f"ifName.ilike.%{word}%",
            f"interface_name.ilike.%{word}%"
            f"vlan_id.ilike.%{word}%"
        ]
        query = query.or_(','.join(or_conditions))
        
    response = query.limit(limit).execute()
    return response.data