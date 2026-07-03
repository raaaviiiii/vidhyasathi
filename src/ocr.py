"""OCR fallback: read scanned / handwritten / image content with a frontier
vision model, ADAPTING the model to page difficulty (Haiku -> Sonnet -> Opus).

Difficulty can't be known from pixels alone (a sharp scan of messy handwriting
looks "easy"), so we:
  1. use a cheap sharpness check ONLY to skip a wasted Haiku pass on blurry scans
  2. start cheap and ESCALATE when the model reports low confidence / illegible text
Results are cached per image, so escalation is paid for once, ever.
"""
import base64, csv, hashlib, io, json, re
from pathlib import Path

from PIL import Image
import numpy as np
import anthropic
from dotenv import load_dotenv

from src.config import (OCR_TIERS, OCR_BLUR_FLOOR, OCR_MAX_TOKENS,
                        OCR_CACHE_PATH, OCR_COST_LOG, OCR_EST_COST)

load_dotenv()
_client = None


def _client_():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


PROMPT = (
    "Transcribe ALL text on this page exactly, in reading order. Write every "
    "equation in LaTeX. For any diagram, give a brief description in [square "
    "brackets]. Use [illegible] for anything you genuinely cannot read. After "
    "the transcription, on a final separate line, output exactly one of: "
    "CONFIDENCE: high | CONFIDENCE: medium | CONFIDENCE: low -- reflecting how "
    "sure you are that you read the whole page correctly."
)


def _load_cache():
    try:
        return json.loads(Path(OCR_CACHE_PATH).read_text())
    except Exception:
        return {}


def _save_cache(c):
    Path(OCR_CACHE_PATH).write_text(json.dumps(c))


def _key(png):
    return hashlib.sha256(png).hexdigest()


def _sharpness(png):
    a = np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=np.float32)
    return float(np.diff(a, axis=1).var() + np.diff(a, axis=0).var())


def _start_tier(png):
    """Only job: skip a wasted Haiku pass on an obviously blurry scan."""
    return 1 if _sharpness(png) < OCR_BLUR_FLOOR else 0


def _confidence(text):
    m = re.search(r"CONFIDENCE:\s*(high|medium|low)", text, re.I)
    return m.group(1).lower() if m else "low"


def _strip(text):
    return re.sub(r"\n?\s*CONFIDENCE:\s*(high|medium|low)\s*$", "", text,
                  flags=re.I).strip()


def _call(model, b64):
    msg = _client_().messages.create(
        model=model, max_tokens=OCR_MAX_TOKENS,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
             "media_type": "image/png", "data": b64}},
            {"type": "text", "text": PROMPT}]}])
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


def _log_cost(model):
    new = not Path(OCR_COST_LOG).exists()
    with open(OCR_COST_LOG, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["model", "est_usd"])
        w.writerow([model, OCR_EST_COST.get(model, 0)])


def ocr_image(png: bytes, verbose: bool = True) -> str:
    """Adaptively OCR one page image, escalating Haiku->Sonnet->Opus until the
    model is confident (or tiers run out). Cached per image."""
    cache = _load_cache()
    k = _key(png)
    if k in cache:
        if verbose:
            print("  [cache hit]")
        return cache[k]

    b64 = base64.standard_b64encode(png).decode()
    tier = _start_tier(png)
    text = ""
    while True:
        model = OCR_TIERS[tier]
        raw = _call(model, b64)
        _log_cost(model)
        conf = _confidence(raw)
        text = _strip(raw)
        illegible = text.lower().count("[illegible]")
        last = tier == len(OCR_TIERS) - 1
        good = (conf == "high" and illegible <= 1) or (conf == "medium" and tier >= 1)
        if verbose:
            short = model.split("-")[1]
            print(f"  [{short}] confidence={conf} illegible={illegible}"
                  f" -> {'accept' if (good or last) else 'escalate'}")
        if good or last:
            break
        tier += 1

    cache[k] = text
    _save_cache(cache)
    return text


def total_spend():
    p = Path(OCR_COST_LOG)
    if not p.exists():
        return 0.0
    with open(p) as f:
        rows = list(csv.reader(f))[1:]
    return round(sum(float(r[1]) for r in rows if len(r) > 1), 4)


if __name__ == "__main__":
    import sys, pymupdf
    from src.config import OCR_DPI
    pdf = sys.argv[1] if len(sys.argv) > 1 else "data/docs/Mod 1 .1.pdf"
    page = int(sys.argv[2]) - 1 if len(sys.argv) > 2 else 0
    png = pymupdf.open(pdf)[page].get_pixmap(dpi=OCR_DPI).tobytes("png")
    print(f"OCR of {pdf} page {page+1}:\n")
    out = ocr_image(png)
    print("\n----- transcription -----\n")
    print(out)
    print(f"\n(running est. spend so far: ${total_spend()})")