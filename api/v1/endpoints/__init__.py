import subprocess
import asyncio
import logging
import pprint
from typing import Dict, List
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect, Query, Depends
from starlette.responses import StreamingResponse
import httpx

from core import settings
from schemas.open_ticket import TicketClosePayload
from schemas.customers_scrapper import Customer, DataPSB, CustomerwithInvoices
from services.open_ticket import (
    create_ticket_as_cs,
    process_ticket_as_noc,
    close_ticket_as_noc,
    forward_ticket_as_noc,
    extract_search_results,
    build_driver,
    maybe_login,
    search_user
)
from services.biling_scaper import BillingScraper


__all__ = [
    "APIRouter", "HTTPException", "Request", "WebSocket", "WebSocketDisconnect",
    "Dict", "List", "StreamingResponse", "httpx", "logging",
    "TicketClosePayload", "create_ticket_as_cs", "process_ticket_as_noc",
    "close_ticket_as_noc", "forward_ticket_as_noc", "extract_search_results",
    "build_driver", "maybe_login", "search_user", "settings"
]