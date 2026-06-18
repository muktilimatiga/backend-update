from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
import re

from core import settings, get_olt_info, get_switch_connection
from services.connection_manager import olt_manager, switch_manager
from services.switch_telnet import SwitchClient
from schemas.bot_api import MonitoringRequest

router = APIRouter()

def _parse_dying_state(result: str) -> str:
    """
    Parse GPON ONU state output and count DyingGasp occurrences.
    
    Example line: 1/2/1:11    enable       disable     DyingGasp    1(GPON)
    
    Returns: Summary string like "DyingGasp: 65 ONU"
    """
    dying_count = 0
    los_count = 0
    offline_count = 0
    working_count = 0
    
    for line in result.splitlines():
        # Match lines with ONU data (format: interface  admin  omcc  phase  channel)
        if re.search(r"^\s*\d+/\d+/\d+:\d+", line):
            if "DyingGasp" in line:
                dying_count += 1
            elif "LOS" in line:
                los_count += 1
            elif "OffLine" in line:
                offline_count += 1
            elif "working" in line:
                working_count += 1
    
    total = dying_count + los_count + offline_count + working_count
    
    return (
        f"Total ONU: {total}\n"
        f"Working: {working_count}\n"
        f"DyingGasp: {dying_count}\n"
        f"LOS: {los_count}\n"
        f"OffLine: {offline_count}"
    )

@router.post("/cek", response_class=PlainTextResponse)
async def cek_monitoring(olt_name: str, request: MonitoringRequest):
    olt_info = get_olt_info(olt_name)
    if not olt_info:
        raise HTTPException(status_code=404, detail="OLT not found")

    try:
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"]
        )
        result = await handler.get_olt_monitoring(request.interface)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/redaman-monitoring", response_class=PlainTextResponse)
async def redaman_monitoring(olt_name: str, request: MonitoringRequest):
    olt_info = get_olt_info(olt_name)
    if not olt_info:
        raise HTTPException(status_code=404, detail="OLT not found")
    try:
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"]
        )
        result = await handler.get_rx_monitoring(request.interface)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/cek-dying", response_class=PlainTextResponse)
async def cek_dying(olt_name: str):
    olt_info = get_olt_info(olt_name)
    if not olt_info:
        raise HTTPException(status_code=404, detail="OLT not found")
    try:
        handler = await olt_manager.get_connection(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info["c600"]
        )
        result = await handler.get_olt_state()
        # Parse and return the count summary
        return _parse_dying_state(result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.post("/switch-description", response_class=PlainTextResponse)
async def switch_description(ip: str):
    switch_info = get_switch_connection(ip)
    if not switch_info:
        raise HTTPException(status_code=404, detail="Switch not found")
    try:
        handler = await switch_manager.get_connection(
            host=switch_info["ip"],
            username=switch_info["username"],
            password=switch_info["password"],
            is_huawei=switch_info["is_huawei"],
            is_ruijie=switch_info["is_ruijie"]
        )
        result = await handler.get_full_status()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))