# probe_claude_ocr.py — prove frontier vision reads the handwriting
import sys, base64
import pymupdf
import anthropic
from dotenv import load_dotenv

load_dotenv()                          # reads ANTHROPIC_API_KEY from .env
client = anthropic.Anthropic()

PDF = sys.argv[1] if len(sys.argv) > 1 else "data/docs/Mod 1 .1.pdf"
png = pymupdf.open(PDF)[0].get_pixmap(dpi=200).tobytes("png")
b64 = base64.standard_b64encode(png).decode()

msg = client.messages.create(
    model="claude-sonnet-5",           # strong + economical; Haiku = cheapest
    max_tokens=2000,
    messages=[{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64",
                "media_type": "image/png", "data": b64}},
            {"type": "text", "text":
                "Transcribe ALL text on this handwritten page exactly. "
                "Write every equation in LaTeX. If there is a diagram, briefly "
                "describe it in [brackets]. Output only the transcription."},
        ],
    }],
)
text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
print(text)