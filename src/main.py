"""Main FastAPI application."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.status import HTTP_409_CONFLICT, HTTP_500_INTERNAL_SERVER_ERROR

from src.config import settings
from src.database import engine, Base
from src.routes import cart, order, wishlist, catalog, home, recommendations, subscriptions, banner_events
from src.services.b2b_client import b2b_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    yield
    await b2b_client.aclose()


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.VERSION,
        description="B2C Buyer module for NeoMarket platform",
        lifespan=lifespan,
    )

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)
        if 'unique' in error_msg.lower() or 'duplicate' in error_msg.lower():
            return JSONResponse(
                status_code=HTTP_409_CONFLICT,
                content={"detail": "Resource already exists", "error": "DUPLICATE_ENTRY"}
            )
        return JSONResponse(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Database integrity error", "error": "DATABASE_INTEGRITY_ERROR"}
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        # If detail is a dict with 'code' and 'message', return as-is (preserving any extra fields)
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            # Build response preserving all fields from detail
            response = {"code": detail["code"], "message": detail["message"]}
            for k, v in detail.items():
                if k not in ("code", "message"):
                    response[k] = v
            return JSONResponse(status_code=exc.status_code, content=response)
        # Fallback: convert old format {error, detail} → {code, message}
        if isinstance(detail, dict):
            return JSONResponse(
                status_code=exc.status_code,
                content={"code": detail.get("error", "HTTP_ERROR"), "message": detail.get("detail", str(exc))},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "HTTP_ERROR", "message": str(detail)},
        )

    # Include routers
    app.include_router(cart.router, prefix="/api/v1/cart", tags=["Cart"])
    app.include_router(order.router, prefix="/api/v1/orders", tags=["Orders"])
    app.include_router(wishlist.router, prefix="/api/v1/wishlist", tags=["Wishlist"])
    app.include_router(catalog.router, prefix="/api/v1/catalog", tags=["Catalog"])
    app.include_router(home.router, prefix="/api/v1/home", tags=["Home"])
    app.include_router(recommendations.router, prefix="/api/v1/recommendations", tags=["Recommendations"])
    app.include_router(banner_events.router, prefix="/api/v1/banner-events", tags=["Banner Events"])
    app.include_router(subscriptions.router, prefix="/api/v1/subscriptions", tags=["Subscriptions"])

    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy", "service": settings.APP_NAME, "version": settings.VERSION}

    return app


app = create_app()
