import os
import json
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request
from openai import OpenAI
from dotenv import load_dotenv
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client as TwilioClient

load_dotenv()

app = Flask(__name__)

# -----------------------------
# ENV / CONFIG
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
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

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is missing")

client = OpenAI(api_key=OPENAI_API_KEY)

# In-memory call state for testing
CALLS = {}

ICC_PUBLIC_NAME = "Italian Custom Cabinets"
ICC_PUBLIC_PHONE = "206-898-7677"
ICC_PUBLIC_EMAIL = "shah@italiancustomcabinets.com"

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
        print("Email sent successfully")
        return True
    except Exception as e:
        print("Email failed:", e)
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
        print("SMS sent:", msg.sid)
        return True
    except Exception as e:
        print("SMS failed:", e)
        return False


def ai_reply(user_text: str) -> str:
    system_prompt = """
You are the official AI phone concierge for Italian Custom Cabinets (ICC).

Your tone:
- warm
- concise
- professional
- helpful

Rules:
- Speak naturally for phone calls
- Keep each answer short
- If the caller asks for pricing, explain that final pricing depends on plans, measurements, and scope
- If the caller asks for a human, say you will arrange follow-up
- Ask only one question at a time
- Focus on collecting:
  1. project type
  2. timeline
  3. whether they want a callback
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
    )
    return response.output_text.strip()


def log_call_summary(call_sid, caller_number):
    call_data = CALLS.get(call_sid, {})
    transcript = call_data.get("transcript", [])
    notes = "\n".join(transcript)

    summary = f"""
New ICC Voice Lead

Call SID: {call_sid}
Caller Number: {caller_number}

Transcript:
{notes}
""".strip()

    record = {
        "timestamp": datetime.now().isoformat(),
        "call_sid": call_sid,
        "caller_number": caller_number,
        "transcript": transcript,
    }

    with open("voice_leads.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    email_ok = send_email("New ICC Voice Lead", summary)
    sms_ok = send_sms(f"New ICC voice lead from {caller_number}")

    print(f"Email sent: {email_ok} | SMS sent: {sms_ok}")


# -----------------------------
# ROUTES
# -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/voice", methods=["POST"])
def voice():
    call_sid = request.form.get("CallSid", "")
    caller_number = request.form.get("From", "Unknown")

    CALLS[call_sid] = {
        "caller_number": caller_number,
        "transcript": [],
    }

    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        speech_timeout="auto",
        action=f"{PUBLIC_BASE_URL}/gather",
        method="POST"
    )
    gather.say(
        "Hello. Thank you for calling Italian Custom Cabinets. "
        "How can I help you today?"
    )
    vr.append(gather)

    vr.say("I didn't catch that. Please call again.")
    return str(vr), 200, {"Content-Type": "application/xml"}


@app.route("/gather", methods=["POST"])
def gather():
    call_sid = request.form.get("CallSid", "")
    caller_number = request.form.get("From", "Unknown")
    speech_result = request.form.get("SpeechResult", "").strip()

    if call_sid not in CALLS:
        CALLS[call_sid] = {
            "caller_number": caller_number,
            "transcript": [],
        }

    if speech_result:
        CALLS[call_sid]["transcript"].append(f"Caller: {speech_result}")

    reply_text = ai_reply(speech_result or "The caller was silent.")
    CALLS[call_sid]["transcript"].append(f"AI: {reply_text}")

    vr = VoiceResponse()

    # simple handoff detection
    lower = speech_result.lower()
    wants_human = any(x in lower for x in [
        "human", "person", "agent", "representative", "call me", "callback"
    ])

    if wants_human:
        vr.say(
            "Absolutely. I will send your call details to our team for follow-up. "
            "Thank you for calling."
        )
        log_call_summary(call_sid, caller_number)
        vr.hangup()
        return str(vr), 200, {"Content-Type": "application/xml"}

    gather_more = Gather(
        input="speech",
        speech_timeout="auto",
        action=f"{PUBLIC_BASE_URL}/gather",
        method="POST"
    )
    gather_more.say(reply_text)
    vr.append(gather_more)

    vr.say("Thank you for calling Italian Custom Cabinets. Goodbye.")
    log_call_summary(call_sid, caller_number)
    vr.hangup()

    return str(vr), 200, {"Content-Type": "application/xml"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)