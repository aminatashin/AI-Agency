import os
import sqlite3
import traceback
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from flask import Flask, request, Response, jsonify
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather


app = Flask(__name__)

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
VOICE_NAME = os.environ.get("VOICE_NAME", "alice")

QUESTIONS = [
    ("project_type", "Thanks for calling Italian Custom Cabinets. What type of project is this? For example, kitchen, bathroom, or closet."),
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


def safe_text(value, max_len=200):
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
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
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
            values.append(safe_text(value, 500))
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
        f"Call SID: {record.get('call_sid', '')}",
        f"From: {record.get('from_number') or 'Unknown'}",
        f"To: {record.get('to_number') or 'Unknown'}",
        f"Status: {record.get('call_status') or 'Unknown'}",
        f"Project type: {normalize_answer(record.get('project_type'))}",
        f"City: {normalize_answer(record.get('city'))}",
        f"Timeline: {normalize_answer(record.get('timeline'))}",
        f"Caller name: {normalize_answer(record.get('caller_name'))}",
        f"Last completed field: {normalize_answer(record.get('last_field'))}",
        f"Error: {normalize_answer(record.get('error_message'), fallback='None')}",
    ])


def send_email(subject, body):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO]):
        print("Email skipped: missing SMTP config")
        return False

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

    return True


def get_twilio_client():
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN]):
        return None
    return Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_sms(body):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, SMS_FROM, SMS_TO]):
        print("SMS skipped: missing Twilio SMS config")
        return False

    client = get_twilio_client()
    if client is None:
        print("SMS skipped: Twilio client not available")
        return False

    client.messages.create(
        body=body[:1500],
        from_=SMS_FROM,
        to=SMS_TO,
    )
    return True


def send_notifications_if_needed(call_sid, reason="completed"):
    record = get_call(call_sid)
    if not record:
        print(f"No record found for {call_sid}")
        return

    if int(record.get("notifications_sent") or 0) == 1:
        print(f"Notifications already sent for {call_sid}")
        return

    summary = build_summary(record)
    subject = f"ICC Call Lead - {normalize_answer(record.get('caller_name'), 'Unknown Caller')}"
    sms_body = (
        f"ICC Lead | "
        f"Name: {normalize_answer(record.get('caller_name'))} | "
        f"Project: {normalize_answer(record.get('project_type'))} | "
        f"City: {normalize_answer(record.get('city'))} | "
        f"Timeline: {normalize_answer(record.get('timeline'))} | "
        f"Status: {record.get('call_status') or 'Unknown'}"
    )

    email_ok = False
    sms_ok = False
    errors = []

    try:
        email_ok = send_email(subject, summary)
        print(f"Email sent: {email_ok}")
    except Exception as exc:
        print(traceback.format_exc())
        errors.append(f"Email failed: {exc}")

    try:
        sms_ok = send_sms(sms_body)
        print(f"SMS sent: {sms_ok}")
    except Exception as exc:
        print(traceback.format_exc())
        errors.append(f"SMS failed: {exc}")

    if email_ok or sms_ok:
        update_call(
            call_sid,
            notifications_sent=1,
            notification_reason=reason
        )
    elif errors:
        update_call(call_sid, error_message=" | ".join(errors))


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
        hints="kitchen,bathroom,closet,laundry room,office,garage,new construction,remodel,seattle,bellevue,kirkland,lynnwood,redmond,bothell"
    )
    gather.say(question_text, voice=VOICE_NAME, language=VOICE_LANGUAGE)
    response.append(gather)

    # If no speech is captured, Twilio should still POST because action_on_empty_result=True.
    # This redirect is only an extra hard fallback.
    response.redirect(f"{BASE_URL}/gather?step={step_number}", method="POST")

    return xml_response(str(response))


def say_and_hangup(text):
    response = VoiceResponse()
    response.say(text, voice=VOICE_NAME, language=VOICE_LANGUAGE)
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
        print(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/voice failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = safe_text(request.values.get("CallSid", ""))
    speech_result = safe_text(request.values.get("SpeechResult", ""))
    step_raw = request.args.get("step", "0")

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

        # Save answer for the current question if this step exists
        if 0 <= step_number < len(QUESTIONS):
            field_name = QUESTIONS[step_number][0]
            answer = normalize_answer(speech_result)
            update_call(call_sid, **{field_name: answer}, last_field=field_name)

        next_step = step_number + 1

        if next_step < len(QUESTIONS):
            return ask_question_twiml(QUESTIONS[next_step][1], next_step)

        # Finished all questions
        record = get_call(call_sid)
        update_call(call_sid, call_status="completed")

        return say_and_hangup(
            f"Thank you {normalize_answer(record.get('caller_name'), 'there')}. "
            f"We have your {normalize_answer(record.get('project_type'))} project in "
            f"{normalize_answer(record.get('city'))}, with timeline {normalize_answer(record.get('timeline'))}. "
            f"We will follow up shortly. Goodbye."
        )

    except Exception as exc:
        print(traceback.format_exc())
        if call_sid:
            update_call(call_sid, error_message=f"/gather failure: {safe_text(str(exc), 500)}")
        return say_and_hangup("Sorry, there was a system issue. We will follow up.")


@app.route("/status", methods=["POST"])
def status_callback():
    call_sid = safe_text(request.values.get("CallSid", ""))
    call_status = safe_text(request.values.get("CallStatus", ""))

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
            send_notifications_if_needed(call_sid, reason=f"status:{call_status}")

        return ("", 204)

    except Exception:
        print(traceback.format_exc())
        return ("", 204)


@app.errorhandler(Exception)
def handle_error(exc):
    call_sid = safe_text(request.values.get("CallSid", "")) if request else ""
    print(traceback.format_exc())

    if call_sid:
        try:
            update_call(call_sid, error_message=f"Unhandled app error: {safe_text(str(exc), 500)}")
        except Exception:
            print(traceback.format_exc())

    return say_and_hangup("Sorry, there was a system issue. We will follow up.")


# -----------------------------
# STARTUP
# -----------------------------
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)