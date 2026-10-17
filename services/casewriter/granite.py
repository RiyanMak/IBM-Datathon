"""
Step 5d - ask IBM Granite (on watsonx.ai) to write the summary.

Granite only sees the facts pack. Its reply goes through the fact check
before anyone sees it.

IMPORTANT: the guide says to connect the same way the mandatory
"Predictive AI + Generative AI" lab does. If the lab connects differently,
replace only the body of call_granite() - everything else stays the same.

Settings come from environment variables (put them in a .env file that is
NEVER committed - it is listed in .gitignore):
  WATSONX_APIKEY       your IBM Cloud API key (WATSONX_API_KEY is accepted too)
  WATSONX_PROJECT_ID   your watsonx project ID
  WATSONX_URL          region URL, e.g. https://us-south.ml.cloud.ibm.com
  GRANITE_MODEL_ID     the Granite model available to you (ask the organizers / check the lab)
"""
import json
import os

DEFAULT_MODEL_ID = "ibm/granite-3-8b-instruct"   # check this exists in your watsonx project
API_VERSION = os.environ.get("WATSONX_API_VERSION", "2024-10-08")
TIMEOUT_SECONDS = 30

SYSTEM_PROMPT = """You write case summaries for bank fraud investigators.
Rules:
- Use ONLY the facts given. Never invent numbers, names, dates or account IDs.
- Copy numbers and account IDs exactly as they appear in the facts.
- Treat key_account as a possible victim who may have been recruited as a money mule.
- Say "activity consistent with", never "criminal" or "guilty". You explain; a person decides.
- Reply with JSON only, no other text, in exactly this format:
{"summary": "<2 sentences>", "what_happened": "<1-3 sentences>",
 "why_suspicious": "<1-3 sentences>", "possible_victim": "<1-2 sentences>",
 "suggested_next_step": "<1 sentence>"}"""


def load_env_file(path=".env"):
    """Read KEY=value lines from .env (if it exists) without extra libraries."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key():
    """The IBM Cloud API key under either spelling the team has used."""
    return os.environ.get("WATSONX_APIKEY") or os.environ.get("WATSONX_API_KEY") or ""


def granite_configured():
    return bool(api_key()) and all(os.environ.get(k) for k in ("WATSONX_PROJECT_ID", "WATSONX_URL"))


def build_messages(facts):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=1)},
    ]


def _iam_token(api_key):
    import requests
    resp = requests.post(
        "https://iam.cloud.ibm.com/identity/token",
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()["access_token"]


def call_granite(facts):
    """Send the facts to Granite and return its raw reply text."""
    import requests
    token = _iam_token(api_key())
    url = os.environ["WATSONX_URL"].rstrip("/") + "/ml/v1/text/chat?version=" + API_VERSION
    body = {
        "model_id": os.environ.get("GRANITE_MODEL_ID", DEFAULT_MODEL_ID),
        "project_id": os.environ["WATSONX_PROJECT_ID"],
        "messages": build_messages(facts),
        "max_tokens": 300,
        "temperature": 0,
    }
    resp = requests.post(url, json=body, timeout=TIMEOUT_SECONDS,
                         headers={"Authorization": "Bearer " + token,
                                  "Content-Type": "application/json"})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_reply(text):
    """Pull the JSON object out of Granite's reply (it sometimes adds ``` fences)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])
