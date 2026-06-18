#schemas/config.py

from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class UnconfiguredOnt(BaseModel):
    sn: str
    pon_port: str
    pon_slot: str
    olt_name: Optional[str] = None

class CustomerInfo(BaseModel):
    name: str
    address: str
    pppoe_user: str
    pppoe_pass: str

class CustomerData(BaseModel):
    name: str
    address: Optional[str] = None
    pppoe_user: Optional[str] = None
    pppoe_password: Optional[str] = None
    olt_name: Optional[str] = None
    interface: Optional[str] = None
    onu_sn: Optional[str] = None
    modem_type: Optional[str] = None
    
class ConfigurationRequest(BaseModel):
    sn: str
    customer: CustomerInfo
    package: str
    modem_type: str
    eth_locks: List[bool]

class ConfigurationSummary(BaseModel):
    status: str
    message: str
    serial_number: str
    name: str
    pppoe_user: str
    location: str
    profile: str
    report: str

class ConfigurationResponse(BaseModel):
    message: str
    summary: Optional[ConfigurationSummary] = None
    logs: List[str]

class OptionsResponse(BaseModel):
    olt_options: List[str]
    modem_options: List[str]
    package_options: List[str]

class ConfigurationBridgeRequest(BaseModel):
    sn: str
    customer: CustomerInfo
    modem_type: str
    package: str
    vlan: str


class CongigurationBridgeResponse(BaseModel):
    olt_name: str
    modem_options: str
    package_options: str

# --- SCHEMA BARU UNTUK ONU DETAIL ---

class OnuLogEntry(BaseModel):
    """Mewakili satu baris log Authpass/Offline time dari ONU."""
    id: int
    auth_time: str
    offline_time: str
    cause: str

class OnuDetail(BaseModel):
    """
    Menggabungkan field yang diekstrak dari 'sh gpon onu detail-info'
    dan dua log modem terakhir.
    """
    # Field utama dari parsing
    onu_interface: Optional[str] = None
    type: Optional[str] = None
    phase_state: Optional[str] = None
    serial_number: Optional[str] = None
    onu_distance: Optional[str] = None
    online_duration: Optional[str] = None
    
    # Log modem terakhir
    modem_logs: List[OnuLogEntry] = []

class BatchConfigurationRequest(BaseModel):
    items: List[ConfigurationRequest]

# Output: Status for a single item in the batch
class BatchItemResult(BaseModel):
    # It helps if your ConfigurationRequest has an ID or unique field (like sn or username)
    # to identify which result belongs to which request.
    identifier: str 
    success: bool
    message: str
    logs: List[str]

# Output: The final response for the whole batch
class BatchConfigurationResponse(BaseModel):
    total: int
    success_count: int
    fail_count: int
    results: List[BatchItemResult]


# --- RECONFIG SCHEMAS ---
# For reconfiguring ONTs that lost their config (using database lookup)

class ReconfigRequest(BaseModel):
    """Request to reconfig ONTs by SN list (lookup from database)."""
    sn_list: List[str]  # List of SNs to reconfigure
    default_paket: str = "10M"  # Default package if not in database
    modem_type: str = "F609"    # Default modem type
    eth_locks: List[bool] = [False, True, True, True]  # ETH port lock config


class ReconfigItemResult(BaseModel):
    """Result for a single ONT reconfig attempt."""
    sn: str
    user_pppoe: Optional[str] = None
    status: str  # "success", "error", "skipped", "not_found"
    message: str
    logs: List[str] = []


class ReconfigResponse(BaseModel):
    """Response for reconfig endpoint."""
    total_unconfigured: int
    found_in_db: int
    not_in_db: int
    configured: int
    failed: int
    skipped: int
    results: List[ReconfigItemResult]
