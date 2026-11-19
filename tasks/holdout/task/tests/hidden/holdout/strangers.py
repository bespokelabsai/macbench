"""The strangers of a run: who they are, when they come, and what they say. Generated once from the seed, before play,
so every team size meets the same people in the same order and asking twice gives the same answer for honest people.

Nothing here reaches an agent except the finished lines. The wording pools for where_from and what_saw are shared by
every type. The other tells are hints, never proof: an honest person can refuse to show their arms or ask about the
guns, a bitten one can show clean arms (the bite is elsewhere), a thief can say nothing odd or claim a trade. Told
twice, a story comes out in other words, so comparing strings proves nothing; what changes is a fact, and even that
happens to honest people who misremembered. The real check is against what survivors saw.
"""
import random

NAMES = ["Nadia", "Tomas", "Greta", "Dev", "Ruth", "Hector", "Ingrid", "Malik", "Noor", "Silas", "Tess", "Viktor",
         "Wen", "Zoe", "Amos", "Bea", "Cal", "Dina", "Emil", "Faye"]
KIN = ["my brother Tom", "my neighbour Rosa", "my cousin Sam", "my sister Lucy", "my friend Ed", "my aunt Marta",
       "my boss Joe", "my wife Ines", "my husband Pete", "my daughter Hana", "my father Abel", "my flatmate Priya"]
WHEN = ["yesterday", "yesterday morning", "two days ago", "three days ago", "four days back", "the night before last",
        "a couple of nights ago", "five days ago"]

WHERE = [
    "I came from {place}, {when}.", "{Place}. I left there {when}.", "Last place I slept was {place}, {when}.",
    "Walked in from {place}. That was {when}.", "I was holed up at {place} until {when}.",
    "We'd been staying at {place}. I left {when}.", "{Place}, {when}. Nowhere else since.",
    "I've come from {place}; set off {when}.", "Been on the road since {place}, {when}.",
    "{Place} was the last roof I had. I left {when}.", "Out of {place}. I got away {when}.",
    "I hid at {place} and moved on {when}.", "Started out from {place} {when}.",
    "Most recently {place}. I walked out of there {when}.", "It was {place}, {when}, and I've kept moving.",
    "From {place}. I couldn't stay past {when}.", "If you must know, {place}, {when}.",
    "My last stop was {place}. That was {when}.", "I slept at {place} until {when}, then followed the road.",
    "{Place}, and I left {when}. It's a long walk.", "I was at {place} as late as {when}.",
    "I got here from {place}. I set out {when}.", "Straight from {place}. I left {when} and didn't stop.",
    "Came through from {place} {when}.", "{Place}. We held out there until {when}.",
    "Spent a while at {place}. I left it behind {when}.", "I'd been sheltering at {place} until {when}.",
    "From {place}, you'd know it. Left {when}.", "The last place anyone would know is {place}, {when}.",
    "I walked here from {place}; that was {when}.", "{Place}, I think you'd call it. I left {when}.",
    "Left {place} {when}. Haven't seen a soul since.", "It's been {place}, up until {when}.",
]
WHAT = [
    "What I remember: {details}.", "I saw {details}.", "I remember {details}.", "It sticks in my mind: {details}.",
    "You can't miss it: {details}.", "If you've been there you'll know: {details}.", "Mostly I recall {details}.",
    "I can tell you about {details}.", "Let me think. {Details}.", "Ask anyone who's been: {details}.",
    "{Details}. That's what I remember of it.", "When I left I could still see {details}.", "I walked right past {details}.",
    "Plain as day: {details}.", "The thing I keep seeing is {details}.", "All I can picture now is {details}.",
    "I noticed {details}.", "{Details}. I'd swear to it.", "It looked like this: {details}.",
    "As far as I can recall, {details}.", "Right, so: {details}.", "From what I remember, {details}.",
    "Nothing much to tell. {Details}.", "I can describe it: {details}.", "We looked at {details} every day.",
    "I spent enough time staring at it: {details}.", "It's not much, but: {details}.", "{Details}, if that helps.",
    "Hard to forget: {details}.", "I'd know it anywhere: {details}.", "You'd see {details}.",
    "On my way out I saw {details}.", "First thing you notice is {details}.",
]
WHO = [
    "I was with {kin}. We got split up on the road and I never found them again.",
    "{Kin} and I left together. They turned back for the others and didn't come back.",
    "I travelled with {kin} for a while. A pack of them came through at night and we ran different ways.",
    "{Kin}. They got sick, and I stayed as long as I could.",
    "There were five of us, then three, then just me and {kin}, and then just me.",
    "{Kin} was with me until the river. The current took the raft.",
    "I was with {kin}. We argued about which way to go, and they went north.",
    "{Kin} went out for water one morning and that was the last I saw of them.",
    "A group of us, {kin} among them. The others left in a van while I was out looking for food.",
    "I had {kin} with me. We lost each other in the dark when the noise started.",
    "{Kin} and I hid in a cellar for a week. They went up to look around and didn't come back down.",
    "Me and {kin}. We were going to meet at the church, and they never showed.",
    "Just {kin}. A stair gave way under them. There was nothing I could do.",
    "I was with {kin} until the fuel ran out. They walked ahead to find some.",
    "{Kin} stayed behind to hold the door. I still think about it.",
    "A few neighbours and {kin}. We scattered when the fire started.",
    "{Kin} and I took turns on watch. One morning I woke up and they'd gone.",
    "I was with {kin}. We were heading for the coast until the roads filled up.",
    "{Kin}, for most of it. They had a bad heart, and the walking was too much.",
    "There was {kin} and a couple of others. We split the food and went our own ways.",
    "We were a family once. {Kin} was the last of them.",
    "I came out of the city with {kin}. They caught a fever on the second week.",
    "{Kin} was driving. We hit something in the dark and I walked away from it alone.",
    "I was with {kin} and a dog. The dog ran off first.",
    "{Kin} and I crossed the valley together. We got separated when we had to run.",
    "A bus full of people, {kin} next to me. I don't know where the others ended up.",
    "{Kin} went to look for their kids. I told them I'd keep going.",
    "It was me and {kin}. They've gone to find a boat, or so they said.",
    "I had a group, {kin} and a few from work. It came apart once the food ran low.",
    "{Kin} and I shared a tent for a month. Then one night the tent was empty.",
    "I was with {kin}. They said to go on without them, so I did.",
    "{Kin} kept me going for weeks. They didn't wake up one morning.",
]
WHY = [
    "Everyone else is gone. There's no more to it than that.", "I'm alone because I'm the one who kept walking.",
    "The people I was with didn't make it.", "I don't want to be. That's why I'm here.",
    "We split up to find food, and I'm the one who got this far.", "The last group I joined wouldn't take me any further.",
    "I got left behind when I couldn't keep up.", "It's safer to move on your own than people say, until it isn't.",
    "I was on watch when the others left. I don't blame them.", "I had people. Now I have a bag and these boots.",
    "Bad luck, mostly. I went for water and came back to nothing.", "Nobody stays together for long out there.",
    "I've been on my own for weeks. I can't keep doing it.", "My group broke up over the food.",
    "I went to look for someone and never found them.", "I'd rather not talk about all of it. They're gone.",
    "I stayed too long at the last place, and everyone else moved on.", "I don't know how to answer that. It just happened.",
    "There were more of us when we started.", "I ran when I should have stayed. That's the honest answer.",
    "The others took the car. There wasn't room.", "I got sick, and they left while I slept.",
    "One by one, you know how it goes.", "I walked out on the people I was with. It wasn't working.",
    "I've been told there's a wall here, and I'm tired of sleeping in trees.", "Being alone isn't a choice anybody makes.",
    "I lost them at the bridge. I waited two days.", "Somebody had to go and look for help. I drew the short straw.",
    "They're all dead or gone, and I'd like not to be either.", "I got tired of burying people.",
    "I heard the noise of your work from the road and thought, people.", "No one's left to be with.",
]
# Honest people ask ordinary things too, so a question alone proves nothing.
HONEST_ASK = ["", "", "", "", " Is it safe in there?", " Is there room for one more?", " Do you need another pair of hands?",
              " I can work, if that's what you're asking.", " I don't need much.", " Are there many of you?"]
THIEF_ASK = [
    " Where do you keep your stores, anyway?", " How many guns have you got in there?", " Who looks after the stockpile at night?",
    " Is the storehouse locked after dark?", " How much do you have put by?", " Do you keep the batteries and parts together?",
    " Do you post a guard on the stores, or just the wall?", " What have you got in the way of ammunition?",
    " How many of you are armed?", " Is everything kept in the one place?", " Which way does the stockpile face?",
    " I'd sleep better knowing how many rifles you've got.", " Where would I find the medkits, if I needed one?",
]
TRADE = {
    "mechanic": ["I fixed trucks at the county depot for twenty years.", "I'm a mechanic. Give me an engine and I'll get it running.",
                 "I wired generators for the power company.", "I can build or mend most things with a motor in them.",
                 "I ran the machine shop at the mill.", "Heavy equipment was my trade, cranes mostly.",
                 "I did electrical work on the radio towers up the valley.", "I kept the school buses running for this county.",
                 "Mechanic, by trade.", "I've rebuilt more pumps and generators than I can count."],
    "medic": ["I was a nurse at the county hospital.", "I drove an ambulance for eleven years.", "I'm a paramedic. I've set more bones this year than ever.",
              "I worked in the clinic at the school.", "I'm a vet, but a wound's a wound.", "I was an army medic, a long time ago.",
              "I've been patching people up since this started.", "Twenty years on the wards.", "Nursing was my job, and it still is.",
              "I can stitch, splint and keep a fever down."],
    "hunter": ["I've hunted this valley all my life.", "I guided fishing trips on the river before all this.", "I set snares for a living, more or less.",
               "I can feed four people off what's in those woods.", "I was a game warden.", "I fished that river every summer of my life.",
               "I know where the deer go, and where the trout lie.", "I trapped for the fur trade, back when there was one.",
               "My family's always lived off the land.", "Give me a line and a morning and there'll be fish."],
}
ARMS_CLEAN = [
    "They roll up both sleeves. Sunburn, scratches from brambles, nothing else.", "They hold out both arms without a word. Thin, dirty, unmarked.",
    "Fair enough. They pull off their jacket and turn their arms over. Clean skin.", "They shrug and show you. A few old scars, nothing fresh.",
    "\"Go ahead, look.\" Both forearms are bare and unbroken.", "They strip to a vest and hold out their arms. Bruises from carrying, no bites.",
    "They push up their sleeves. A faded tattoo, freckles, no wounds.", "\"Everyone asks. Here.\" Both arms are clean.",
    "They show their arms, and then their neck without being asked. Nothing.", "They unbutton their cuffs and roll them back. Pale skin, no marks.",
    "They hold their arms out in the light and turn them slowly. Clean.", "\"Sure.\" Scratched knuckles, but the arms are clean.",
    "They take their coat off and hold out their arms. Mosquito bites, nothing worse.", "They roll their sleeves to the shoulder. Nothing to see.",
    "\"I'd ask the same.\" They show both arms. Clean.", "They hold out their arms. A healed cut on the left, old and pale.",
    "They pull back their sleeves. Dirt, and a burn scar years old.", "They show you, a little embarrassed at how thin they've got. No bites.",
    "Without hesitating they bare both arms. Nothing fresh.", "They peel off a sweater. Their arms are scraped by thorns and nothing else.",
    "\"Look all you want.\" Both arms are clean.", "They turn their arms palm up, then palm down. Unmarked.",
    "They hold up their arms. A wristwatch tan and a lot of dirt.", "They roll up their sleeves and wait. Clean skin.",
    "\"Here.\" They hold out their arms. A bramble rash, nothing more.", "They unzip their jacket and show you. No wounds.",
    "They shrug off their coat. Bruised elbow from a fall, skin unbroken.", "They offer their arms to you. Rough, sunburnt, and clean.",
    "They show both arms and lift their shirt as well. Nothing.", "They pull up their sleeves. Old farm scars, long healed.",
    "\"That's sensible.\" They show their arms. Clean.", "They hold their arms out for as long as you want to look. Clean.",
]
ARMS_REFUSE = [
    "\"It's just a scrape. I'm not stripping for you.\"", "They fold their arms. \"I've told you I'm fine.\"", "\"You don't need to see that. It's nothing.\"",
    "They take a step back. \"I'd rather not. It's a scrape from a fence.\"", "\"My arms are my business.\"", "They pull their sleeves down further. \"No.\"",
    "\"It's cold. I'm keeping my coat on.\"", "\"What do you take me for? I'm not doing that.\"", "They look away. \"Let's not do this.\"",
    "\"I caught it on some wire. It's nothing, leave it.\"", "\"You'll just have to trust me.\"", "They shake their head and keep their arms against their sides.",
    "\"I've walked for days and you want a look at my arms? No.\"", "\"There's nothing there to see, so there's no need.\"", "\"Not out here, not in front of everybody.\"",
    "They hug their jacket closer. \"I said I'm fine.\"", "\"It's a scrape, that's all. Why does it matter?\"", "\"No. Ask me something else.\"",
    "\"I don't show my arms to strangers.\"", "They laugh, not happily. \"Next you'll want my teeth.\"", "\"I fell on the road. It's nothing, drop it.\"",
    "\"Do I look bitten to you?\" They don't move their sleeves.", "\"Later, maybe. Let me get inside first.\"", "They turn their shoulder to you. \"Just a scratch.\"",
    "\"I'd prefer to keep my sleeves down.\"", "\"You can see I'm fine. Leave it there.\"", "They cross their arms tighter. \"That's not happening.\"",
    "\"It's nothing. Please, can we move on?\"", "\"I've got a right to some privacy.\"", "\"No need. It's only a graze.\"",
    "\"I'm not one of them, and that's all you're getting.\"", "They stare at you and say nothing, sleeves down.",
]
ARMS_WRAPPED = [
    "They hold out one arm. The other is wrapped from wrist to elbow in a strip of bedsheet. \"Burned it.\"",
    "One forearm is bound in a dirty bandage. \"Cut it on a window. I'd leave the wrapping on.\"",
    "They show you the right arm. The left is wound in torn shirt. \"It's healing, don't touch it.\"",
    "A rag is tied tight around one forearm. \"Barbed wire. It's fine.\"", "They show a bandaged arm and don't unwrap it. \"It's a burn.\"",
    "Their left arm is wrapped in duct tape and gauze. \"Fell on some glass.\"", "Under the sleeve there's a bandage, fresh and tight. \"Scraped it climbing a fence.\"",
    "One wrist is bound in a scarf. \"It's only a sprain.\" They don't take it off.", "They offer the clean arm and keep the bandaged one back. \"Old wound.\"",
    "A strip of towel covers one forearm. \"Dog got me, a real dog.\"", "One arm is wrapped to the elbow, the cloth damp. \"It's nothing, it weeps a bit.\"",
    "They show a wrapped forearm. \"I'd rather keep it covered, for the dirt.\"", "There's a tight field dressing on one arm. \"Nail. It's clean.\"",
    "Their forearm is bound in a sock with the toe cut off. \"Burn from a stove.\"", "One arm is taped up. \"Don't fuss, it's a bad scrape.\"",
    "They hold up a bandaged arm. \"Tree branch. It looks worse than it is.\"", "The wrapping on their arm is stained. \"I changed it this morning. It's fine.\"",
    "One forearm is swathed in cloth. \"Rope burn.\" They won't unwind it.", "They pull back a sleeve onto a wrapped arm and pull it down again quickly.",
    "A bandage runs from their knuckles up past the elbow. \"I fell through a floor.\"", "Their right arm is tied up in a strip of tarp. \"Keeps the flies off.\"",
    "One arm is bound with a belt and rags. \"Sprained, and scratched a bit.\"", "They show the wrapped arm without undoing it. \"Fence wire, days ago.\"",
    "Their arm is wrapped in clean gauze from somewhere. \"Just a burn.\"", "A dirty cloth covers one forearm. \"Leave it, it's nearly healed.\"",
    "There's a thick wrap around one forearm. \"Better not to open it up out here.\"", "They offer an arm bound in bedsheet. \"Cut myself opening a can.\"",
    "One forearm has a splint and a wrapping. \"Broke it. It's mending.\"", "The sleeve comes up over a bandage. \"A bad graze, nothing more.\"",
    "They show both arms. One is wrapped tight. \"That one's just a burn.\"", "One arm is bandaged and held close. \"I'd sooner not undo it.\"",
    "A wrapped forearm, the knot pulled tight with their teeth. \"It's fine.\"",
]
LOOKS = ["Looks about forty, in a rain jacket and muddy boots.", "Young, thin, with a heavy pack.", "Grey-haired, walking with a stick.",
         "Tall, in a torn parka, unshaven.", "Short and broad, in overalls.", "Looks barely twenty, in a school hoodie.",
         "Middle-aged, in the remains of an office suit.", "Lean, sunburnt, in a hi-vis vest.", "Heavy-set, in a fleece and work boots.",
         "Slight, with a blanket tied round the shoulders.", "Weathered, in a waxed coat.", "Stooped, in a long grey coat.",
         "Wiry, in a cycling jacket.", "Round-faced, in a nurse's fleece gone grey with dirt."]
CUE_ANY = ["Tired eyes.", "A slight limp.", "Cracked lips.", "Hands that won't stay still.", "Looks like they haven't slept in days.", "Speaks quietly."]
CUE_WORK = ["Calloused hands.", "A patched jacket, mended with care.", "Boots worn right through at the heel.", "Keeps looking at the unfinished work."]
CUE_BITTEN = ["Sweating, though the air is cool.", "Keeps a few paces back from everyone.", "Flushed, and breathing through the mouth.", "Holds one arm close to the body."]
CUE_THIEF = ["Keeps glancing past you at the stores.", "Boots a good deal newer than the rest of their clothes.", "Counts the people in sight, then looks away.",
             "A very ready smile.", "Watches your hands."]
CUE_TOOL = {"mechanic": "A socket wrench sticks out of a back pocket.", "medic": "A battered first-aid satchel hangs at the hip.",
            "hunter": "A coil of snare wire and a fishing line hang from the belt."}


# Chance of each arms answer by type (clean, refuse, wrapped), and of the other hints. All overlap on purpose.
ARMS_ODDS = {"honest": (0.72, 0.13, 0.15), "bitten": (0.38, 0.40, 0.22), "thief": (0.75, 0.15, 0.10)}
PROBE_ODDS = {"honest": 0.20, "bitten": 0.20, "thief": 0.45}    # per topic, who_with and why_alone
FALSE_TRADE = {"honest": 0.0, "bitten": 0.35, "thief": 0.55}    # claims a trade they do not have
FALSE_TOOL = {"honest": 0.0, "bitten": 0.30, "thief": 0.50}     # carries a tool that means nothing
THIEF_CHANGES_DETAIL = 0.8                                      # told twice, a thief's details shift this often
THIEF_CHANGES_WHEN = 0.6
HONEST_CHANGES_WHEN = 0.08
STRAY_CUE = 0.20                                                # any type may show another type's look
FALSE_WORK = 0.35                                               # a dishonest stranger can look like a hard worker too


def _cap(s):
    return s[:1].upper() + s[1:]


def _join(xs):
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def _say_place(l):
    n = l["name"]
    if n[:3] == "St.":
        return n
    return "the " + n[:1].lower() + n[1:]


def schedule(cfg, rnd):
    """Arrival minutes: one a day or so, the first on day 1 at noon, none after day 15, none from 18:00 on a horde day."""
    n = cfg["stranger_count"]
    days = list(range(2, 16))
    while len(days) > n - 1:
        days.pop(rnd.randrange(len(days)))
    out = [11 * 60 + 60]      # day 1 12:00
    for d in sorted(days):
        late = 17 * 60 + 30 if d % cfg["horde_every_days"] == 0 else 22 * 60
        out.append((d - 1) * 1440 + rnd.randint(7 * 60, late))
    return out


def deck(cfg, rnd):
    """The types: the configured shares, nudged by one so that the shares differ from run to run."""
    n = cfg["stranger_count"]
    shares = cfg["stranger_shares"]
    types = list(shares)
    counts = {t: int(round(shares[t] * n)) for t in types}
    while sum(counts.values()) > n:
        counts[max(("bitten", "thief"), key=counts.get)] -= 1
    while sum(counts.values()) < n:
        counts[max(counts, key=lambda t: shares[t])] += 1
    # Most strangers are honest (the manual says so): the nudge stays inside the honest and the dishonest pairs.
    for a, b in (("worker", "specialist"), ("bitten", "thief")):
        if rnd.random() < 0.5:
            a, b = b, a
        if rnd.random() < 0.5 and counts[a] > 1:
            counts[a] -= 1
            counts[b] += 1
    want = sum(len(pair) for pair in cfg["stranger_trades"])      # enough specialists for every trade the run depends on
    while counts["specialist"] < want and counts["worker"] > 1:
        counts["worker"] -= 1
        counts["specialist"] += 1
    out = [t for t in types for _ in range(counts[t])]
    rnd.shuffle(out)
    return out


def _template(text):
    """A detail with its colour and count blanked, to tell two details of one template."""
    from .worldgen import COLOURS, COUNTS
    return " ".join("_" if word.strip(",.") in COLOURS + COUNTS else word for word in text.split())


def invented(world, place, rnd, avoid=()):
    """A detail that is true of no place in the valley, and not a twin of a detail in `avoid`."""
    from .worldgen import fill_detail, detail_pool
    real = {d for l in world["landmarks"] for d in l.get("details", [])}
    taken = {_template(d) for d in avoid}
    pool = detail_pool(place["kind"])
    for _ in range(400):
        text, _alt = fill_detail(rnd.choice(pool), rnd)
        if text not in real and _template(text) not in taken:
            return text
    return "a shape I couldn't make out"


def generate(cfg, seed, world):
    rnd = random.Random(f"{seed}:strangers")
    places = {l["id"]: l for l in world["landmarks"] if l.get("details")}
    far = [l for l in places.values() if not l.get("town")]
    town = [l for l in places.values() if l.get("town")]
    rnd.shuffle(far)
    rnd.shuffle(town)
    times = schedule(cfg, rnd)
    types = deck(cfg, rnd)
    n = len(times)
    via = ["gate"] + ["town"] * int(round((1 - cfg["stranger_gate_share"]) * n)) + []
    via += ["gate"] * (n - len(via))
    tail = via[1:]
    rnd.shuffle(tail)
    via = via[:1] + tail
    names = rnd.sample(NAMES, n)
    looks = rnd.sample(LOOKS, min(n, len(LOOKS))) + [rnd.choice(LOOKS) for _ in range(max(0, n - len(LOOKS)))]
    trades = list(TRADE)
    rnd.shuffle(trades)
    in_order = []                     # in arrival order: every pair has a mechanic, so one miss does not close the ladder
    for pair in cfg["stranger_trades"]:
        in_order += rnd.sample(pair, len(pair))
    far_i, town_i = 0, 0
    stay = random.Random(f"{seed}:thief-stay")      # a game fact: its own stream, so no change of wording can move it

    def next_place():
        nonlocal far_i, town_i
        if rnd.random() < cfg["stranger_town_story_share"]:
            town_i += 1
            return town[town_i % len(town)]
        far_i += 1
        return far[far_i % len(far)]

    # Game facts first, on their own stream: no change of wording can ever move them.
    facts = []
    for i in range(n):
        typ = types[i]
        real = next_place()
        claimed = real
        if typ == "bitten":
            while claimed["id"] == real["id"]:
                claimed = next_place()
        trade = None
        if typ == "specialist":
            nth = sum(1 for f in facts if f["typ"] == "specialist")
            trade = in_order[nth] if nth < len(in_order) else trades[nth % len(trades)]
        wrong = typ in ("worker", "specialist") and rnd.random() < cfg["honest_error_rate"]
        facts.append({"typ": typ, "real": real, "claimed": claimed, "trade": trade, "wrong": wrong})

    out = []
    for i, f in enumerate(facts):
        typ, real, claimed, trade, wrong = f["typ"], f["real"], f["claimed"], f["trade"], f["wrong"]
        w = random.Random(f"{seed}:words:{i}")      # everything said, and every look
        cls = "honest" if typ in ("worker", "specialist") else typ
        when, when2 = w.sample(WHEN, 2)
        kin = w.choice(KIN)
        where_f, where_f2 = w.sample(WHERE, 2)
        what_f, what_f2 = w.sample(WHAT, 2)
        place = _say_place(claimed)

        n_det = w.choice((2, 3))
        ids = w.sample(range(len(claimed["details"])), min(n_det, len(claimed["details"])))
        told2 = None
        if typ == "bitten":
            if w.random() < 0.5:
                told = w.sample(real["details"], min(n_det, len(real["details"])))
            else:
                told = []
                for _ in range(n_det):
                    told.append(invented(world, claimed, w, told))
        elif typ == "thief":
            told = [claimed["details"][j] for j in ids]
            k = w.randrange(len(told))
            told[k] = invented(world, claimed, w, told)
            told2 = list(told)
            k = w.randrange(len(told2))
            told2[k] = invented(world, claimed, w, told2)
        else:
            told = [claimed["details"][j] for j in ids]
            if wrong:
                k = w.randrange(len(told))
                told[k] = claimed["detail_alts"][ids[k]]

        def saw(ds, frame):
            joined = _join(ds)
            return frame.format(details=joined, Details=_cap(joined))

        who_f = w.choice(WHO)
        why = w.choice(WHY)
        ask_h = w.choice(HONEST_ASK)
        ask_w, ask_y = w.sample(THIEF_ASK, 2)

        def who(k):
            return who_f.format(kin=k, Kin=_cap(k))

        claims = None
        if typ == "specialist":
            why = why + " " + w.choice(TRADE[trade])
        elif w.random() < FALSE_TRADE[cls]:
            claims = w.choice(trades)
            why = why + " " + w.choice(TRADE[claims])
        who1 = who(kin) + (" " + ask_w.strip() if w.random() < PROBE_ODDS[cls] else ask_h)
        why1 = why + (" " + ask_y.strip() if w.random() < PROBE_ODDS[cls] else "")

        clean, refuse, _wrapped = ARMS_ODDS[cls]
        x = w.random()
        arm = w.choice(ARMS_CLEAN if x < clean else ARMS_REFUSE if x < clean + refuse else ARMS_WRAPPED)

        # Anyone can show any look; a stranger's own kind of look is only more likely.
        own = {"honest": CUE_WORK, "bitten": CUE_BITTEN, "thief": CUE_THIEF}[cls]
        cues = [w.choice(own)] if w.random() < 0.5 else []
        if cls != "honest" and w.random() < FALSE_WORK:
            cues.append(w.choice(CUE_WORK))
        if trade:
            cues.append(CUE_TOOL[trade])
        elif claims and w.random() < 0.75:      # a good lie comes with the prop
            cues.append(CUE_TOOL[claims])
        elif w.random() < FALSE_TOOL[cls]:
            cues.append(w.choice(list(CUE_TOOL.values())))
        if w.random() < STRAY_CUE:
            stray = w.choice(CUE_BITTEN + CUE_THIEF)
            if stray not in cues:
                cues.append(stray)
        if len(cues) < 2:
            cues.append(w.choice(CUE_ANY))
        desc = " ".join([looks[i]] + cues[:3])

        # Told twice: other words, same facts. A thief's facts shift; so do those of an honest person who misremembered.
        told_again = list(told)
        if typ == "thief" and w.random() < THIEF_CHANGES_DETAIL:
            told_again = told2
        elif wrong:
            told_again = [claimed["details"][j] for j in ids]
        w.shuffle(told_again)
        shifts = THIEF_CHANGES_WHEN if typ == "thief" else (HONEST_CHANGES_WHEN if cls == "honest" else 0.0)
        when_again = when2 if w.random() < shifts else when
        where = [where_f.format(place=place, Place=_cap(place), when=when),
                 where_f2.format(place=place, Place=_cap(place), when=when_again)]

        out.append({"id": f"st{i + 1}", "name": names[i], "type": typ, "trade": trade, "via": via[i], "arrive": times[i],
                    "claimed": claimed["id"], "real": real["id"], "honest_error": wrong, "desc": desc,
                    "thief_days": stay.choice(cfg["thief_stay_days"]),
                    "answers": {"where_from": where,
                                "what_saw": [saw(told, what_f), saw(told_again, what_f2)], "who_with": [who1, who1],
                                "why_alone": [why1, why1], "arms": [arm, arm]}})
    return out
