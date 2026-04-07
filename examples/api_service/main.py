import os
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        
        response = {
            "status": "ok",
            "message": "Hello from Heimdall Native Python Dummy API!",
            "path": self.path
        }
        self.wfile.write(json.dumps(response).encode('utf-8'))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"Server started globally on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
