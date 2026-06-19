import logging
import re
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, Query

from core import settings
from schemas.config_handler import CustomerData as ConfigCustomerData
from schemas.customers_scrapper import (
    DataPSB,
    CustomerwithInvoices,
    Customer,
    CustomerBillingInfo,
    CustomerData,
    CustomerSearchResponse,
    CustomerDataWithInvoices,
    CustomerInvoice,
    CustomerNOC,
    CustomerNOCResponse,
    CutomerLosiResponse,
    CustomerLosiCoordsResponse
)
from services.biling_scaper import BillingScraper, NOCScrapper
from services.supabase_client import search_customers, save_billing_data_sync
from services.playwright import CustomerService

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_bandwidth(package: str) -> str:
    """Extract bandwidth like '100M' from full package name."""
    if not package:
        return package
    match = re.search(r"(\d+M)", package)
    return match.group(1) if match else package


def get_scraper() -> NOCScrapper:
    try:
        return NOCScrapper()
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"NMS unavailable: {e}")


def get_billing() -> BillingScraper:
    """Create BillingScraper with its own session - billing requires separate auth from NMS."""
    try:
        return BillingScraper()  # Let it create its own session and login to billing
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Billing unavailable: {e}")


def get_customer_service() -> CustomerService:
    try:
        return CustomerService()
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Playwright unavailable: {e}")


def get_noc() -> NOCScrapper:
    try:
        return NOCScrapper()
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"NOC login failed: {e}")


# Endpoint show psb avaible
@router.get("/psb", response_model=List[DataPSB])
async def get_psb_data():
    from services.playwright import run_sync

    def _noc_psb():
        noc = NOCScrapper()
        return noc._get_data_psb()

    results = await run_sync(_noc_psb)

    return [
        DataPSB(
            name=r.get("name"),
            address=r.get("address"),
            user_pppoe=r.get("user_pppoe"),
            pppoe_password=r.get("pppoe_password"),
            paket=r.get("paket"),
        ) for r in results
    ]


@router.get("/customers-billing", response_model=List[Customer])
def get_customer_details_route(
    query: str = Query(..., min_length=1),
    billing_scraper: BillingScraper = Depends(get_billing),
):
    logger.info(f"[customers-billing] Searching for query: {query}")

    # 1. Search for customers to get their IDs
    search_results = billing_scraper.search(query)
    logger.info(f"[customers-billing] Search results: {search_results}")

    if not search_results:
        logger.warning(f"[customers-billing] No customer found for query: {query}")
        raise HTTPException(
            status_code=404, detail=f"No customer found for query: '{query}'"
        )

    detailed_customers = []

    # 2. Iterate through search results and fetch full details for each
    for result in search_results:
        cid = result.get("id")
        logger.info(f"[customers-billing] Fetching details for customer ID: {cid}")
        if cid:
            customer_obj = billing_scraper.get_customer_details(cid)
            logger.info(f"[customers-billing] Customer details result: {customer_obj}")

            if customer_obj:
                detailed_customers.append(customer_obj)

                # Save billing data (coordinate, name, address) to Supabase
                try:
                    save_billing_data_sync({
                        "user_pppoe": customer_obj.user_pppoe,
                        "nama": customer_obj.name,
                        "alamat": customer_obj.address,
                        "coordinates": customer_obj.coordinate,
                        "paket": _extract_bandwidth(customer_obj.package),
                    })
                except Exception as e:
                    logger.warning(f"[customers-billing] Failed to save to Supabase: {e}")

    if not detailed_customers:
        logger.warning(
            f"[customers-billing] Found customers but failed to retrieve details"
        )
        raise HTTPException(
            status_code=404, detail="Found customers but failed to retrieve details."
        )

    logger.info(f"[customers-billing] Returning {len(detailed_customers)} customers")
    return detailed_customers


@router.get("/customers-data", response_model=List[CustomerData])
async def get_customer_data(
    search: str = Query(
        ..., min_length=1, description="Search by name, address, or pppoe"
    ),
):
    """Get customer data from Supabase."""

    def _clean_field(value: any) -> Optional[str]:
        if not value:
            return None
        clean_value = str(value).strip()
        if clean_value in ("", "0", "-", "N/A"):
            return None
        return clean_value

    try:
        customers = search_customers(search)
        if not customers:
            raise HTTPException(
                status_code=404, detail=f"No customer found for query: '{search}'"
            )
        result = []

        for c in customers:
            result.append(
                CustomerData(
                    name=c.get("nama", "Unknown"),
                    address=_clean_field(c.get("alamat", "")),
                    pppoe_user=c.get("user_pppoe", ""),
                    pppoe_password=_clean_field(c.get("pppoe_password", "")),
                    olt_name=_clean_field(c.get("olt_name", "")),
                    interface=_clean_field(c.get("interface", "")),
                    onu_sn=_clean_field(c.get("onu_sn", "")),
                    modem_type=_clean_field(c.get("modem_type", "")),
                )
            )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch customers: {e}")


@router.get("/customer-fast", response_model=CustomerSearchResponse)
async def get_customer_data_fast(
    search: str = Query(
        ..., min_length=1, description="Search by name, address, or pppoe"
    ),
):
    """
    Get customer data using playwright.

    Returns:
        - If multiple customers found: List of basic customer info for selection
        - If single customer found: Full customer details with invoices
    """
    from services.playwright import get_customer_with_invoices_sync, run_sync

    # Run sync playwright in thread pool (Windows compatible)
    search_results, invoices_data = await run_sync(
        get_customer_with_invoices_sync, search, True
    )

    if not search_results:
        raise HTTPException(
            status_code=404, detail=f"No customer found for query: '{search}'"
        )

    # Multiple results: return list for frontend selection
    if len(search_results) > 1:
        customers = [CustomerData(**c) for c in search_results]
        return CustomerSearchResponse(
            multiple=True, count=len(customers), customers=customers, customer=None
        )

    # Single result: with invoices already fetched
    customer_dict = search_results[0]

    invoices = None
    if invoices_data:
        invoices = CustomerInvoice(**invoices_data)

    customer_with_invoices = CustomerDataWithInvoices(
        **customer_dict, invoices=invoices
    )

    return CustomerSearchResponse(
        multiple=False, count=1, customers=None, customer=customer_with_invoices
    )

@router.get("/customer-noc", response_model=CustomerNOCResponse)
async def get_customer_data_noc(
    search: str = Query(
        ..., min_length=1, description="Search by name, address, or pppoe"
    ),
):
    from services.playwright import run_sync

    def _noc_search():
        noc = NOCScrapper()
        return noc.get_customer_data_noc(search)

    customer_list = await run_sync(_noc_search)

    if not customer_list:
        raise HTTPException(
            status_code=404, detail=f"No customer found for query: '{search}'"
        )

    return CustomerNOCResponse(
        customers=[CustomerNOC(**c) for c in customer_list],
        count=len(customer_list),
    )


@router.get("/customer-noc-billing-test", response_model=CustomerNOCResponse)
async def get_customer_data_noc_billing_test(
    search: str = Query(
        ..., min_length=1, description="Search by name, address, or pppoe"
    ),
    noc_scraper: NOCScrapper = Depends(get_noc),
):
    """NOC customer search using NOCScrapper (requests) for speed comparison."""
    logger.info(f"[customer-noc-billing-test] Searching for: {search}")

    noc_scraper._login()
    detail_links = noc_scraper._search_noc(search)
    if not detail_links:
        raise HTTPException(
            status_code=404, detail=f"No customer found for query: '{search}'"
        )

    customers = []
    for link in detail_links:
        obj = noc_scraper._scrape_noc_detail(link["href"])
        if obj and obj.get("nama"):
            customers.append(CustomerNOC(**obj))

    logger.info(f"[customer-noc-billing-test] Returning {len(customers)} customers")
    return CustomerNOCResponse(customers=customers, count=len(customers))

@router.get("/customer-losi", response_model=List[CutomerLosiResponse])
async def get_customer_data_losi(
    olt_name: str = Query(
        ..., min_length=1, description="OLT Name"
    ),
    interface: str = Query(
        ..., min_length=1, description="Interface Name"
    ),
):
    pass

@router.get("/customer-losi-coords", response_model=List[CustomerLosiCoordsResponse])
async def get_customer_data_losi_coords(
    olt_name: str = Query(
        ..., min_length=1, description="OLT Name"
    ),
    interface: str = Query(
        ..., min_length=1, description="Interface Name"
    ),
):
    pass
    
    