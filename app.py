import os
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request
from dotenv import load_dotenv
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

load_dotenv()
app = Flask(__name__)

# -----------------------------
# ENV / CONFIG
# -----------------------------
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT", "465")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
ALERT_SMS_TO = os.getenv("ALERT_SMS_TO")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

CALLS = {}

FIELDS = ["project_type", "city", "timeline", "name"]

QUESTIONS = {
    "project_type": "Are you calling about a kitchen, bathroom, closet, or another project?",
    "city": "What city is the project in?",
    "timeline": "What is your timeline for the project?",
    "name": "May I have your name, please?"
}


# -----------------------------
# HELPERS
# -----------------------------
def send_email(subject, body):
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, ALERT_EMAIL_TO, ALERT_EMAIL_FROM]):
        print("Email skipped: missing SMTP settings")
        return False

    msg = MIMEMultipart()
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, int(SMTP_PORT)) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        print("✅ Email sent successfully")
        return True
    except Exception as e:
        print("❌ Email failed:", e)
        return False


def send_sms(body):
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, ALERT_SMS_TO]):
        print("SMS skipped: missing Twilio settings")
        return False

    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = twilio_client.messages.create(
            body=body,
            from_=TWILIO_FROM_NUMBER,
            to=ALERT_SMS_TO
        )
        print("✅ SMS sent:", msg.sid)
        return True
    except Exception as e:
        print("❌ SMS failed:", e)
        return False


def create_call(call_sid, caller_number):
    CALLS[call_sid] = {
        "caller_number": caller_number,
        "current_step": 0,
        "answers": {
            "project_type": "",
            "city": "",
            "timeline": "",
            "name": ""
        },
        "transcript": [],
        "notified": False
    }


def current_field(call_sid):
    step = CALLS[call_sid]["current_step"]
    if step < len(FIELDS):
        return FIELDS[step]
    return None


def next_question(call_sid):
    field = current_field(call_sid)
    if field:
        return QUESTIONS[field]
    return None


def save_answer(call_sid, speech_text):
    field = current_field(call_sid)
    if field:
        CALLS[call_sid]["answers"][field] = speech_text.strip()[:120]
        CALLS[call_sid]["current_step"] += 1


def build_summary(call_sid):
    data = CALLS[call_sid]
    a = data["answers"]
    transcript = "\n".join(data["transcript"][-8:])

    return f"""
New ICC Voice Lead

Caller Number: {data['caller_number']}
Project Type: {a['project_type']}
City: {a['city']}
Timeline: {a['timeline']}
Name: {a['name']}

Transcript:
{transcript}
""".strip()


def build_spoken_summary(call_sid):
    a = CALLS[call_sid]["answers"]

    parts = []

    if a["project_type"]:
        parts.append(f"project type: {a['project_type']}")
    if a["city"]:
        parts.append(f"city: {a['city']}")
    if a["timeline"]:
        parts.append(f"timeline: {a['timeline']}")
    if a["name"]:
        parts.append(f"name: {a['name']}")

    if not parts:
        return "I have recorded your request."

    return "Here is a quick summary. " + ". ".join(parts) + "."


def save_lead_file(call_sid):
    data = CALLS[call_sid]
    record = {
        "timestamp": datetime.now().isoformat(),
        "caller_number": data["caller_number"],
        "answers": data["answers"],
        "transcript": data["transcript"][-8:]
    }
    with open("voice_leads.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def notify_if_needed(call_sid):
    data = CALLS[call_sid]
    if data["notified"]:
        return

    summary = build_summary(call_sid)
    save_lead_file(call_sid)

    email_ok = send_email("New ICC Voice Lead", summary)
    sms_ok = send_sms(
        f"New ICC lead from {data['caller_number']} | "
        f"{data['answers']['project_type']} | {data['answers']['city']} | {data['answers']['name']}"
    )

    print(f"Email sent: {email_ok} | SMS sent: {sms_ok}")
    data["notified"] = True


def wants_human(text):
    lower = text.lower()
    triggers = [
        "human", "person", "agent", "representative",
        "call me", "callback", "someone call me"
    ]
    return any(t in lower for t in triggers)


# -----------------------------
# ROUTES
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    return "ICC AI Voice Agent is running.", 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid", "")
    caller_number = request.form.get("From", "Unknown")

    create_call(call_sid, caller_number)

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        timeout=4,
        action=f"{PUBLIC_BASE_URL}/gather",
        method="POST"
    )
    gather.say(
        "Hello. Thank you for calling Italian Custom Cabinets. "
        "Please answer briefly. "
        + QUESTIONS["project_type"]
    )
    vr.append(gather)

    vr.say("I did not catch that. Please call again.")
    return str(vr), 200, {"Content-Type": "application/xml"}


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = request.form.get("CallSid", "")
    caller_number = request.form.get("From", "Unknown")
    speech_result = request.form.get("SpeechResult", "").strip()

    if call_sid not in CALLS:
        create_call(call_sid, caller_number)

    vr = VoiceResponse()

    if not speech_result:
        gather_more = Gather(
            input="speech",
            speech_timeout="auto",
            timeout=4,
            action=f"{PUBLIC_BASE_URL}/gather",
            method="POST"
        )
        gather_more.say("Sorry, I did not catch that. Please answer briefly.")
        vr.append(gather_more)
        vr.say("Thank you for calling. Goodbye.")
        vr.hangup()
        return str(vr), 200, {"Content-Type": "application/xml"}

    speech_result = speech_result[:160]
    CALLS[call_sid]["transcript"].append(f"Caller: {speech_result}")
    CALLS[call_sid]["transcript"] = CALLS[call_sid]["transcript"][-8:]

    # If caller asks for human at any point, finish early and notify
    if wants_human(speech_result):
        notify_if_needed(call_sid)
        summary_text = build_spoken_summary(call_sid)
        vr.say(summary_text)
        vr.say("Thank you. A team member will follow up with you soon. Goodbye.")
        vr.hangup()
        return str(vr), 200, {"Content-Type": "application/xml"}

    field = current_field(call_sid)
    if field:
        save_answer(call_sid, speech_result)

    # If all questions are complete, summarize + notify + end call
    if CALLS[call_sid]["current_step"] >= len(FIELDS):
        notify_if_needed(call_sid)
        summary_text = build_spoken_summary(call_sid)
        vr.say(summary_text)
        vr.say("Thank you. A team member will follow up with you soon. Goodbye.")
        vr.hangup()
        return str(vr), 200, {"Content-Type": "application/xml"}

    # Ask next fixed question
    question = next_question(call_sid)
    CALLS[call_sid]["transcript"].append(f"AI: {question}")
    CALLS[call_sid]["transcript"] = CALLS[call_sid]["transcript"][-8:]

    gather_more = Gather(
        input="speech",
        speech_timeout="auto",
        timeout=4,
        action=f"{PUBLIC_BASE_URL}/gather",
        method="POST"
    )
    gather_more.say(question)
    vr.append(gather_more)

    vr.say("Thank you for calling. Goodbye.")
    vr.hangup()

    return str(vr), 200, {"Content-Type": "application/xml"}


@app.route("/sms", methods=["POST"])
def sms():
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)