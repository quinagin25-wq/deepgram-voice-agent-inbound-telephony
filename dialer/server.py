from flask import send_file

@app.get("/app")
def app_page():
    return send_file("dialer.html")
