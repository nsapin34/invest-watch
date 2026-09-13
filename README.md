# invest-watch

Automated daily press monitoring for portfolio companies (≤ 50).

Collects articles published the previous day, deduplicates, classifies, summarises via IBM watsonx.ai, and generates a structured Markdown + JSON report.

---

## Requirements

- Python ≥ 3.11
- IBM watsonx.ai account (API key + project ID)
- Brave Search API key (free tier — 1,000 req/month)

## Installation

```bash
git clone <repo-url> invest-watch
cd invest-watch
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

pip install -e ".[dev]"
```

## Configuration

```bash
# 1 — Copy the example config
copy conf\config.example.toml conf\config.toml   # Windows
# cp conf/config.example.toml conf/config.toml   # Linux / macOS

# 2 — Create your .env with secrets (never commit this file)
copy .env.example .env   # then fill in your keys
```

`.env` minimum content:

```dotenv
WATSONX_API_KEY=your-ibm-cloud-api-key
WATSONX_URL=https://eu-de.ml.cloud.ibm.com
WATSONX_PROJECT_ID=your-project-uuid
WATSONX_MODEL_ID=ibm/granite-3-8b-instruct
BRAVE_API_KEY=your-brave-search-api-key
```

## Usage

```bash
# Add a company to track
invest-watch company add "BYD" --ticker BYDDY --country CN --alias "比亚迪"

# Run the daily collection manually
invest-watch run

# Display today's report
invest-watch report

# Full-text search
invest-watch search "electric vehicles"

# Show status (DB, last run, LLM)
invest-watch status
```

## Scheduling (Windows)

```text
schtasks /Create /TN "invest-watch-daily" /TR "py -3 -m invest_watch run" /SC DAILY /ST 07:00 /F
```

## Tests

```bash
pytest                        # all tests (offline, no API key needed)
pytest tests/unit/            # unit tests only
pytest --cov-report=html      # coverage report in htmlcov/
```

## Architecture

See [`docs/architecture-eng.md`](docs/architecture-eng.md) for the full architecture document and implementation plan.

See [`docs/SPECIFICATIONS-eng.md`](docs/SPECIFICATIONS-eng.md) for functional and technical specifications v1.3.

## Project structure

See [`docs/PROJECT_STRUCTURE-eng.md`](docs/PROJECT_STRUCTURE-eng.md) for the full directory and module layout.

---

*invest-watch v0.1 — personal use, local data, no cloud storage.*
