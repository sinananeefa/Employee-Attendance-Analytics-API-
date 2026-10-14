# Employee Attendance & Analytics API

A FastAPI service backed by MongoDB for employee attendance, punch-in/punch-out, regularization, and attendance analytics.

## Requirements

- Python 3.10+
- MongoDB 6.0+
- MongoDB connection settings: `MONGO_URI` and `MONGO_DB`

## Setup

Create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` as environment variables or in a local `.env` file. Never commit credentials.

## Run

```bash
uvicorn app.main:app --reload --port 8000
```

- API documentation: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

MongoDB indexes are created when the application starts.