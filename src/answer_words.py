"""Word lists shared by the answer engine and the conversation layer."""
import re

# Letters + digits + Indic combining marks (virama, vowel signs) + ZWJ/ZWNJ, so a
# Malayalam or Hindi word stays ONE word instead of splitting at every vowel sign.
WORD = re.compile(r"(?:[^\W_]|[\u0900-\u0DFF\u200C\u200D])+", re.UNICODE)
STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "from", "about",
    "is", "are", "was", "be", "it", "its", "this", "that", "these", "those", "what",
    "which", "how", "why", "me", "my", "i", "you", "your", "do", "does", "can", "please",
    "give", "tell", "explain", "describe", "list", "all", "every", "everything", "main",
    "key", "important", "whole", "entire", "briefly", "go", "over", "s", "us", "we", "our",
    # politeness / framing of the request, never the topic
    "could", "would", "will", "kindly", "hey", "hi", "hello", "just", "quick", "quickly",
    "short", "shortly", "brief", "simple", "simply", "easy", "detailed", "help", "want",
    "need", "like", "make", "write", "prepare", "create", "show", "so", "understand",
    "bullet", "bullets", "point", "points", "form", "format", "way", "table", "words",
    "tomorrow", "tomorrows", "today", "tonight", "exam", "exams", "test", "tests", "quiz",
    "revision", "revise", "study", "studying", "prep", "before", "upcoming", "next",
    # how much / how it should be explained, never the topic
    "each", "detail", "details", "detailed", "depth", "fully", "properly", "thoroughly",
    "completely", "elaborately", "one", "by",
}
# words that only name the scope or the request, never a topic
SCOPE = {
    "module", "modules", "chapter", "chapters", "unit", "units", "note", "notes",
    "material", "materials", "syllabus", "document", "documents", "doc", "docs", "file",
    "files", "pdf", "pptx", "ppt", "docx", "slide", "slides", "lecture", "lectures",
    "concept", "concepts", "topic", "topics", "idea", "ideas", "summarize", "summarise",
    "summary", "overview", "outline", "cover", "covers", "covered", "covering",
    "presentation", "deck", "point", "points",
}



# Words that make a follow-up refer back to the previous turn rather than name a
# new topic ("why?", "give me an example", "explain that again").
FOLLOWUP = {
    "why", "how", "what", "example", "examples", "more", "again", "simpler", "simply",
    "mean", "means", "meaning", "work", "works", "working", "elaborate", "detail",
    "details", "clarify", "understand", "dont", "don", "get", "next", "continue",
    "another", "step", "steps", "show", "prove", "proof", "they", "them", "then", "so",
    "okay", "ok", "yes", "no", "sure", "and", "but", "also", "else", "other", "same",
    "one", "case", "cases", "use", "used", "happens", "happen", "part", "bit", "lot",
    "term", "terms", "simple", "simpler", "easier", "plain", "real", "life", "analogy",
    "confused", "confusing", "lost", "slower", "slow", "slowly", "repeat", "understood",
    "sorry", "didn", "didnt", "still", "not", "very", "really", "little", "less", "shorter",
    "say", "said", "mean", "meant", "point", "idea", "thing", "things", "previous", "above",
}



def words(text: str) -> list[str]:
    return [w for w in WORD.findall((text or "").lower()) if len(w) > 1 or w.isdigit()]


# Broad-intent triggers: phrases that ask for an OVERVIEW. Scope words on their own
# ("the module", "my notes") do NOT make a question broad — "Explain Kruskal's
# algorithm from the module" is a pointed question.
BROAD_PHRASES = (
    "all the concept", "all concepts", "every concept", "all the topic", "all topics",
    "all the idea", "main concept", "key concept", "main topic", "key topic", "main idea",
    "key idea", "explain concepts", "explain the concepts", "concepts in", "concepts of",
    "topics in", "everything in", "everything about", "explain everything",
    "overview of", "an overview", "summarize", "summarise", "summary of", "a summary",
    "outline of", "go over everything", "what does this cover", "what is covered",
    "what do my notes cover", "what does my material cover",
    "each concept", "each topic", "each of the concept", "each of the topic",
    "every topic", "all of them", "each of them", "each one", "one by one",
    "the whole module", "whole chapter", "entire module", "entire chapter",
    "the whole document", "whole unit", "all the points", "each point",
)



def is_broad(question: str) -> bool:
    q = (question or "").lower()
    return any(p in q for p in BROAD_PHRASES)
