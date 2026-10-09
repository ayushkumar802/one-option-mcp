# One Option Manpower — Google Sheets MCP Server

Lightweight MCP Server for **One Option** (manpower & recruitment consultancy) managing job postings and customer inquiries on Google Sheets using SQL-driven tools with strict safety protections.

---

## 🏢 Company Context
**One Option** is a manpower & staffing agency in Indore (Madhya Pradesh) connecting candidates with commercial job roles (security, catering, drivers, housekeeping, warehouse, logistics) and receiving employer/candidate requests.

---

## 🧰 The 2 MCP Tools

### 1. `manage_recent_jobs(query: str)`
Manages the `recent_jobs` table.

#### Allowed Categories Enum:
`['Catering', 'Construction', 'Driver', 'Hospitality', 'Housekeeping', 'Logistics', 'Manufacturing', 'Office', 'Security', 'Warehouse']`

#### Key Guidelines for the LLM:
1. **Details to ask the user if missing**:
   - `title`, `category` (must match the allowed list), `salaryRange`, `location`
   - `description`, `responsibilities`, `skills`, `experience`, `openings`, `jobType`
   *If any essential detail is missing from the user's prompt, the LLM will ASK the user to provide it before inserting.*
2. **Details handled automatically by the system**:
   - `postedDate`: Automatically generated as the current date in standard format (e.g. `'Oct 09, 2026'`). Never need to ask the user for this.
   - `slug`: Automatically derived from `title` and `location` (e.g. `'hotel-security-guard-indore'`).
   - `subcategory`: Defaults to the category or is generated automatically.

#### Supported Operations:
- **SELECT**: `SELECT * FROM recent_jobs WHERE category = 'Security'`
- **INSERT**: `INSERT INTO recent_jobs (title, companyName, category, location, salaryRange, jobType, experience, openings) VALUES ('Light Vehicle Driver', 'Indore Transport', 'Driver', 'AB Road, Indore, MP', '₹16,000 – ₹21,000 / month', 'Full Time', '2–5 Years', '3 Openings')`
- **UPDATE**: `UPDATE recent_jobs SET salaryRange = '₹18,000 – ₹22,000 / month' WHERE slug = 'hotel-security-guard-indore'`
- **DELETE**: `DELETE FROM recent_jobs WHERE slug = 'hotel-security-guard-indore'` *(Strictly 1 job at a time!)*

---

### 2. `manage_recent_mails(query: str)`
Dedicated tool for viewing and deleting records in the `recent_mails` table (candidate applications and client inquiries).
- **Columns**: `Timestamp`, `Name`, `Email`, `Phone`, `City`, `Query Type`, `Job Name`, `Message`
- **Supported Operations**:
  - **SELECT**: `SELECT * FROM recent_mails` or `SELECT * FROM recent_mails WHERE Email = 'rahul@mail.com'`
  - **DELETE**: `DELETE FROM recent_mails WHERE Email = 'rahul@mail.com'` *(Strictly 1 inquiry at a time!)*
- **Important Restrictions**:
  - **`INSERT` is NOT permitted**: Mails and inquiries are automatically generated and submitted by the web application contact form.
  - **`UPDATE` is NOT supported**: Inquiries are treated as immutable log entries.

---

## 🛡️ Validation & Guardrails

1. **Table Enum Check**: Only `recent_jobs` and `recent_mails` are permitted.
2. **Category Validation**: Rejects unrecognized categories and guides the user toward the allowed categories.
3. **Missing Detail Check**: Blocks insertions if essential fields (`title`, `category`, `salaryRange`) are missing, instructing the LLM to ask the user.
4. **Auto-Generated Date & Slug**: Injects current `postedDate` (`MMM dd, yyyy`) and constructs consistent slugs (`title` + city suffix).
5. **Strict Single-Item Deletion**: Blocks mass deletion queries (e.g. `DELETE without WHERE` or `WHERE 1=1`) and ensures deletion targets **exactly 1 item**.
6. **Harmful Command Prevention**: Blocks destructive commands (`DROP`, `TRUNCATE`, `ALTER`).
7. **Mail Insert Protection**: Disallows manual insertion of inquiries in `recent_mails`, reserving creation to the web application.

---

## 🚀 Running the Server

```bash
# Run self-test suite
python main.py --test

# Run MCP server
python main.py
```
