from pydantic import BaseModel, Field
from typing import Optional, List

# =================================================================
# 1. INPUT PAYLOADS 
# (What your Frontend sends to FastAPI)
# =================================================================

class OltBasePayload(BaseModel):
    """
    Base payload. The Frontend sends the OLT Name (e.g., 'OLT-Surabaya-01'),
    and FastAPI looks up the IP/User/Pass in the backend config.
    """
    olt_name: str = Field(..., description="The name of the OLT as defined in backend config")

class OnuTargetPayload(OltBasePayload):
    """
    Used for actions that target a specific ONU Interface.
    Endpoints: /detail-search, /reboot-onu
    """
    interface: str = Field(..., description="Full interface string (e.g., '1/2/3:4')")

class PortTargetPayload(OltBasePayload):
    """
    Used for actions that target a PON Port.
    Endpoints: /onu-state, /onu-rx
    """
    olt_port: str = Field(..., description="PON port string (e.g., '1/2/3')")

class RegistSnPayload(OnuTargetPayload):
    """
    Used for registering a new SN to an existing interface.
    Endpoint: /regist-sn
    """
    sn: str = Field(..., description="The new Serial Number to register")

class NoOnuPayload(PortTargetPayload):
    """
    Used for removing an ONU. Requires Port + ONU ID.
    Endpoint: /no-onu
    """
    onu_id: int = Field(..., description="The ONU ID to remove (e.g., 4)")


# =================================================================
# 2. RESPONSE MODELS
# (What FastAPI sends back to the Frontend)
# =================================================================

class EthPortStatus(BaseModel):
    interface: str
    is_unlocked: bool

class OnuDetailRequest(BaseModel):
    interface: str
    olt_name: str

class OnuDetailResponse(BaseModel):
    result: str

class OnuDbaResponse(BaseModel):
    result: float

class OnuFullResponse(BaseModel):
    detail_data: str
    attenuation_data: str
    
class CustomerOnuDetail(BaseModel):
    """
    The main dashboard data response.
    """
    # Basic Info
    type: Optional[str] = None
    phase_state: Optional[str] = None
    serial_number: Optional[str] = None
    onu_distance: Optional[str] = None
    online_duration: Optional[str] = None
    
    # Logs
    modem_logs: Optional[str] = None
    
    # Real-time Metrics
    redaman: str = "N/A"         # Attenuation
    ip_remote: str = "0.0.0.0"   # WAN IP
    
    # List of Ethernet Port Statuses
    eth_port: List[EthPortStatus] = []

class OnuStateRespons(BaseModel):
    onu_state_data: str

class OnuRxRespons(BaseModel):
    onu_rx_data: str

class RebootResponse(BaseModel):
    status: str

class NoOnuResponse(BaseModel):
    status: str

class RegistSnResponse(BaseModel):
    status: str

class ErrorResponse(BaseModel):
    detail: str

class LockEthRequest(BaseModel):
    interface: str
    olt_name: str
    is_unlocked: bool

class LockEthResponse(BaseModel):
    status: str

class EditCapacityRequest(BaseModel):
    interface: str
    olt_name: str
    new_capacity: str

class EditCapacityResponse(BaseModel):
    status: str