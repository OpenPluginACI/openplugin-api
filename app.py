from flask import Flask, request, jsonify
from dotenv import load_dotenv
from flask_cors import CORS
import os
import json
from datetime import datetime
from collections import deque
from typing import Dict, List, TypedDict
from openplugincore import openplugin_completion, OpenPluginMemo
from datetime import datetime
from urllib.parse import unquote


load_dotenv()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT'))

open_plugin_memo = OpenPluginMemo()
open_plugin_memo.init()

app = Flask(__name__)
CORS(app)

class BucketItem(TypedDict):
    date_sent: datetime
    plugin_name: str

class TokenInfo(TypedDict):
    total_use: int
    bucket: List[BucketItem]

early_access_tokens = [
    '__extra__-c22a34e2-89a8-48b2-8474-c664b577526b', # public
    '__extra__-692df72b-ec3f-49e4-a1ce-fb1fbc34aebd' # public
]
request_data: Dict[str, TokenInfo] = {token: {"total_use": 0, "bucket": []} for token in early_access_tokens}
print("request_data: \n", json.dumps(request_data, indent=4))

# Maximum requests allowed per minute per token
MAX_REQUESTS_PER_DAY = 200

def rate_limiter_pass(early_access_token: str, plugin_name: str) -> bool:
    now = datetime.utcnow()

    token_info = request_data[early_access_token]

    print(f"Request from \"{early_access_token}\" with plugin \"{plugin_name}\"")

    # Filter out requests that are older than a day from the token bucket
    valid_requests = [req for req in token_info["bucket"] if (now - req["date_sent"]).total_seconds() < 86400]

    # Update the token bucket with valid requests
    token_info["bucket"] = valid_requests

    # Check the length of valid requests
    if len(valid_requests) < MAX_REQUESTS_PER_DAY:
        valid_requests.append({
            "date_sent": now,
            "plugin_name": plugin_name
        })
        token_info["total_use"] += 1
        return True

    return False


@app.route('/chat_completion', methods=['POST'])
def chat_completion():
    try:
        data = request.get_json()

        early_access_token = data.get('early_access_token', None)
        if not early_access_token:
            raise Exception("early_access_token is missing")
        if early_access_token not in request_data:
            raise Exception("early_access_token is invalid")
        if not rate_limiter_pass(early_access_token, data["plugin_name"]):
            raise Exception("Rate limit exceeded")
        
        chatgpt_args = data.copy()
        plugin_name = chatgpt_args["plugin_name"]
        del chatgpt_args["plugin_name"]
        del chatgpt_args["early_access_token"]

        messages = chatgpt_args.get("messages", None)
        # raise error if last message content is empty
        if not messages:
            raise ValueError("Last message content is empty")
        
        # delete messages from chatgpt_args
        del chatgpt_args["messages"]
        
        response = openplugin_completion(
            openai_api_key=OPENAI_API_KEY,
            plugin_name=plugin_name,
            messages=messages,
            **chatgpt_args,
        )
        return jsonify(response)

    except Exception as e:
        error_class = type(e).__name__
        error_message = str(e)
        return jsonify({"error": f"{error_class} error: {error_message}"}), 500



@app.route('/plugin', methods=['POST'])
def plugin():
    authorization = request.headers.get('authorization')
    if authorization != os.getenv('AUTHORIZATION_SECRET'):
        return jsonify({"error": "Unauthorized"}), 401    

    if not open_plugin_memo.plugins_directory:
        open_plugin_memo.init()
    # get the body
    data = request.get_json()
    
    if not data.get("openplugin_namespace") and not data.get("openplugin_root_url"):
        return jsonify({"error": "Invalid openplugin namespace or root url"}), 400
    if data.get("openplugin_namespace") and not open_plugin_memo.plugins_directory[data["openplugin_namespace"]]:
        return jsonify({"error": "Invalid openplugin namespace"}), 
    if not data["messages"] or len(data["messages"]) == 0:
        return jsonify({"error": "No messages"}), 400
    
    if data.get("openplugin_namespace"):
        plugin = open_plugin_memo.get_plugin(data["openplugin_namespace"])
    elif data.get("openplugin_root_url"):
        plugin = open_plugin_memo.init_openplugin(root_url=data["openplugin_root_url"])
    if not plugin:
        try:
            plugin = open_plugin_memo.init_plugin(data["openplugin_namespace"])
        except Exception as e:
            error_class = type(e).__name__
            error_message = str(e)
            return jsonify({"error": f"{error_class} error: {error_message}"}), 500
    try:
        plugin_response = plugin.fetch_plugin(
            messages=data["messages"],
            truncate=True,
            model="gpt-3.5-turbo-0613",
            temperature=0,
        )
    except Exception as e:
        error_class = type(e).__name__
        error_message = str(e)
        plugin_response = {
            "error": f"{error_class} error: {error_message}"
        }

    return jsonify(plugin_response), 200

@app.route('/eval/tentative', methods=['GET'])
def evaluate_tentative():
    try:
        # Retrieve the plugin_name or root_url from the request parameters
        plugin_name = request.args.get('plugin_name')
        root_url = request.args.get('root_url')
        if root_url:
            root_url = unquote(root_url)

        # Ensure that either plugin_name or root_url is provided
        if not plugin_name and not root_url:
            return jsonify({"error": "Either plugin_name or root_url must be provided"}), 400

        # Initialize the plugin
        plugin = None
        try:
            if plugin_name:
                plugin = open_plugin_memo.get_plugin(plugin_name)
            elif root_url:
                plugin = open_plugin_memo.init_openplugin(root_url=root_url)
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        # Ensure the plugin was initialized successfully and has a manifest
        if not plugin or not hasattr(plugin, 'manifest'):
            return jsonify({"error": "Failed to initialize the plugin or the plugin lacks a manifest."}), 400

        # Retrieve the manifest from the plugin
        manifest = plugin.manifest

        # Extract the relevant openplugin_info values from the manifest
        openplugin_info = {
            "namespace": manifest.get("name_for_model"),
            "description_for_human": manifest.get("description_for_human"),
            "description_for_model": manifest.get("description_for_model"),
            "domain": plugin.root_url,
            "auth": manifest.get("auth"),
            "blacklisted": False,
            "whitelisted": True,
        }

        # Ensure all required values are present in the openplugin_info
        required_keys = ["namespace", "description_for_human", "description_for_model", "domain", "auth"]
        for key in required_keys:
            if not openplugin_info.get(key):
                return jsonify({"error": f"Missing value for {key} in the manifest."}), 400

        return jsonify(openplugin_info), 200

    except Exception as e:
        error_class = type(e).__name__
        error_message = str(e)
        return jsonify({"error": f"{error_class} error: {error_message}"}), 500

@app.route('/admin', methods=['GET'])
def admin_view():
    try:
        authorization = request.headers.get('authorization')
        if authorization != os.getenv('AUTHORIZATION_SECRET'):
            return jsonify({"error": "Unauthorized"}), 401  
        return jsonify(request_data)
    except Exception as e:
        error_class = type(e).__name__
        error_message = str(e)
        return jsonify({"error": f"{error_class} error: {error_message}"}), 403


on_heroku = 'DYNO' in os.environ

if __name__ == '__main__':
    if on_heroku:
        app.run(host='0.0.0.0', port=PORT)
    else:
        app.run(host='0.0.0.0', port=PORT, debug=True)
