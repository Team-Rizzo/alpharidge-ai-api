"""Trust registry: base trust and penalties, bans and primary sources."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.services import trust
from app.services.entities import EntityRecord
from app.services.store import ClaimRow, LedgerRow, MemoryStore, SourceEventRow, SourceRow
from app.utils.clock import DAY_MS, HOUR_MS

FIXTURES = Path(__file__).parent / "fixtures"
DAY = DAY_MS
H = HOUR_MS
NOW = 1_000 * DAY
CATS = ("finance", "politics", "sports")


def state(sid, owner, base=0.1, **kw):
    src = SourceRow(domain=f"s{sid}.com", owner_id=owner, base_trust=base, id=sid, **kw)
    return trust.SourceState(src, {})


class Links:
    def __init__(self, entity):
        self.entity = entity

    def link(self, actor, event_type=None):
        return self


CLAIM = SimpleNamespace(actors=[None], event_type=None)


def ev(sid, role, event_id, at, published=None, **kw):
    return SourceEventRow(source_id=sid, role=role, category="finance", at_ms=at,
                          event_id=event_id, published_ms=published, **kw)


def scores(states, events):
    return {(r.source_id, r.category): r.trust for r in trust.nightly(states, events, NOW, CATS)}


def test_trust_is_the_base_less_penalties():
    official, outlet = state(1, "gov", base=1.0), state(9, "outlet")
    events = []
    for e in range(60):
        at = NOW - (3 + e) * DAY
        events.append(ev(9, trust.FIRST, e, at, at - 1000))
        events.append(ev(1, trust.CONFIRM, e, at + 2 * H, at + 2 * H))
    t = scores([official, outlet], events)
    assert t[(9, "finance")] == 0.1 and t[(1, "finance")] == 1.0
    events.append(ev(1, trust.ANSWER_KEY, 99, NOW - DAY))
    assert 0.85 < scores([official, outlet], events)[(1, "finance")] < 1.0


async def test_ban():
    store = MemoryStore()
    src = await trust.add(store, "example.com", 0.8, "trusted outlet", "operator:test")
    assert trust.trust(src, "finance") == 0.8
    claim = await store.create_claim(ClaimRow(
        hotkey="5F", signature="00", nonce="n", signed_at=0, body_hash="b", body="",
        received_ms=NOW, bucket=0, kind="event", category="finance", event_type=None,
        payload={}, source_url="https://example.com/a", status="provisional", route="event"))
    await store.update_claim(claim.id, "provisional", source_id=src.id)
    await store.append_ledger(LedgerRow("5F", claim.id, None, "provisional", 4.0, 1))

    banned = await trust.set_status(store, src, trust.BANNED, "fabricated", "operator:test",
                                    head=500)
    assert trust.trust(banned, "finance") == 0.0
    assert (await store.get_claim(claim.id)).status == "clawed_back"
    assert sum((await store.claim_points(claim.id)).values()) == 0.0
    assert [e.field for e in await store.trust_log(source_id=src.id)][-1] == "status"


async def test_primary_source_scope_and_page_rules():
    store = MemoryStore()
    acme = EntityRecord("wikidata", "Q1", "Acme", xrefs=("cik:320193",),
                        website="https://www.acme.com/")
    beta = EntityRecord("wikidata", "Q2", "Beta", website="https://beta.example/")
    blog = EntityRecord("wikidata", "Q3", "Gamma", website="https://gamma.wordpress.com/")
    src = await trust.resolve(store, "https://www.acme.com/news/2026/deal")
    assert (src.domain, src.host, src.status) == ("acme.com", "acme.com", trust.PENDING)
    url = "https://www.acme.com/news/2026/deal"
    reg = trust.TrustRegistry()
    assert await trust.of_claim(store, reg, Links(acme), CLAIM, src, "finance", url) == \
        trust.ClaimTrust(1.0, "primary", True, official=True)
    assert await trust.of_claim(store, reg, Links(beta), CLAIM, src, "finance", url) == \
        trust.ClaimTrust(0.1, "low", False)
    looked_up = replace(acme, origin="registry")
    assert await trust.of_claim(store, reg, Links(looked_up), CLAIM, src, "finance", url) == \
        trust.ClaimTrust(0.1, "low", False)
    ir = await trust.resolve(store, "https://ir.acme.com/news/2026/deal")
    assert ir.id != src.id and not trust.is_primary(ir, acme)
    tenant = await trust.resolve(store, "https://gamma.wordpress.com/a")
    assert not trust.is_primary(tenant, blog)
    filing = await trust.resolve(store, "https://www.sec.gov/Archives/edgar/data/320193/x.htm")
    # A filing folder alone is not the filer: the index page decides (Filings).
    assert not trust.is_primary(filing, acme,
                                "https://www.sec.gov/Archives/edgar/data/320193/x.htm")

    # Sources are per host; "*." rows cover subdomains, path rules end at a segment.
    one = await trust.add(store, "a.example.org", 0.7, "catalogue", "operator:test")
    assert (await trust.resolve(store, "https://b.example.org/x")).id != one.id
    wild = await trust.add(store, "*.example.net", 0.7, "catalogue", "operator:test")
    assert (await trust.resolve(store, "https://b.example.net/x")).id == wild.id
    sites = await trust.add(store, "forbes.com/sites/", 0.1, "contributor", "operator:test")
    assert (await trust.resolve(store, "https://www.forbes.com/sites/x/1")).id == sites.id
    assert (await trust.resolve(store, "https://www.forbes.com/sitesx/1")).id != sites.id
    # Repeated slashes in a path are one.
    assert (await trust.resolve(store, "https://www.forbes.com//sites//x/1")).id == sites.id
    from app.services.novelty import doc_hash
    assert doc_hash("https://a.org//x/1", "t") == doc_hash("https://a.org/x/1", "t")
    assert reg.page_excluded("https://www.forbes.com/sites/x/2026/10/13/a") == "contributor"
    assert reg.page_excluded("https://news.example.com/markets/tag/fed") == "index page"
    assert reg.page_excluded("https://news.example.com/markets/fed-holds-rates") is None
    assert reg.page_excluded("https://comments.cftc.gov/x") == "comment docket"
    assert reg.is_wire("https://apnews.com/press-release/x/y")
    assert not reg.is_wire("https://apnews.com/article/y")
    await trust.add_rule(store, "news.example.com/markets/*", "exclude", "spam", "operator:test")
    await reg.refresh(store, force=True)
    assert reg.page_excluded("https://news.example.com/markets/fed-holds-rates") == "spam"


async def test_official_row_with_a_subject_speaks_only_for_it():
    from alpharidge_ai.schema import Actor, EventType, Fact
    from app.services.entities import EntityIndex

    store = MemoryStore()
    seed = trust.Seed((
        trust.OfficialSeed("acme.com", "/newsroom/", "company", ("technology",),
                           trust.Subject(("wikidata:Q1",), ("Acme Corporation",))),
        trust.OfficialSeed("club.example", "/news/", "club", ("sports",),
                           trust.Subject((), ("Rovers FC", "Rovers"))),
        trust.OfficialSeed("gov.example", "", "government", ("politics",)),
        trust.OfficialSeed("wire.example", "/news/", "wire", ("technology",))))
    reg = trust.TrustRegistry(seed)
    await trust.seed(store, seed)
    index = EntityIndex()

    def claim(event_type, actors, facts=()):
        return SimpleNamespace(
            event_type=EventType(event_type),
            actors=[Actor(id_scheme=s, id=i, role=r, name=n) for s, i, r, n in actors],
            facts=[Fact(field=f, value=v) for f, v in facts])

    async def of(c, url, category="technology"):
        src = await trust.resolve(store, url)
        ct = await trust.of_claim(store, reg, index, c, src, category, url)
        return ct.t, ct.official

    page = "https://www.acme.com/newsroom/2026/x"
    own = claim("product_launch", [("wikidata", "Q1", "company", "Acme")])
    by_name = claim("product_launch", [("other", "Acme", "company", "Acme Corp.")])
    partner = claim("partnership", [("other", "Beta", "party", "Beta"),
                                    ("other", "Acme", "party", "Acme")])
    other = claim("product_launch", [("other", "Beta", "company", "Beta")])
    assert await of(own, page) == await of(by_name, page) == await of(partner, page) == (1.0, True)
    assert await of(other, page) == (trust.TRUST_BASE_CATALOGUE, False)

    club = "https://club.example/news/2026/x"
    signing = claim("transfer", [("other", "Jo", "player", "Jo Bloggs")],
                    [("from_team", "Rovers"), ("to_team", "United")])
    rumour = claim("transfer", [("other", "Al", "player", "Al Smith")],
                   [("from_team", "City"), ("to_team", "United")])
    assert await of(signing, club, "sports") == (1.0, True)
    assert await of(rumour, club, "sports") == (trust.TRUST_BASE_CATALOGUE, False)
    # Rows without a subject, and wires, keep their trust; a wire page settles nothing.
    assert (await of(other, "https://gov.example/a", "politics")) == (1.0, True)
    assert (await of(other, "https://wire.example/news/a")) == (1.0, False)


def _company(name, role, cik):
    return (f'<div class="companyInfo"><span class="companyName">{name} ({role})\n'
            f' <acronym title="Central Index Key">CIK</acronym>: <a href="/cgi-bin/browse-edgar?'
            f'CIK={cik}&amp;action=getcompany">{cik} (see all company filings)</a></span>')


class IndexPages:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    async def filing_index(self, url):
        from alpharidge_ai.verify.fetch import filing_index_url
        self.calls.append(filing_index_url(url))
        return self.pages.get(self.calls[-1])

    async def get_text(self, url):
        self.calls.append(url)
        return self.pages.get(url)


async def test_filing_is_primary_for_its_filer_named_by_cik(monkeypatch):
    from alpharidge_ai.schema import Actor
    monkeypatch.setitem(trust.EVENT_LANE, "AK_SEC_USER_AGENT", "Bot ops@example.com")
    folder = "https://www.sec.gov/Archives/edgar/data/{}/000000000126000001/"
    index = folder + "0000000001-26-000001-index.htm"
    pages = IndexPages({index.format(11): (_company("Target Co", "Subject", "0000000022")
                                           + _company("Buyer LLC", "Filed by", "0000000011"))})
    store = MemoryStore()
    reg = trust.TrustRegistry(filings=trust.Filings(pages, sec=pages))

    async def of(cik, folder_cik, role="acquirer"):
        url = folder.format(folder_cik) + "doc.htm"
        src = await trust.resolve(store, url)
        claim = SimpleNamespace(event_type="merger_acquisition", actors=[
            Actor(id_scheme="other", id="y", name="Y", role="target"),
            Actor(id_scheme="cik", id=cik, name="Buyer", role=role)])
        return (await trust.of_claim(store, reg, Links(None), claim, src, "finance", url)).band

    assert await of("11", 11) == "primary"
    assert await of("0000000022", 22) == "low"
    assert await of("11", 22) == "primary"
    # The filer named as a party outside the type's subject roles gets no primary trust.
    assert await of("11", 11, role="counterparties") == "low"
    # One read per accession; the subject's folder holds the same filing, still the buyer's.
    assert sum(c.endswith("-index.htm") for c in pages.calls) == 1
    monkeypatch.setitem(trust.EVENT_LANE, "AK_SEC_USER_AGENT", "")
    assert await of("11", 11) == "low"


async def test_filer_by_cik_or_by_a_name_for_it(monkeypatch):
    from alpharidge_ai.schema import Actor
    monkeypatch.setitem(trust.EVENT_LANE, "AK_SEC_USER_AGENT", "Bot ops@example.com")
    url = "https://www.sec.gov/Archives/edgar/data/1090727/000162828026067381/doc.htm"
    index = ("https://www.sec.gov/Archives/edgar/data/1090727/000162828026067381/"
             "0001628280-26-067381-index.htm")
    # Index pages through the page fetcher, the ticker list with the SEC user agent.
    pages = IndexPages({index: _company("UNITED PARCEL SERVICE INC", "Filer", "0001090727")})
    sec = IndexPages({
        trust.TRUST_TICKERS_URL: '{"0": {"cik_str": 1090727, "ticker": "UPS", "title": "x"}, '
                                 '"1": {"cik_str": 1090727, "ticker": "BRWN", "title": "x"}, '
                                 '"2": {"cik_str": 1, "ticker": "UAL", "title": "y"}}'})
    filings = trust.Filings(pages, sec=sec)

    async def filer(name, scheme="other", id="x"):
        return await filings.filer([Actor(id_scheme=scheme, id=id, name=name,
                                          role="company")], url, "earnings")

    assert await filer("UPS", "cik", "1090727") == "0001090727"
    assert await filer("Brown Delivery", "cik", "1090727") == "0001090727"
    assert await filer("Brown Delivery", "cik", "1090728") is None
    assert await filer("UPS") == "0001090727"
    assert await filer("United Parcel Service, Inc.") == "0001090727"
    assert await filer("United Airlines") is None
    assert await filer("brwn") == "0001090727"
    assert await filer("UAL") is None
    assert trust.names_filer("Carter", "CARTERS INC")
    assert trust.names_filer("Carter's", "CARTERS INC")
    assert not trust.names_filer("Cart", "CARTERS INC")


def test_a_filer_named_shorter_without_a_cik():
    for name, registered in (("FHLBank Chicago", "Federal Home Loan Bank of Chicago"),
                             ("SST VI", "Strategic Storage Trust VI, Inc."),
                             ("NSAC I", "NorthStrive Acquisition Corp I."),
                             ("PSEG", "PUBLIC SERVICE ENTERPRISE GROUP INC")):
        assert trust.names_filer(name, registered), name
    assert not trust.names_filer("FHLB Boston", "Federal Home Loan Bank of Chicago")
    assert not trust.names_filer("The Office Gurus", "SUPERIOR GROUP OF COMPANIES, INC.")


async def test_a_schedule_filed_by_an_outside_investor_is_not_the_subjects_own(monkeypatch):
    from alpharidge_ai.schema import Actor
    monkeypatch.setitem(trust.EVENT_LANE, "AK_SEC_USER_AGENT", "Bot ops@example.com")
    folder = "https://www.sec.gov/Archives/edgar/data/{}/000000000126000002/"
    index = folder + "0000000001-26-000002-index.htm"
    pages = IndexPages({index.format(11): (_company("ACME CORP", "Subject", "0000000022")
                                           + _company("FUND PARTNERS LP", "Filed by",
                                                      "0000000011"))})
    reg = trust.TrustRegistry(filings=trust.Filings(pages, sec=pages))
    store = MemoryStore()
    url = folder.format(11) + "sc13d.htm"
    src = await trust.resolve(store, url)
    claim = SimpleNamespace(event_type="layoffs", actors=[
        Actor(id_scheme="cik", id="22", name="Acme Corp", role="company"),
        Actor(id_scheme="cik", id="11", name="Fund Partners", role="sites")])
    got = await trust.of_claim(store, reg, Links(None), claim, src, "finance", url)
    assert (got.band, got.primary, got.filer) == ("low", False, None)


async def test_seed_and_nightly_job():
    from app.jobs import trust_nightly

    store = MemoryStore()
    assert await trust.seed(store, catalogue=trust.CATALOGUE, now_ms=NOW) > 0
    assert await trust.seed(store, catalogue=trust.CATALOGUE, now_ms=NOW) == 0
    await trust_nightly.run(store, NOW)
    league = await trust.find(store, "nba.com")
    wire = await trust.resolve(store, "https://apnews.com/article/x")
    assert (trust.trust(league, "sports"), trust.trust(league, "finance")) == (1.0, 0.1)
    assert league.status == trust.ACTIVE and trust.trust(wire, "politics") == 0.7
    # Seeded paths only; the rest of the host starts at the base for other sources.
    news = await trust.resolve(store, "https://www.sec.gov/newsroom/press-releases/2026-1")
    rest = await trust.resolve(store, "https://www.sec.gov/comments/s7-1/x.htm")
    assert (trust.trust(news, "finance"), trust.trust(rest, "finance")) == (1.0, 0.1)
    asx = await trust.resolve(store, "https://announcements.asx.com.au/asxpdf/20261022/pdf/x.pdf")
    times = await trust.resolve(store, "https://www.irishtimes.com/business/2026/10/23/x/")
    assert (trust.trust(asx, "finance"), trust.trust(times, "finance")) == (1.0, 0.7)
    # The paid wires' own release pages are seeded, so the wire cap does not apply there.
    reg = trust.TrustRegistry()
    for url, t in (("https://www.prnewswire.com/news-releases/acme-301.html", 1.0),
                   ("https://www.morningstar.com/news/pr-newswire/2026/acme", 0.1),
                   ("https://www.nbcnews.com/business/markets/acme-rcna1", 0.7)):
        src = await trust.resolve(store, url)
        claim = SimpleNamespace(actors=[], event_type="earnings")
        assert (await trust.of_claim(store, reg, Links(None), claim, src, "finance", url)).t == t


def test_body_dated_sites_are_official_seed_sources():
    import json

    from alpharidge_ai.verify import official
    data = json.loads(trust.SEED_PATH.read_text())
    seeded = {o["host"] + p for o in data["official"] for p in o["paths"] or [""]}
    assert set(official.SITES) <= seeded


async def test_trusted_list_shows_each_winning_source_and_whom_it_speaks_for():
    store = MemoryStore()
    seed = trust.Seed((
        trust.OfficialSeed("acme.com", "/newsroom/", "company", ("technology",),
                           trust.Subject(("wikidata:Q1",), ("Acme Corporation",))),
        trust.OfficialSeed("gov.example", "", "government", ("politics",)),
        trust.OfficialSeed("wire.example", "/news/", "wire", ("technology",))))
    reg = trust.TrustRegistry(seed)
    await trust.seed(store, seed)
    await trust.resolve(store, "https://unlisted.example/a")
    listed = await trust.trusted_list(store, reg, ("technology", "politics"))
    rows = {r["source"]: r for r in listed["sources"]}
    assert set(rows) == {"acme.com/newsroom/", "gov.example", "wire.example/news/"}
    assert rows["acme.com/newsroom/"]["official_for"] == ["Acme Corporation"]
    assert rows["gov.example"] == {"source": "gov.example", "trust": {"politics": 1.0},
                                   "kind": "official"}
    assert rows["wire.example/news/"]["kind"] == "wire"
