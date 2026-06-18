from . import BaseModel, List

class TerminalResponse(BaseModel):
    port: int
    pid: int
    command: str
    message: str

class StopResponse(BaseModel):
    port: int
    pid: int
    message: str

class ListResponse(BaseModel):
    count: int
    running_ports: List[int]