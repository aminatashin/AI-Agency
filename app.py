import os
import json
import sqlite3
import traceback
import smtplib
import logging
from datetime import datetime, timezone
from email.message import EmailMessage

import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, request, Response, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather


app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# -----------------------------
# ENV
# -----------------------------
DB_PATH = os.environ.get("DB_PATH", "calls.db")
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
SMS_FROM = os.environ.get("SMS_FROM", "")
SMS_TO = os.environ.get("SMS_TO", "")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")

VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "en-US")
VOICE_NAME = os.environ.get("VOICE_NAME", "Polly.Joanna")

HUMAN_TRANSFER_NUMBER = os.environ.get("HUMAN_TRANSFER_NUMBER", "")
GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

QUESTIONS = [
    ("project_type", "What type of project is this? For example, kitchen, bathroom, or closet."),
    ("city", "What city is the project in?"),
    ("timeline", "What is your timeline for the project?"),
    ("caller_name", "What is your name?"),
]


# -----------------------------
# HELPERS
# -----------------------------
def xml_response(twiml: str):
    return Response(twiml, mimetype="application/xml")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_text(value, max_len=300):
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    while "  " in text:
        text = text.replace("  ", " ")
    return text[:max_len]


def normalize_answer(value, fallback="Not provided"):
    text = safe_text(value)
    return text if text else fallback


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS calls (
            call_sid TEXT PRIMARY KEY,
            from_number TEXT,
            to_number TEXT,
            call_status TEXT,
            project_type TEXT,
            city TEXT,
            timeline TEXT,
            caller_name TEXT,
            last_field TEXT,
            notifications_sent INTEGER DEFAULT 0,
            notification_reason TEXT,
            error_message TEXT,
            transfer_requested INTEGER DEFAULT 0,
            transfer_to TEXT,
            sheet_logged INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_column_exists(column_name, column_sql):
    conn = get_conn()
    cols = conn.execute("PRAGMA table_info(calls)").fetchall()
    existing = {row["name"] for row in cols}
    if column_name not in existing:
        conn.execute(f"ALTER TABLE calls ADD COLUMN {column_sql}")
        conn.commit()
    conn.close()


def upsert_call(call_sid, from_number="", to_number="", call_status=""):
    ts = now_iso()
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO calls (call_sid, from_number, to_number, call_status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(call_sid) DO UPDATE SET
            from_number = COALESCE(NULLIF(excluded.from_number, ''), calls.from_number),
            to_number = COALESCE(NULLIF(excluded.to_number, ''), calls.to_number),
            call_status = COALESCE(NULLIF(excluded.call_status, ''), calls.call_status),
            updated_at = excluded.updated_at
        """,
        (call_sid, safe_text(from_number), safe_text(to_number), safe_text(call_status), ts, ts),
    )
    conn.commit()
    conn.close()


def update_call(call_sid, **kwargs):
    if not kwargs:
        return

    columns = []
    values = []
    for key, value in kwargs.items():
        columns.append(f"{key} = ?")
        if isinstance(value, str):
            values.append(safe_text(value, 1000))
        else:
            values.append(value)

    columns.append("updated_at = ?")
    values.append(now_iso())
    values.append(call_sid)

    conn = get_conn()
    conn.execute(f"UPDATE calls SET {', '.join(columns)} WHERE call_sid = ?", values)
    conn.commit()
    conn.close()


def get_call(call_sid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM calls WHERE call_sid = ?", (call_sid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def build_summary(record):
    if not record:
        return "No call record found."

    return "\n".join([
        "ICC call summary",
        f"Timestamp: {now_iso()}",
        f"Call SID: {record.get('call_sid', '')}",
        f"From: {record.get('from_number') or 'Unknown'}",
        f"To: {record.get('to_number') or 'Unknown'}",
        f"Status: {record.get('call_status') or 'Unknown'}",
        f"Transferred to human: {'Yes' if int(record.get('transfer_requested') or 0) == 1 else 'No'}",
        f"Transfer destination: {normalize_answer(record.get('transfer_to'), fallback='None')}",
        f"Project type: {normalize_answer(record.get('project_type'))}",
        f"City: {normalize_answer(record.get('city'))}",
        f"Timeline: {normalize_answer(record.get('timeline'))}",
        f"Caller name: {normalize_answer(record.get('caller_name'))}",
        f"Last completed field: {normalize_answer(record.get('last_field'))}",
        f"Error: {normalize_answer(record.get('error_message'), fallback='None')}",
    ])


# -----------------------------
# EMAIL / SMS
# -----------------------------
def send_email(subject, body):
    logging.info("EMAIL DEBUG: starting send_email")

    if not all([SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        logging.error("EMAIL DEBUG: missing SMTP config")
        logging.error(f"EMAIL DEBUG: EMAIL_FROM={EMAIL_FROM}")
        logging.error(f"EMAIL DEBUG: EMAIL_TO={EMAIL_TO}")
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg.set_content(body)

        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.starttls()
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as server:
                server.login(SMTP_USERNAME, SMTP_PASSWORD)
                server.send_message(msg)

        logging.info("EMAIL DEBUG: email sent successfully")
        return True

    except Exception as exc:
        logging.error(f"EMAIL DEBUG: send failed: {exc}")
        logging.error(traceback.format_exc())
        return False


def get_twilio_client():
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN]):
        return None
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(body):
    logging.info("SMS DEBUG: starting send_sms")

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, SMS_FROM, SMS_TO]):
        logging.error("SMS DEBUG: missing Twilio SMS config")
        logging.error(f"SMS DEBUG: SMS_FROM={SMS_FROM}")
        logging.error(f"SMS DEBUG: SMS_TO={SMS_TO}")
        return False

    try:
        client = get_twilio_client()
        if client is None:
            logging.error("SMS DEBUG: Twilio client not available")
            return False

        message = client.messages.create(
            body=body[:1500],
            from_=SMS_FROM,
            to=SMS_TO,
        )
        logging.info(f"SMS DEBUG: sms sent successfully sid={message.sid}")
        return True

    except Exception as exc:
        logging.error(f"SMS DEBUG: send failed: {exc}")
        logging.error(traceback.format_exc())
        return False


# -----------------------------
# GOOGLE SHEETS
# -----------------------------
def append_to_google_sheet_if_needed(call_sid):
    logging.info(f"SHEET DEBUG: starting append for {call_sid}")

    record = get_call(call_sid)
    if not record:
        logging.error("SHEET DEBUG: no record found")
        return False

    if int(record.get("sheet_logged") or 0) == 1:
        logging.info("SHEET DEBUG: already logged")
        return True

    if not GOOGLE_SHEET_NAME or not GOOGLE_SERVICE_ACCOUNT_JSON:
        logging.error("SHEET DEBUG: missing Google Sheet config")
        return False

    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        client = gspread.authorize(credentials)
        sheet = client.open(GOOGLE_SHEET_NAME).sheet1

        row = [
            now_iso(),
            record.get("call_sid", "") or "",
            record.get("from_number", "") or "",
            record.get("to_number", "") or "",
            record.get("call_status", "") or "",
            "Yes" if int(record.get("transfer_requested") or 0) == 1 else "No",
            record.get("transfer_to", "") or "",
            record.get("project_type", "") or "",
            record.get("city", "") or "",
            record.get("timeline", "") or "",
            record.get("caller_name", "") or "",
            record.get("error_message", "") or "",
        ]

        sheet.append_row(row, value_input_option="USER_ENTERED")
        update_call(call_sid, sheet_logged=1)
        logging.info("SHEET DEBUG: row appended successfully")
        return True

    except Exception as exc:
        logging.error(f"SHEET DEBUG: append failed: {exc}")
        logging.error(traceback.format_exc())
        return False


# -----------------------------
# NOTIFICATIONS
# -----------------------------
def process_end_of_call_if_needed(call_sid, reason="completed"):
    logging.info(f"FINALIZE DEBUG: starting process for {call_sid} reason={reason}")

    record = get_call(call_sid)
    if not record:
        logging.error("FINALIZE DEBUG: no record found")
        return

    # Email + SMS
    if int(record.get("notifications_sent") or 0) != 1:
        summary = build_summary(record)
        subject = f"ICC Call Lead - {normalize_answer(record.get('caller_name'), 'Unknown Caller')}"
        sms_body = (
            f"ICC Lead | "
            f"Human: {'Yes' if int(record.get('transfer_requested') or 0) == 1 else 'No'} | "
            f"Name: {normalize_answer(record.get('caller_name'))} | "
            f"Project: {normalize_answer(record.get('project_type'))} | "
            f"City: {normalize_answer(record.get('city'))} | "
            f"Timeline: {normalize_answer(record.get('timeline'))} | "
            f"Status: {record.get('call_status') or 'Unknown'}"
        )

        email_ok = send_email(subject, summary)
        sms_ok = send_sms(sms_body)

        if email_ok or sms_ok:
            update_call(call_sid, notifications_sent=1, notification_reason=reason)
            logging.info("FINALIZE DEBUG: notifications marked as sent")
        else:
            update_call(call_sid, error_message="Email and SMS both failed")
            logging.error("FINALIZE DEBUG: email and sms both failed")
    else:
        logging.info("FINALIZE DEBUG: notifications already sent")

    # Google Sheet
    append_to_google_sheet_if_needed(call_sid)


# -----------------------------
# TWIML BUILDERS
# -----------------------------
def intro_menu_twiml():
    response = VoiceResponse()

    gather = Gather(
        input="dtmf",
        num_digits=1,
        action=f"{BASE_URL}/menu",
        method="POST",
        timeout=5,
    )
    gather.say(
        "Thank you for calling Italian Custom Cabinets. "
        "For a new project or estimate, stay on the line. "
        "To speak with someone right away, press 1.",
        voice=VOICE_NAME,
        language=VOICE_LANGUAGE,
    )
    response.append(gather)

    response.redirect(f"{BASE_URL}/start-intake", method="POST")
    return xml_response(str(response))


def ask_question_twiml(question_text, step_number):
    response = VoiceResponse()

    gather = Gather(
        input="speech",
        action=f"{BASE_URL}/gather?step={step_number}",
        method="POST",
        language=VOICE_LANGUAGE,
        timeout=4,
        speech_timeout="auto",
        action_on_empty_result=True,
        hints="kitchen,bathroom,closet,laundry room,office,garage,new construction,remodel,seattle,bellevue,kirkland,lynnwood,redmond,bothell",
    )
    gather.say(question_text, voice=VOICE_NAME, language=VOICE_LANGUAGE)
    response.append(gather)

    response.redirect(f"{BASE_URL}/gather?step={step_number}", method="POST")
    return xml_response(str(response))


def say_and_hangup(text):
    response = VoiceResponse()
    response.say(text, voice=VOICE_NAME, language=VOICE_LANGUAGE)
    response.hangup()
    return xml_response(str(response))


def transfer_to_human_twiml(call_sid):
    response = VoiceResponse()

    if not HUMAN_TRANSFER_NUMBER:
        update_call(call_sid, error_message="Human transfer requested but HUMAN_TRANSFER_NUMBER is missing")
        response.say(
            "Sorry, we could not connect you right now. We will follow up shortly.",
            voice=VOICE_NAME,
            language=VOICE_LANGUAGE,
        )
        response.hangup()
        return xml_response(str(response))

    update_call(
        call_sid,
        transfer_requested=1,
        transfer_to=HUMAN_TRANSFER_NUMBER,
        last_field="human_transfer",
    )

    response.say("Please hold while I connect you.", voice=VOICE_NAME, language=VOICE_LANGUAGE)
    response.dial(HUMAN_TRANSFER_NUMBER, timeout=20, caller_id=request.values.get("To", ""))
    response.say("Sorry, no one is available right now. We will follow up shortly.", voice=VOICE_NAME, language=VOICE_LANGUAGE)
    response.hangup()
    return xml_response(str(response))


# -----------------------------
# ROUTES
# -----------------------------
@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "icc-voice-agent"}), 200


@app.route("/sms", methods=["POST"])
def sms_webhook():
    return ("OK", 200)


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = safe_text(request.values.get("CallSid", ""))
    logging.info(f"VOICE DEBUG: incoming call sid={call_sid}")

    try:
        if not call_sid:
            return say_and_hangup("Sorry, this call could not be processed.")

        upsert_call(
            call_sid=call_sid,
            from_number=request.values.get("From", ""),
            to_number=request.values.get("To", ""),
            call_status=request.values.get("CallStatus", "in-progress"),
        )

        return intro_menu_twiml()

    except Exception as exc:
        logging.error(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/voice failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/menu", methods=["POST"])
def menu():
    call_sid = safe_text(request.values.get("CallSid", ""))
    digit = safe_text(request.values.get("Digits", ""))
    logging.info(f"MENU DEBUG: sid={call_sid} digit={digit}")

    try:
        if not call_sid:
            return say_and_hangup("Sorry, this call could not be processed.")

        upsert_call(
            call_sid=call_sid,
            from_number=request.values.get("From", ""),
            to_number=request.values.get("To", ""),
            call_status=request.values.get("CallStatus", "in-progress"),
        )

        if digit == "1":
            return transfer_to_human_twiml(call_sid)

        return ask_question_twiml(QUESTIONS[0][1], 0)

    except Exception as exc:
        logging.error(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/menu failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/start-intake", methods=["POST"])
def start_intake():
    call_sid = safe_text(request.values.get("CallSid", ""))
    logging.info(f"START INTAKE DEBUG: sid={call_sid}")

    try:
        if not call_sid:
            return say_and_hangup("Sorry, this call could not be processed.")

        upsert_call(
            call_sid=call_sid,
            from_number=request.values.get("From", ""),
            to_number=request.values.get("To", ""),
            call_status=request.values.get("CallStatus", "in-progress"),
        )

        return ask_question_twiml(QUESTIONS[0][1], 0)

    except Exception as exc:
        logging.error(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/start-intake failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = safe_text(request.values.get("CallSid", ""))
    speech_result = safe_text(request.values.get("SpeechResult", ""))
    step_raw = request.args.get("step", "0")
    logging.info(f"GATHER DEBUG: sid={call_sid} step={step_raw} speech={speech_result}")

    try:
        if not call_sid:
            return say_and_hangup("Sorry, this call could not be processed.")

        try:
            step_number = int(step_raw)
        except ValueError:
            step_number = 0

        upsert_call(
            call_sid=call_sid,
            from_number=request.values.get("From", ""),
            to_number=request.values.get("To", ""),
            call_status=request.values.get("CallStatus", "in-progress"),
        )

        if 0 <= step_number < len(QUESTIONS):
            field_name = QUESTIONS[step_number][0]
            answer = normalize_answer(speech_result)
            update_call(call_sid, **{field_name: answer}, last_field=field_name)

        next_step = step_number + 1

        if next_step < len(QUESTIONS):
            return ask_question_twiml(QUESTIONS[next_step][1], next_step)

        record = get_call(call_sid)
        update_call(call_sid, call_status="completed")
        logging.info(f"GATHER DEBUG: completed call sid={call_sid}")

        return say_and_hangup(
            f"Thank you {normalize_answer(record.get('caller_name'), 'there')}. "
            f"We have your {normalize_answer(record.get('project_type'))} project in "
            f"{normalize_answer(record.get('city'))}, with timeline {normalize_answer(record.get('timeline'))}. "
            f"We will follow up shortly. Goodbye."
        )

    except Exception as exc:
        logging.error(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/gather failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/status", methods=["POST"])
def status_callback():
    call_sid = safe_text(request.values.get("CallSid", ""))
    call_status = safe_text(request.values.get("CallStatus", ""))
    logging.info(f"STATUS DEBUG: sid={call_sid} status={call_status}")

    try:
        if not call_sid:
            return ("", 204)

        upsert_call(
            call_sid=call_sid,
            from_number=request.values.get("From", ""),
            to_number=request.values.get("To", ""),
            call_status=call_status,
        )

        if call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
            process_end_of_call_if_needed(call_sid, reason=f"status:{call_status}")

        return ("", 204)

    except Exception:
        logging.error(traceback.format_exc())
        return ("", 204)


@app.errorhandler(Exception)
def handle_error(exc):
    call_sid = safe_text(request.values.get("CallSid", "")) if request else ""
    logging.error(traceback.format_exc())

    if call_sid:
        try:
            update_call(call_sid, error_message=f"Unhandled app error: {safe_text(str(exc), 500)}")
        except Exception:
            logging.error(traceback.format_exc())

    return say_and_hangup("Sorry, there was a system issue. We will follow up.")


# -----------------------------
# STARTUP
# -----------------------------
init_db()
ensure_column_exists("transfer_requested", "transfer_requested INTEGER DEFAULT 0")
ensure_column_exists("transfer_to", "transfer_to TEXT")
ensure_column_exists("sheet_logged", "sheet_logged INTEGER DEFAULT 0")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)