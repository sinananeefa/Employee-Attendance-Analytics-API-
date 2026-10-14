
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, EmailStr, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from datetime import date
from fastapi import Query, HTTPException

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def truncate_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()) * 1000


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def at_ist(date_str: str, hhmm: str, days: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    d = date.fromisoformat(date_str) + timedelta(days=days)
    return datetime.combine(d, time(h, m), tzinfo=IST)


def attendance_date(pin: datetime, shift_start: str, shift_end: str) -> str:
    local = pin.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M") < shift_end:
        d -= timedelta(days=1)
    return d.isoformat()



def compute_late_minutes(
    pin: Optional[datetime],
    shift_start: str,
    date_str: str,
) -> int:
    if pin is None:
        return 0

    if pin.tzinfo is None:
        pin = pin.replace(tzinfo=timezone.utc)

    pin_ist = pin.astimezone(IST)
    shift_start_ist = at_ist(date_str, shift_start)

    seconds_late = (
        pin_ist - shift_start_ist
    ).total_seconds()

    # The first 10 minutes are a grace period.
    if seconds_late <= 600:
        return 0

    return int(seconds_late // 60)




def compute_overtime(
    pout: Optional[datetime],
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    """
    Rule A: Count minutes after the scheduled shift end.

    Overtime is recorded only when the employee punches out
    at least 30 minutes after the scheduled shift end.

    Supports regular and overnight shifts.
    """
    if pout is None:
        return 0

    # Interpret naive timestamps as UTC, consistent with
    # the rest of this application's timestamp handling.
    if pout.tzinfo is None:
        pout = pout.replace(tzinfo=UTC)

    # Convert punch-out to Indian Standard Time.
    punch_out_ist = pout.astimezone(IST)

    # Calculate the scheduled shift-end datetime.
    # Overnight shifts end on the following calendar day.
    overnight = is_overnight(shift_start, shift_end)

    scheduled_end = at_ist(
        date_str,
        shift_end,
        days=1 if overnight else 0,
    )

    # Calculate the number of complete minutes after shift end.
    seconds_after_end = (
        punch_out_ist - scheduled_end
    ).total_seconds()

    minutes_after_end = int(seconds_after_end // 60)

    # Require at least 30 minutes beyond scheduled shift end.
    if minutes_after_end < 30:
        return 0

    return minutes_after_end


def half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(pin: datetime, pout: datetime) -> float:
    seconds = int((pout - pin).total_seconds())
    return half_up(Decimal(seconds) / Decimal(3600), 2)


def compute_half_day(hours: Optional[float]) -> bool:
    return hours is not None and hours < 4.50


def json_time(value):
    if isinstance(value, datetime):
        return dt_to_ms(value)
    return value


def serialize_history(history):
    output = []
    for entry in history or []:
        item = dict(entry)
        item["at"] = json_time(item.get("at"))
        changes = {}
        for field, change in item.get("changes", {}).items():
            changes[field] = {
                "from": json_time(change.get("from")),
                "to": json_time(change.get("to")),
            }
        item["changes"] = changes
        output.append(item)
    return output


def serialize(doc: dict) -> dict:
    result = dict(doc)
    result.pop("_id", None)
    result.pop("id", None)

    for field in ("punch_in", "punch_out", "created_at"):
        if field in result:
            result[field] = json_time(result[field])

    result.setdefault("history", [])
    result["history"] = serialize_history(result["history"])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("work_hours", None)
    return result


def validate_date_string(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise HTTPException(422, f"{field_name} must be YYYY-MM-DD")


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year, mon, calendar.monthrange(year, mon)[1])
    return start.isoformat(), end.isoformat()


def working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def require_employee(emp_code: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def validate_ms(value):
    if value is None:
        return None
    if type(value) is not int or not MIN_MS <= value <= MAX_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds")
    return truncate_seconds(ms_to_dt(value))


def is_present_expr(prefix="$"):
    return {"$in": [f"{prefix}status", PRESENT_STATUSES]}


def weekday_expr(date_expr):
    return {
        "$in": [
            {"$dayOfWeek": date_expr},
            [2, 3, 4, 5, 6],
        ]
    }


def attendance_query(
    emp_code=None, date_from=None, date_to=None, status=None
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            validate_date_string(date_from, "date_from")
            q["date"]["$gte"] = date_from
        if date_to:
            validate_date_string(date_to, "date_to")
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
    return q


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: date

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Database indexes and application
# ---------------------------------------------------------------------------

def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)], name="ix_employee_department_joined"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee"
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)], name="ix_attendance_punch_in"
    )
    db.attendance_logs.create_index(
        [("date", 1), ("status", 1)], name="ix_attendance_date_status"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        return JSONResponse(status_code=503, content={"detail": "mongo down"})


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = truncate_seconds(datetime.now(UTC))

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc["created_at"] = dt_to_ms(doc["created_at"])
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = (
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = []
    for emp in cursor:
        emp["created_at"] = json_time(emp.get("created_at"))
        items.append(emp)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = require_employee(body.emp_code)
    ts = validate_ms(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = require_employee(body.emp_code)
    ts = validate_ms(body.punched_at) if body.punched_at is not None else truncate_seconds(datetime.now(UTC))

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
            "status": {"$in": PRESENT_STATUSES},
        },
        sort=[("punch_in", -1)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")

    pin = rec["punch_in"]
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= pin or ts - pin > timedelta(hours=24):
        raise HTTPException(422, "invalid punch-out timestamp")

    hours = compute_work_hours(pin, ts)
    update = {
        "punch_out": ts,
        "work_hours": hours,
        "overtime_minutes": compute_overtime(
            ts, emp["shift_start"], emp["shift_end"], rec["date"]
        ),
        "half_day": compute_half_day(hours),
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": update},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = (
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize(d) for d in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    validate_date_string(date)
    emp = require_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "attendance record not found")

    old = dict(rec)
    new_status = body.status if body.status is not None else old["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE cannot have punch times")
        pin = pout = None
    else:
        pin = validate_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in")
        pout = validate_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out")
        if pin is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_date(pin, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in does not belong to the requested attendance date")
        if pout is not None and (pout <= pin or pout - pin > timedelta(hours=24)):
            raise HTTPException(422, "invalid punch_out")

    hours = compute_work_hours(pin, pout) if pin and pout else None
    late = compute_late_minutes(pin, emp["shift_start"], date)
    overtime = compute_overtime(pout, emp["shift_start"], emp["shift_end"], date) if pout else 0
    half_day = compute_half_day(hours)

    new_values = {
        "status": new_status,
        "punch_in": pin,
        "punch_out": pout,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    changes = {}
    for field, value in new_values.items():
        previous = old.get(field)
        if previous != value:
            changes[field] = {"from": previous, "to": value}

    if not changes:
        raise HTTPException(422, "regularization does not change any fields")

    entry = {
        "at": truncate_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history = old.get("history", [])
    # Guard concurrent edits by comparing the old values being changed.
    guard = {"emp_code": emp_code, "date": date}
    for field in changes:
        guard[field] = old.get(field)

    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": new_values, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "concurrent edit; retry")
    return serialize(updated)


# ---------------------------------------------------------------------------
# Analytics pipeline builders
# ---------------------------------------------------------------------------

def build_employee_monthly_pipeline(emp_code: str, start: str, end: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$addFields": {
            "_day": {"$dateFromString": {"dateString": "$date"}},
            "_late": {"$ifNull": ["$late_minutes", 0]},
            "_ot": {"$ifNull": ["$overtime_minutes", 0]},
            "_half": {"$ifNull": ["$half_day", False]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENT_STATUSES]},
                    {"$in": [{"$dayOfWeek": "$_day"}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": ["$_half", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "total_overtime_minutes": {"$sum": "$_ot"},
        }},
    ]


def build_department_summary_pipeline(start: str, end: str, department=None):
    match = {"joined_on": {"$lte": end}}
    if department:
        match["department"] = department

    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {
            "_status": "$logs.status",
            "_day": {"$cond": [
                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                {"$dateFromString": {"dateString": "$logs.date"}},
                None,
            ]},
            "_late": {"$ifNull": ["$logs.late_minutes", 0]},
            "_ot": {"$ifNull": ["$logs.overtime_minutes", 0]},
        }},
        {"$group": {
            "_id": "$department",
            "codes": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", PRESENT_STATUSES]},
                    {"$in": [{"$dayOfWeek": "$_day"}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$_status", PRESENT_STATUSES]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                "$logs.work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$_late"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$_status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$_status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$codes"},
            "present_days": 1,
            "avg_work_hours": "$hours",
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def build_leaderboard_pipeline(start: str, end: str, department=None, limit=10):
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
            ]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def build_trend_pipeline(codes, start: str, end: str, joined_dates):
    start_dt = datetime.combine(
        date.fromisoformat(start), time.min, tzinfo=UTC
    )
    end_exclusive = datetime.combine(
        date.fromisoformat(end) + timedelta(days=1),
        time.min,
        tzinfo=UTC,
    )

    return [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {"$gte": start, "$lte": end},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENT_STATUSES]},
                            {
                                "$cond": [
                                    {"$ifNull": ["$half_day", False]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$addFields": {
                "d": {
                    "$dateFromString": {"dateString": "$_id"}
                }
            }
        },
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$d",
                    }
                },
                "present_count": {
                    "$ifNull": ["$present_count", 0]
                },
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$literal": joined_dates},
                            "as": "joined",
                            "cond": {
                                "$lte": [
                                    "$$joined",
                                    {
                                        "$dateToString": {
                                            "format": "%Y-%m-%d",
                                            "date": "$d",
                                        }
                                    },
                                ]
                            },
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_count",
                                        "$headcount",
                                    ]
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$attendance_rate",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_7d",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

# ---------------------------------------------------------------------------
# Analytics endpoints
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = require_employee(emp_code)
    start, end = month_bounds(month)
    joined = date.fromisoformat(emp["joined_on"])
    month_end = date.fromisoformat(end)
    month_start = date.fromisoformat(start)
    first_day = max(joined, month_start)
    days = working_days(first_day, month_end)

    rows = list(db.attendance_logs.aggregate(
        build_employee_monthly_pipeline(emp_code, start, end)
    ))
    values = rows[0] if rows else {}
    present = values.get("present_days", 0)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": half_up(present, 2),
        "leave_days": values.get("leave_days", 0),
        "late_count": values.get("late_count", 0),
        "total_late_minutes": values.get("total_late_minutes", 0),
        "total_overtime_minutes": values.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / days * 100, 2) if days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = list(db.employees.aggregate(
        build_department_summary_pipeline(start, end, department)
    ))
    for row in rows:
        row["present_days"] = half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = (
            half_up(row["avg_work_hours"], 2)
            if row.get("avg_work_hours") is not None else None
        )
    return {"month": month, "items": rows}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(
        build_leaderboard_pipeline(start, end, department, limit)
    ))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    start = from_date.isoformat()
    end = to_date.isoformat()

    # Reject invalid ranges, including ranges longer than 92 days.
    if from_date > to_date:
        raise HTTPException(
            status_code=422,
            detail="'from' date must be before or equal to 'to' date",
        )

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="date range must not exceed 92 days",
        )

    # Include employees who joined on or before the end of the range.
    employees = list(
        db.employees.find(
            {
                "department": department,
                "joined_on": {"$lte": end},
            },
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )

    if not employees:
        raise HTTPException(404, "department has no employees")

    emp_codes = [employee["emp_code"] for employee in employees]
    joined_dates = [employee["joined_on"] for employee in employees]

    # Calculate the daily series and seven-day moving average in MongoDB.
    pipeline = build_trend_pipeline(
        emp_codes,
        start,
        end,
        joined_dates,
    )
    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "department": department,
        "from": start,
        "to": end,
        "items": items,
    }


# ---------------------------------------------------------------------------
# Explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary",
        "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        command = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        start, end = month_bounds(month)
        pipe = build_employee_monthly_pipeline(emp_code, start, end)
        command = {"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}},
                   "verbosity": "executionStats"}
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        pipe = build_department_summary_pipeline(start, end, department)
        command = {"explain": {"aggregate": "employees", "pipeline": pipe, "cursor": {}},
                   "verbosity": "executionStats"}
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        start, end = month_bounds(month)
        pipe = build_leaderboard_pipeline(
            start, end, department, limit
        )
        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipe,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        collection = "attendance_logs"

    
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                422, "department, from and to are required"
            )

        validate_date_string(from_date, "from")
        validate_date_string(to_date, "to")

        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)

        if start_date > end_date:
            raise HTTPException(
                422, "'from' date must not exceed 'to' date"
            )

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                422, "date range must not exceed 92 days"
            )

        employees = list(
            db.employees.find(
                {
                    "department": department,
                    "joined_on": {"$lte": to_date},
                },
                {
                    "_id": 0,
                    "emp_code": 1,
                    "joined_on": 1,
                },
            )
        )

        if not employees:
            raise HTTPException(
                404, "department has no employees"
            )

        emp_codes = [
            employee["emp_code"] for employee in employees
        ]
        joined_dates = [
            employee["joined_on"] for employee in employees
        ]

        pipeline = build_trend_pipeline(
            emp_codes,
            from_date,
            to_date,
            joined_dates,
        )

        command = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }

        collection = "attendance_logs"

    from bson.json_util import dumps, loads

    result = db.command(command)

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": loads(dumps(result)),
    }
