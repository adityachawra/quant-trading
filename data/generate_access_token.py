import webbrowser
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv, set_key

load_dotenv()

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

kite = KiteConnect(api_key=API_KEY)

# A shared dictionary to pass the token from the server thread back to the main thread
captured_token = {}

class RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # self.path is everything after the domain, e.g. "/?request_token=abc123&status=success"
        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query)
        token = query_params.get("request_token", [None])[0]

        if token:
            captured_token["token"] = token
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Login successful. You can close this tab and return to your terminal.</h2>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No request_token found in redirect.")

    def log_message(self, format, *args):
        pass  # silences the server's default per-request console logging

def run_server_until_token_received():
    server = HTTPServer(("127.0.0.1", 5000), RedirectHandler)
    while "token" not in captured_token:
        server.handle_request()  # blocks, handling exactly one incoming request at a time

# Start the server in the background so it doesn't block the rest of the script
server_thread = threading.Thread(target=run_server_until_token_received)
server_thread.start()

# Open the Kite login page in your default browser automatically
login_url = kite.login_url()
print(f"Opening login page in your browser: {login_url}")
webbrowser.open(login_url)

print("Waiting for you to log in (enter Zerodha username, password, and TOTP)...")
server_thread.join()  # main script pauses here until the server thread finishes

request_token = captured_token["token"]
print(f"Captured request_token automatically: {request_token}")

# Exchange the request_token for an access_token
session_data = kite.generate_session(request_token, api_secret=API_SECRET)
access_token = session_data["access_token"]
print(f"New access_token: {access_token}")

# Save the access_token into .env, so your other scripts can read it later
set_key(".env", "KITE_ACCESS_TOKEN", access_token)
print("Access token generated and saved to .env")