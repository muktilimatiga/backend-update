import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.v1.api import api_router

# [FIX] Removed docs_url=None and redoc_url=None to enable default public docs
app = FastAPI(
    title="Lexxadata Customer Scraper API",
    description="A structured API to search and scrape customer data from the NMS portal.",
    version="1.0.0"
)

# --- FIX: Define specific origins ---
origins = [
    "http://localhost:3000",     # React (localhost)
    "http://127.0.0.1:3000",     # React (127.0.0.1)
    "http://localhost:8001",     # Swagger UI internal calls
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

# --- YOUR API ROUTERS ---
@app.get("/")
def root():
    return {"API is running"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=True, loop="asyncio")
