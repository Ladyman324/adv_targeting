"""Is "Bob Smith" the same person as "Robert Smith"? Used to judge a match, not
to rename anybody.

WHY THIS EXISTS
---------------
The crosswalk matched Act! contacts to SEC advisors on surname, firm and
location and scored 27,850 of them `high`. Comparing the FIRST names afterwards
showed 862 disagreements -- and most were not nicknames. They were different
people sharing a surname at the same firm: Jeffrey and Victoria Thompson,
Raymond and Rosemary Abreu, Terri and Darren Hunter.

340 of those were syncable, meaning a logged call would have been written onto
the wrong person's contact record: correctly attributed, entirely plausible, and
impossible to find afterwards.

So a first-name disagreement demotes a match out of `high`. That rule is only
safe if "Jim" and "James" are known to agree -- otherwise it demotes several
hundred CORRECT matches and quietly shrinks the sync instead.

WHAT THIS IS NOT
----------------
It is not a renaming table. The SEC feed holds the registration record and stays
the display name; this only answers "could these two strings be one person".

CALIBRATION
-----------
Deliberately generous. A false "same person" leaves a bad match in `high`, which
is the harm we are removing -- but a false "different person" only costs sync on
a correct match, and the person is still callable, still logged locally, still
in the CRM. Missing a nickname is cheap; inventing one is not. Where the two
compete, this errs toward calling them the same and lets the OTHER signals in
score_contacts do the work.
"""
from __future__ import annotations

import difflib
import re

# THE PROJECT ALREADY HAS A NICKNAME TABLE. `forbes_match.NICKNAME_GROUPS` has
# been matching Barron's and Forbes names to the SEC feed since long before
# this, and a second independent table is precisely the drift this codebase
# keeps getting caught by -- two lists that agree today, disagree in a year, and
# make the same pair of names one person in one place and two in another.
#
# So this EXTENDS that table rather than replacing it. The additions below are
# the ones the crosswalk comparison turned up and forbes_match did not carry:
# more women's names, more truncations, and the handful that appear in Act!'s
# own data. Anything already there is inherited.
from forbes_match import NICKNAME_GROUPS as _BASE           # noqa: E402

EXTRA = [
    ("robert", "rob", "bob", "bobby", "bert"),
    ("william", "will", "bill", "billy", "willie", "liam"),
    ("james", "jim", "jimmy", "jamie"),
    ("john", "jon", "johnny", "jack"),
    ("richard", "rick", "ricky", "dick", "rich"),
    ("michael", "mike", "mickey", "mick"),
    ("charles", "charlie", "chuck", "chas"),
    ("thomas", "tom", "tommy"),
    ("christopher", "chris", "kit"),
    ("daniel", "dan", "danny"),
    ("matthew", "matt"),
    ("anthony", "tony"),
    ("donald", "don", "donnie"),
    ("joseph", "joe", "joey"),
    ("edward", "ed", "eddie", "ted", "teddy", "ned"),
    ("kenneth", "ken", "kenny"),
    ("steven", "stephen", "steve", "stevie"),
    ("andrew", "andy", "drew"),
    ("lawrence", "larry", "laurence", "lawrie"),
    ("gregory", "greg"),
    ("frederick", "fred", "freddie", "rick"),
    ("nicholas", "nick", "nicky"),
    ("benjamin", "ben", "benji"),
    ("samuel", "sam", "sammy"),
    ("alexander", "alex", "al", "sandy", "xander"),
    ("patrick", "pat", "paddy"),
    ("timothy", "tim", "timmy"),
    ("jeffrey", "geoffrey", "jeff", "geoff"),
    ("ronald", "ron", "ronnie"),
    ("jonathan", "jon", "john"),
    ("albert", "al", "bert"),
    ("douglas", "doug"),
    ("peter", "pete"),
    ("kevin", "kev"),
    ("theodore", "ted", "teddy", "theo"),
    ("eugene", "gene"),
    ("vincent", "vince", "vinny"),
    ("raymond", "ray"),
    ("philip", "phillip", "phil"),
    ("francis", "frank", "francisco", "fran"),
    ("gerald", "gerry", "jerry"),
    ("jerome", "jerry"),
    ("walter", "walt"),
    ("arthur", "art", "artie"),
    ("harold", "harry", "hal"),
    ("henry", "hank", "harry"),
    ("leonard", "leo", "len", "lenny"),
    ("martin", "marty"),
    ("maurice", "morrie"),
    ("nathaniel", "nathan", "nate"),
    ("russell", "russ", "rusty"),
    ("stuart", "stewart", "stu"),
    ("terrence", "terence", "terry", "terrance"),
    ("zachary", "zach", "zack"),
    ("bradley", "brad"),
    ("bernard", "bernie", "barney"),
    ("calvin", "cal"),
    ("clifford", "cliff"),
    ("curtis", "curt"),
    ("dennis", "denny"),
    ("derek", "derrick"),
    ("duane", "dwayne", "duke"),
    ("everett", "rett"),
    ("gordon", "gordy"),
    ("herbert", "herb"),
    ("howard", "howie"),
    ("jacob", "jake"),
    ("joshua", "josh"),
    ("julian", "jules"),
    ("marshall", "marsh"),
    ("mitchell", "mitch"),
    ("norman", "norm"),
    ("oliver", "ollie"),
    ("randall", "randal", "randolph", "randy", "randale", "rand"),
    ("reginald", "reggie"),
    ("rodney", "rod"),
    ("roger", "rodge"),
    ("ronald", "ron"),
    ("scott", "scotty"),
    ("sidney", "sid"),
    ("solomon", "sol"),
    ("spencer", "spence"),
    ("sylvester", "sly"),
    ("wallace", "wally"),
    ("warren", "ren"),
    ("wesley", "wes"),
    ("winston", "win"),
    ("elizabeth", "liz", "beth", "betsy", "betty", "eliza", "lisa", "libby", "bess"),
    ("margaret", "maggie", "meg", "peggy", "margie", "greta", "marge"),
    ("katherine", "catherine", "kathryn", "kate", "katie", "kathy", "kathey",
     "cathy", "kitty", "kay", "katharine", "kathleen", "kathlene"),
    ("jennifer", "jen", "jenny"),
    ("patricia", "pat", "patty", "trish", "tricia", "pam"),
    ("barbara", "barb", "babs"),
    ("susan", "sue", "susie", "suzanne", "suzy"),
    ("deborah", "debra", "deb", "debbie"),
    ("jessica", "jess", "jessie"),
    ("sarah", "sara", "sally"),
    ("nancy", "nan"),
    ("karen", "kari"),
    ("cynthia", "cindy", "cyndi"),
    ("angela", "angie"),
    ("melissa", "missy", "mel", "lissa"),
    ("rebecca", "becca", "becky", "reba"),
    ("stephanie", "steph", "stevie"),
    ("virginia", "ginny", "ginger"),
    ("victoria", "vicky", "vicki", "tori"),
    ("theresa", "teresa", "terri", "terry", "tess", "tracy"),
    ("christina", "christine", "chris", "christy", "tina", "kristine",
     "kristina", "kris", "kristy"),
    ("alexandra", "alex", "sandra", "sandy", "lexi"),
    ("sandra", "sandy", "sandi"),
    ("dorothy", "dot", "dottie", "dolly"),
    ("eleanor", "ellie", "nell"),
    ("frances", "fran", "frankie"),
    ("gwendolyn", "gwen"),
    ("helen", "nell"),
    ("irene", "renee"),
    ("joanne", "joann", "jo", "joanna"),
    ("judith", "judy"),
    ("laura", "laurie", "lori"),
    ("linda", "lynn", "lindy"),
    ("marilyn", "mary"),
    ("michelle", "michele", "shelly", "misha"),
    ("nicole", "nikki", "nicki"),
    ("pamela", "pam"),
    ("priscilla", "cilla"),
    ("rachel", "rae"),
    ("samantha", "sam", "sammy"),
    ("veronica", "ronnie", "vera"),
    ("amanda", "mandy"),
    ("candace", "candice", "candy"),
    ("charlotte", "lottie", "charlie"),
    ("danielle", "dani"),
    ("gabrielle", "gabby"),
    ("kimberly", "kim"),
    ("madeline", "maddie"),
    ("natalie", "nat"),
    ("olivia", "liv", "livvy"),
    ("penelope", "penny"),
    ("roberta", "bobbi", "bobbie"),
    ("valerie", "val"),
    ("yvonne", "vonnie"),
]

GROUPS = [set(g) for g in _BASE] + [set(g) for g in EXTRA]

# name -> the set of every name it can stand for.
_EQUIV: dict[str, set[str]] = {}
for _g in GROUPS:
    for _n in _g:
        _EQUIV.setdefault(_n, set()).update(_g)


def normalise(name: str) -> str:
    """The first given name, lower-cased, stripped of honorifics and punctuation."""
    s = str(name or "").strip()
    s = re.sub(r"^(mr|mrs|ms|dr|miss|rev|sir)\.?\s+", "", s, flags=re.I)
    # "SANDRA (SANDY) CERQUEIRA" -- the parenthetical is the preferred name and
    # is handled by same_person, so it is not stripped here.
    s = re.split(r"[\s.,]+", s)[0] if s else ""
    return re.sub(r"[^a-z]", "", s.lower())


def _parenthetical(name: str) -> set[str]:
    """Preferred names the source itself supplied, e.g. JUAN (JOHN) OECHSLE."""
    return {re.sub(r"[^a-z]", "", m.lower())
            for m in re.findall(r"\(([^)]{2,20})\)", str(name or ""))}


def same_person(a: str, b: str) -> bool:
    """Could these two full names denote one person, judged on the first name?

    Returns True when they agree, are a known nickname pair, differ only by an
    initial or a spelling slip, or when one source supplied the other in
    parentheses. Deliberately permissive -- see the module docstring.
    """
    fa, fb = normalise(a), normalise(b)
    if not fa or not fb:
        return True                      # nothing to disagree about
    if fa == fb:
        return True
    # "JUAN (JOHN) OECHSLE" vs "John Oechsle"
    if fb in _parenthetical(a) or fa in _parenthetical(b):
        return True
    if fa in _EQUIV.get(fb, ()) or fb in _EQUIV.get(fa, ()):
        return True
    # A bare initial agrees with anything starting the same way.
    if len(fa) == 1 or len(fb) == 1:
        return fa[0] == fb[0]
    # Diminutive by truncation: Chris/Christopher, Rob/Robert, Sam/Samuel.
    if fa.startswith(fb) or fb.startswith(fa):
        return True
    # Spelling variance in the same name: Gengshend/Gengsheng, Fransesco/
    # Francesco, Michele/Michelle. Tight enough that Darren/Doreen (0.67) and
    # Terri/Darren fail it.
    return difflib.SequenceMatcher(None, fa, fb).ratio() >= 0.82


def any_name_agrees(a: str, b: str) -> bool:
    """Do these two full names share ANY given name, allowing for nicknames?

    Comparing only the first token is wrong in both directions. "STEVEN ARTHUR
    SHERMAN" and "Arthur Sherman" are one person who goes by his middle name;
    "Terry Irrgang" and "R.Terrence Irrgang" are one person whose first token is
    an initial. Judging either on token one alone calls them strangers.

    So every given name on one side is tried against every given name on the
    other, and one agreement is enough. Surnames are excluded -- they already
    matched, which is how the pair got here, and including them would let a
    shared surname vouch for the very disagreement being tested.
    """
    def givens(s: str) -> list[str]:
        s = re.sub(r"^(mr|mrs|ms|dr|miss|rev|sir)\.?\s+", "", str(s or "").strip(),
                   flags=re.I)
        s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", " ", s, flags=re.I)
        parts = [re.sub(r"[^a-z]", "", p.lower()) for p in re.split(r"[\s.,]+", s)]
        parts = [p for p in parts if p]
        # Drop the surname; keep everything before it. A single token is a given
        # name only if there is nothing else, in which case there is nothing to
        # compare and the caller gets a permissive answer anyway.
        return parts[:-1] if len(parts) > 1 else parts

    ga, gb = givens(a), givens(b)
    # The parenthetical form is a given name the source supplied itself.
    ga = ga + sorted(_parenthetical(a))
    gb = gb + sorted(_parenthetical(b))
    if not ga or not gb:
        return True
    return any(same_person(x, y) for x in ga for y in gb)
