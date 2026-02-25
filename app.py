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
import datetime

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
def generate_ticket(section, ticket_type):
    ticket_id = f"COE2026-{uuid.uuid4().hex[:6].upper()}"

    # Generate QR Code
    qr = qrcode.make(ticket_id)
    qr = qr.resize((340, 340))
    qr = qr.convert("RGBA")

    # Load template
    template = Image.open("ticket_template.png").convert("RGBA")

    # Paste QR under "SCAN ME!"
    x, y = 1600, 190
    template.paste(qr, (x, y), qr)

    # Save final ticket
    output_path = f"tickets/{ticket_id}.png"
    template.save(output_path)

    # Save to Postgres
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO tickets (ticket_id, section, ticket_type, status) VALUES (%s, %s, %s, %s)",
              (ticket_id, section, ticket_type, "unused"))
    conn.commit()
    conn.close()

    return ticket_id, output_path

# --- Login Routes ---
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

# --- Routes ---
@app.route("/")
def home():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))
    return render_template("form.html")

@app.route("/generate", methods=["POST"])
def generate():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    section = request.form["section"]
    count = int(request.form["count"])
    ticket_type = request.form["ticket_type"]

    generated_ids = []
    for i in range(count):
        ticket_id, output_path = generate_ticket(section, ticket_type)
        generated_ids.append((ticket_id, output_path))

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
        conn.commit()
        result = {"status": "valid", "message": "✅ Ticket accepted"}

    c.execute("INSERT INTO scans (ticket_id, status) VALUES (%s, %s)", (ticket_id, result["status"]))
    conn.commit()
    conn.close()

    return jsonify(result)

@app.route("/admin")
def admin():
    if "role" not in session or session["role"] != "admin":
        return redirect(url_for("login"))

    section_filter = request.args.get("section")
    ajax = request.args.get("ajax")

    conn = get_db_connection()
    c = conn.cursor()

    if ajax == "logs":
        c.execute("SELECT ticket_id, status, timestamp FROM scans ORDER BY timestamp DESC LIMIT 20")
        logs = c.fetchall()
        conn.close()
        return jsonify(logs)

    if section_filter:
        c.execute("SELECT section, ticket_id, ticket_type, status FROM tickets WHERE section=%s ORDER BY ticket_id", (section_filter,))
    else:
        c.execute("SELECT section, ticket_id, ticket_type, status FROM tickets ORDER BY section, ticket_id")
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
    conn.close()

    return render_template("admin.html", grouped=grouped, summary=summary, section_filter=section_filter, logs=logs)

# --- Run App ---
if __name__ == "__main__":
    app.run(debug=True)