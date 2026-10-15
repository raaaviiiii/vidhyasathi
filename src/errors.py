"""Typed failures the web layer turns into clear, plain-language JSON errors."""


class ServiceDown(RuntimeError):
    """A dependency the app needs isn't reachable."""
    user_message = "A service Vidhyāsathi needs isn't running."


class LLMUnavailable(ServiceDown):
    user_message = ("The language model isn't responding. Make sure Ollama is running "
                    "(`ollama serve`) and the model is pulled (`ollama pull llama3.1:8b`).")


class StoreUnavailable(ServiceDown):
    user_message = ("The knowledge base (Qdrant) isn't reachable. Start it with "
                    "`docker start qdrant` and try again.")


class IngestError(ValueError):
    """A file couldn't be read. The message is safe to show the student."""
