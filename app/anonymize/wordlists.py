"""Word lists of `denial-agent anonymize` (docs/SPEC.md section 16).

``GIVEN_NAMES``, ``FAMILY_NAMES``, ``STREET_NAMES`` and ``TOWNS`` are the pseudonym pools, frozen
here so that an edit to the synthetic test data (``synthetic/names.py``) never changes what a
pseudonym can be. They are invented names; any word of them that is also an ordinary English word
(:data:`COMMON_WORDS`) is left out, so a restore never mistakes a word of a letter for a pseudonym.

``COMMON_WORDS`` lists names that are also ordinary words ("Low back pain", "will return", "May 3",
"X-ray"); ``FREQUENT_WORDS`` is the part of it notes and letters use as ordinary words all the time.
A patient name token in ``FREQUENT_WORDS``, in ``COMMON_WORDS`` when the batch writes it in lower
case, or of two letters or fewer is *ambiguous*: free text replaces it only in a name context
(``app.anonymize.text``), never as a bare word.
"""

from __future__ import annotations

_GIVEN = (
    "Adalyn Amias Ansel Bexley Blythe Brisa Caspian Cormac Corwin Dariela Dashiell Delphine Elowen "
    "Elspeth Emerick Eudora Fenna Finnian Florian Garrick Gideon Greer Gwendolyn Hadley Halden Harlow "
    "Hollis Idris Ignatius Isadora Ivette Jesper Jorund Jovan Juniper Kalista Kenji Keziah Kieran "
    "Lavinia Leopold Liesl Lorcan Magnus Maren Marisol Mireille Nerissa Niamh Nikolai Nolwenn Odalys "
    "Orla Osric Ottilie Pascal Percival Perrin Phelan Quentin Quilla Rhoswen Romilly Sabine Soren "
    "Tamsin Thaddeus Ulla Ulric Vashti Verity Wendel Wilhelmina Xander Xiomara Yannick Yolanda Zephyr "
    "Zinnia"
)
_FAMILY = (
    "Amberly Applewhite Ashgrove Bellweather Birchmont Brennawick Caldermoor Coldwater Cresswick "
    "Dormerly Dravenholt Dunweathy Easterbrook Elmsworth Eversleigh Fairhollow Falconridge Fennimore "
    "Gadsbury Glenmorrow Grimsdell Hallorwick Hazelden Hollingbrook Inchbald Ingleby Ironwood Jarnwick "
    "Joybell Kestwick Kettleburn Kirkmoor Lambourne Larkhaven Lockridge Marrowby Merriwether Mossgrove "
    "Nethercott Nettlefold Northwick Oakhurst Oldcastle Ostrander Pemberwick Pendergrast Pennywhistle "
    "Quarrington Quillfeather Quimberly Ravensworth Redfern Rosendahl Sallowmere Silverthorne "
    "Stonebridge Tanglewood Thistlewood Thornbury Ulverston Umbercroft Upworthy Vandermere Vexley "
    "Vossberg Wexcombe Winterbourne Wyckford Yarrowdale Yelverton Yewbranch Zellinger Zimmerlane "
    "Zorander"
)
_STREETS = (
    "Alder Hollow Road",
    "Birchwater Lane",
    "Bramblewood Court",
    "Cobblestone Court",
    "Copperleaf Drive",
    "Dovetail Lane",
    "Driftwood Avenue",
    "Elderberry Drive",
    "Evergreen Hollow",
    "Fernbrook Road",
    "Foxglove Way",
    "Gingerroot Lane",
    "Hearthstone Drive",
    "Hollyhock Court",
    "Juniperwood Road",
    "Kingfisher Lane",
    "Lanternfield Way",
    "Larkspur Lane",
    "Maplecrest Drive",
    "Meadowlark Road",
    "Millbrook Court",
    "Mossbank Avenue",
    "Nightjar Lane",
    "Orchardgate Road",
    "Pebblebrook Way",
    "Quailrun Drive",
    "Ravenhill Road",
    "Saddleback Court",
    "Silvermist Lane",
    "Sparrowhawk Drive",
    "Thimbleberry Way",
    "Willowbend Road",
)
_TOWNS = (
    "Larkspur Falls",
    "Cedar Hollow",
    "Port Alder",
    "Silverbrook",
    "Thistle Creek",
    "Maple Bluff",
    "Ravenna Springs",
    "Brightwater",
    "Kettle Harbor",
    "Fernhollow",
    "Juniper Flats",
    "Alder Crossing",
    "Copper Ridge",
    "Mossy Point",
    "Quail Grove",
    "Wren Hollow",
)

# Names that are also ordinary words: clinical words, months, colours, trades, nature words and the
# short forms of first names. Lower case, folded (see app.anonymize.matching.name_key).
COMMON_WORDS: frozenset[str] = frozenset(
    """
    abbey able acre adams age alder all allen amber angel apple april arch archer arm armstrong art
    ash ashley aster august autumn avery baby back badger bailey baker ball bank banks barber bard
    barley barn baron barr bass batch bates bath beach bean bear beard beck bed bee bell belle bells
    bench bend benny berry best bill birch bird bishop black blade blain blake blank bliss bloom
    blue boat bob bolt bond bone booth boss bow bowl bowman box boyd brand brandy bray brick bridge
    bright brisk brook brooks brown buck bud bull bunny burns bush butcher butler butter cain
    calm camp candy cane cannon cap cape card care carol carter carver case cash castle cat cave chad
    chance chapel chase cherry chester chip christian church clay clear clement cliff close cloud
    coal coat cobb coffee cole coles colt comfort cook cooper copper coral corn cotton court cox coy
    crane creek crisp crook cross crow crown cruz crystal cure curtis cutter daisy dale dame dancer
    dart dash daw dawn day deal dean dear deer dell den desire diamond dick doctor dollar don dove
    down drake drew duck duke dunn dusty early earl easter east echo edge eden elder ember english
    eve ever faith fall falls fame farmer fawn fay fellow fern ferry field fields fine finch fir fish
    fisher fitch fleet flint flood flower flowers ford forest fort fortune foster fowler fox frank free
    freeman french friend frost fry fuller gale game garden gardner gay gem gene gentry ghost gift
    gill ginger glad glass glen glenn glory gold golden good goodman goose grace grand grant grape
    grass gray grey green grove guard guy hale hall hand hardy hare harmony harp hart harvest
    hatch haven hawk hay hazel head heart heath heather heaven hedge hero heron hickory high hill
    hills hogg holder holly home honey honor hood hook hope horn horne horse house howe hull hunt
    hunter hyde ice iris iron ivy jack jade jasmine jay jewel john joy judge june junior juniper just
    justice keen kemp kid kidd kind king kingdom kit knight knot lace lake lamb lance land lane lark
    larkin law lawless lay lea leaf lean lee lemon levy liberty light lily lime linden link lion
    little lock lodge long lord love lovely low lowe lucky lyon mace major mallow mann mano maple marine
    mark marsh marshal mason master max may mayo meadow means mellow mercy merry mild miles mill miller
    mills mint minor misty moody moon moore more morning moss mount mountain mullet nail nash neal
    needle nest new newman nickel night noble north nurse oak oakes oats ocean odd old olive opal
    orange orchard osborn page paine palm paris park parker parks parrish pascal pass patience
    payne peace peach peak pear pearl pebble penny pepper perry person petty pick pierce pike pine
    piper plain plant pleasant plum poe pond pool poole pope porter post potter pound power powers
    pratt pray precious price pride prince prior pure queen quick rabbit rain rainey raven ray read
    red reed rich ridge ring river robin rock rocky rod rogers rook root rose ross rowe ruby rush
    rusty sable sage sale salmon salt sand sands savage sawyer scales scott seal sharp shaw shelby
    shell shepherd shields ship short silver simple singer skinner sky slack slate slaughter small
    smart smith snow sole solo song sorrow south spark sparks sparrow speed spencer spring squire
    stack stamp star stark starr steel steele stem sterling stern steward stewart stock stone storm
    stout strange straw street strong stump sugar summer summers sun sunny swallow swan sweet swift
    sword tall tanner taylor temple terry thatcher thorn thorne thrush tiger tiny todd tower trace
    tracy trapp travis tree trent trout true tucker tulip turner tyler upton vale valentine valley
    van vane vaughn verity vice vine violet wade wait wake walker wall wallace walls walter ward ware
    warren wash water waters watt way weaver webb weeks well wells west whale wheat wheeler white
    wild will willow wills wilson wind wing winter wise wolf wolfe wood woods word worth wren wright
    yard york young zephyr zinnia
    bill bob dan don ed al art bo cal del eli flo gus hal jan jo kay lee len lou mac mae mo nan ned
    pat peg ray rob rod ron roy sal sam sid sue ted tim tom val vic wes
    """.split()
)


# The part of COMMON_WORDS that notes and letters use as ordinary words all the time ("May 3",
# "will return", "Low back pain", "X-Ray"): always ambiguous. The rest of COMMON_WORDS is ambiguous
# only when the batch itself writes the word in lower case.
FREQUENT_WORDS: frozenset[str] = frozenset(
    """
    april may june august will mark bill fall low long ray back care hope grace joy rose faith
    patience chase case cash price page rush best early young white black brown green gray grey
    king rich sharp short small strong swift wise well wells west north south east hand head foot
    neck hip bone heart rest ice cold mild high light free true frank art pat sue gene guy jack max
    drew don rob doctor nurse little park hall hill field wood stone lane street day night
    """.split()
)


def _usable(words: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(word for word in words if word.lower() not in COMMON_WORDS)


GIVEN_NAMES: tuple[str, ...] = _usable(tuple(_GIVEN.split()))
FAMILY_NAMES: tuple[str, ...] = _usable(tuple(_FAMILY.split()))
STREET_NAMES: tuple[str, ...] = _STREETS
TOWNS: tuple[str, ...] = _TOWNS

__all__ = ["COMMON_WORDS", "FAMILY_NAMES", "FREQUENT_WORDS", "GIVEN_NAMES", "STREET_NAMES", "TOWNS"]
