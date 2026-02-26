from flask import Flask, render_template, request, jsonify, session, send_file, redirect, url_for
import qrcode
import uuid
import os
from PIL import Image
import psycopg2
import zipfile
import io
import csv
import xlsxwriter

app = Flask(__name__)
app.secret_key = "secret_key_for_session"

# Ensure folders exist
os.makedirs("static", exist_ok=True)
os.makedirs("tickets", exist_ok=True)
os.makedirs("backups", exist_ok=True)

# --- Database Connection ---
def get_db_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASS"],
        dbname=os.environ["DB_NAME"],
        port=os.environ.get("DB_PORT", 5432)
    )

# --- Simple User Accounts ---
USERS = {
    "coeadmin": {"password": "Concert2026!", "role": "admin"},
    "coescanner": {"password": "Scan4Cause!", "role": "scanner"}
}

# --- Ticket Generator ---
def generate_ticket(section, ticket_type, template, cursor):
    ticket_id = f"COE2026-{uuid.uuid4().hex[:6].upper()}"

    qr = qrcode.make(ticket_id)
    qr = qr.resize((250, 250))
    qr = qr.convert("RGBA")

    ticket_img = template.copy()
    x, y = 1600, 190
    ticket_img.paste(qr, (x, y), qr)

    output_path = f"tickets/{ticket_id}.png"
    ticket_img.save(output_path)

    cursor.execute("INSERT INTO tickets (ticket_id, section, ticket_type, status) VALUES (%s, %s, %s, %s)",
                   (ticket_id, section, ticket_type, "unused"))

    return ticket_id, output_path

# --- Routes ---
@app.route("/")
def home():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))
    return render_template("form.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        user = USERS.get(username)
        if user and user["password"] == password:
            session["user"] = username
            session["role"] = user["role"]
            if user["role"] == "admin":
                return redirect(url_for("admin"))
            else:
                return redirect(url_for("scanner"))
        else:
            return "❌ Invalid credentials"
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return render_template("logout.html")

@app.route("/generate", methods=["POST"])
def generate():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    section = request.form["section"]
    count = int(request.form["count"])
    ticket_type = request.form["ticket_type"]

    template = Image.open("ticket_template.png").convert("RGBA")
    generated_ids = []
    batch_size = 20

    for start in range(0, count, batch_size):
        end = min(start + batch_size, count)
        conn = get_db_connection()
        c = conn.cursor()
        for i in range(start, end):
            ticket_id, output_path = generate_ticket(section, ticket_type, template, c)
            generated_ids.append((ticket_id, output_path))
        conn.commit()
        conn.close()

    session["last_results"] = {
        "section": section,
        "count": count,
        "ids": generated_ids
    }

    return render_template("result.html", ids=generated_ids, section=section, count=count)

@app.route("/results")
def results():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    last = session.get("last_results")
    if not last:
        return "No tickets generated yet."
    return render_template("result.html", ids=last["ids"], section=last["section"], count=last["count"])

@app.route("/download_zip")
def download_zip():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    last = session.get("last_results")
    if not last:
        return "No tickets generated yet."

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zipf:
        for ticket_id, path in last["ids"]:
            zipf.write(path, os.path.basename(path))
    zip_buffer.seek(0)

    return send_file(
        zip_buffer,
        as_attachment=True,
        download_name=f"{last['section']}_tickets.zip",
        mimetype="application/zip"
    )

@app.route("/scanner")
def scanner():
    if "role" not in session or session["role"] != "scanner":
        return redirect(url_for("login"))
    return render_template("scanner.html")

@app.route("/verify", methods=["POST"])
def verify():
    ticket_id = request.json.get("ticket_id")
    conn = get_db_connection()
    c = conn.cursor()

    c.execute("SELECT status FROM tickets WHERE ticket_id=%s", (ticket_id,))
    row = c.fetchone()

    if not row:
        result = {"status": "invalid", "message": "❌ Ticket not found"}
    elif row[0] == "used":
        result = {"status": "invalid", "message": "⚠️ Ticket already used"}
    else:
        c.execute("UPDATE tickets SET status='used' WHERE ticket_id=%s", (ticket_id,))
        result = {"status": "valid", "message": "✅ Ticket accepted"}

    c.execute("INSERT INTO scans (ticket_id, status) VALUES (%s, %s)", (ticket_id, result["status"]))
    conn.commit()
    conn.close()

    return jsonify(result)

# --- Admin Dashboard ---
@app.route("/admin")
def admin():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    section_filter = request.args.get("section")
    status_filter = request.args.get("status")
    search_query = request.args.get("search")
    page_size = int(request.args.get("page_size", 10))
    ajax = request.args.get("ajax")

    conn = get_db_connection()
    c = conn.cursor()

    if ajax == "logs":
        c.execute("SELECT ticket_id, status, timestamp FROM scans ORDER BY timestamp DESC LIMIT 20")
        logs = c.fetchall()
        conn.close()
        return jsonify(logs)

    query = "SELECT section, ticket_id, ticket_type, status FROM tickets WHERE 1=1"
    params = []

    if section_filter:
        query += " AND section=%s"
        params.append(section_filter)

    if status_filter:
        query += " AND status=%s"
        params.append(status_filter)

    if search_query:
        query += " AND ticket_id ILIKE %s"
        params.append(f"%{search_query}%")

    query += " ORDER BY section, ticket_id LIMIT %s"
    params.append(page_size)

    c.execute(query, tuple(params))
    rows = c.fetchall()

    grouped = {}
    summary = {}
    for section, ticket_id, ticket_type, status in rows:
        if section not in grouped:
            grouped[section] = []
            summary[section] = {"valid":0, "used":0, "invalid":0}
        grouped[section].append((ticket_id, ticket_type, status))
        if status in summary[section]:
            summary[section][status] += 1

    c.execute("SELECT ticket_id, status, timestamp FROM scans ORDER BY timestamp DESC LIMIT 20")
    logs = c.fetchall()

    c.execute("SELECT DISTINCT section FROM tickets ORDER BY section")
    sections = [row[0] for row in c.fetchall()]
    conn.close()

    return render_template("admin.html", grouped=grouped, summary=summary,
                           section_filter=section_filter, logs=logs,
                           search_query=search_query, status_filter=status_filter,
                           page_size=page_size, sections=sections)

# --- Delete Ticket ---
@app.route("/delete_ticket/<ticket_id>", methods=["POST"])
def delete_ticket(ticket_id):
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM tickets WHERE ticket_id=%s", (ticket_id,))
    conn.commit()
    conn.close()

    session["message"] = f"✅ Ticket {ticket_id} deleted successfully."
    return redirect(url_for("admin"))

# --- Reset ---
@app.route("/reset")
def reset():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM tickets")
    c.execute("DELETE FROM scans")
    conn.commit()
    conn.close()

    session["message"] = "🗑️ All tickets and logs cleared."
    return redirect(url_for("admin"))

# --- Export Routes ---
@app.route("/export_csv")
def export_csv():
    section = request.args.get("section")
    conn = get_db_connection()
    c = conn.cursor()
    if section:
        c.execute("SELECT ticket_id, section, ticket_type, status FROM tickets WHERE section=%s", (section,))
    else:
        c.execute("SELECT ticket_id, section, ticket_type, status FROM tickets")
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Ticket ID", "Section", "Type", "Status"])
    for r in rows:
        writer.writerow(r)

    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode("utf-8")),
                     mimetype="text/csv",
                     as_attachment=True,
                     download_name="tickets.csv")

@app.route("/export_excel")
def export_excel():
    section = request.args.get("section")
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet()

    conn = get_db_connection()
    c = conn.cursor()
    if section:
        c.execute("SELECT ticket_id, section, ticket_type, status FROM tickets WHERE section=%s", (section,))
    else:
        c.execute("SELECT ticket_id, section, ticket_type, status FROM tickets")
    rows = c.fetchall()
    conn.close()

    headers = ["Ticket ID", "Section", "Type", "Status"]
    for col, h in enumerate(headers):
        worksheet.write(0, col, h)
    for row_idx, row in enumerate(rows, start=1):
        for col_idx, val in enumerate(row):
            worksheet.write(row_idx, col_idx, val)

    workbook.close()
    output.seek(0)
    return send_file(output,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name="tickets.xlsx")

@app.route("/export_logs_csv")
def export_logs_csv():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT ticket_id, status, timestamp FROM scans ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Ticket ID", "Status", "Timestamp"])
    for r in rows:
        writer.writerow(r)

    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode("utf-8")),
                     mimetype="text/csv",
                     as_attachment=True,
                     download_name="scan_logs.csv")

@app.route("/export_logs_excel")
def export_logs_excel():
    output = io.BytesIO()
    workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    worksheet = workbook.add_worksheet()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT ticket_id, status, timestamp FROM scans ORDER BY timestamp DESC")
    rows = c.fetchall()
    conn.close()

    headers = ["Ticket ID", "Status", "Timestamp"]
    for col, h in enumerate(headers):
        worksheet.write(0, col, h)
    for row_idx, row in enumerate(rows, start=1):
        for col_idx, val in enumerate(row):
            worksheet.write(row_idx, col_idx, val)

    workbook.close()
    output.seek(0)
    return send_file(output,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name="scan_logs.xlsx")

# --- Run App ---
if __name__ == "__main__":
    app.run(debug=True)