"""Application configuration."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    APP_NAME: str = "NeoMarket B2C Service"
    VERSION: str = "0.1.0"
    
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://neomarket:neomarket_pass@db:5432/neomarket_b2c"
    
    # B2B API
    B2B_API_URL: str = "http://localhost:8001/api/v1"
    
    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]


settings = Settings()
