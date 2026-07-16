from flask import Flask, request, jsonify
import os
import csv
from typing import Dict, Tuple, Optional
from flask_cors import CORS

app = Flask(__name__)
CORS(app)   # Allow all origins (needed for browser extension)

PORT = 5112
EXTENSION_FILE = "extension_controlle.txt"

def parse_extension_file() -> Dict[str, Dict[str, str]]:
    """
    Parse the extension_controlle.txt file and return a dictionary
    Structure: {extension_name: {version: status}}
    """
    extensions = {}
    
    if not os.path.exists(EXTENSION_FILE):
        return extensions
    
    try:
        with open(EXTENSION_FILE, 'r') as file:
            reader = csv.reader(file)
            for row in reader:
                if len(row) >= 3:
                    ext_name = row[0].strip()
                    version = row[1].strip()
                    status = row[2].strip()
                    
                    if ext_name not in extensions:
                        extensions[ext_name] = {}
                    extensions[ext_name][version] = status
    except Exception as e:
        print(f"Error reading {EXTENSION_FILE}: {e}")
    
    return extensions

def check_extension_status(extension_name: str, version: str) -> str:
    """
    Check the status of a specific extension version
    Returns status string or "disallow" if not found or file doesn't exist
    """
    # Check if file exists
    if not os.path.exists(EXTENSION_FILE):
        return "disallow"
    
    # Parse the file
    extensions = parse_extension_file()
    
    # Check if extension exists
    if extension_name not in extensions:
        return "disallow"
    
    # Check if version exists for this extension
    if version not in extensions[extension_name]:
        return "disallow"
    
    # Return the status
    return extensions[extension_name][version]

@app.route('/check-extension', methods=['GET', 'POST'])
def check_extension():
    """
    Endpoint to check extension status
    Accepts both GET and POST methods
    """
    # Extract parameters from request
    extension_name = None
    version = None
    
    if request.method == 'GET':
        extension_name = request.args.get('Extension Name')
        version = request.args.get('Version')
    else:  # POST
        # Check if JSON
        if request.is_json:
            data = request.get_json()
            extension_name = data.get('Extension Name') or data.get('extension_name')
            version = data.get('Version') or data.get('version')
        else:
            # Check form data
            extension_name = request.form.get('Extension Name')
            version = request.form.get('Version')
    
    # Validate parameters
    if not extension_name or not version:
        return jsonify({
            "error": "Missing required parameters",
            "required": ["Extension Name", "Version"]
        }), 400
    
    # Get status
    status = check_extension_status(extension_name, version)
    
    # Return response
    return jsonify({
        "Extension Name": extension_name,
        "Version": version,
        "status": status
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "running", "port": PORT})

if __name__ == '__main__':
    print(f"Starting extension controller on port {PORT}")
    print(f"Checking for {EXTENSION_FILE}...")
    
    if os.path.exists(EXTENSION_FILE):
        print(f"Found {EXTENSION_FILE}")
        extensions = parse_extension_file()
        print(f"Loaded extensions: {list(extensions.keys())}")
    else:
        print(f"Warning: {EXTENSION_FILE} not found. All requests will return 'disallow'")
    
    print(f"\nServer running on http://tsstools.online:{PORT}")
    print("Endpoint: /check-extension")
    print("\nExample requests:")
    print(f"  GET http://tsstools.online:{PORT}/check-extension?Extension%20Name=First%20Extension&Version=4.3.2")
    print(f"  POST http://tsstools.online:{PORT}/check-extension -H 'Content-Type: application/json' -d '{{\"Extension Name\": \"First Extension\", \"Version\": \"4.3.2\"}}'")
    print("\nPress Ctrl+C to stop\n")
    
    app.run(host='0.0.0.0', port=PORT, debug=False)