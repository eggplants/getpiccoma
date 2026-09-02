"""Download and unscramble manga pages from Piccoma."""

from __future__ import annotations

import json
import math
import re
import warnings
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from bs4.element import Tag
from pathvalidate import sanitize_filename
from PIL import Image
from requests import Session
from requests.adapters import HTTPAdapter, Retry
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://piccoma.com/",
}

VALID_HOSTS = ("piccoma.com",)

BASE_URL = "https://piccoma.com"
LOGIN_URL = f"{BASE_URL}/web/acc/email/signin"

#: What `eType` in the viewer's `_pdata_` calls the two kinds of listing.
EpisodeType = Literal["E", "V"]

#: The viewer slices every page into a grid of squares this wide and shuffles them.
TILE_SIZE = 50

# Piccoma bounces an episode the session may not open to its sign-in page.
_SIGNIN_PREFIX = "/web/acc/"

_VIEWER_PATH = re.compile(r"^/web/viewer/(\d+)/(\d+)/?$")
_PRODUCT_PATH = re.compile(r"^/web/product/(\d+)(?:/episodes)?/?$")

# `_pdata_` is a JavaScript object literal, not JSON: single-quoted strings,
# bare keys and trailing commas. Only the parts that matter are read out of it.
_PDATA = re.compile(r"var\s+_pdata_\s*=\s*\{(.*?)\n\s*\}", re.DOTALL)
_PDATA_FIELD = re.compile(r"'(?P<key>\w+)'\s*:\s*(?P<value>'[^']*'|true|false|-?\d+)")
_PDATA_IMAGE = re.compile(
    r"'path'\s*:\s*'(?P<path>[^']+)'"
    r"(?:[^{}]*?'width'\s*:\s*(?P<width>\d+))?"
    r"(?:[^{}]*?'height'\s*:\s*(?P<height>\d+))?",
)
_LOGIN_FLAG = re.compile(r"'login'\s*:\s*(?P<value>true|false)")

# Indices `_seed_key` leaves alone below index 10, and the ones it always flips above it.
_KEPT_BELOW_TEN = frozenset({3, 4, 5, 8})
_FLIPPED_ABOVE_TEN = frozenset({13, 14, 16})


class PiccomaError(Exception):
    """Base class for every error this module raises."""


class NotAPiccomaPageError(PiccomaError):
    """The fetched page carries no Piccoma viewer."""


class LoginError(PiccomaError):
    """Piccoma refused the credentials."""


class NeedPurchase(Warning):
    """The episode is not readable without buying it, waiting, or logging in."""


class Page(TypedDict):
    """One page of an episode, as the viewer's `_pdata_` describes it."""

    url: str
    width: int
    height: int


@dataclass(frozen=True)
class Episode:
    """What a viewer page says about itself."""

    url: str
    product_id: str
    episode_id: str
    series_title: str
    episode_title: str
    episode_type: EpisodeType
    scrambled: bool
    pages: tuple[Page, ...] = ()
    next_url: str | None = None


@dataclass(frozen=True)
class Entry:
    """One row of a product's episode or volume list."""

    id: str
    title: str
    url: str
    is_free: bool = False
    is_zero_plus: bool = False
    is_read_for_free: bool = False
    is_wait_until_free: bool = False
    is_already_read: bool = False
    is_purchased: bool = False

    @property
    def is_readable(self) -> bool:
        """Whether the row looks readable right now.

        `is_zero_plus` and `is_wait_until_free` are campaign badges rather than
        an entitlement -- both still want a ticket -- so neither counts here.
        """
        return self.is_free or self.is_read_for_free or self.is_purchased


# ---------------------------------------------------------------------------
# Descrambling
#
# The viewer shuffles the tiles of a page with the seedrandom PRNG (an ARC4
# stream keyed by a seed string), so putting a page back together means running
# the same PRNG over the same seed and replaying the shuffle. The seed is
# derived from the image URL alone; `parse_seed` does that derivation.
# ---------------------------------------------------------------------------

_ARC4_WIDTH = 256
_ARC4_MASK = _ARC4_WIDTH - 1
_PRNG_CHUNKS = 6
_PRNG_STARTDENOM = _ARC4_WIDTH**_PRNG_CHUNKS
_PRNG_SIGNIFICANCE = 2**52
_PRNG_OVERFLOW = _PRNG_SIGNIFICANCE * 2


class _ARC4:
    """The ARC4 keystream seedrandom draws its bits from."""

    def __init__(self, key: list[int]) -> None:
        self._state = list(range(_ARC4_WIDTH))
        self._i = 0
        self._j = 0

        j = 0
        for i in range(_ARC4_WIDTH):
            swapped = self._state[i]
            j = _ARC4_MASK & (j + key[i % len(key)] + swapped)
            self._state[i] = self._state[j]
            self._state[j] = swapped
        # seedrandom discards a full round before handing any bits out.
        self.generate(_ARC4_WIDTH)

    def generate(self, count: int) -> int:
        """Return `count` keystream bytes packed into one big-endian integer."""
        state, i, j, result = self._state, self._i, self._j, 0
        for _ in range(count):
            i = _ARC4_MASK & (i + 1)
            swapped = state[i]
            j = _ARC4_MASK & (j + swapped)
            state[i] = state[j]
            state[j] = swapped
            result = result * _ARC4_WIDTH + state[_ARC4_MASK & (state[i] + state[j])]
        self._i, self._j = i, j
        return result


def _mixkey(seed: str) -> list[int]:
    """Fold a seed string into an ARC4 key the way seedrandom does."""
    key: list[int] = []
    for index, char in enumerate(seed):
        key.insert(_ARC4_MASK & index, ord(char))
    return key


def seedrandom(seed: str) -> Callable[[], float]:
    """Build the seeded PRNG the viewer shuffles its tiles with.

    Args:
        seed: The seed string, as `parse_seed` returns it.

    Returns:
        A callable handing out floats in `[0, 1)`.

    Raises:
        PiccomaError: The seed is empty, which keys nothing.
    """
    if not seed:
        msg = "cannot seed the shuffle with an empty string."
        raise PiccomaError(msg)
    arc4 = _ARC4(_mixkey(seed))

    def prng() -> float:
        numerator: float = arc4.generate(_PRNG_CHUNKS)
        denominator: float = _PRNG_STARTDENOM
        extra = 0
        # Pull more bytes until the fraction carries a full mantissa of them,
        # then halve it back under the range a double represents exactly.
        while numerator < _PRNG_SIGNIFICANCE:
            numerator = (numerator + extra) * _ARC4_WIDTH
            denominator *= _ARC4_WIDTH
            extra = arc4.generate(1)
        while numerator >= _PRNG_OVERFLOW:
            numerator /= 2
            denominator /= 2
            extra >>= 1
        return (numerator + extra) / denominator

    return prng


def shuffle_order(size: int, seed: str) -> list[int]:
    """Replay the viewer's shuffle of `size` tiles.

    Args:
        size: How many tiles are being shuffled.
        seed: The seed string, as `parse_seed` returns it.

    Returns:
        The source tile index for each destination tile.
    """
    prng = seedrandom(seed)
    remaining = list(range(size))
    return [remaining.pop(math.floor(prng() * len(remaining))) for _ in range(size)]


def _seed_key(checksum: str) -> str:
    """Flip the low bit of the bytes of `checksum` that the viewer flips.

    The viewer leaves a handful of positions alone, so the transform is a fixed
    pattern of positions rather than anything derived from the value itself.

    Args:
        checksum: The rotated checksum taken off the image URL.

    Returns:
        The seed string to key the PRNG with.
    """
    last = len(checksum) - 1
    out = bytearray()
    for index, code in enumerate(checksum.encode()):
        if index < 10:  # noqa: PLR2004 (the viewer's own boundary)
            flip = index not in _KEPT_BELOW_TEN
        elif index in _FLIPPED_ABOVE_TEN:
            flip = True
        else:
            flip = index in (last, last - 1)
        out.append(code ^ 1 if flip else code)
    return out.decode()


def parse_seed(image_url: str) -> str | None:
    """Work out the seed a page image was shuffled with.

    The CDN path carries a per-episode checksum, which the viewer rotates right
    by every non-zero digit of the URL's `expires` stamp and then perturbs.
    Unscrambled pages are served under a lowercase checksum instead.

    Args:
        image_url: The page's image URL, `expires` query and all.

    Returns:
        The seed string, or None when the image is not scrambled.

    Raises:
        PiccomaError: The URL carries no checksum or no `expires` stamp.
    """
    parsed = urlparse(image_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 2:  # noqa: PLR2004 (a checksum and a file name)
        msg = f"{image_url!r} carries no checksum path segment."
        raise PiccomaError(msg)
    checksum = segments[-2]

    expires = parse_qs(parsed.query).get("expires")
    if not expires:
        msg = f"{image_url!r} carries no 'expires' stamp to rotate the checksum by."
        raise PiccomaError(msg)

    for digit in expires[0]:
        shift = int(digit) if digit.isdigit() else 0
        if shift:
            checksum = checksum[-shift:] + checksum[:-shift]

    return _seed_key(checksum) if checksum.isupper() else None


def _tile_groups(width: int, height: int, tile_size: int) -> Iterator[list[tuple[int, int]]]:
    """Group the tiles of an image by shape, top-left corner first.

    Tiles along the right and bottom edge are cut short, and the viewer shuffles
    each differently shaped group among itself rather than all tiles as one.

    Args:
        width: The image's width.
        height: The image's height.
        tile_size: The side of a full tile.

    Yields:
        The corners of every tile of one shape, in row-major order.
    """
    columns = math.ceil(width / tile_size)
    rows = math.ceil(height / tile_size)
    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for index in range(columns * rows):
        row, column = divmod(index, columns)
        x, y = column * tile_size, row * tile_size
        shape = (min(tile_size, width - x), min(tile_size, height - y))
        groups.setdefault(shape, []).append((x, y))
    yield from groups.values()


def descramble(image: Image.Image, seed: str, tile_size: int = TILE_SIZE) -> Image.Image:
    """Put a shuffled page back together.

    Args:
        image: The page exactly as the CDN serves it.
        seed: The seed string, from `parse_seed`.
        tile_size: The side of a full tile.

    Returns:
        A new image with the tiles back where they belong.
    """
    out = image.copy()
    for corners in _tile_groups(*image.size, tile_size):
        tile_width = min(tile_size, image.width - corners[0][0])
        tile_height = min(tile_size, image.height - corners[0][1])
        group_x, group_y = corners[0]
        # A group's own grid is as wide as the run of tiles sharing its first row.
        group_columns = sum(1 for _, y in corners if y == group_y)

        for (dest_x, dest_y), source in zip(corners, shuffle_order(len(corners), seed), strict=True):
            row, column = divmod(source, group_columns)
            x, y = group_x + column * tile_width, group_y + row * tile_height
            out.paste(image.crop((x, y, x + tile_width, y + tile_height)), (dest_x, dest_y))
    return out


class Piccoma:
    """Fetch episodes from Piccoma."""

    def __init__(self, session: Session | None = None) -> None:
        """Build a client.

        Args:
            session: A session to reuse. A retrying one is made when omitted.
        """
        if session is None:
            session = Session()
            adapter = HTTPAdapter(max_retries=Retry(total=10, backoff_factor=1))
            session.mount("https://", adapter)
            session.mount("http://", adapter)
        self._session = session
        self._logged_in = False
        self._lists: dict[tuple[str, EpisodeType], list[Entry]] = {}
        self._series_titles: dict[str, str] = {}

    @property
    def logged_in(self) -> bool:
        """Whether `login` has signed this client in."""
        return self._logged_in

    @staticmethod
    def is_valid_uri(url: str) -> bool:
        """Report whether `url` is a Piccoma viewer or product page.

        Args:
            url: The URL to check.

        Returns:
            True when the URL is an https Piccoma viewer or product URL.
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in VALID_HOSTS:
            return False
        return bool(_VIEWER_PATH.match(parsed.path) or _PRODUCT_PATH.match(parsed.path))

    @staticmethod
    def parse_uri(url: str) -> tuple[str, str | None]:
        """Split a Piccoma URL into the ids it names.

        Args:
            url: A viewer or product URL.

        Returns:
            The series' id, and the episode's id when the URL names one.

        Raises:
            PiccomaError: The URL is not one this module reads.
        """
        parsed = urlparse(url)
        if parsed.hostname in VALID_HOSTS:
            viewer = _VIEWER_PATH.match(parsed.path)
            if viewer:
                return viewer.group(1), viewer.group(2)
            product = _PRODUCT_PATH.match(parsed.path)
            if product:
                return product.group(1), None
        msg = f"'{url}' is not a Piccoma viewer or product url."
        raise PiccomaError(msg)

    @staticmethod
    def viewer_url(product_id: str | int, episode_id: str | int) -> str:
        """Build the viewer URL of one episode.

        Args:
            product_id: The series' id.
            episode_id: The episode's id.

        Returns:
            The viewer URL.
        """
        return f"{BASE_URL}/web/viewer/{product_id}/{episode_id}"

    def get(  # noqa: PLR0913
        self,
        url: str,
        save_path: str | Path = ".",
        *,
        overwrite: bool = False,
        only_first: bool = False,
        save_metadata: bool = False,
        print_log: bool = False,
    ) -> tuple[str | None, Path, bool]:
        """Download one episode and unscramble every page of it.

        Args:
            url: The viewer URL.
            save_path: Directory to build `<series>/<episode>/` under.
            overwrite: Download again even if the directory already exists.
            only_first: Stop after the first page.
            save_metadata: Also write what the viewer said about the episode.
            print_log: Draw a progress bar.

        Returns:
            The next episode's URL (or None), the directory written, and
            whether anything was actually downloaded.
        """
        episode = self.episode_info(url)
        save_dir = Path(save_path) / sanitize_filename(episode.series_title) / sanitize_filename(episode.episode_title)
        if save_dir.exists() and not overwrite:
            return episode.next_url, save_dir, False

        if not episode.pages:
            warnings.warn(episode.episode_title, NeedPurchase, stacklevel=2)
            return episode.next_url, save_dir, False

        save_dir.mkdir(parents=True, exist_ok=True)
        if save_metadata:
            (save_dir / "metadata.json").write_text(
                json.dumps(
                    {
                        "url": episode.url,
                        "product_id": episode.product_id,
                        "episode_id": episode.episode_id,
                        "series_title": episode.series_title,
                        "episode_title": episode.episode_title,
                        "episode_type": episode.episode_type,
                        "scrambled": episode.scrambled,
                        "next_url": episode.next_url,
                        "pages": list(episode.pages),
                    },
                    indent=4,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        self._save_pages(episode, save_dir, only_first=only_first, print_log=print_log)
        return episode.next_url, save_dir, True

    def login(self, email: str, password: str) -> None:
        """Sign in, so episodes the account may read become readable.

        The sign-in form is a plain Django one: a CSRF token off the page goes
        back with the credentials, and the session cookie lands on the shared
        session. This grants nothing the account does not already own.

        Args:
            email: The address the account uses.
            password: The account's password.

        Raises:
            LoginError: Piccoma refused the credentials.
        """
        form = self._session.get(LOGIN_URL, headers=HEADERS, timeout=30)
        form.raise_for_status()
        token = BeautifulSoup(form.content, "html.parser").find("input", attrs={"name": "csrfmiddlewaretoken"})
        if not isinstance(token, Tag):
            msg = f"{LOGIN_URL} carries no sign-in form."
            raise LoginError(msg)

        res = self._session.post(
            LOGIN_URL,
            data={
                "csrfmiddlewaretoken": str(token.attrs.get("value", "")),
                "next_url": "/web/",
                "email": email,
                "password": password,
            },
            headers={**HEADERS, "Origin": BASE_URL, "Referer": LOGIN_URL},
            timeout=30,
        )
        res.raise_for_status()

        if not self._login_status():
            msg = f"piccoma.com refused the credentials for {email!r}."
            raise LoginError(msg)
        self._logged_in = True

    def episode_info(self, url: str) -> Episode:
        """Read the page list and the titles off a viewer page.

        Args:
            url: The viewer URL.

        Returns:
            The parsed episode. `pages` is empty when it is not readable.

        Raises:
            NotAPiccomaPageError: The page carries no viewer.
        """
        res = self._session.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        if urlparse(res.url).path.startswith(_SIGNIN_PREFIX):
            return self._locked_episode(url)
        html = res.text

        block = _PDATA.search(html)
        if block is None:
            msg = f"no '_pdata_' on {url}; is it a Piccoma viewer page?"
            raise NotAPiccomaPageError(msg)
        body = block.group(1)
        fields = {match["key"]: _literal(match["value"]) for match in _PDATA_FIELD.finditer(body)}

        product_id = str(fields.get("product_id") or "")
        episode_id = str(fields.get("episode_id") or "")
        episode_type: EpisodeType = "V" if fields.get("eType") == "V" else "E"
        episode_title = str(fields.get("title") or "").strip() or episode_id

        return Episode(
            url=url,
            product_id=product_id,
            episode_id=episode_id,
            series_title=_title_part(BeautifulSoup(html, "html.parser"), 1) or product_id,
            episode_title=episode_title,
            episode_type=episode_type,
            scrambled=bool(fields.get("isScrambled")),
            pages=tuple(_pages(body)),
            next_url=self._next_url(product_id, episode_id, episode_type),
        )

    def entries(self, product_id: str, episode_type: EpisodeType = "E") -> list[Entry]:
        """List a series' episodes or volumes, in reading order.

        The result is cached, so walking a whole series costs one request for
        the list however many episodes are downloaded off it.

        Args:
            product_id: The series' id.
            episode_type: `"E"` for the episode list, `"V"` for the volume one.

        Returns:
            The rows of the list. Empty when the product has none of that kind.
        """
        cached = self._lists.get((product_id, episode_type))
        if cached is not None:
            return cached

        url = f"{BASE_URL}/web/product/{product_id}/episodes?etype={episode_type}"
        res = self._session.get(url, headers=HEADERS, timeout=30)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")
        # The product page names the series; the viewer page of a locked
        # episode does not, so it is worth keeping hold of.
        self._series_titles.setdefault(product_id, _title_part(soup, 0))
        listing = soup.find(id="js_volumeList" if episode_type == "V" else "js_episodeList")

        entries: list[Entry] = []
        if isinstance(listing, Tag):
            entries = [
                entry
                for item in listing.find_all("li", recursive=False)
                if isinstance(item, Tag) and (entry := _entry(item, product_id, episode_type)) is not None
            ]
        self._lists[product_id, episode_type] = entries
        return entries

    def series_title(self, product_id: str) -> str:
        """Read a series' title off its product page.

        Args:
            product_id: The series' id.

        Returns:
            The title, or the id when the page does not name one.
        """
        if product_id not in self._series_titles:
            self.entries(product_id)
        return self._series_titles.get(product_id) or product_id

    def _locked_episode(self, url: str) -> Episode:
        """Describe an episode Piccoma would not open, from its series' list."""
        product_id, episode_id = self.parse_uri(url)
        episode_id = episode_id or ""
        entry = None
        for episode_type in ("E", "V"):
            entry = next((row for row in self.entries(product_id, episode_type) if row.id == episode_id), None)
            if entry is not None:
                break
        return Episode(
            url=url,
            product_id=product_id,
            episode_id=episode_id,
            series_title=self.series_title(product_id),
            episode_title=entry.title if entry else episode_id,
            episode_type="E",
            scrambled=False,
            next_url=self._next_url(product_id, episode_id, "E"),
        )

    def _next_url(self, product_id: str, episode_id: str, episode_type: EpisodeType) -> str | None:
        """Find the episode that follows `episode_id` in its series' list."""
        if not product_id or not episode_id:
            return None
        entries = self.entries(product_id, episode_type)
        ids = [entry.id for entry in entries]
        try:
            position = ids.index(episode_id)
        except ValueError:
            return None
        return entries[position + 1].url if position + 1 < len(entries) else None

    def _login_status(self) -> bool:
        """Ask any page whether the session is signed in."""
        res = self._session.get(LOGIN_URL, headers=HEADERS, timeout=30)
        res.raise_for_status()
        flag = _LOGIN_FLAG.search(res.text)
        return flag is not None and flag["value"] == "true"

    def _save_pages(
        self,
        episode: Episode,
        save_dir: Path,
        *,
        only_first: bool = False,
        print_log: bool = False,
    ) -> None:
        wanted = episode.pages[:1] if only_first else episode.pages
        width = len(str(len(wanted)))
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("("),
            MofNCompleteColumn(),
            TextColumn("pages )"),
            TextColumn("remain:"),
            TimeRemainingColumn(),
            TextColumn("spent:"),
            TimeElapsedColumn(),
            disable=not print_log,
        )
        with progress:
            task = progress.add_task("[red]Downloading...", total=len(wanted))
            for index, page in enumerate(wanted):
                image = self._image(page, scrambled=episode.scrambled)
                image.save(save_dir / f"{index:0{width}d}.jpg", quality=95)
                progress.update(task, advance=1)

    def _image(self, page: Page, *, scrambled: bool) -> Image.Image:
        res = self._session.get(page["url"], headers=HEADERS, timeout=60)
        res.raise_for_status()
        image = Image.open(BytesIO(res.content))
        seed = parse_seed(page["url"]) if scrambled else None
        return descramble(image, seed) if seed else image


def _title_part(soup: BeautifulSoup, index: int) -> str:
    """Read one `｜`-separated part of a page's title.

    A viewer page is titled `"<episode>｜<series>｜ピッコマ"` and a product page
    `"<series>｜<blurb>｜<authors>"`, so which part names the series depends on
    which page it came off.

    Args:
        soup: The parsed page.
        index: Which part to take.

    Returns:
        The part, or `""` when the title has no such part.
    """
    og = soup.find("meta", property="og:title")
    heading = str(og.attrs.get("content", "")) if isinstance(og, Tag) else ""
    if not heading and soup.title:
        heading = soup.title.get_text()
    parts = [part.strip() for part in heading.split("｜") if part.strip()]
    return parts[index] if len(parts) > index + 1 else ""


def _literal(value: str) -> str | int | bool:
    """Read one `_pdata_` value: a single-quoted string, a number or a bool."""
    if value == "true":
        return True
    if value == "false":
        return False
    if value.startswith("'"):
        return value[1:-1]
    return int(value)


def _pages(body: str) -> Iterator[Page]:
    """Read the `img` array of a `_pdata_` block, in reading order."""
    for match in _PDATA_IMAGE.finditer(body):
        path = match["path"]
        yield Page(
            url=f"https:{path}" if path.startswith("//") else path,
            width=int(match["width"] or 0),
            height=int(match["height"] or 0),
        )


def _entry(item: Tag, product_id: str, episode_type: EpisodeType) -> Entry | None:
    """Turn one `<li>` of an episode or volume list into an `Entry`."""
    link = item.find("a", attrs={"data-episode_id": True})
    if not isinstance(link, Tag):
        return None
    episode_id = str(link.attrs["data-episode_id"])

    heading = item.find("h2")
    title = heading.get_text(strip=True) if isinstance(heading, Tag) else episode_id
    classes = set(item.get("class") or [])
    for tag in item.select("[class]"):
        classes.update(tag.get("class") or [])

    if episode_type == "V":
        return Entry(
            id=episode_id,
            title=title or episode_id,
            url=Piccoma.viewer_url(product_id, episode_id),
            is_free="PCM-prdVol_freeBtn" in classes,
            is_read_for_free="PCM-prdVol_readBtn" in classes and "PCM-prdVol_campaign_free" in classes,
            is_wait_until_free="PCM-prdVol_campaign_free" in classes,
            is_already_read="PCM-volList_read" in classes,
            is_purchased="PCM-prdVol_readBtn" in classes,
        )
    return Entry(
        id=episode_id,
        title=title or episode_id,
        url=Piccoma.viewer_url(product_id, episode_id),
        is_free="PCM-epList_status_free" in classes,
        is_zero_plus="PCM-epList_status_zeroPlus" in classes,
        is_read_for_free="PCM-epList_status_waitfreeRead" in classes,
        # The list has spelled the wait-ticket badge both ways.
        is_wait_until_free=bool(classes & {"PCM-epList_status_waitfree", "PCM-epList_status_webwaitfree"}),
        is_already_read="PCM-epList_read" in classes,
        is_purchased="PCM-epList_status_buy" in classes,
    )
