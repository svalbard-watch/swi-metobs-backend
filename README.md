# SWI MetObs Backend

[![Code Coverage](https://img.shields.io/codecov/c/github/UNIS-Svalbard-Weather-Information/swi-metobs-backend)](https://codecov.io/gh/UNIS-Svalbard-Weather-Information/swi-metobs-backend)
[![Test and Lint](https://github.com/UNIS-Svalbard-Weather-Information/swi-metobs-backend/actions/workflows/test-and-lint.yml/badge.svg)](https://github.com/UNIS-Svalbard-Weather-Information/swi-metobs-backend/actions/workflows/test-and-lint.yml)
[![Docker Build and Push](https://github.com/UNIS-Svalbard-Weather-Information/swi-metobs-backend/actions/workflows/docker-build-push.yml/badge.svg)](https://github.com/UNIS-Svalbard-Weather-Information/swi-metobs-backend/actions/workflows/docker-build-push.yml)

A backend service for meteorological observations, serving as the public data API for the SWIApp (Svalbard Weather Information). This service provides precached realtime and historical weather observation and forecast data, as well as multiple other public data.

## Installation

### Prerequisites

- Python 3.8+ (use python 3.13 for Production)
- Docker (optional, for containerized deployment)

### Steps

1. Clone the repository:

   ```bash
   git clone https://github.com/UNIS-Svalbard-Weather-Information/swi-metobs-backend.git
   cd swi-metobs-backend
   ```

2. Install dependencies using uv:

   ```bash
   uv venv
   uv sync --extra dev
   ```

3. Run the application:

   ```bash
   uvicorn app.main:app --reload
   ```

**Note**: This project requires Python 3.13.

## Configuration

The spheres functionality can be configured using environment variables:

**Sphere Data Sources:**
- `SPHERE_LIP_GEOJSON_FETCH`: URL for The Living Ice Project GeoJSON data (default: `https://livingiceproject.com/static/shapes/spheres.geojson`)
- `SPHERE_LIP_BASE_URL`: Base URL for The Living Ice Project assets (default: `https://livingiceproject.com/`)
- `SPHERE_SVALBOX_GEOJSON_FETCH`: URL for the Svalbox photo sphere WFS, as GeoJSON (default: the `svalbox:photo_sphere` layer on `svalbox.unis.no`). Spheres less than 3 m apart are reduced to the most recent picture, and title, authors and license are read once per DOI from Zenodo (cached 24h)

**Caching:**
- The system automatically caches GeoJSON data and computed distance/bearing matrices for 1 hour
- Each project's sphere list (for Svalbox: already deduplicated and credited) is also stored for 1 hour in the shared cache (Redis when `SWI_METSERVICES_REDIS_HOST`/`PORT` are set, in-memory otherwise), and Zenodo records for 24 hours, so workers and nodes do not each refetch them
- The spheres are loaded in the background when the server starts, so the first request does not wait for the providers

**Legal documents and project information (served from GitHub through a cache):**

The privacy policy, the terms of service and the About-panel data (contributors, references, release) are fetched from GitHub by the server, so that visitors never contact GitHub and their IP address stays with the operator.

- `SWI_LEGAL_REPO`: `owner/repo` that publishes the documents as releases tagged `privacy-policy-vX.Y.Z` and `terms-of-service-vX.Y.Z` (files `privacy_policy_EN.md`, `terms_of_service_EN.md`). The text is read at the newest release tag, never from a branch. **Unset by default**: a neutral placeholder shipped in `app/legal/` is served, so an instance never publishes another organisation's documents by accident. A configured document without a release answers 503, it is not replaced by the placeholder.
- `SWI_PROJECT_INFO_CONTRIBUTORS_URL`, `SWI_PROJECT_INFO_REFERENCES_URL`, `SWI_PROJECT_INFO_RELEASE_URL`: where to read the JSON files (default: the files of the UNIS-Svalbard-Weather-Information organisation). They must be files on `raw.githubusercontent.com`.
- `SWI_GITHUB_TOKEN`: optional, raises the GitHub API limit.
- `SWI_GITHUB_REFRESH_HOURS` (default `6`): hours between two refreshes.
- `SWI_GITHUB_MAX_CALLS_PER_HOUR` (default `20`) and `SWI_GITHUB_MAX_RAW_CALLS_PER_HOUR` (default `120`): hard caps on calls to `api.github.com` and `raw.githubusercontent.com`, well under GitHub's limits.

GitHub is never called because of a visitor's request. The data is refreshed when the server starts (so a redeploy never serves what the previous version cached) and then every `SWI_GITHUB_REFRESH_HOURS`. One worker refreshes at a time (lock in Redis), files are revalidated with their ETag, a legal text is only downloaded again when a new release tag appears, and after any failure or rate-limit answer no request is made until the `Retry-After` / `X-RateLimit-Reset` time has passed. The last good copy is kept for 30 days and served while GitHub is unreachable. Without Redis each worker keeps its own copy and refreshes alone.

## Usage

### API Endpoints

The API provides the following endpoints:

- `/api/v3/stations`: Retrieve station information.
- `/api/v3/forecast`: Retrieve forecast data.
- `/api/v3/observations`: Retrieve observational data.
- `/api/v3/spheres`: Access spherical panorama data and navigation.
- `/v3/legal/{privacy-policy|terms-of-service}?lang=en`: Current privacy policy or terms of service (markdown, version, effective date).
- `/v3/project/{contributors|references|release}`: Project information, proxied from GitHub.

### Example Request

```bash
curl http://localhost:8000/api/v3/stations
```

## Testing

Run the test suite:

```bash
uv sync --extra test
uv run pytest
```

> [!NOTE]
> This is designed to run on linux based system so some tests will fail on windows because of the need of elevated privilege to create symlink.

## Docker

Build and run the Docker container:

```bash
docker build -t swi-metobs-backend .
docker run -p 8000:8000 swi-metobs-backend
```

## Contributing

Contributions are welcome! Please coordinate with the complete SWI project team before starting work on significant changes. All pull requests must:

- Be well-documented
- Pass all tests
- Pass linting checks
- Follow the project's coding standards

## License

This project is licensed under the European Union Public License (EUPL) 1.2. See the LICENCE file for details.