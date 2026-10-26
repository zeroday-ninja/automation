"""Corrections, so the same one never has to be given twice.

A document a model forgot to read is indistinguishable from a document that
does not exist, which is why memory alone does not stop repetition. A lesson
here moves through four stages, and the later ones are what make it stick:

1. **captured** from something that actually happened -- a parked change you
   answered, a review that asked for a change, a thumbs-down. A correction the
   user *made* is recorded without asking them; an agent's guess about their
   preferences stays a proposal, which is a different table.
2. **scoped**, so it reaches the work it applies to and not everything else.
3. **injected** into every cycle in that scope. A cycle that could not read
   them is a failed cycle, not a quiet one.
4. **mechanised** where it can be: a lesson that can be checked becomes a
   check, and a model that skims the document still fails the check.

A repeat is treated as a defect of this pipeline rather than as a nuisance, so
every repeat records *which stage broke*. That diagnosis is the difference
between a memory feature and a learning one.

Stdlib only: this module is packaged into the improvement program's tarball so
the cycle can scope and render its own lessons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


#: Who a lesson applies to.
#:
#: ``user`` — how this person wants to be worked for, everywhere.
#: ``project`` — how one repository is handled.
#: ``runtime`` — a quirk of one agent runtime.
#: ``surface`` — a preference about one thing in the interface.
LESSON_SCOPES: tuple[str, ...] = ("user", "project", "runtime", "surface")

#: What produced the correction. Every one of these is something that already
#: happens, which is the point: nothing here asks the user for a lesson.
LESSON_TRIGGERS: tuple[str, ...] = (
    "parked_resolution",
    "change_requested",
    "thumbs_down",
    "comment",
    "chat_instruction",
    "rejected_approach",
    # An agent runtime's adapter met an event it has no translation for. Not
    # a person's words but the system's own observation about itself, filed
    # against the runtime so the cycles that touch it find it — the one
    # trigger here that nobody had to notice first.
    "adapter_incident",
)

#: Which stage of the pipeline failed, when a correction arrives twice.
#:
#: Ordered from earliest to latest, because the earliest stage that could have
#: caught it is the one worth fixing.
LESSON_FAILURE_STAGES: tuple[str, ...] = (
    "never_captured",
    "out_of_scope",
    "not_injected",
    "ignored",
    "not_mechanisable",
)

#: Above this share of shared significant words, two corrections are the same
#: correction.
#:
#: Measured against stemmed words with the scaffolding stripped out, which is
#: why it is not higher: two statements of the same rule share their nouns and
#: verbs and differ in how the negation is phrased, so a threshold tuned for
#: raw words would miss them. Not lower either -- a false match hides a real
#: new correction behind an old one, which is worse than one duplicate in a
#: list a person reads.
REPEAT_SIMILARITY = 0.5

#: Words carried by almost every sentence, which would make any two look alike.
#:
#: The second group is intensity rather than identity: "never force-push" and
#: "do not force-push" are the same correction, and "please" and "really"
#: identify nothing. Words that can *change* a rule -- before, after, without,
#: unless, only -- are deliberately absent, because those do distinguish two
#: corrections about the same subject.
_NOISE = frozenset(
    """a an and are as at be but by do for from has have i if in is it
    its me my of on or so than that the their them then there these
    they this to was what when which who you your
    never always must need dont cant cannot please just really want
    make sure should would will use used using not""".split()
)

_WORD = re.compile(r"[a-z0-9]+")

#: Suffixes stripped before comparing, longest first.
#:
#: Crude on purpose, and crude in the safe direction: over-stemming makes two
#: corrections look more alike, which is caught by the threshold, while no
#: stemming at all makes "telling me" and "tell me" different corrections,
#: which is not caught by anything.
_SUFFIXES = ("ing", "ed", "es", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    # Then a trailing "e", so "renaming" and "rename" reach the same stem.
    # Without this they do not: stripping "ing" leaves "renam" while "rename"
    # stays whole, and the same word reads as two different ones.
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return word


def significant_words(text: str) -> frozenset[str]:
    """The words that carry the correction, lowercased and stemmed."""
    return frozenset(
        _stem(word)
        for word in _WORD.findall(text.casefold())
        if len(word) > 2 and word not in _NOISE
    )


def similarity(left: str, right: str) -> float:
    """How alike two corrections are, as a share of shared significant words.

    Jaccard over word sets rather than an edit distance: the same correction
    given twice is rarely phrased the same way, and word order carries almost
    nothing here.
    """
    a = significant_words(left)
    b = significant_words(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True, slots=True)
class LessonMatch:
    """An existing lesson this correction is a repeat of."""

    lesson_id: str
    score: float
    stage: str
    detail: str


def diagnose_repeat(
    text: str,
    *,
    scope: str,
    subject: str | None,
    existing: list[dict],
    injected_lesson_ids: list[str] | None,
) -> LessonMatch | None:
    """Whether this correction has been given before, and what failed.

    ``existing`` is the active lessons in the same scope. ``injected_lesson_ids``
    is what the work that caused the repeat actually had in front of it —
    reported by the cycle, not inferred, because that report is the only thing
    that tells "it ignored the lesson" apart from "it never saw the lesson".

    Returns ``None`` when nothing matches, which is the ``never_captured``
    case and is handled by simply storing the lesson.
    """
    best: LessonMatch | None = None
    seen = set(injected_lesson_ids or [])
    for lesson in existing:
        lesson_id = str(lesson.get("id") or "")
        if not lesson_id:
            continue
        score = similarity(text, str(lesson.get("text") or ""))
        if score < REPEAT_SIMILARITY:
            continue
        if best is not None and score <= best.score:
            continue

        lesson_subject = lesson.get("subject")
        if lesson.get("scope") != scope or (
            lesson_subject is not None
            and subject is not None
            and lesson_subject != subject
        ):
            stage = "out_of_scope"
            detail = (
                "The lesson exists but is filed against "
                f"{lesson.get('scope')}/{lesson_subject}, so this work never "
                "had it in scope."
            )
        elif injected_lesson_ids is None:
            stage = "not_injected"
            detail = (
                "The lesson was in scope, but the work did not report which "
                "lessons it loaded, so it cannot be shown to have had this "
                "one."
            )
        elif lesson_id not in seen:
            stage = "not_injected"
            detail = "The lesson was in scope and was not loaded into the work."
        elif not lesson.get("mechanised"):
            stage = "ignored"
            detail = (
                "The lesson was loaded and the work went against it anyway. "
                "Nothing checks it, so nothing could stop that."
            )
        else:
            stage = "not_mechanisable"
            detail = (
                "The lesson was loaded and its check passed, so the check "
                "does not cover this case."
            )
        best = LessonMatch(lesson_id=lesson_id, score=score, stage=stage, detail=detail)
    return best


def lessons_in_scope(
    lessons: list[dict],
    *,
    repository: str | None,
    runtime: str | None,
) -> list[dict]:
    """The lessons one cycle has to work under.

    A user-level lesson applies everywhere. A project-level one applies to its
    own repository; one filed against no repository applies to all of them,
    because that is what "I never said which" means. Surface-level lessons are
    about the interface and are not a builder's business.
    """
    chosen = []
    for lesson in lessons:
        if not isinstance(lesson, dict) or lesson.get("status") == "retired":
            continue
        scope = lesson.get("scope")
        subject = lesson.get("subject")
        if scope == "user":
            chosen.append(lesson)
        elif scope == "project" and (subject is None or subject == repository):
            chosen.append(lesson)
        elif scope == "runtime" and (subject is None or subject == runtime):
            chosen.append(lesson)
    return chosen


def lessons_section(lessons: list[dict], budget: int) -> str:
    """The lessons, as instructions, bounded.

    First in the context and stated as rules rather than as history: a lesson
    rendered as "the user once said" invites a judgement about whether it still
    applies, which is exactly the judgement that produces a repeat.

    Truncation says so. A silently cut list is a list whose last entries were
    never injected, which is a `not_injected` failure disguised as a complete
    context.
    """
    if not lessons:
        return ""
    lines = [
        "## Corrections you are already under",
        "",
        "These were given once. Giving them again is a defect, so follow them "
        "without re-deciding whether they apply.",
        "",
    ]
    kept = 0
    for lesson in lessons:
        text = str(lesson.get("text") or "").strip()
        if not text:
            continue
        entry = f"- {text}"
        why = str(lesson.get("why") or "").strip()
        if why:
            entry += f" ({why})"
        if sum(len(line) + 1 for line in lines) + len(entry) > budget:
            lines.append(
                f"- [{len(lessons) - kept} more did not fit in the context "
                "budget and were not injected]"
            )
            break
        lines.append(entry)
        kept += 1
    return "\n".join(lines) + "\n"


def normalize_scope(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    return folded if folded in LESSON_SCOPES else None


def normalize_trigger(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    folded = value.strip().casefold()
    return folded if folded in LESSON_TRIGGERS else None


#: Below this many significant words, an answer is an acknowledgement rather
#: than a correction. "Yes", "go ahead", "not now" tell the next cycle nothing
#: it could act on, and storing them as lessons would bury the real ones.
MIN_LESSON_WORDS = 4


def resolution_is_a_correction(resolution: str) -> bool:
    """Whether a person's answer says anything a later cycle could act on."""
    return len(significant_words(resolution)) >= MIN_LESSON_WORDS
