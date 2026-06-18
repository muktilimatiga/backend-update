from pydantic import BaseModel

class MonitoringRequest(BaseModel):
    interface: str

class MonitoringResponse(BaseModel):
    status: str
    redaman: str