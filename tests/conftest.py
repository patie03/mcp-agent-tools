from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")  # lets the live tests see OPENAI_API_KEY


@pytest.fixture(autouse=True)
def _clear_weather_cache():
    from src.server.tools import weather_tool
    weather_tool._cache.clear()
    yield
