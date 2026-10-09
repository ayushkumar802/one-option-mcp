"""
Google Sheets MCP Server for "One Option" Manpower Company.
Vercel Serverless Entrypoint.
Provides:
1. Health check & status at GET /
2. Server-Sent Events (SSE) at GET /sse and POST /messages
3. Streamable HTTP (MCP) at POST/GET /mcp
"""

import os
import sys
import re
import json
from datetime import datetime
import requests
import sqlglot
from sqlglot import exp
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
    except ImportError:
        from mcp.server import FastMCP
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from mcp.server.fastmcp.server import StreamableHTTPASGIApp

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SCRIPT_URL = os.environ.get(
    "GOOGLE_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbwWqJB2suETIxJwigR0OdUc75B6XKFkWlTTC-iE69al8ivkO_UKc902l26Jjx_hI_WICQ/exec"
).strip()
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1bTkHhKxKF2bB9lvngF1tFqaQ7Ay8UqPOyxzXQDyepGs").strip()

mcp = FastMCP(
    "one-option-sheets-mcp",
    instructions="MCP Server for 'One Option' manpower consultancy managing job postings and customer inquiries on Google Sheets.",
    stateless_http=True
)

# Disable DNS rebinding check so requests from cloud hosts and remote clients are accepted
if hasattr(mcp.settings, "transport_security") and mcp.settings.transport_security:
    mcp.settings.transport_security.enable_dns_rebinding_protection = False


# ---------------------------------------------------------------------------
# Allowed Schemas, Categories & Table Enums
# ---------------------------------------------------------------------------
VALID_TABLES = {"recent_jobs", "recent_mails"}

ALLOWED_CATEGORIES = [
    "Catering",
    "Construction",
    "Driver",
    "Hospitality",
    "Housekeeping",
    "Logistics",
    "Manufacturing",
    "Office",
    "Security",
    "Warehouse"
]

JOBS_COLUMNS = [
    "slug", "title", "companyName", "category", "subcategory",
    "location", "salaryRange", "postedDate", "jobType", "experience",
    "openings", "shiftTiming", "logo", "description", "responsibilities",
    "skills", "benefits", "featured"
]

MAILS_COLUMNS = [
    "Timestamp", "Name", "Email", "Phone", "City",
    "Query Type", "Job Name", "Message"
]


# ---------------------------------------------------------------------------
# Helpers: Google Sheets API
# ---------------------------------------------------------------------------
def fetch_sheet_rows(sheet_name: str) -> list:
    """Fetch rows from Google Sheet."""
    params = {"id": SHEET_ID, "sheet": sheet_name}
    res = requests.get(SCRIPT_URL, params=params, timeout=20)
    res.raise_for_status()
    data = res.json()
    return data if isinstance(data, list) else []


def post_sheet_action(payload: dict) -> dict:
    """Send write action (insert, update, delete) to Google Apps Script."""
    payload["sheetId"] = SHEET_ID
    res = requests.post(SCRIPT_URL, json=payload, timeout=20)
    res.raise_for_status()
    return res.json()


# ---------------------------------------------------------------------------
# Helpers: Slug, Date, Category Formatter
# ---------------------------------------------------------------------------
def slugify(text: str) -> str:
    """Generate a clean URL-friendly slug from text."""
    clean = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", clean)


def generate_job_slug(title: str, location: str = "") -> str:
    """
    Auto-create clean slug from title and location.
    Examples:
    - 'Hotel Security Guard', 'Palasia, Indore, MP' -> 'hotel-security-guard-indore'
    - 'Production Helper', 'Pithampur, MP' -> 'production-helper-pithampur'
    """
    clean_title = slugify(title)
    loc_lower = (location or "").lower()
    city = "indore" if "indore" in loc_lower or not location else slugify(location.split(",")[0])
    if not clean_title.endswith(city):
        return f"{clean_title}-{city}"
    return clean_title


def validate_and_normalize_category(cat: str) -> tuple[bool, str]:
    """Check if category matches allowed enum (case-insensitive) and return normalized title."""
    if not cat:
        return False, ""
    for allowed in ALLOWED_CATEGORIES:
        if cat.strip().lower() == allowed.lower():
            return True, allowed
    return False, cat.strip()


def get_current_posted_date() -> str:
    """Auto-generate postedDate in the standard format (e.g. 'Oct 09, 2026')."""
    return datetime.now().strftime("%b %d, %Y")


# ---------------------------------------------------------------------------
# Query Parser & Executor
# ---------------------------------------------------------------------------
def process_sql_query(query: str, sheet_name: str, allowed_columns: list) -> dict:
    query_clean = query.strip().rstrip(";")

    # 1. Harmful commands check
    query_upper = query_clean.upper()
    harmful_keywords = ["DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE"]
    for kw in harmful_keywords:
        if re.search(rf"\b{kw}\b", query_upper):
            return {
                "success": False,
                "error": f"Harmful or unauthorized command detected: '{kw}'. Only SELECT, INSERT, UPDATE, and DELETE are allowed."
            }

    # 2. Parse query using sqlglot
    try:
        parsed = sqlglot.parse_one(query_clean)
    except Exception as e:
        return {"success": False, "error": f"SQL Syntax Error: {e}"}

    # 3. Table Enum validation
    table_node = parsed.find(exp.Table)
    if table_node and table_node.name:
        tbl_name = table_node.name.lower().strip("\"'`")
        if tbl_name not in VALID_TABLES:
            return {
                "success": False,
                "error": f"Invalid table '{tbl_name}'. Supported tables are: {list(VALID_TABLES)}"
            }

    def extract_where_conditions(where_node) -> dict:
        conditions = {}
        if where_node:
            for eq in where_node.find_all(exp.EQ):
                col = eq.this.sql().strip("'\"`")
                val = eq.expression.sql().strip("'\"`")
                conditions[col.lower()] = val
        return conditions

    # 4. Enforce Read & Delete only policy for Recent Mails (Inquiries generated by web app)
    if sheet_name == "Recent Mails" and not isinstance(parsed, (exp.Select, exp.Delete)):
        op = "INSERT" if isinstance(parsed, exp.Insert) else ("UPDATE" if isinstance(parsed, exp.Update) else "This operation")
        return {
            "success": False,
            "error": f"{op}_NOT_ALLOWED",
            "message": (
                f"{op} operations are not permitted on 'recent_mails'. "
                "Recent mails and inquiries are generated automatically by the web application. "
                "The manage_recent_mails tool is strictly for viewing (SELECT) or deleting (DELETE) mails."
            )
        }

    # =======================================================================
    # DELETE
    # =======================================================================
    if isinstance(parsed, exp.Delete):
        where = parsed.find(exp.Where)
        if not where:
            return {
                "success": False,
                "error": "Harmful query blocked: DELETE without WHERE clause is not allowed to prevent wiping the sheet."
            }

        where_text = where.sql().replace("WHERE", "", 1).strip()
        if where_text.lower() in ["1=1", "true", "1", "'1'='1'"]:
            return {
                "success": False,
                "error": "Harmful query blocked: Tautological condition ('WHERE 1=1') is not allowed."
            }

        conditions = extract_where_conditions(where)
        rows = fetch_sheet_rows(sheet_name)
        matched = []

        for r in rows:
            match = True
            if conditions:
                for col_k, expected_v in conditions.items():
                    actual_v = next((str(r[c]).strip() for c in r if c.lower() == col_k), None)
                    if actual_v is None or actual_v.lower() != expected_v.lower():
                        match = False
                        break
            else:
                r_slug = str(r.get("slug", "")).lower()
                match = bool(r_slug and r_slug in where_text.lower())
            if match:
                matched.append(r)

        if len(matched) == 0:
            return {"success": False, "error": f"No record found matching condition: {where_text}"}

        # Enforce single item deletion rule
        if len(matched) > 1:
            sample = [m.get("slug") or m.get("title") or m.get("Name") for m in matched[:3]]
            return {
                "success": False,
                "error": f"Safety check failed: Condition matches {len(matched)} records ({sample}...). Deletion is strictly allowed on ONE job/mail at a time. Please target by unique 'slug' or 'Email'."
            }

        target = matched[0]
        if "slug" in target and target.get("slug"):
            match_col = "slug"
        elif conditions:
            # Pick a column from conditions that exists in target with non-empty value
            condition_col = next((c for c in target if c.lower() in conditions and target.get(c)), None)
            match_col = condition_col or allowed_columns[0]
        else:
            match_col = allowed_columns[0]
        match_val = target.get(match_col)

        res = post_sheet_action({
            "action": "delete",
            "sheet": sheet_name,
            "identifierColumn": match_col,
            "identifierValue": match_val
        })
        return {
            "success": True,
            "operation": "DELETE",
            "message": f"Deleted 1 record from {sheet_name}",
            "deleted_record": target,
            "response": res
        }

    # =======================================================================
    # UPDATE
    # =======================================================================
    elif isinstance(parsed, exp.Update):
        where = parsed.find(exp.Where)
        if not where:
            return {"success": False, "error": "UPDATE without WHERE clause is not allowed."}

        where_text = where.sql().replace("WHERE", "", 1).strip()
        conditions = extract_where_conditions(where)

        # Extract SET key=value pairs
        updates = {}
        for eq in parsed.expressions:
            if isinstance(eq, exp.EQ):
                col_name = eq.this.sql().strip("'\"`")
                val = eq.expression.sql().strip("'\"`")
                updates[col_name] = val

        if not updates:
            return {"success": False, "error": "No columns specified in UPDATE SET clause."}

        # Category validation if updating category
        if sheet_name == "Recent Jobs" and "category" in [k.lower() for k in updates]:
            cat_key = next(k for k in updates if k.lower() == "category")
            is_valid, norm_cat = validate_and_normalize_category(updates[cat_key])
            if not is_valid:
                return {
                    "success": False,
                    "error": "INVALID_CATEGORY",
                    "message": f"Invalid category '{updates[cat_key]}'. Allowed categories are: {ALLOWED_CATEGORIES}. Please choose an allowed category."
                }
            updates[cat_key] = norm_cat

        # Find target record identifier from WHERE clause
        rows = fetch_sheet_rows(sheet_name)
        matched = []
        for r in rows:
            match = True
            if conditions:
                for col_k, expected_v in conditions.items():
                    actual_v = next((str(r[c]).strip() for c in r if c.lower() == col_k), None)
                    if actual_v is None or actual_v.lower() != expected_v.lower():
                        match = False
                        break
            else:
                r_slug = str(r.get("slug", "")).lower()
                match = bool(r_slug and r_slug in where_text.lower())
            if match:
                matched.append(r)

        if not matched:
            return {"success": False, "error": f"No record found to update matching: {where_text}"}

        target = matched[0]
        id_col = "slug" if "slug" in target else allowed_columns[0]
        id_val = target.get(id_col)

        res = post_sheet_action({
            "action": "update",
            "sheet": sheet_name,
            "identifierColumn": id_col,
            "identifierValue": id_val,
            "updates": updates
        })
        return {
            "success": True,
            "operation": "UPDATE",
            "message": f"Updated record ({id_val}) in {sheet_name}",
            "target": target,
            "updates": updates,
            "response": res
        }

    # =======================================================================
    # INSERT
    # =======================================================================
    elif isinstance(parsed, exp.Insert):
        cols = [c.name for c in parsed.this.expressions] if hasattr(parsed.this, "expressions") and parsed.this.expressions else []
        values_exp = parsed.expression
        values = []

        if isinstance(values_exp, exp.Values):
            for t in values_exp.expressions:
                vals = []
                for e in t.expressions:
                    v = e.name if hasattr(e, "name") and e.name else e.sql().strip("'\"")
                    vals.append(v)
                values.append(vals)

        if not values:
            return {"success": False, "error": "INSERT statement does not contain valid VALUES."}

        first_vals = values[0]
        row_data = dict(zip(cols, first_vals)) if cols else dict(zip(allowed_columns[:len(first_vals)], first_vals))

        # SPECIAL HANDLING & VALIDATIONS FOR RECENT JOBS
        if sheet_name == "Recent Jobs":
            # 1. Check for essential missing details
            missing_fields = []
            for field in ["title", "category", "salaryRange"]:
                if not row_data.get(field):
                    missing_fields.append(field)

            if missing_fields:
                return {
                    "success": False,
                    "error": "MISSING_ESSENTIAL_JOB_DETAILS",
                    "missing_fields": missing_fields,
                    "message": (
                        f"Cannot insert job: Missing essential field(s): {missing_fields}. "
                        "Please ask the user to provide these details (title, category, salaryRange, location, etc.) "
                        "before creating the job."
                    )
                }

            # 2. Category Enum validation & auto-normalization
            cat_val = row_data.get("category", "")
            is_valid, norm_cat = validate_and_normalize_category(cat_val)
            if not is_valid:
                return {
                    "success": False,
                    "error": "INVALID_CATEGORY",
                    "provided_category": cat_val,
                    "allowed_categories": ALLOWED_CATEGORIES,
                    "message": (
                        f"Category '{cat_val}' is invalid. Allowed categories are: {ALLOWED_CATEGORIES}. "
                        "Please select one of these categories or ask the user."
                    )
                }
            row_data["category"] = norm_cat

            # 3. Auto-generate postedDate with current date (e.g. 'Oct 09, 2026')
            row_data["postedDate"] = get_current_posted_date()

            # 4. Auto-generate slug from title and location
            title_val = row_data.get("title", "")
            loc_val = row_data.get("location", "")
            row_data["slug"] = generate_job_slug(title_val, loc_val)

            # 5. Defaults for optional fields if omitted
            if not row_data.get("subcategory"):
                row_data["subcategory"] = norm_cat
            if not row_data.get("featured"):
                row_data["featured"] = "FALSE"
            if not row_data.get("jobType"):
                row_data["jobType"] = "Full Time"

        res = post_sheet_action({
            "action": "insert",
            "sheet": sheet_name,
            "job": row_data
        })
        return {
            "success": True,
            "operation": "INSERT",
            "message": f"Added record to {sheet_name}",
            "record": row_data,
            "response": res
        }

    # =======================================================================
    # SELECT
    # =======================================================================
    elif isinstance(parsed, exp.Select):
        rows = fetch_sheet_rows(sheet_name)
        where = parsed.find(exp.Where)

        if not where:
            return {
                "success": True,
                "operation": "SELECT",
                "count": len(rows),
                "data": rows[:15]
            }

        conditions = extract_where_conditions(where)
        filtered = []
        for r in rows:
            match = True
            for col_k, expected_v in conditions.items():
                actual_v = next((str(r[c]).strip() for c in r if c.lower() == col_k), None)
                if actual_v is None or actual_v.lower() != expected_v.lower():
                    match = False
                    break
            if match:
                filtered.append(r)

        return {
            "success": True,
            "operation": "SELECT",
            "count": len(filtered),
            "data": filtered
        }

    return {"success": False, "error": "Unsupported SQL operation."}


# ---------------------------------------------------------------------------
# MCP Tools for LLM
# ---------------------------------------------------------------------------

@mcp.tool()
def manage_recent_jobs(query: str) -> str:
    """
    CRUD tool for the 'Recent Jobs' sheet of 'One Option' (manpower & recruitment consultancy).

    Company Context:
    'One Option' connects job seekers with commercial roles in Indore and MP (security, catering, logistics, etc.).

    Table Name: recent_jobs

    Allowed Categories Enum:
    ['Catering', 'Construction', 'Driver', 'Hospitality', 'Housekeeping', 'Logistics', 'Manufacturing', 'Office', 'Security', 'Warehouse']

    Columns & Structure:
    - slug (auto-created by system from title and city, e.g. 'hotel-security-guard-indore')
    - title (string, e.g. 'Hotel Security Guard')
    - companyName (string, e.g. 'Sayaji Hotels & Banquets')
    - category (string, MUST be one of the Allowed Categories above)
    - subcategory (string, auto-generated or specified, e.g. 'Hotel Security')
    - location (string, e.g. 'Vijay Nagar, Indore, MP')
    - salaryRange (string, e.g. '₹16,000 – ₹20,000 / month')
    - postedDate (auto-injected by system as current date 'MMM dd, yyyy', DO NOT specify manually)
    - jobType (string, e.g. 'Full Time', 'Part Time')
    - experience (string, e.g. 'Fresher', '1–2 Years')
    - openings (string, e.g. '5 Openings')
    - shiftTiming (string, e.g. 'Day Shift', 'Rotational Shift (8 Hours)')
    - description (string)
    - responsibilities (string)
    - skills (string)
    - benefits (string)
    - featured (string: 'TRUE' or 'FALSE')

    IMPORTANT GUIDELINES FOR THE LLM:
    1. Important details to ask the user if missing:
       - title, category (must match allowed list), salaryRange, location
       - description, responsibilities, skills, experience, openings, jobType
       If any crucial detail is missing from the user's request, ASK the user to provide it first before running an INSERT query.
    2. Details you DO NOT ask the user for (handled automatically):
       - postedDate: The system automatically timestamps the current date.
       - slug: The system auto-generates this from title and location.
       - subcategory: Generate an appropriate subcategory yourself if user didn't specify.

    Supported SQL Operations:
    - SELECT: 'SELECT * FROM recent_jobs WHERE category = "Security"'
    - INSERT: 'INSERT INTO recent_jobs (title, companyName, category, location, salaryRange, jobType, experience, openings) VALUES ("Hotel Security Guard", "Sayaji Hotels", "Security", "Palasia, Indore, MP", "₹16,000 – ₹20,000 / month", "Full Time", "Fresher", "5 Openings")'
    - UPDATE: 'UPDATE recent_jobs SET salaryRange = "₹18,000 – ₹22,000 / month" WHERE slug = "hotel-security-guard-indore"'
    - DELETE: 'DELETE FROM recent_jobs WHERE slug = "hotel-security-guard-indore"' (strictly 1 job at a time!)
    """
    result = process_sql_query(query, "Recent Jobs", JOBS_COLUMNS)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
def manage_recent_mails(query: str) -> str:
    """
    Tool for viewing and deleting records in the 'Recent Mails' sheet of 'One Option' (manpower & recruitment consultancy).

    Company Context:
    Stores inquiries, candidate applications, and client staffing requests submitted through the portal.

    Table Name: recent_mails
    Columns & Structure:
    - Timestamp (string, submission time)
    - Name (string, applicant/client name)
    - Email (string, contact email)
    - Phone (string, contact mobile number)
    - City (string, location)
    - Query Type (string, e.g. 'Job Request', 'Manpower Hiring', 'General Inquiry')
    - Job Name (string, targeted role or budget)
    - Message (string, applicant message or query details)

    IMPORTANT USAGE RESTRICTIONS:
    1. INSERTS ARE PROHIBITED:
       - Recent mails are generated and submitted automatically by the web application contact form.
       - Do NOT execute INSERT statements. If asked to insert a mail, inform the user that mails are created automatically via the web application.
    2. UPDATES ARE NOT SUPPORTED:
       - Inquiries are immutable records.
    3. ONLY VIEW (SELECT) AND DELETE ARE PERMITTED:
       - View inquiries: Use SELECT.
       - Delete inquiries: Use DELETE with a specific WHERE condition (e.g. Email = 'user@example.com'). Strictly 1 record at a time!

    Supported Operations:
    - SELECT: 'SELECT * FROM recent_mails' or 'SELECT * FROM recent_mails WHERE Email = "rahul@mail.com"'
    - DELETE: 'DELETE FROM recent_mails WHERE Email = "rahul@mail.com"' (strictly 1 inquiry at a time!)
    """
    result = process_sql_query(query, "Recent Mails", MAILS_COLUMNS)
    return json.dumps(result, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Vercel ASGI Application & Routes
# ---------------------------------------------------------------------------

async def root_status(request):
    return JSONResponse({
        "status": "online",
        "name": "one-option-sheets-mcp",
        "description": "MCP Server for One Option Manpower Consultancy (Google Sheets)",
        "endpoints": {
            "streamable_http": "/mcp",
            "sse": "/sse",
            "messages": "/messages"
        },
        "instructions": (
            "For Gemini Enterprise / Connected Apps: use https://<your-domain>/mcp. "
            "For SSE-based MCP clients: use https://<your-domain>/sse."
        )
    })


# Initialize Streamable HTTP and SSE handlers
stream_routes = mcp.streamable_http_app().routes
streamable_handler = StreamableHTTPASGIApp(mcp.session_manager)
sse_routes = mcp.sse_app().routes

routes = [
    # Human-readable status check on GET /
    Route("/", endpoint=root_status, methods=["GET"]),
    # Handle MCP requests on POST / (in case URL is entered without /mcp)
    Route("/", endpoint=streamable_handler, methods=["POST"]),
    # Standard MCP StreamableHTTP endpoint
    Route("/mcp", endpoint=streamable_handler, methods=["GET", "POST", "DELETE"]),
    Route("/api", endpoint=root_status, methods=["GET"]),
    Route("/api/index", endpoint=root_status, methods=["GET"]),
    Route("/api/index.py", endpoint=root_status, methods=["GET"]),
    *sse_routes
]

class LifespanFallbackMiddleware:
    """Ensures StreamableHTTP session manager task group is active in serverless environments."""
    def __init__(self, inner_app, session_manager):
        self.inner_app = inner_app
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and getattr(self.session_manager, "_task_group", None) is None:
            async with self.session_manager._run_lock:
                self.session_manager._has_started = False
            async with self.session_manager.run():
                await self.inner_app(scope, receive, send)
            return
        await self.inner_app(scope, receive, send)


base_app = Starlette(debug=False, routes=routes)
base_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
app = LifespanFallbackMiddleware(base_app, mcp.session_manager)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        print("Testing One Option MCP Tools with New Enums & Auto-Fields...")

        print("\n1. Test SELECT Jobs:")
        print(manage_recent_jobs("SELECT * FROM recent_jobs WHERE category = 'Security'"))

        print("\n2. Test SELECT Mails:")
        print(manage_recent_mails("SELECT * FROM recent_mails"))

        print("\n3. Test INSERT Mails (Should be Blocked):")
        print(manage_recent_mails("INSERT INTO recent_mails (Name, Email) VALUES ('Test', 'test@test.com')"))
    else:
        # Run local FastMCP server (default stdio or transport specified)
        transport = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("stdio", "sse", "streamable-http") else "stdio"
        mcp.run(transport=transport)
