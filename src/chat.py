"""Interactive CLI for multi-turn modes (Teach Me) + the Layer-3 web toggle.
 
Single-shot Quick Answer stays on `python -m src.answer ...`; this module is for
holding a real back-and-forth so Teach Me's memory (and the web fallback) can be
exercised.
 
Run:      python -m src.chat
Commands: /quit           exit
          /new            reset the current thread (clear memory)
          /mode teach     switch to Teach Me (multi-turn, default)
          /mode quick     switch to Quick Answer (single-shot, no memory)
          /web on|off     Layer-3 web fallback when material misses (default off)
"""
from src.answer import answer
from src.store import close
 
 
def _print_result(r: dict) -> None:
    d = r["decision"]
    if d == "refuse":
        label = "Tutor (not in material)"
    elif d == "answer_web":
        label = "Tutor (from the web)"
    else:
        label = "Tutor"
    print(f"\n{label}: {r['answer']}")
    if r.get("web_used"):
        urls = ", ".join(s["url"] for s in r["web_sources"])
        print(f"\U0001F310 Not in your notes \u2014 learned from the web, verify before trusting: {urls}")
    if r["caveat"]:
        print(r["caveat"])
    if r["offer_help"]:
        print("\u2192 Not fully sure \u2014 you can ask a human helper. (routing comes later)")
    print(f"  [{r['mode']} \u00b7 grounded: {r.get('grounded')} \u00b7 decision: {d} \u00b7 score {r['top_score']:.3f}]")
 
 
def main() -> None:
    mode = "teach"
    web_on = False
    history: list[dict] = []
    print("Vidhyasathi \u2014 interactive.  mode: teach.  web: off.  "
          "/quit  /new  /mode quick|teach  /web on|off\n")
    try:
        while True:
            try:
                msg = input("You: ").strip()
            except EOFError:
                break
            if not msg:
                continue
            if msg == "/quit":
                break
            if msg == "/new":
                history = []
                print("(thread reset)\n")
                continue
            if msg.startswith("/mode"):
                parts = msg.split()
                if len(parts) == 2 and parts[1] in ("quick", "teach"):
                    mode, history = parts[1], []
                    print(f"(mode: {mode}, thread reset)\n")
                else:
                    print("usage: /mode quick|teach\n")
                continue
            if msg.startswith("/web"):
                parts = msg.split()
                if len(parts) == 2 and parts[1] in ("on", "off"):
                    web_on = (parts[1] == "on")
                    print(f"(web fallback: {'on' if web_on else 'off'})\n")
                else:
                    print("usage: /web on|off\n")
                continue
 
            r = answer(msg, mode=mode,
                       history=history if mode == "teach" else None, web=web_on)
            _print_result(r)
            if mode == "teach":
                history.append({"role": "user", "content": msg})
                history.append({"role": "assistant", "content": r["answer"]})
            print()
    finally:
        close()
 
 
if __name__ == "__main__":
    main()
 