from . import BaseModel, Optional, List, HttpUrl

class TicketItem(BaseModel):
    ref_id: Optional[str] = None
    date_created: Optional[str] = None
    description: Optional[str] = None
    action: Optional[str] = None

class Customer(BaseModel):
    id: str
    name: Optional[str] = None
    address: Optional[str] = None
    user_pppoe: Optional[str] = None
    pppoe_password: Optional[str] = None
    package: Optional[str] = None
    user_join: Optional[str] = None
    mobile: Optional[str] = None
    coordinate: Optional[str] = None
    ip_address: Optional[str] = None
    maps_link: Optional[str] = None
    wa_link: Optional[str] = None
    last_payment: Optional[str] = None
    detail_url: Optional[HttpUrl] = None
    invoices: Optional[str] = None
    tickets: Optional[List[TicketItem]] = None

class CustomerData(BaseModel):
    name: str
    address: Optional[str] = None
    pppoe_user: Optional[str] = None
    pppoe_password: Optional[str] = None
    olt_name: Optional[str] = None
    interface: Optional[str] = None
    onu_sn: Optional[str] = None
    modem_type: Optional[str] = None

class CustomerInvoice(BaseModel):
    status: Optional[str] = None
    tickets: Optional[List[TicketItem]] = None
    invoice_description: Optional[str] = None

class CustomerDataWithInvoices(CustomerData):
    """CustomerData with invoices attached (for single result)."""
    invoices: Optional[CustomerInvoice] = None

class CustomerSearchResponse(BaseModel):
    """Response model for customer-fast endpoint."""
    multiple: bool
    count: int
    customers: Optional[List[CustomerData]] = None  # When multiple=True
    customer: Optional[CustomerDataWithInvoices] = None  # When multiple=False

class DataPSB(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    user_pppoe: Optional[str] = None
    pppoe_password: Optional[str] = None


class InvoiceItem(BaseModel):
    status: Optional[str] = None
    package: Optional[str] = None
    period: Optional[str] = None
    month: Optional[int] = None
    year: Optional[int] = None
    payment_link: Optional[str] = None
    description: Optional[str] = None

class BillingSummary(BaseModel):
    this_month: Optional[str] = None
    arrears_count: int = 0
    last_paid_month: Optional[str] = None

class CustomerwithInvoices(Customer):
    paket: Optional[str] = None
    invoices: Optional[List[InvoiceItem]] = None
    summary: Optional[BillingSummary] = None

    class Config:
        from_attributes = True

class CustomerBillingInfo(BaseModel):
    """Simplified customer billing info for quick lookups."""
    name: Optional[str] = None
    address: Optional[str] = None
    user_pppoe: Optional[str] = None
    last_payment: Optional[str] = None
    description: Optional[str] = None

class RadiusAcctRow(BaseModel):
    username: Optional[str] = None
    framed_ip: Optional[str] = None
    start: Optional[str] = None
    session_time: Optional[str] = None
    stop: Optional[str] = None
    status: Optional[str] = None
    terminating: Optional[str] = None

class CustomerNOC(BaseModel):
    user_pppoe: Optional[str] = None
    nama: Optional[str] = None
    alamat: Optional[str] = None
    pppoe_password: Optional[str] = None
    coordinates: Optional[str] = None
    ip_address: Optional[str] = None
    radius_acct: Optional[List[RadiusAcctRow]] = None

class CustomerNOCResponse(BaseModel):
    customers: List[CustomerNOC]
    count: int