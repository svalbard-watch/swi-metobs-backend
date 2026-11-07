from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.v3.endpoints.legal import refresh_legal
from app.api.v3.endpoints.project import refresh_project
from app.api.v3.endpoints.spheres import warm_up as warm_up_spheres
from app.api.v3.router import api_router
from app.api.health import router as health_router
from app.utils.github_proxy import refresh_loop
from app.version import VERSION
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import os


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load the spheres in the background: the API is up at once, and the first
    # sphere request does not wait for the providers
    warm_up_task = asyncio.create_task(warm_up_spheres())
    # Legal documents and project info come from GitHub through a cache that is refreshed
    # here, at start-up and then on a schedule, never by a visitor's request. Without a
    # cache there is nowhere to keep them, so nothing is fetched.
    tasks = [warm_up_task]
    if os.getenv("SWI_METSERVICES_CACHE_DISABLED", "false").lower() != "true":
        tasks.append(
            asyncio.create_task(refresh_loop([refresh_legal, refresh_project]))
        )
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="SWI MetObs API", version=VERSION, lifespan=lifespan)

# Read the environment variable for allowed origins
allowed_origins = os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add health endpoints (not versioned)
app.include_router(health_router)

# Add versioned API endpoints
app.include_router(api_router, prefix="/v3")
