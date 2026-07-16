import imaplib
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={
    r"/check_app_password": {"origins": "*"}
})
GMAIL_IMAP_SERVER = "imap.gmail.com"
GMAIL_IMAP_PORT = 993


def check_app_password(email_addr, app_password):
    try:
        mail = imaplib.IMAP4_SSL(
            GMAIL_IMAP_SERVER,
            GMAIL_IMAP_PORT,
            timeout=20
        )

        mail.login(email_addr, app_password)
        mail.logout()

        return True

    except Exception:
        return False


@app.route("/check_app_password", methods=["POST"])
def check_password():

    data = request.get_json(silent=True)

    if not data:
        return jsonify({
            "status": "not_ok",
            "error": "No JSON received"
        }), 400

    email = data.get("email", "").strip()
    app_password = data.get("app_password", "").strip()

    if not email or not app_password:
        return jsonify({
            "status": "not_ok",
            "error": "email and app_password are required"
        }), 400

    if check_app_password(email, app_password):
        return jsonify({
            "status": "ok"
        })

    return jsonify({
        "status": "not_ok"
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5314,
        threaded=True
    )