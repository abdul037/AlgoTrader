from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers.trading import router as trading_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield


settings = get_settings()
app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.client_origin, "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "marketDataProvider": settings.market_data_provider,
        "liveExecutionEnabled": settings.enable_live_execution,
    }


app.include_router(trading_router, prefix="/api/trading", tags=["trading"])
