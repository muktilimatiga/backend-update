from fastapi import APIRouter, Depends
from api.v1.endpoints import (
    cli, customer_scrapper, open_ticket, telnet, file_handler, onu_handler, bot_api, ocr
)

api_router = APIRouter()

api_router.include_router(
    cli.router,
    prefix="/cli",
    tags=["cli"],
)

# Handle Invoices
api_router.include_router(
    customer_scrapper.router,
    prefix="/customer",
    tags=["Customer"],
)

# Handle Open Ticket
api_router.include_router(
    open_ticket.router,
    prefix="/ticket",
    tags=["Ticket"],
)

#Handle Config
api_router.include_router(
    telnet.router,
    prefix="/config",
    tags=["Config"],
)

#Handle ONU 
api_router.include_router(
    onu_handler.router,
    prefix="/onu",
    tags=["ONU"],
)

api_router.include_router(
    file_handler.router,
    prefix="/file",
    tags=["File"],
)

api_router.include_router(
    bot_api.router,
    prefix="/bot",
    tags=["Bot"],
)

api_router.include_router(
    ocr.router,
    prefix="/ocr",
    tags=["OCR"],
)