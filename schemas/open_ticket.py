from . import BaseModel, Optional, Field, List, Dict
from typing import Any

# ==========================================
# 1. PAYLOADS (INPUTS)
# ==========================================


class TicketCreateOnlyPayload(BaseModel):
    query: str
    description: str
    priority: str = "LOW"
    jenis: str = "FREE"

class TicketCreateAndProcessPayload(BaseModel):
    query: str
    description: str
    priority: str = "LOW"
    jenis: str = "FREE"
    noc_username: str
    noc_password: str

class TicketProcessPayload(BaseModel):
    query: str
    noc_username: str
    noc_password: str

class TicketClosePayload(BaseModel):
    query: str
    close_reason: str
    onu_sn: str
    noc_username: str
    noc_password: str

class TicketForwardPayload(BaseModel):
    query: str
    service_impact: str
    root_cause: str
    network_impact: str
    recomended_action: str
    onu_index: str
    sn_modem: str
    priority: str = "HIGH"
    person_in_charge: str = "ALL TECHNICIANS"
    noc_username: str
    noc_password: str
    
class SearchPayload(BaseModel):
    query: str

# ==========================================
# 2. RESPONSES (OUTPUTS)
# ==========================================

# Unified Response for ALL Actions (Create, Process, Close, Forward)
class TicketOperationResponse(BaseModel):
    success: bool
    message: str
    creation_result: Optional[str] = None
    processing_result: Optional[str] = None

# Specific Response for Search (Returns Data)
class SearchResponse(BaseModel):
    query: str
    results: List[Dict[str, Any]]