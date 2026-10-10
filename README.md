# One Option Manpower — Google Sheets MCP Server

Lightweight MCP Server for **One Option** (manpower & recruitment consultancy) managing job postings and customer inquiries on Google Sheets using SQL-driven tools with strict safety protections and real-time administrator alerts.

---

## 🏢 Company Context
**One Option** is a manpower & staffing agency in Indore (Madhya Pradesh) connecting candidates with commercial job roles (security, catering, drivers, housekeeping, warehouse, logistics) and receiving employer/candidate requests.

---

## 🧰 The 2 MCP Tools

### 1. `manage_recent_jobs(query: str)`
Manages the `recent_jobs` table.

#### Allowed Categories Enum:
`['Catering', 'Construction', 'Driver', 'Hospitality', 'Housekeeping', 'Logistics', 'Manufacturing', 'Office', 'Security', 'Warehouse']`

#### Key Guidelines:
1. **Details handled automatically by the system**:
   - `postedDate`: Automatically generated as the current date in standard format (e.g. `'Oct 09, 2026'`).
   - `slug`: Automatically constructed using `job_title` + `job_company` + random number (e.g. `'hotel-security-guard-sayaji-hotels-4821'`) to avoid collisions between identical jobs.
   - `subcategory`: Defaults to the category or is generated automatically.
2. **Safety & Notifications**:
   - **Real-Time Admin Email Alerts**: Every time a job is **created (INSERT)** or **deleted (DELETE)**, an email notification is automatically dispatched to the admin (`ayushkumarrio44@gmail.com`).
   - **Strict Single-Job Deletion**: Deletions are strictly restricted to 1 job at a time to prevent accidental data loss.

#### Supported Operations:
- **SELECT**: `SELECT * FROM recent_jobs WHERE category = 'Security'`
- **INSERT**: `INSERT INTO recent_jobs (title, companyName, category, location, salaryRange, jobType, experience, openings) VALUES ('Light Vehicle Driver', 'Indore Transport', 'Driver', 'AB Road, Indore, MP', '₹16,000 – ₹21,000 / month', 'Full Time', '2–5 Years', '3 Openings')`
- **UPDATE**: `UPDATE recent_jobs SET salaryRange = '₹18,000 – ₹22,000 / month' WHERE slug = 'light-vehicle-driver-indore-transport-4821'`
- **DELETE**: `DELETE FROM recent_jobs WHERE slug = 'light-vehicle-driver-indore-transport-4821'` *(Strictly 1 job at a time; dispatches email alert!)*

---

### 2. `manage_recent_mails(query: str)`
Dedicated tool for viewing and deleting records in the `recent_mails` table (candidate applications and client inquiries).
- **Columns**: `Timestamp`, `Name`, `Email`, `Phone`, `City`, `Query Type`, `Job Name`, `Message`
- **Supported Operations**:
  - **SELECT**: `SELECT * FROM recent_mails` or `SELECT * FROM recent_mails WHERE Email = 'rahul@mail.com'`
  - **DELETE (Single or Bulk)**: 
    - `DELETE FROM recent_mails WHERE Email = 'spam@mail.com'`
    - `DELETE FROM recent_mails WHERE Name = ''`
    - `DELETE FROM recent_mails WHERE Message LIKE '%loan%'`
    *(Bulk deletion is fully supported to purge spam inquiries! No emails are sent for recent mails operations).*
- **Important Restrictions**:
  - **`INSERT` is NOT permitted**: Mails and inquiries are automatically generated and submitted by the web application contact form.
  - **`UPDATE` is NOT supported**: Inquiries are treated as immutable log entries.

---

## 📧 Email Notifications Configuration
The server supports automatic email notifications via Gmail SMTP:
- `EMAIL_HOST_USER`: `ayushkumarrio22@gmail.com` (Sender Gmail)
- `EMAIL_HOST_PASSWORD`: Google App Password
- `ADMIN_EMAIL`: `ayushkumarrio44@gmail.com` (Admin notification recipient)
- `EMAIL_HOST`: `smtp.gmail.com`
- `EMAIL_PORT`: `587`

Notifications are sent for:
- 🟢 **Job Creation**: Dispatches detailed email with company, role, salary, openings, and slug.
- 🔴 **Job Deletion**: Dispatches audit email with deleted job parameters and timestamp.

---

## 🛡️ Validation & Guardrails

1. **Table Enum Check**: Only `recent_jobs` and `recent_mails` are permitted.
2. **Category Validation**: Rejects unrecognized categories and guides the user toward the allowed categories.
3. **Missing Detail Check**: Blocks insertions if essential fields (`title`, `category`, `salaryRange`) are missing.
4. **Auto-Generated Date & Slug**: Injects current `postedDate` (`MMM dd, yyyy`) and constructs collision-resistant slugs (`job_title` + `job_company` + random number).
5. **Job Deletion Safety**: Strictly enforces single-item deletions for jobs.
6. **Bulk Mail Cleanup**: Permits bulk deletion for `recent_mails` to clean spam submissions safely.
7. **Harmful Command Prevention**: Blocks destructive commands (`DROP`, `TRUNCATE`, `ALTER`).
8. **Mail Insert Protection**: Disallows manual insertion of inquiries in `recent_mails`, reserving creation to the web application.

---

## 🚀 Running the Server Locally

```bash
# Run self-test suite
python api/index.py --test

# Run MCP server locally
python api/index.py
```

