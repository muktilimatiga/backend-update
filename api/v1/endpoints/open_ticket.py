import asyncio
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import settings
from services.open_ticket import (
    process_ticket_as_noc,
    close_ticket_as_noc,
    forward_ticket_as_noc,
    extract_search_results,
    build_driver,
    maybe_login,
    search_user,
)
from services.playwright import CustomerService
from schemas.open_ticket import (
    TicketCreateOnlyPayload,
    TicketCreateAndProcessPayload,
    TicketProcessPayload,
    TicketClosePayload,
    TicketForwardPayload,
    SearchPayload,
    TicketOperationResponse,
    SearchResponse,
)

router = APIRouter()


# Async Wrapper for HTTP-only ticket creation
# In api/v1/endpoints/open_ticket.py

async def run_creation_async(query, desc, prio, jenis):
    def _create():
        service = CustomerService()
        try:
            # You must start the browser before calling anything else
            service.start(headless=True) 
            
            result = service.create_ticket(query, desc, prio, jenis)
            if result:
                return f"OK: Ticket created for {query}"
            else:
                return f"FAILED: Could not create ticket for {query}"
        finally:
            # Always ensure the browser is closed when done, even if it fails
            service.close()

    return await asyncio.to_thread(_create)

async def run_processing_async(noc_user, noc_pass, query, headless=True):
    return await asyncio.to_thread(
        process_ticket_as_noc,
        noc_username=noc_user,
        noc_password=noc_pass,
        query=query,
        headless=headless,
    )


async def run_ticket_close_async(
    noc_user, noc_pass, query, onu_sn, close_notes, headless=True
):
    return await asyncio.to_thread(
        close_ticket_as_noc,
        noc_username=noc_user,
        noc_password=noc_pass,
        query=query,
        onu_sn=onu_sn,
        action_close_notes=close_notes,
        headless=headless,
    )


async def run_ticket_forward_async(noc_user, noc_pass, query, **kwargs):
    return await asyncio.to_thread(
        forward_ticket_as_noc,
        noc_username=noc_user,
        noc_password=noc_pass,
        ticket_page_url=settings.TICKET_NOC_URL,
        query=query,
        headless=True,
        **kwargs,
    )


# --- A. CREATE ONLY ---
@router.post("/create", response_model=TicketOperationResponse)
async def create_ticket_only(payload: TicketCreateOnlyPayload):
    creation_msg = await run_creation_async(
        query=payload.query,
        desc=payload.description,
        prio=payload.priority,
        jenis=payload.jenis,
    )

    if not creation_msg.startswith("OK:"):
        raise HTTPException(status_code=400, detail=creation_msg)

    return {
        "success": True,
        "message": "Ticket created successfully.",
        "creation_result": creation_msg,
    }


# --- B. CREATE AND PROCESS ---
@router.post("/create-and-process", response_model=TicketOperationResponse)
async def create_and_process_ticket(payload: TicketCreateAndProcessPayload):
    # 1. Create (CS) - HTTP only
    creation_msg = await run_creation_async(
        query=payload.query,
        desc=payload.description,
        prio=payload.priority,
        jenis=payload.jenis,
    )

    if not creation_msg.startswith("OK:"):
        raise HTTPException(
            status_code=400, detail=f"Creation Phase Failed: {creation_msg}"
        )

    # 2. Process (NOC)
    processing_msg = await run_processing_async(
        noc_user=payload.noc_username,
        noc_pass=payload.noc_password,
        query=payload.query,
    )

    if not processing_msg.startswith("OK:"):
        return {
            "success": True,
            "message": "Ticket created, but NOC processing failed.",
            "creation_result": creation_msg,
            "processing_result": processing_msg,
        }

    return {
        "success": True,
        "message": "Ticket created and processed successfully.",
        "creation_result": creation_msg,
        "processing_result": processing_msg,
    }


# --- C. PROCESS ONLY ---
@router.post("/process", response_model=TicketOperationResponse)
async def process_ticket_only(payload: TicketProcessPayload):
    result = await run_processing_async(
        noc_user=payload.noc_username,
        noc_pass=payload.noc_password,
        query=payload.query,
    )
    if not result.startswith("OK:"):
        raise HTTPException(status_code=400, detail=result)

    return {
        "success": True,
        "message": "Ticket processed successfully.",
        "processing_result": result,
    }


# --- D. CLOSE TICKET ---
@router.post("/close", response_model=TicketOperationResponse)
async def close_ticket(payload: TicketClosePayload):
    result = await run_ticket_close_async(
        noc_user=payload.noc_username,
        noc_pass=payload.noc_password,
        query=payload.query,
        onu_sn=payload.onu_sn,
        close_notes=payload.close_reason,
    )
    if not result.startswith("OK:"):
        raise HTTPException(status_code=400, detail=result)

    return {"success": True, "message": result}


# --- E. FORWARD TICKET ---
@router.post("/forward", response_model=TicketOperationResponse)
async def forward_ticket(payload: TicketForwardPayload):
    result = await run_ticket_forward_async(
        noc_user=payload.noc_username,
        noc_pass=payload.noc_password,
        query=payload.query,
        service_impact=payload.service_impact,
        root_cause=payload.root_cause,
        network_impact=payload.network_impact,
        onu_index=payload.onu_index,
        sn_modem=payload.sn_modem,
        priority=payload.priority,
        person_in_charge=payload.person_in_charge,
        recomended_action=payload.recomended_action,
    )
    if "Failed" in result:
        raise HTTPException(status_code=500, detail=result)

    return {"success": True, "message": result}


# --- F. SEARCH (Different Response) ---
@router.post("/search", response_model=SearchResponse)
async def search_ticket(payload: SearchPayload):
    def _search():
        driver = build_driver(headless=True)
        try:
            maybe_login(
                driver,
                settings.BILLING_MODULE_BASE,
                settings.NMS_USERNAME_BILING,
                settings.NMS_PASSWORD_BILING,
            )
            search_user(driver, payload.query)
            return extract_search_results(driver)
        finally:
            driver.quit()

    try:
        results = await asyncio.to_thread(_search)
        return {"query": payload.query, "results": results}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
