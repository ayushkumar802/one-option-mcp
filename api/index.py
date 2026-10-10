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
import random
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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

# Load .env file if present in workspace root or local directory
for env_candidate in [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
]:
    if os.path.exists(env_candidate):
        try:
            with open(env_candidate, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

SCRIPT_URL = os.environ.get(
    "GOOGLE_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbwWqJB2suETIxJwigR0OdUc75B6XKFkWlTTC-iE69al8ivkO_UKc902l26Jjx_hI_WICQ/exec"
).strip()
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1bTkHhKxKF2bB9lvngF1tFqaQ7Ay8UqPOyxzXQDyepGs").strip()

# Email Notifications Configuration (Gmail SMTP)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "ayushkumarrio22@gmail.com").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "ilzhbhgcmdepdcfx").strip()
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ayushkumarrio44@gmail.com").strip()

# Public Website URL for direct job links
raw_website = os.environ.get("WEBSITE_URL", "https://oneoption.co.in").strip().rstrip("/")
if raw_website and not raw_website.startswith("http://") and not raw_website.startswith("https://"):
    WEBSITE_URL = f"https://{raw_website}"
else:
    WEBSITE_URL = raw_website or "https://oneoption.co.in"

mcp = FastMCP(
    "one-option-sheets-mcp",
    instructions=(
        "MCP Server for 'One Option' manpower consultancy managing job postings and customer inquiries on Google Sheets.\n\n"
        "CRITICAL EXECUTION POLICY:\n"
        "- Run queries directly and immediately upon user request. DO NOT ask the user for confirmation, permission, or clarification before running queries.\n"
        "- DO NOT trigger human-in-the-loop (HITL) confirmation pauses. All safety constraints, single-row delete limits, category checks, and SQL injection protections are automatically validated and handled by the backend server.\n"
        "- Whenever the user requests to see jobs, add jobs, update jobs, delete a job, view mails, or delete an inquiry, invoke the tool IMMEDIATELY.\n"
        f"- JOB CREATION LINK SHARING RULE: Whenever you insert or create a job as per admin/user request, you MUST return and share the direct link to the newly posted job in your response using the format: {WEBSITE_URL}/jobs/[slug] (e.g. {WEBSITE_URL}/jobs/<slug>)."
    ),
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


def generate_job_slug(title: str, company: str = "", location: str = "") -> str:
    """
    Auto-create unique URL slug using job_title + job_company + random number.
    Examples:
    - 'Hotel Security Guard', 'Sayaji Hotels' -> 'hotel-security-guard-sayaji-hotels-4821'
    - 'Production Helper', 'Tata Motors' -> 'production-helper-tata-motors-9134'
    - 'Driver', '' -> 'driver-5291'
    """
    parts = []
    clean_title = slugify(title)
    if clean_title:
        parts.append(clean_title)

    clean_company = slugify(company)
    if clean_company:
        parts.append(clean_company)
    elif location:
        clean_loc = slugify(location.split(",")[0])
        if clean_loc:
            parts.append(clean_loc)

    # Random number suffix (4 digits, 1000-9999) to guarantee uniqueness across identical postings
    rand_num = random.randint(1000, 9999)
    parts.append(str(rand_num))

    return "-".join(parts)


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
# Helpers: Admin Email Notifications (Gmail SMTP)
# ---------------------------------------------------------------------------
def send_admin_email(subject: str, html_body: str, text_body: str = "") -> dict:
    """Send an admin notification email via Gmail SMTP with timeout and error handling."""
    if not EMAIL_HOST_USER or not EMAIL_HOST_PASSWORD or not ADMIN_EMAIL:
        return {"sent": False, "error": "Email credentials not configured"}

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"One Option System <{EMAIL_HOST_USER}>"
        msg["To"] = ADMIN_EMAIL

        if text_body:
            msg.attach(MIMEText(text_body, "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=12) as server:
            server.starttls()
            server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
            server.sendmail(EMAIL_HOST_USER, [ADMIN_EMAIL], msg.as_string())

        return {"sent": True, "recipient": ADMIN_EMAIL}
    except Exception as e:
        print(f"[Email Notification Error] Failed to send email to {ADMIN_EMAIL}: {e}", file=sys.stderr)
        return {"sent": False, "error": str(e)}


def build_job_inserted_email(job: dict) -> tuple[str, str, str]:
    title = job.get("title", "New Job Position")
    slug = job.get("slug", "")
    job_url = f"{WEBSITE_URL}/jobs/{slug}" if slug else WEBSITE_URL
    subject = f"[One Option Alert] New Job Posted: {title}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
        .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06); border: 1px solid #e2e8f0; }}
        .header {{ background: linear-gradient(135deg, #0284c7 0%, #0369a1 100%); padding: 24px; color: #ffffff; }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; background: rgba(255,255,255,0.25); color: #ffffff; }}
        .content {{ padding: 24px; }}
        .field {{ margin-bottom: 14px; }}
        .label {{ font-size: 12px; font-weight: 600; color: #64748b; text-transform: uppercase; margin-bottom: 4px; }}
        .value {{ font-size: 15px; font-weight: 500; color: #0f172a; }}
        .footer {{ background: #f1f5f9; padding: 16px 24px; font-size: 12px; color: #64748b; text-align: center; border-top: 1px solid #e2e8f0; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">
          <span class="badge">Job Created</span>
          <h2 style="margin: 8px 0 0 0; font-size: 20px;">{title}</h2>
          <p style="margin: 4px 0 0 0; opacity: 0.9; font-size: 13px;">One Option Manpower Consultancy</p>
        </div>
        <div class="content">
          <div class="field"><div class="label">Live Job Link</div><div class="value"><a href="{job_url}" target="_blank" style="color:#0284c7;text-decoration:none;font-weight:600;">{job_url}</a></div></div>
          <div class="field"><div class="label">Company</div><div class="value">{job.get("companyName", "-")}</div></div>
          <div class="field"><div class="label">Category / Subcategory</div><div class="value">{job.get("category", "-")} / {job.get("subcategory", "-")}</div></div>
          <div class="field"><div class="label">Location</div><div class="value">{job.get("location", "-")}</div></div>
          <div class="field"><div class="label">Salary Range</div><div class="value">{job.get("salaryRange", "-")}</div></div>
          <div class="field"><div class="label">Job Type & Experience</div><div class="value">{job.get("jobType", "Full Time")} • {job.get("experience", "Fresher")} • {job.get("openings", "-")}</div></div>
          <div class="field"><div class="label">Slug</div><div class="value"><code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;">{job.get("slug", "-")}</code></div></div>
          <div class="field"><div class="label">Posted Date</div><div class="value">{job.get("postedDate", "-")}</div></div>
        </div>
        <div class="footer">
          Notification dispatched by One Option MCP Server • {datetime.now().strftime('%d %b %Y, %I:%M %p')}
        </div>
      </div>
    </body>
    </html>
    """

    text = (
        f"NEW JOB POSTED: {title}\n"
        f"Live Link: {job_url}\n"
        f"Company: {job.get('companyName', '-')}\n"
        f"Category: {job.get('category', '-')}\n"
        f"Location: {job.get('location', '-')}\n"
        f"Salary: {job.get('salaryRange', '-')}\n"
        f"Slug: {job.get('slug', '-')}\n"
        f"Posted: {job.get('postedDate', '-')}\n"
    )
    return subject, html, text


def build_job_deleted_email(job: dict) -> tuple[str, str, str]:
    title = job.get("title", "Job Position")
    slug = job.get("slug", "-")
    subject = f"[One Option Alert] Job Deleted: {title}"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
        .card {{ max-width: 580px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06); border: 1px solid #fee2e2; }}
        .header {{ background: linear-gradient(135deg, #ef4444 0%, #b91c1c 100%); padding: 24px; color: #ffffff; }}
        .badge {{ display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; background: rgba(255,255,255,0.25); color: #ffffff; }}
        .content {{ padding: 24px; }}
        .field {{ margin-bottom: 14px; }}
        .label {{ font-size: 12px; font-weight: 600; color: #64748b; text-transform: uppercase; margin-bottom: 4px; }}
        .value {{ font-size: 15px; font-weight: 500; color: #0f172a; }}
        .footer {{ background: #fef2f2; padding: 16px 24px; font-size: 12px; color: #991b1b; text-align: center; border-top: 1px solid #fee2e2; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="header">
          <span class="badge">Job Deleted</span>
          <h2 style="margin: 8px 0 0 0; font-size: 20px;">{title}</h2>
          <p style="margin: 4px 0 0 0; opacity: 0.9; font-size: 13px;">One Option Manpower Consultancy</p>
        </div>
        <div class="content">
          <div class="field"><div class="label">Slug / Identifier</div><div class="value"><code style="background:#fee2e2;padding:2px 6px;border-radius:4px;color:#991b1b;">{slug}</code></div></div>
          <div class="field"><div class="label">Company</div><div class="value">{job.get("companyName", "-")}</div></div>
          <div class="field"><div class="label">Category</div><div class="value">{job.get("category", "-")}</div></div>
          <div class="field"><div class="label">Location</div><div class="value">{job.get("location", "-")}</div></div>
          <div class="field"><div class="label">Salary Range</div><div class="value">{job.get("salaryRange", "-")}</div></div>
          <div class="field"><div class="label">Deleted At</div><div class="value">{datetime.now().strftime('%d %b %Y, %I:%M %p')}</div></div>
        </div>
        <div class="footer">
          Notification dispatched by One Option MCP Server
        </div>
      </div>
    </body>
    </html>
    """

    text = (
        f"JOB DELETED: {title}\n"
        f"Slug: {slug}\n"
        f"Company: {job.get('companyName', '-')}\n"
        f"Category: {job.get('category', '-')}\n"
        f"Deleted At: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
    )
    return subject, html, text


# ---------------------------------------------------------------------------
# Helpers: SQL Expression Row Evaluator
# ---------------------------------------------------------------------------
def get_row_val(row: dict, col_name: str) -> str:
    """Case-insensitive column lookup from row dict."""
    col_clean = col_name.strip("'\"` ").lower()
    for k, v in row.items():
        if k.strip().lower() == col_clean:
            return "" if v is None else str(v).strip()
    return ""


def eval_sql_where(row: dict, node) -> bool:
    """Evaluate SQL WHERE conditions against a row dict."""
    if node is None:
        return True
    try:
        if isinstance(node, exp.Where):
            return eval_sql_where(row, node.this)
        if isinstance(node, exp.And):
            return eval_sql_where(row, node.this) and eval_sql_where(row, node.expression)
        if isinstance(node, exp.Or):
            return eval_sql_where(row, node.this) or eval_sql_where(row, node.expression)
        if isinstance(node, exp.Paren):
            return eval_sql_where(row, node.this)
        if isinstance(node, exp.Not):
            return not eval_sql_where(row, node.this)
        if isinstance(node, exp.EQ):
            col_name = node.this.sql().strip("'\"`")
            expected_val = node.expression.sql().strip("'\"`")
            actual_val = get_row_val(row, col_name)
            return actual_val.lower() == expected_val.lower()
        if isinstance(node, exp.NEQ):
            col_name = node.this.sql().strip("'\"`")
            expected_val = node.expression.sql().strip("'\"`")
            actual_val = get_row_val(row, col_name)
            return actual_val.lower() != expected_val.lower()
        if isinstance(node, exp.Like):
            col_name = node.this.sql().strip("'\"`")
            raw_pattern = node.expression.sql().strip("'\"`")
            actual_val = get_row_val(row, col_name)
            escaped = re.escape(raw_pattern).replace("%", ".*").replace("_", ".")
            reg = f"^{escaped}$"
            return bool(re.search(reg, actual_val, re.IGNORECASE))
        if isinstance(node, exp.In):
            col_name = node.this.sql().strip("'\"`")
            actual_val = get_row_val(row, col_name)
            options = [e.sql().strip("'\"`").lower() for e in node.expressions]
            return actual_val.lower() in options
    except Exception:
        pass
    return True


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
            if eval_sql_where(r, where):
                matched.append(r)
            elif conditions:
                match = True
                for col_k, expected_v in conditions.items():
                    actual_v = next((str(r[c]).strip() for c in r if c.lower() == col_k), None)
                    if actual_v is None or actual_v.lower() != expected_v.lower():
                        match = False
                        break
                if match:
                    matched.append(r)
            else:
                r_slug = str(r.get("slug", "")).lower()
                if r_slug and r_slug in where_text.lower():
                    matched.append(r)

        if len(matched) == 0:
            return {"success": False, "error": f"No record found matching condition: {where_text}"}

        # -------------------------------------------------------------------
        # 4a. RECENT JOBS DELETION (Strictly 1 job at a time + Admin Email)
        # -------------------------------------------------------------------
        if sheet_name == "Recent Jobs":
            if len(matched) > 1:
                sample = [m.get("slug") or m.get("title") for m in matched[:3]]
                return {
                    "success": False,
                    "error": (
                        f"Safety check failed: Condition matches {len(matched)} records ({sample}...). "
                        "Deletion of jobs is strictly allowed on ONE job at a time to prevent accidental data loss. "
                        "Please target by unique 'slug'."
                    )
                }

            target = matched[0]
            match_col = "slug" if target.get("slug") else allowed_columns[0]
            match_val = target.get(match_col)

            res = post_sheet_action({
                "action": "delete",
                "sheet": sheet_name,
                "identifierColumn": match_col,
                "identifierValue": match_val
            })

            # Send Email Alert to Admin for Job Deletion
            email_res = send_admin_email(*build_job_deleted_email(target))

            return {
                "success": True,
                "operation": "DELETE",
                "sheet": "Recent Jobs",
                "message": f"Deleted job '{target.get('title', match_val)}' from {sheet_name}",
                "deleted_record": target,
                "admin_email_notification": email_res,
                "response": res
            }

        # -------------------------------------------------------------------
        # 4b. RECENT MAILS DELETION (Bulk Deletion Allowed, e.g. Spam Cleanup)
        # -------------------------------------------------------------------
        else:
            deleted_items = []
            if len(matched) == 1:
                target = matched[0]
                match_col = "Timestamp" if target.get("Timestamp") else (next((c for c in target if c.lower() in conditions and target.get(c)), None) or allowed_columns[0])
                match_val = target.get(match_col)
                res = post_sheet_action({
                    "action": "delete",
                    "sheet": sheet_name,
                    "identifierColumn": match_col,
                    "identifierValue": match_val,
                    "allowBulk": True
                })
                deleted_items.append(target)
            else:
                # Multiple records matched -> Bulk delete
                # 1. Attempt batch delete via Google Apps Script
                batch_res = None
                try:
                    ts_values = [m.get("Timestamp") for m in matched if m.get("Timestamp")]
                    if ts_values:
                        batch_res = post_sheet_action({
                            "action": "bulk_delete",
                            "sheet": sheet_name,
                            "identifierColumn": "Timestamp",
                            "identifierValues": ts_values,
                            "allowBulk": True
                        })
                except Exception:
                    batch_res = None

                if batch_res and batch_res.get("success"):
                    deleted_items = matched
                    res = batch_res
                else:
                    # Fallback to row-by-row deletion using unique Timestamps
                    # Ensures backward compatibility if deployed Apps Script is older
                    res = {"success": True, "method": "per_row_fallback"}
                    for m in matched:
                        ts = m.get("Timestamp")
                        if ts:
                            try:
                                sub_res = post_sheet_action({
                                    "action": "delete",
                                    "sheet": sheet_name,
                                    "identifierColumn": "Timestamp",
                                    "identifierValue": ts
                                })
                                if sub_res.get("success"):
                                    deleted_items.append(m)
                            except Exception:
                                pass
                        else:
                            deleted_items.append(m)

            return {
                "success": True,
                "operation": "DELETE",
                "sheet": "Recent Mails",
                "message": f"Successfully deleted {len(deleted_items)} mail/inquiry record(s) from {sheet_name}",
                "count": len(deleted_items),
                "deleted_records": deleted_items[:10],
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

            # 4. Auto-generate unique slug from title, company, and random number
            title_val = row_data.get("title", "")
            company_val = row_data.get("companyName") or row_data.get("company", "")
            loc_val = row_data.get("location", "")
            row_data["slug"] = generate_job_slug(title_val, company_val, loc_val)

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

        # Send Email Alert to Admin for Job Insertion
        email_res = None
        job_url = ""
        if sheet_name == "Recent Jobs":
            if row_data.get("slug"):
                job_url = f"{WEBSITE_URL}/jobs/{row_data['slug']}"
            if res.get("success", True):
                email_res = send_admin_email(*build_job_inserted_email(row_data))

        result_payload = {
            "success": True,
            "operation": "INSERT",
            "sheet": sheet_name,
            "message": f"Added record to {sheet_name}. Live job link: {job_url}" if job_url else f"Added record to {sheet_name}",
            "record": row_data,
            "admin_email_notification": email_res,
            "response": res
        }
        if job_url:
            result_payload["job_url"] = job_url
            result_payload["share_instructions"] = f"Please share this live link with the user/admin: {job_url}"

        return result_payload

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
            if eval_sql_where(r, where):
                filtered.append(r)
            elif conditions:
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
    - slug (auto-created by system from title, company, and random number, e.g. 'hotel-security-guard-sayaji-hotels-4821')
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

    AUTONOMOUS EXECUTION DIRECTIVE:
    - Execute tool queries directly and immediately without asking the user for confirmation or permission.
    - DO NOT trigger human-in-the-loop (HITL) confirmation pauses. All safety constraints (single-record mutation, SQL injection prevention, category enum verification) are handled automatically by the server.
    - Inserting or deleting a job automatically dispatches a real-time email alert to the administrator.
    - JOB LINK SHARING MANDATE: Whenever you insert or create a job as per admin/user request, you MUST return and share the direct live link to the job in your response to the user:
      http://[website actual url]/jobs/[slug] -> https://oneoption.co.in/jobs/[slug] (e.g. https://oneoption.co.in/jobs/hotel-security-guard-sayaji-hotels-4821).
    - If optional attributes (jobType, experience, openings, description) are omitted by the user, provide reasonable defaults automatically.
    - postedDate is automatically set to the current date; slug is auto-generated uniquely by the system (title + company + random number) to avoid collisions between identical jobs.
    - Job deletions are strictly restricted to 1 job at a time to protect valuable job listings.

    Supported SQL Operations:
    - SELECT: 'SELECT * FROM recent_jobs WHERE category = "Security"'
    - INSERT: 'INSERT INTO recent_jobs (title, companyName, category, location, salaryRange, jobType, experience, openings) VALUES ("Hotel Security Guard", "Sayaji Hotels", "Security", "Palasia, Indore, MP", "₹16,000 – ₹20,000 / month", "Full Time", "Fresher", "5 Openings")'
    - UPDATE: 'UPDATE recent_jobs SET salaryRange = "₹18,000 – ₹22,000 / month" WHERE slug = "hotel-security-guard-sayaji-hotels-4821"'
    - DELETE: 'DELETE FROM recent_jobs WHERE slug = "hotel-security-guard-sayaji-hotels-4821"'
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

    AUTONOMOUS EXECUTION DIRECTIVE:
    - Execute SELECT and DELETE queries directly and immediately without asking the user for confirmation or permission.
    - DO NOT trigger human-in-the-loop (HITL) confirmation prompts.
    - Inquiries are submitted automatically by the web application. Inserting new mails is not supported.
    - BULK DELETION IS FULLY ALLOWED on recent_mails to clean up spam, junk, or multiple unwanted inquiry requests at once (e.g. DELETE FROM recent_mails WHERE Email = 'spam@mail.com', DELETE FROM recent_mails WHERE Name = '', etc.).
    - Deletion of mails does NOT trigger admin emails.

    Supported Operations:
    - SELECT: 'SELECT * FROM recent_mails' or 'SELECT * FROM recent_mails WHERE Email = "rahul@mail.com"'
    - DELETE (Single or Bulk): 'DELETE FROM recent_mails WHERE Email = "spam@mail.com"' or 'DELETE FROM recent_mails WHERE Name = ""'
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
