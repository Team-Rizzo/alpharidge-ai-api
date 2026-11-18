"""The news catalogue's categories against what each outlet covers, and the operator command
that applies them to sources already in the registry."""
from pathlib import Path

from alpharidge_ai.schema import OPEN_TYPES, EventType, category_of
from app.services import scoops, trust
from app.services.store import MemoryStore

# What an outlet covers, as open event types.
COMPANY = {"earnings", "merger_acquisition", "bankruptcy", "personnel", "layoffs", "ipo_listing",
           "funding", "partnership", "product_launch"}
MACRO = {"monetary_policy", "economic_data"}
POLITICS = {t.value for t in OPEN_TYPES if category_of(t).value == "politics"}
SPORTS = {t.value for t in OPEN_TYPES if category_of(t).value == "sports"}
TECHNOLOGY = {t.value for t in OPEN_TYPES if category_of(t).value == "technology"}
BEATS = {
    # A business desk: companies, the economy and markets, and the policy that moves them.
    "business": COMPANY | MACRO | {"delisting", "trading_halt", "security_incident",
                                   "sanction_or_tariff", "tech_regulation",
                                   "infrastructure_investment"},
    # Trade press: its industry's companies, their breaches, tariffs and plants.
    "trade": COMPANY | {"security_incident", "sanction_or_tariff", "infrastructure_investment"},
    "markets": COMPANY | MACRO | {"delisting", "trading_halt"},
    "tech": COMPANY | TECHNOLOGY,
    "science": {"breakthrough"},
    # Drug and device makers: their deals and results, and trial readouts.
    "biotech": COMPANY | {"breakthrough"},
    "security": {"security_incident", "vulnerability", "outage"},
    "datacenter": {"infrastructure_investment", "outage"},
    "sports": SPORTS,
    "politics": POLITICS,
    # Politics desks that also cover the regulation of technology.
    "policy": POLITICS | {"tech_regulation"},
    # A general newsroom: politics and the economy.
    "general": POLITICS | MACRO,
    # Court reporting: lawsuits and rulings, which are not open types.
    "courts": set(),
}
# Every outlet the catalogue lists in one category, and every outlet whose categories were
# corrected, with its beat. An outlet added in one category must be placed here.
OUTLETS = {
    "aa.com.tr/en/economy/": "business", "ansa.it/english/": "business",
    "arabnews.com": "business", "bankingdive.com": "trade",
    "batimes.com.ar/news/": "business", "bernama.com/en/business/": "business",
    "brecorder.com/news/": "business", "business.inquirer.net": "business",
    "businessdailyafrica.com/bd/": "business", "cbc.ca/news/business/": "business",
    "cfodive.com": "trade", "constructiondive.com/news/": "trade",
    "courthousenews.com": "courts", "dailysabah.com/business/": "business",
    "dawn.com/news/": "business", "dutchnews.nl": "business",
    "e.vnexpress.net/news/business/": "business",
    "en.mercopress.com": "business", "fiercehealthcare.com": "trade",
    "fiercepharma.com": "trade", "finextra.com/newsarticle/": "trade",
    "focustaiwan.tw/business/": "business", "fooddive.com/news/": "trade",
    "freightwaves.com/news/": "trade", "grocerydive.com/news/": "trade",
    "healthcaredive.com/news/": "trade", "hoteldive.com/news/": "trade",
    "hrdive.com/news/": "trade", "independent.ie/business/": "business",
    "insurancejournal.com": "trade", "interest.co.nz": "markets",
    "investmentnews.com": "markets", "iol.co.za/business/": "business",
    "jakartaglobe.id/business/": "business", "japantimes.co.jp/business/": "business",
    "legaldive.com/news/": "trade", "malaymail.com/news/money/": "business",
    "manufacturingdive.com/news/": "trade", "mexiconewsdaily.com/business/": "business",
    "modernretail.co": "trade", "nationthailand.com/business/": "business",
    "ndtvprofit.com": "markets", "nltimes.nl": "business", "notesfrompoland.com": "business",
    "offshore-energy.biz": "trade", "oilprice.com/Latest-Energy-News/World-News/": "trade",
    "paymentsdive.com": "trade", "philstar.com/business/": "business",
    "premiumtimesng.com/business/": "business", "pymnts.com": "trade",
    "restaurantdive.com/news/": "trade", "retaildive.com": "trade", "rigzone.com": "trade",
    "rnz.co.nz/news/": "business", "seekingalpha.com": "markets",
    "supplychaindive.com/news/": "trade", "swissinfo.ch/eng/": "business",
    "taipeitimes.com/News/biz/": "business", "thedailystar.net/business/": "business",
    "thenationalnews.com/business/": "business", "thestar.com.my/business/": "business",
    "thisismoney.co.uk": "business", "utilitydive.com": "trade",
    # Technology only.
    "calcalistech.com/ctechnews/": "tech", "datacenterdynamics.com/en/news/": "datacenter",
    "datacenterknowledge.com": "datacenter", "entrackr.com": "tech", "heise.de/news/": "tech",
    "quantamagazine.org": "science", "restofworld.org": "tech",
    "sciencenews.org/article/": "science", "siliconrepublic.com": "tech", "tech.eu": "tech",
    "technode.com": "tech", "technologyreview.com": "tech", "thenextweb.com": "tech",
    "tomshardware.com": "tech", "wired.com": "tech",
    # Politics only.
    "kyivindependent.com": "politics", "news.un.org/en/story/": "politics",
    "politico.eu": "policy", "propublica.org": "politics", "rollcall.com": "politics",
    "texastribune.org": "politics", "taipeitimes.com/News/": "politics",
    "japantimes.co.jp/news/": "politics", "irishtimes.com/politics/": "politics",
    "thenationalnews.com/news/": "politics",
    # Sports only.
    **dict.fromkeys((
        "cbssports.com", "en.as.com/nba/", "en.as.com/nfl/", "en.as.com/soccer/", "espn.co.uk",
        "espn.com", "espn.com.au", "espn.in", "foxsports.com", "foxsports.com.au",
        "frontofficesports.com", "goal.com", "irishtimes.com/sport/", "nbcsports.com",
        "planetrugby.com", "rugby.com.au/news/", "rugbypass.com", "skysports.com",
        "sportingnews.com", "sportsnet.ca", "supersport.com", "the-race.com",
        "timesofindia.indiatimes.com", "tntsports.co.uk", "tsn.ca"), "sports"),
    # Corrected: listed in one category that missed their beat.
    "databreaches.net": "security", "statnews.com": "biotech", "endpoints.news": "biotech",
    "fiercebiotech.com": "biotech", "biopharmadive.com/news/": "biotech",
    "medtechdive.com/news/": "biotech", "venturebeat.com": "tech", "kedglobal.com": "business",
    "axios.com": "general", "npr.org": "general", "news.sky.com/story/": "general",
    "ctvnews.ca": "general", "abcnews.com": "general", "foxnews.com": "general",
    "pbs.org": "general", "thehill.com": "general", "lemonde.fr/en/": "general",
    "sbs.com.au": "general", "standard.co.uk": "general",
}


def _catalogue():
    return {d + p: set(cats) for d, p, cats in trust.read_catalogue()}


def test_every_outlet_can_confirm_the_types_of_its_beat():
    rows = _catalogue()
    single = {k for k, cats in rows.items() if len(cats) == 1}
    assert single <= set(OUTLETS), f"place these in OUTLETS: {sorted(single - set(OUTLETS))}"
    assert set(OUTLETS) <= set(rows), sorted(set(OUTLETS) - set(rows))
    open_types = {t.value for t in OPEN_TYPES}
    missed = {}
    for outlet, beat in OUTLETS.items():
        assert BEATS[beat] <= open_types, beat
        cant = sorted(t for t in BEATS[beat]
                      if not rows[outlet] & set(scoops.confirm_categories(EventType(t))))
        if cant:
            missed[outlet] = cant
    assert not missed


def test_a_type_shares_only_categories_its_news_is_covered_in():
    assert scoops.confirm_categories(EventType("vulnerability")) == ("technology",)
    assert scoops.confirm_categories(EventType("security_incident")) == ("technology", "finance")
    assert scoops.confirm_categories(EventType("tech_regulation")) == (
        "technology", "finance", "politics")
    for t in ("breakthrough", "outage", "match_result", "election_result", "monetary_policy",
              "delisting", "trading_halt"):
        assert scoops.confirm_categories(EventType(t)) == (category_of(t).value,), t


def _script():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "trust_script", Path(__file__).parent.parent / "scripts" / "trust.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_catalogue_command_adds_missing_categories_only_when_applied(tmp_path,
                                                                             capsys):
    script, store = _script(), MemoryStore()
    csv = tmp_path / "catalogue.csv"
    csv.write_text("domain,path,categories\n"
                   "breaches.example,,finance;technology\n"
                   "paper.example,/news/,finance;politics\n"
                   "high.example,,finance;technology\n"
                   "banned.example,,finance;technology\n"
                   "new.example,,finance\n")
    await trust.add(store, "breaches.example", trust.TRUST_BASE_CATALOGUE, "catalogue import",
                    "seed", ["finance"])
    await trust.add(store, "paper.example/news/", trust.TRUST_BASE_CATALOGUE,
                    "catalogue import", "seed", ["finance", "politics"])
    # An operator's higher base is never lowered.
    await trust.add(store, "high.example", 0.9, "reviewed", "operator:x")
    banned = await trust.add(store, "banned.example", trust.TRUST_BASE_CATALOGUE,
                             "catalogue import", "seed", ["finance"])
    await trust.set_status(store, banned, trust.BANNED, "fake news", "operator:x")

    async def bases(text):
        source = await trust.find(store, text)
        return {c: await trust.base_of(store, source, c) for c in ("finance", "technology")}

    changes = await script.apply_catalogue(store, csv, "catalogue categories", "operator:x")
    out = capsys.readouterr().out
    assert [(c.source, c.category) for c in changes] == [("breaches.example", "technology")]
    assert ("breaches.example technology: no trust row (falls back to 0.10); would add it at "
            "0.70 (dry run)") in out
    assert ("python scripts/trust.py set breaches.example --trust 0.7 --category technology "
            "--reason 'catalogue categories'") in out
    assert "banned.example: banned, skipped" in out and "new.example: not in the registry" in out
    assert await bases("breaches.example") == {"finance": 0.7, "technology": 0.1}

    await script.apply_catalogue(store, csv, "catalogue categories", "operator:x", apply=True)
    assert await bases("breaches.example") == {"finance": 0.7, "technology": 0.7}
    assert await bases("high.example") == {"finance": 0.9, "technology": 0.9}
    assert (await trust.find(store, "banned.example")).status == trust.BANNED
    log = [e for e in await store.trust_log() if e.field == "base:technology"]
    assert [(e.old, e.new, e.reason) for e in log] == [("0.1", "0.7", "catalogue categories")]
    assert await script.apply_catalogue(store, csv, "again", "operator:x", apply=True) == []


async def test_the_catalogue_command_never_raises_a_base_an_operator_lowered(tmp_path, capsys):
    script, store = _script(), MemoryStore()
    csv = tmp_path / "catalogue.csv"
    csv.write_text("domain,path,categories\naxios.example,,finance;technology\n")
    source = await trust.add(store, "axios.example", trust.TRUST_BASE_CATALOGUE,
                             "catalogue import", "seed", ["finance"])
    await trust.set_trust(store, source, 0.4, "too many corrections", "operator:x", ["finance"])

    async def bases():
        found = await trust.find(store, "axios.example")
        return {c: await trust.base_of(store, found, c) for c in ("finance", "technology")}

    changes = await script.apply_catalogue(store, csv, "catalogue categories", "operator:x",
                                           apply=True)
    # Only the category with no trust row is filled; the lowered one stays as set.
    assert [(c.source, c.category) for c in changes] == [("axios.example", "technology")]
    assert await bases() == {"finance": 0.4, "technology": 0.7}
    assert "axios.example finance" not in capsys.readouterr().out
