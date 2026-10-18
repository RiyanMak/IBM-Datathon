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
  WATSONX_SPACE_ID     or a deployment space ID instead of the project (used when set)
  WATSONX_URL          region URL, e.g. https://us-south.ml.cloud.ibm.com
  GRANITE_MODEL_ID     the Granite model available to you (ask the organizers / check the lab)
"""
import json
import os
import sys
import time

DEFAULT_MODEL_ID = "ibm/granite-4-h-small"   # granite-3-8b-instruct was withdrawn; check the project's list
API_VERSION = os.environ.get("WATSONX_API_VERSION", "2024-10-08")
TIMEOUT_SECONDS = 15   # the guide's limit; a hung network must not hold a ring for long

# IAM tokens live 60 minutes. Reuse one and fetch a new one after about 50, so a call
# never goes out with a token that expires on the way.
TOKEN_REFRESH_SECONDS = 50 * 60
_token = {}   # "value", "refresh_at" (time.time() seconds)

# A 401 or 404 will not fix itself (wrong key, model ID not in the project). After the
# first one, Granite stays off until restart and every case says why, instead of every
# ring paying for another failed call.
_disabled_reason = ""


class GraniteDisabled(RuntimeError):
    """Raised instead of calling watsonx once a 401 or 404 has switched Granite off."""
    def __init__(self, reason):
        super().__init__("granite disabled: " + reason)
        self.reason = reason


def disabled_reason():
    """'' while Granite may be called, else why it was switched off ('404 model_not_supported')."""
    return _disabled_reason


def reset_state():
    """Forget the cached token and the disabled flag (tests, or after fixing the settings)."""
    global _disabled_reason
    _token.clear()
    _disabled_reason = ""


def _disable(resp):
    """Switch Granite off after a response that will not get better on retry, then raise."""
    global _disabled_reason
    code = ""
    try:
        body = resp.json()
        errors = body.get("errors") or [{}]
        code = errors[0].get("code") or body.get("errorCode") or ""
    except Exception:
        pass
    reason = "%d %s" % (resp.status_code, code or getattr(resp, "reason", "") or "error")
    reason = reason.strip()
    if not _disabled_reason:
        print("granite disabled: %s. Every case uses the template until the service restarts."
              % reason, file=sys.stderr)
    _disabled_reason = reason
    raise GraniteDisabled(reason)

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


def scope():
    """Where watsonx bills the call: {"space_id": ...} or {"project_id": ...}, or {} if neither is set.

    A deployment space works when the project UI cannot attach a Runtime (the space
    is made through the REST API with the Runtime's CRN). The space wins if both are set.
    """
    if os.environ.get("WATSONX_SPACE_ID"):
        return {"space_id": os.environ["WATSONX_SPACE_ID"]}
    if os.environ.get("WATSONX_PROJECT_ID"):
        return {"project_id": os.environ["WATSONX_PROJECT_ID"]}
    return {}


def watsonx_configured():
    return bool(api_key()) and bool(scope()) and bool(os.environ.get("WATSONX_URL"))


# --- Granite on the machine itself, through llama.cpp -----------------------------
# The LinuxONE Jupyter image ships llama_cpp_python and a Granite GGUF built for
# s390x (big-endian). When watsonx credentials are absent and that file is found,
# the case writer runs Granite locally: no network, no key, and the summary is
# written on IBM Z. Point GRANITE_GGUF at the file to choose a different one.
LOCAL_GGUF_ENV = "GRANITE_GGUF"
LOCAL_SEARCH_DIRS = ("/data/models", "/home/ibm-user", "/home/jovyan", "/opt", "/app", "/models", ".")
_local_llm = None


def local_model_path():
    """The GGUF file to use, or '' if none is available."""
    explicit = os.environ.get(LOCAL_GGUF_ENV, "")
    if explicit:
        return explicit if os.path.exists(explicit) else ""
    import glob
    for folder in LOCAL_SEARCH_DIRS:
        hits = sorted(glob.glob(os.path.join(folder, "granite*.gguf")))
        if hits:
            return hits[0]
    return ""


def local_configured():
    """Opt-in: GRANITE_LOCAL=1 (or an explicit GRANITE_GGUF). Measured on the VM, the packaged
    350M base model takes about 90 s per ring on one core and rarely closes its JSON, so it
    stays off unless asked for; watsonx is the production path."""
    if os.environ.get("GRANITE_LOCAL", "") not in ("1", "true", "yes") and not os.environ.get(LOCAL_GGUF_ENV):
        return False
    if not local_model_path():
        return False
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return False
    return True


def granite_configured():
    return watsonx_configured() or local_configured()


def granite_backend():
    """'watsonx', 'local' or '' (template only)."""
    if watsonx_configured():
        return "watsonx"
    if local_configured():
        return "local"
    return ""


# The reply shape, as a llama.cpp grammar: the model can only produce this JSON object
# with these five keys, so "no JSON in reply" cannot happen and the fact check judges
# the content alone.
REPLY_GRAMMAR = r'''
root ::= "{" ws "\"summary\":" ws str "," ws "\"what_happened\":" ws str "," ws "\"why_suspicious\":" ws str "," ws "\"possible_victim\":" ws str "," ws "\"suggested_next_step\":" ws str ws "}"
str ::= "\"" ( [^"\\\n] | "\\" ["\\/bfnrt] )* "\""
ws ::= [ \t\n]*
'''

def example_pair():
    """A worked example for the base model: the case writer's own fake scatter-gather ring,
    its facts pack as the code builds it, and the template's words as the reply. Built from
    the real code paths so it always has the full facts shape and passes the fact check."""
    import os.path
    from services.casewriter.facts import build_facts, parse_ring
    from services.casewriter.template import template_summary
    with open(os.path.join(os.path.dirname(__file__), "fake_rings.json")) as fh:
        ring = json.load(fh)[0]
    fields = {k: (json.dumps(v) if isinstance(v, (list, dict)) else str(v)) for k, v in ring.items()}
    facts = build_facts(parse_ring(fields))
    return facts, template_summary(facts)


def local_prompt(facts):
    """A completion prompt for the base model: the rules, one worked example, then the facts."""
    example_facts, example_reply = example_pair()
    return (SYSTEM_PROMPT + "\n\nFacts:\n" + json.dumps(example_facts, indent=1)
            + "\n\nReply:\n" + json.dumps(example_reply, indent=1)
            + "\n\nFacts:\n" + json.dumps(facts, indent=1) + "\n\nReply:\n")


def call_granite_local(facts):
    """Run Granite through llama.cpp on this machine and return its raw reply text.

    The packaged model is a base (completion) model, not a chat model, so it gets one
    worked example and a grammar that forces the reply shape. Whatever it produces goes
    through the same fact check as a watsonx reply.
    """
    global _local_llm
    import time
    from llama_cpp import Llama, LlamaGrammar

    if _local_llm is None:
        t0 = time.monotonic()
        _local_llm = Llama(model_path=local_model_path(), n_ctx=int(os.environ.get("GRANITE_CTX", "4096")),
                           n_threads=int(os.environ.get("GRANITE_THREADS", "1")), verbose=False)
        print("granite (local): model loaded in %.0f s from %s" % (time.monotonic() - t0, local_model_path()))
    t0 = time.monotonic()
    out = _local_llm(local_prompt(facts), max_tokens=int(os.environ.get("GRANITE_MAX_TOKENS", "260")),
                     temperature=0.0, grammar=LlamaGrammar.from_string(REPLY_GRAMMAR, verbose=False))
    text = out["choices"][0]["text"]
    print("granite (local): %s in %.0f s, %d tokens" % (facts.get("ring_id"), time.monotonic() - t0,
                                                         out.get("usage", {}).get("completion_tokens", 0)))
    return text


def build_messages(facts):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Facts:\n" + json.dumps(facts, indent=1)},
    ]


def _iam_token(api_key):
    """A cached IAM token; a new one only when the old one is about 50 minutes old."""
    if _token and time.time() < _token["refresh_at"]:
        return _token["value"]
    import requests
    resp = requests.post(
        "https://iam.cloud.ibm.com/identity/token",
        data={"grant_type": "urn:ibm:params:oauth:grant-type:apikey", "apikey": api_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=TIMEOUT_SECONDS)
    if resp.status_code in (400, 401):   # IAM answers 400 for an unknown API key
        _disable(resp)
    resp.raise_for_status()
    data = resp.json()
    # refresh early: at 50 minutes, or a minute before expiry if IAM ever issues shorter tokens
    lifetime = float(data.get("expires_in") or 3600)
    _token.update(value=data["access_token"],
                  refresh_at=time.time() + min(TOKEN_REFRESH_SECONDS, max(lifetime - 60, 0)))
    return _token["value"]


def call_granite(facts):
    """Send the facts to Granite and return its raw reply text: watsonx when configured,
    otherwise the local llama.cpp model. Raises GraniteDisabled after a 401 or 404."""
    if not watsonx_configured():
        if local_configured():
            return call_granite_local(facts)
        raise RuntimeError("Granite is not configured: no watsonx credentials and no local GGUF")
    if _disabled_reason:
        raise GraniteDisabled(_disabled_reason)
    import requests
    url = os.environ["WATSONX_URL"].rstrip("/") + "/ml/v1/text/chat?version=" + API_VERSION
    body = {
        "model_id": os.environ.get("GRANITE_MODEL_ID", DEFAULT_MODEL_ID),
        "messages": build_messages(facts),
        "max_tokens": 300,
        "temperature": 0,
    }
    body.update(scope())

    def post():
        return requests.post(url, json=body, timeout=TIMEOUT_SECONDS,
                             headers={"Authorization": "Bearer " + _iam_token(api_key()),
                                      "Content-Type": "application/json"})
    resp = post()
    if resp.status_code == 401:
        # the cached token may have been revoked early: one try with a fresh one
        _token.clear()
        resp = post()
    if resp.status_code in (401, 404):
        _disable(resp)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def parse_reply(text):
    """Pull the JSON object out of Granite's reply (it sometimes adds ``` fences)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])
