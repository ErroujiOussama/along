"""Every OpenAI Responses call Al makes (router turns, skill turns, checks, looks) goes through call(): one place for the
request, and a log of the exact input/output for the dashboard.

.along_calls.jsonl, one line per call:
  {"id", "t", "kind": "route"|"skill"|"fail"|"check"|"look"|..., "episode", "model", "tier", "prev" (previous_response_id
   or null), "resp" (this response id), "secs", "usage": {"input", "cached", "output"}, "instructions" (text, or "=<id>"
   = same as that earlier call), "input": [{"text": ...} | {"image": "<file in .along_calls/>", "label": ...}],
   "output": parsed JSON (schema calls) or text, "error": null or text}
Images go to .along_calls/<id>_<n>.jpg. The newest KEEP calls are kept.
"""
import base64, json, os, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG, IMGS, KEEP = f"{HERE}/.along_calls.jsonl", f"{HERE}/.along_calls", 400
_lock = threading.Lock()
_last_instr = {}                                         # instructions text -> id of the call that logged it in full


API_KEY = None                                           # set by along (from its env file)
# Money gate: every paid call (Responses here, Codex in codex_agent, GPT-Live in along) checks spend(). Only the along
# process itself (`along ...` / the daemon) turns it on; importing along in a test or script spends nothing unless
# ALONG_SPEND=1 is set on purpose (dry runs against the real model).
SPEND = os.environ.get("ALONG_SPEND") == "1"


def spend(what):
    if not SPEND:
        raise RuntimeError(f"paid call blocked ({what}): not the along process; set ALONG_SPEND=1 to allow it")


def call(kind, content, instructions=None, schema=None, prev=None, episode=None, model=None, tier=None, effort="low",
         timeout=60):
    """content: list of str (text) or ("label", data_url) tuples (images, label shown before them).
    Returns (output, response_id): output = parsed JSON with a schema, else the text."""
    import requests
    spend(f"responses {kind}")
    model = model or os.environ.get("ALONG_ROUTE_MODEL", "gpt-6.1-sol")
    tier = tier or os.environ.get("ALONG_LOOK_TIER", "ultrafast")
    cid = f"c{time.time():.3f}".replace(".", "_")
    parts, logged = [], []
    for c in content:
        if isinstance(c, str):
            parts.append({"type": "input_text", "text": c}); logged.append({"text": c})
        else:
            label, url = c
            parts.append({"type": "input_text", "text": f"{label}:"}); parts.append({"type": "input_image", "image_url": url})
            logged.append({"image": _save_img(cid, len(logged), url), "label": label})
    body = {"model": model, "service_tier": tier, "reasoning": {"effort": effort},
            "input": [{"role": "user", "content": parts}]}
    if instructions:
        body["instructions"] = instructions
    if schema:
        body["text"] = {"format": {"type": "json_schema", "name": kind, "strict": True, "schema": schema}}
    if prev:
        body["previous_response_id"] = prev
    t0, out, rid, usage, err = time.time(), None, None, {}, None
    try:
        r = requests.post("https://api.openai.com/v1/responses", headers={"Authorization": f"Bearer {API_KEY}"},
                          json=body, timeout=timeout)
        r.raise_for_status(); j = r.json()
        rid = j.get("id")
        u = j.get("usage") or {}
        usage = {"input": u.get("input_tokens"), "cached": (u.get("input_tokens_details") or {}).get("cached_tokens"),
                 "output": u.get("output_tokens")}
        text = "".join(c.get("text", "") for o in j.get("output", []) if o.get("type") == "message"
                       for c in o.get("content", [])).strip()
        out = json.loads(text) if schema else text
        return out, rid
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        instr = instructions
        if instructions and instructions in _last_instr:
            instr = "=" + _last_instr[instructions]
        elif instructions:
            _last_instr[instructions] = cid
        _append({"id": cid, "t": t0, "kind": kind, "episode": episode, "model": model, "tier": tier, "prev": prev,
                 "resp": rid, "secs": round(time.time() - t0, 2), "usage": usage, "instructions": instr,
                 "input": logged, "output": out, "error": err})


def image_url(path_or_img):
    """A jpg file path or a cv2 image -> data URL."""
    if isinstance(path_or_img, str):
        data = open(path_or_img, "rb").read()
    else:
        import cv2
        data = cv2.imencode(".jpg", path_or_img, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def _save_img(cid, n, url):
    os.makedirs(IMGS, exist_ok=True)
    name = f"{cid}_{n}.jpg"
    try:
        open(f"{IMGS}/{name}", "wb").write(base64.b64decode(url.split(",", 1)[1]))
    except Exception:
        return None
    return name


def _append(rec):
    with _lock:
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            lines = open(LOG).readlines()
        except FileNotFoundError:
            return
        if len(lines) > KEEP + 50:                       # prune: keep the newest KEEP calls and their images
            keep = lines[-KEEP:]
            open(LOG + ".tmp", "w").writelines(keep); os.replace(LOG + ".tmp", LOG)
            alive = {i["image"] for l in keep for i in json.loads(l).get("input", []) if i.get("image")}
            for fn in os.listdir(IMGS):
                if fn not in alive:
                    os.remove(f"{IMGS}/{fn}")


def tag(resp, episode):
    """File a call under an episode after the fact (the router call decides which episode it belongs to)."""
    with _lock:
        with open(LOG, "a") as f:
            f.write(json.dumps({"tag": resp, "episode": episode}) + "\n")


def calls(n=100, episode=None):
    """The newest n logged calls (for the dashboard), oldest first; tag lines applied."""
    try:
        lines = [json.loads(l) for l in open(LOG).readlines() if l.strip()]
    except FileNotFoundError:
        return []
    tags = {l["tag"]: l["episode"] for l in lines if "tag" in l}
    recs = [{**r, "episode": tags.get(r.get("resp"), r.get("episode"))} for r in lines if "tag" not in r]
    if episode:
        recs = [r for r in recs if r.get("episode") == episode]
    return recs[-n:]
