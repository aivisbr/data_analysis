from flask import Flask, render_template, jsonify
from openai import OpenAI
import os
from dotenv import load_dotenv
from flask_cors import CORS

load_dotenv('.env.local')

app = Flask(__name__)
CORS(app) # Enable CORS for all routes and origins
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

@app.route('/')
def home():
    return render_template('index.html')

import httpx

@app.route('/api/chatkit/session', methods=['POST'])
def create_chatkit_session():
    try:
        workflow_id = os.environ.get("WORKFLOW_ID")
        api_key = os.environ.get("OPENAI_API_KEY")
        if not workflow_id:
            return jsonify({"error": "WORKFLOW_ID not set"}), 500
        if not api_key:
            return jsonify({"error": "OPENAI_API_KEY not set"}), 500
            
        # Implementing ChatKit session creation via raw API call since SDK support is missing/beta
        url = "https://api.openai.com/v1/chatkit/sessions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "chatkit_beta=v1"
        }
        json_data = {
            "workflow": {"id": workflow_id},
            "user": "user-1234"
        }
        
        with httpx.Client() as http_client:
            response = http_client.post(url, headers=headers, json=json_data)
            response.raise_for_status()
            data = response.json()
            
        return jsonify({"client_secret": data.get("client_secret")})
    except Exception as e:
        print(f"Error creating session: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
