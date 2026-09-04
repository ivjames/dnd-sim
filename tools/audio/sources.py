"""Search adapters over the openly-licensed audio libraries.

Three sources, chosen because each has a real API and a license field we can
trust; everything else worth having (OpenGameArt, Kenney, Pixabay, Sonniss)
has no search API and is handled by pasting a direct URL into the picker.

    freesound   effects, stings, swells, ambience. Needs FREESOUND_API_KEY
                (free, instant: https://freesound.org/apiv2/apply/).
                60 req/min, 2000 req/day.
    jamendo     full-length music beds. Needs JAMENDO_CLIENT_ID
                (free: https://devportal.jamendo.com/).
    incompetech Kevin MacLeod's catalogue, published as one JSON file and so
                searched locally. No key, one request, all CC BY — which is
                where the music actually comes from, CC0 being too thin.
    archive     Internet Archive. No key at all, so `harvest` always has
                something to show; the metadata is user-supplied and much
                messier, and items without a license URL are dropped.

Every adapter returns `Candidate`s with a normalised license code, a URL the
browser can play in an `<audio>` element without credentials, and a URL the
fetcher can download. Nothing here writes files or reads the environment for
anything but credentials.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

import httpx

__all__ = [
    "Candidate",
    "Source",
    "FreesoundSource",
    "JamendoSource",
    "IncompetechSource",
    "ArchiveSource",
    "SOURCES",
    "build_sources",
    "normalize_license",
    "PERMISSIVE",
    "LICENSE_NAMES",
]

USER_AGENT = "dnd-sim-audio-sourcing/1 (+https://github.com/ivjames/dnd-sim)"

# Normalised license codes, loosest first. `PERMISSIVE` is what the fetcher
# will download without being argued with: public domain, or attribution-only.
PERMISSIVE = ("cc0", "pd", "by", "by-sa")

LICENSE_NAMES = {
    "cc0": "CC0 (public domain)",
    "pd": "Public domain",
    "by": "CC BY (credit required)",
    "by-sa": "CC BY-SA (credit + share-alike)",
    "by-nc": "CC BY-NC (non-commercial)",
    "by-nc-sa": "CC BY-NC-SA (non-commercial)",
    "by-nd": "CC BY-ND (no derivatives)",
    "by-nc-nd": "CC BY-NC-ND (non-commercial, no derivatives)",
    "sampling+": "Sampling Plus",
    "unknown": "unknown — check the page before using",
}


def normalize_license(raw: str | None) -> str:
    """Map a Freesound license name or a Creative Commons URL to a code."""
    if not raw:
        return "unknown"
    s = raw.strip().lower()
    if "publicdomain/mark" in s or "publicdomain mark" in s:
        return "pd"
    if "publicdomain" in s or "zero" in s or s in ("cc0", "creative commons 0"):
        return "cc0"
    if "sampling" in s:
        return "sampling+"
    # URL form: .../licenses/by-nc-sa/3.0/
    for code in ("by-nc-nd", "by-nc-sa", "by-nc", "by-nd", "by-sa", "by"):
        if f"/{code}/" in s:
            return code
    # Freesound's human names.
    if s.startswith("attribution"):
        rest = s[len("attribution"):].strip()
        if "noncommercial" in rest and "sharealike" in rest:
            return "by-nc-sa"
        if "noncommercial" in rest:
            return "by-nc"
        if "sharealike" in rest:
            return "by-sa"
        if "noderiv" in rest:
            return "by-nd"
        return "by"
    if "public domain" in s:
        return "pd"
    return "unknown"


@dataclass
class Candidate:
    source: str
    source_id: str
    title: str
    author: str
    license: str
    license_url: str
    page_url: str
    preview_url: str          # plays in a browser with no credentials
    download_url: str         # what the fetcher pulls; may equal preview_url
    duration: float | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d


class Source:
    """Adapter interface. `search` returns candidates for one query string."""

    name = "abstract"
    needs = ""       # env var required, "" where none is

    def __init__(self, client: httpx.Client, credential: str = ""):
        self.client = client
        self.credential = credential

    def search(self, query: str, *, dur: tuple[float, float], limit: int,
               group: str = "") -> list[Candidate]:
        """Candidates for one query string.

        `group` is the cue's group, for a source whose answer should differ:
        the Archive uses it to ask for field recordings rather than songs when
        the cue wants ambience. Sources that have one kind of thing ignore it.
        """
        raise NotImplementedError

    def _json(self, url: str, params: dict, headers: dict | None = None) -> dict:
        r = self.client.get(url, params=params, headers=headers or {})
        r.raise_for_status()
        return r.json()


class FreesoundSource(Source):
    name = "freesound"
    needs = "FREESOUND_API_KEY"
    API = "https://freesound.org/apiv2/search/text/"
    FIELDS = "id,name,username,license,url,previews,duration,tags,filesize,type"

    # Freesound license names, as its `filter` expects them.
    FILTER_LICENSES = '("Creative Commons 0" OR "Attribution" OR "Attribution NonCommercial")'

    def search(self, query: str, *, dur: tuple[float, float], limit: int,
               group: str = "") -> list[Candidate]:
        lo, hi = dur
        payload = self._json(
            self.API,
            {
                "query": query,
                "filter": f"duration:[{lo:g} TO {hi:g}] license:{self.FILTER_LICENSES}",
                "fields": self.FIELDS,
                "sort": "rating_desc",
                "page_size": min(int(limit), 150),
            },
            headers={"Authorization": f"Token {self.credential}"},
        )
        out = []
        for s in payload.get("results") or []:
            previews = s.get("previews") or {}
            mp3 = previews.get("preview-hq-mp3") or previews.get("preview-lq-mp3") or ""
            if not mp3:
                continue
            out.append(Candidate(
                source=self.name,
                source_id=str(s.get("id")),
                title=s.get("name") or f"sound {s.get('id')}",
                author=s.get("username") or "",
                license=normalize_license(s.get("license")),
                license_url=s.get("license") or "",
                page_url=s.get("url") or f"https://freesound.org/s/{s.get('id')}/",
                preview_url=mp3,
                # Originals need OAuth2; the HQ preview is a 128kbps MP3 and is
                # what we ship. See AUDIO.md, "Quality and the preview caveat".
                download_url=mp3,
                duration=float(s.get("duration") or 0) or None,
                tags=list(s.get("tags") or [])[:12],
                extra={"format": s.get("type"), "bytes": s.get("filesize"), "preview": True},
            ))
        return out


class JamendoSource(Source):
    name = "jamendo"
    needs = "JAMENDO_CLIENT_ID"
    API = "https://api.jamendo.com/v3.0/tracks/"

    def search(self, query: str, *, dur: tuple[float, float], limit: int,
               group: str = "") -> list[Candidate]:
        lo, hi = dur
        payload = self._json(self.API, {
            "client_id": self.credential,
            "format": "json",
            "limit": min(int(limit), 200),
            "fuzzytags": " ".join(query.split()),
            "durationbetween": f"{int(lo)}_{int(hi)}",
            "audioformat": "mp32",
            "include": "musicinfo licenses",
            "boost": "popularity_month",
        })
        head = payload.get("headers") or {}
        if head.get("status") != "success":
            raise RuntimeError(f"jamendo: {head.get('error_message') or head}")
        out = []
        for t in payload.get("results") or []:
            stream = t.get("audio") or ""
            if not stream:
                continue
            info = t.get("musicinfo") or {}
            tags = info.get("tags") or {}
            out.append(Candidate(
                source=self.name,
                source_id=str(t.get("id")),
                title=t.get("name") or f"track {t.get('id')}",
                author=t.get("artist_name") or "",
                license=normalize_license(t.get("license_ccurl")),
                license_url=t.get("license_ccurl") or "",
                page_url=t.get("shareurl") or "",
                preview_url=stream,
                download_url=t.get("audiodownload") or stream,
                duration=float(t.get("duration") or 0) or None,
                tags=[*tags.get("genres", []), *tags.get("instruments", []), *tags.get("vartags", [])][:12],
                extra={"album": t.get("album_name"), "format": "mp3"},
            ))
        return out


class IncompetechSource(Source):
    """Kevin MacLeod's catalogue, searched locally.

    incompetech publishes the whole thing as one JSON file — 1400+ pieces with
    title, length, genre, instruments and a `feel` vocabulary that happens to be
    exactly the one this game needs (Dark, Eerie, Mysterious, Epic, Action,
    Suspenseful, Somber). So this adapter fetches that file once per run and
    searches it in memory: no key, no rate limit, one request.

    Everything in it is CC BY 4.0 — incompetech's free option is "Creative
    Commons — Free. No charge. Requires that you credit the music." The credit
    line is not optional and `fetch` writes it into CREDITS.md.

    It is here because CC0 music is thin: the `sheep` repo sourced two rounds
    of it and neither survived audition, then switched to this catalogue.
    """

    name = "incompetech"
    needs = ""
    CATALOG = "https://incompetech.com/music/royalty-free/pieces.json"
    FILES = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
    PAGE = "https://incompetech.com/music/royalty-free/index.html?isrc="
    AUTHOR = "Kevin MacLeod"
    LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
    GENRES = {
        "2": "African", "3": "Blues", "4": "Classical", "5": "Contemporary",
        "6": "Disco", "7": "Electronica", "8": "Funk", "9": "Holiday",
        "10": "Horror", "11": "Jazz", "12": "Latin", "13": "Modern",
        "14": "Musical", "15": "Polka", "16": "Pop", "18": "Reggae",
        "19": "Rock", "20": "Silent Film Score", "21": "Ska", "22": "Soundtrack",
        "23": "Stings", "24": "Unclassifiable", "25": "World", "26": "Urban",
    }
    # Words that match everything in a music catalogue and so rank nothing.
    STOP = frozenset({"music", "loop", "short", "bed", "track", "sound", "the", "and", "for"})

    def __init__(self, client: httpx.Client, credential: str = ""):
        super().__init__(client, credential)
        self._pieces: list[dict] | None = None

    def catalog(self) -> list[dict]:
        if self._pieces is None:
            r = self.client.get(self.CATALOG)
            r.raise_for_status()
            self._pieces = r.json()
        return self._pieces

    def _haystack(self, p: dict) -> str:
        return " ".join(str(p.get(k) or "") for k in
                        ("title", "description", "feel", "instruments")
                        ).lower() + " " + self.GENRES.get(str(p.get("genre")), "").lower()

    def search(self, query: str, *, dur: tuple[float, float], limit: int,
               group: str = "") -> list[Candidate]:
        words = [w for w in (w.strip(",.").lower() for w in query.split())
                 if len(w) > 2 and w not in self.STOP]
        scored: list[tuple[int, str, dict, float]] = []
        for p in self.catalog():
            secs = _seconds(p.get("length"))
            if secs is None or not (dur[0] <= secs <= dur[1]):
                continue
            hay = self._haystack(p)
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, p.get("title") or "", p, secs))
        # Best match first; title breaks ties so a re-run returns the same order.
        scored.sort(key=lambda t: (-t[0], t[1]))

        out = []
        for _, _, p, secs in scored[:limit]:
            isrc = (p.get("isrc") or p.get("uuid") or "").strip()
            # A few catalogue rows carry a trailing newline in the title, and
            # one in a filename would be percent-encoded into a broken URL.
            filename = (p.get("filename") or "").strip()
            title = (p.get("title") or filename or "untitled").strip()
            tags = [t.strip() for t in (p.get("feel") or "").split(",") if t.strip()]
            genre = self.GENRES.get(str(p.get("genre")))
            if genre:
                tags.append(genre)
            out.append(Candidate(
                source=self.name,
                source_id=str(isrc),
                title=title,
                author=self.AUTHOR,
                license="by",
                license_url=self.LICENSE_URL,
                page_url=f"{self.PAGE}{isrc}" if p.get("isrc") else "https://incompetech.com/music/royalty-free/music.html",
                preview_url=self.FILES + _quote(filename),
                download_url=self.FILES + _quote(filename),
                duration=secs,
                tags=tags[:12],
                extra={"bpm": p.get("bpm"), "description": p.get("description"), "format": "mp3"},
            ))
        return out


class ArchiveSource(Source):
    """Internet Archive: one search, then one metadata call per item.

    Kept to a handful of items per query — the file lists are large and the
    hit rate is low compared with the other two.
    """

    name = "archive"
    needs = ""
    SEARCH = "https://archive.org/advancedsearch.php"
    META = "https://archive.org/metadata/"
    ITEMS_PER_QUERY = 6
    AUDIO_FORMATS = ("VBR MP3", "128Kbps MP3", "64Kbps MP3", "MP3", "Ogg Vorbis")
    # The bulk of the Archive's CC audio is NonCommercial or NoDerivatives, so
    # the licence filter goes into the query rather than throwing away a whole
    # page of results afterwards.
    LICENSE_Q = r"(licenseurl:*publicdomain* OR licenseurl:*licenses\/by\/* OR licenseurl:*licenses\/by-sa\/*)"
    # A search for "cave ambience" over all of the Archive's audio returns
    # songs about caves. Ambience wants recordings *of* places, so ask the
    # part of the Archive that holds them — radio aporee alone is thousands of
    # CC-licensed field recordings.
    FIELD_Q = '(subject:"field recording" OR collection:aporee)'
    FIELD_GROUPS = ("ambience",)

    # Words that match most of the corpus and so rank nothing. "ambience" is
    # in every field recording's description, so ORing it in returns the whole
    # collection in its own order and the place word does no work.
    STOP = frozenset({"ambience", "ambient", "sound", "sounds", "audio", "loop",
                      "recording", "field", "room", "tone", "background", "the"})

    @classmethod
    def _terms(cls, query: str) -> str:
        """OR the words: the Archive ANDs them by default, and a five-word
        search phrase like "cave ambience water drips" then finds nothing.

        Generic words are dropped first — what is left is the part that picks
        one recording out rather than all of them.
        """
        words = list(dict.fromkeys(
            w for w in (w.strip(",.").lower() for w in query.split())
            if len(w) > 2 and w not in cls.STOP))
        return " OR ".join(words) or query

    def search(self, query: str, *, dur: tuple[float, float], limit: int,
               group: str = "") -> list[Candidate]:
        scope = f"{self.FIELD_Q} AND " if group in self.FIELD_GROUPS else ""
        payload = self._json(self.SEARCH, {
            "q": f"mediatype:audio AND {scope}({self._terms(query)}) AND {self.LICENSE_Q}",
            "fl[]": ["identifier", "title", "creator", "licenseurl"],
            "rows": self.ITEMS_PER_QUERY,
            "page": 1,
            "output": "json",
        })
        docs = ((payload.get("response") or {}).get("docs")) or []
        out: list[Candidate] = []
        for doc in docs:
            ident = doc.get("identifier")
            if not ident:
                continue
            lic = normalize_license(_first(doc.get("licenseurl")))
            if lic == "unknown":
                continue
            try:
                meta = self._json(f"{self.META}{ident}", {})
            except httpx.HTTPError:
                continue
            # An item holds the same recording in several encodings. Take the
            # best one and move on: two formats of one field recording are one
            # candidate, and offering both wastes an audition.
            best = None
            for f in meta.get("files") or []:
                fmt = f.get("format")
                if fmt not in self.AUDIO_FORMATS:
                    continue
                length = _seconds(f.get("length"))
                if length is not None and not (dur[0] <= length <= dur[1]):
                    continue
                rank = self.AUDIO_FORMATS.index(fmt)
                if best is None or rank < best[0]:
                    best = (rank, f, length)
            if best is None:
                continue
            _, f, length = best
            url = f"https://archive.org/download/{ident}/{_quote(f['name'])}"
            out.append(Candidate(
                source=self.name,
                source_id=f"{ident}/{f['name']}",
                title=doc.get("title") or f.get("title") or f["name"],
                author=_first(doc.get("creator")) or "",
                license=lic,
                license_url=_first(doc.get("licenseurl")) or "",
                page_url=f"https://archive.org/details/{ident}",
                preview_url=url,
                download_url=url,
                duration=length,
                tags=[],
                extra={"item": ident, "format": f.get("format"), "bytes": _int(f.get("size"))},
            ))
            if len(out) >= limit:
                return out
        return out


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _seconds(v) -> float | None:
    """Archive lengths come as "213.4" or "3:33.40"."""
    if v is None:
        return None
    s = str(v)
    try:
        if ":" in s:
            mins, _, secs = s.rpartition(":")
            total = 0.0
            for part in mins.split(":"):
                total = total * 60 + float(part or 0)
            return total * 60 + float(secs)
        return float(s)
    except ValueError:
        return None


def _quote(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


SOURCES = (FreesoundSource, JamendoSource, IncompetechSource, ArchiveSource)


def build_sources(client: httpx.Client, env: dict | None = None,
                  only: tuple[str, ...] | None = None) -> tuple[list[Source], list[str]]:
    """Instantiate every source whose credential is present.

    Returns the usable sources and a note per source that had to be skipped,
    so `harvest` can say why a search came back thin instead of silently
    returning less.
    """
    env = os.environ if env is None else env
    live: list[Source] = []
    skipped: list[str] = []
    for cls in SOURCES:
        if only and cls.name not in only:
            continue
        cred = env.get(cls.needs, "") if cls.needs else ""
        if cls.needs and not cred:
            skipped.append(f"{cls.name}: no {cls.needs} in the environment")
            continue
        live.append(cls(client, cred))
    return live, skipped
