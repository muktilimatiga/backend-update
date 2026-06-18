from fastapi import APIRouter, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
from pathlib import Path

from services.exceltopostgress import ExcelHandler
from core import settings
from core.olt_config import OLT_OPTIONS, get_olt_info

router = APIRouter()


# ============================================================
# Excel to Database
# ============================================================

@router.post("/exceltodb")
def upload_excel(file: UploadFile = File(...)):
    """
    Upload an Excel file (.xlsx) to sync fiber customer data.
    """
    if not file.filename.endswith(('.xls', '.xlsx')):
        raise HTTPException(status_code=400, detail="Invalid file type. Please upload .xlsx or .xls")

    try:
        # Pass the file-like object directly to pandas
        total_rows = ExcelHandler.process_file(file.file)
        
        return {
            "status": "success",
            "filename": file.filename,
            "rows_processed": total_rows,
            "message": "Data upserted successfully"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Processing error: {str(e)}")


# ============================================================
# Batch Config Generator
# ============================================================

@router.get("/generate-batch-config")
async def generate_batch_config(
    olt_name: str = Query(..., description="OLT name, e.g., KAUMAN"),
    interface: str = Query(..., description="Interface, e.g., 1/3/1"),
    default_package: str = Query("10M", description="Default package if not found")
):
    """
    Generate batch config for unconfigured ONUs and download the file directly.
    """
    from fastapi.responses import FileResponse
    from services.telnet import TelnetClient
    from services.generated import BatchConfigGenerator
    
    # Validate OLT name
    olt_info = get_olt_info(olt_name)
    if not olt_info:
        raise HTTPException(
            status_code=400, 
            detail=f"OLT '{olt_name}' tidak ditemukan. Pilihan: {list(OLT_OPTIONS.keys())}"
        )
    
    try:
        # Connect to OLT
        client = TelnetClient(
            host=olt_info["ip"],
            username=settings.OLT_USERNAME,
            password=settings.OLT_PASSWORD,
            is_c600=olt_info.get("c600", False),
            olt_name=olt_name.upper()
        )
        
        await client.connect()
        await client._login()
        await client._disable_pagination()
        
        # Generate batch config
        generator = BatchConfigGenerator(client)
        result = await generator.generate_batch_config(
            interface=interface,
            default_package=default_package,
            skip_missing_customers=False
        )
        
        await client.close()
        
        # Return file download
        if result["filepath"]:
            from pathlib import Path
            filename = Path(result["filepath"]).name
            return FileResponse(
                path=result["filepath"],
                filename=filename,
                media_type="text/plain"
            )
        else:
            raise HTTPException(
                status_code=404, 
                detail="Tidak ada ONT unconfigured ditemukan pada interface tersebut"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


