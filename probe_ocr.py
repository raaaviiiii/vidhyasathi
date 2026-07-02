# probe_ocr.py — one-off: can a vision model read the handwriting?
import sys
import fitz            # pymupdf
import ollama

PDF = sys.argv[1] if len(sys.argv) > 1 else "data/docs/Mod_1__1.pdf"
MODEL = "minicpm-v"

doc = fitz.open(PDF)
png = doc[0].get_pixmap(dpi=200).tobytes("png")   # render page 1 -> PNG

resp = ollama.chat(
    model=MODEL,
    messages=[{
        "role": "user",
        "content": ("Transcribe ALL text on this handwritten page exactly, "
                    "including every equation. Output only the transcription."),
        "images": [png],
    }],
)
print(resp["message"]["content"])