from __future__ import annotations

import json
import re
from io import BytesIO

import pytest
from PIL import Image
from requests import Session

from getpiccoma import __version__
from getpiccoma.piccoma import (
    BASE_URL,
    TILE_SIZE,
    VALID_HOSTS,
    Entry,
    LoginError,
    NeedPurchase,
    NotAPiccomaPageError,
    Piccoma,
    PiccomaError,
    descramble,
    parse_seed,
    seedrandom,
    shuffle_order,
)

# The checksum and the `expires` stamp of a real page image, and the seed the
# viewer derives from them.
CHECKSUM = "G0UQD7CENPH26H9YIEIYEU"
EXPIRES = "1788372000"
SEED = "OQI26H8XIDIYETF0TQD7BD"
IMAGE_URL = f"//pcm.kakaocdn.net/dna/ta9nw/btqGHxiZm9S/{CHECKSUM}/i00001.jpg?credential=abc&expires={EXPIRES}"

# `shuffle_order(12, SEED)`, as the reference implementation of the viewer's
# PRNG produces it.
ORDER_12 = [1, 10, 11, 0, 3, 6, 8, 7, 2, 9, 4, 5]

VIEWER_HTML = f"""
<html><head>
<title>第1話 その一｜ひげ(しめさば)｜ピッコマ</title>
<meta property="og:title" content="第1話 その一｜ひげ(しめさば)｜ピッコマ">
</head><body>
<script>
    var _init_ = {{
        'login': false,
        'os': 'PC'
    }}
</script>
<script>
    var _pdata_ = {{
        'product_id': 8195,
        'episode_id': 1185884,
        'is_bookmark': 0,
        'title': '第1話 その一',
        'isScrambled': true,
        'eType': 'E',
        'img': [
        {{'path':'{IMAGE_URL}','width':1441, 'height':2048}},
        {{'path':'{IMAGE_URL.replace("i00001", "i00002")}','width':1441, 'height':2048}}
        ],
        'for_viewer_end': {{"ticket_type": "FREE"}},
        reduction_balloon_text: null,
    }}
</script>
</body></html>
"""

# The last episode of the list, and a locked one with an empty `img` array.
LAST_HTML = VIEWER_HTML.replace("1185884", "1185887")


LOCKED_HTML = re.sub(r"'img': \[.*?\]", "'img': [\n        ]", VIEWER_HTML, flags=re.DOTALL)

EPISODE_LIST_HTML = """
<html><head>
<meta property="og:title" content="ひげ｜無料漫画（まんが）ならピッコマ｜しめさば">
</head><body>
<ul id="js_episodeList">
  <li class="PCM-epList_read">
    <a href="#" data-product_id="8195" data-episode_id="1185884">
      <div class="PCM-epList_title"><h2>第1話 その一</h2></div>
      <div class="PCM-epList_status"><p class="PCM-epList_status_free"><span>0</span></p></div>
    </a>
  </li>
  <li>
    <a href="#" data-product_id="8195" data-episode_id="1185887">
      <div class="PCM-epList_title"><h2>第2話 その二</h2></div>
      <div class="PCM-epList_status">
        <div class="PCM-epList_status_waitfree"></div>
        <div class="PCM-epList_status_zeroPlus"></div>
      </div>
    </a>
  </li>
</ul>
</body></html>
"""

VOLUME_LIST_HTML = """
<html><body>
<ul id="js_volumeList">
  <li class="PCM-volList_read">
    <div class="PCM-prdVol_title"><h2>1巻</h2></div>
    <div class="PCM-prdVol_btns"><a href="#" class="PCM-prdVol_readBtn" data-episode_id="900"></a></div>
  </li>
</ul>
</body></html>
"""

SIGNIN_HTML = """
<html><body>
<script>var _init_ = { 'login': false }</script>
<form><input type="hidden" name="csrfmiddlewaretoken" value="token-1"></form>
</body></html>
"""

SIGNED_IN_HTML = SIGNIN_HTML.replace("'login': false", "'login': true")


class FakeResponse:
    def __init__(self, text="", content=None, url=None):
        self.text = text
        self.content = content if content is not None else text.encode()
        # None means "wherever it was asked for"; a value stands for a redirect.
        self.url = url

    def raise_for_status(self):
        return None


class FakeSession(Session):
    """Answers by substring match on the requested URL.

    A route may hold a list of responses, handed out in order and then repeated.
    """

    def __init__(self, routes):
        super().__init__()
        self.routes = routes
        self.calls = []
        self.posts = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self._route(url)

    def post(self, url, data=None, json=None, **kwargs):
        self.posts.append((url, data))
        return self._route(url)

    def _route(self, url):
        for needle, response in self.routes.items():
            if needle in url:
                if isinstance(response, list):
                    response = response.pop(0) if len(response) > 1 else response[0]
                if response.url is None:
                    response.url = url
                return response
        raise AssertionError(url)


def tile_image(values, columns=4, rows=3, tile=TILE_SIZE):
    """A grid of flat tiles, tile n painted with a colour derived from values[n]."""
    image = Image.new("RGB", (columns * tile, rows * tile))
    for index, value in enumerate(values):
        row, column = divmod(index, columns)
        patch = Image.new("RGB", (tile, tile), (value * 7 % 256, value * 13 % 256, value * 29 % 256))
        image.paste(patch, (column * tile, row * tile))
    return image


def viewer_client(routes=None):
    # Routes are matched in order, so the caller's own come first, and the
    # defaults fill in behind them from the most specific to the least.
    merged = dict(routes or {})
    for needle, response in (
        ("/web/viewer/8195/1185887", FakeResponse(LAST_HTML)),
        ("/web/viewer/", FakeResponse(VIEWER_HTML)),
        ("/web/product/8195/episodes?etype=E", FakeResponse(EPISODE_LIST_HTML)),
        ("/web/product/8195/episodes?etype=V", FakeResponse(VOLUME_LIST_HTML)),
    ):
        merged.setdefault(needle, response)
    session = FakeSession(merged)
    return Piccoma(session), session


# --- the PRNG and the shuffle -----------------------------------------------


def test_shuffle_order_matches_the_viewer():
    assert shuffle_order(12, SEED) == ORDER_12


def test_shuffle_order_is_a_permutation():
    for size in (1, 2, 41, 300):
        assert sorted(shuffle_order(size, SEED)) == list(range(size))


def test_shuffle_order_is_deterministic():
    assert shuffle_order(41, SEED) == shuffle_order(41, SEED)


def test_shuffle_order_depends_on_the_seed():
    assert shuffle_order(41, SEED) != shuffle_order(41, CHECKSUM)


def test_seedrandom_stays_in_the_unit_interval():
    prng = seedrandom(SEED)
    assert all(0.0 <= prng() < 1.0 for _ in range(500))


def test_seedrandom_rejects_an_empty_seed():
    with pytest.raises(PiccomaError, match="empty"):
        seedrandom("")


# --- the seed -----------------------------------------------------------------


def test_parse_seed_reads_the_seed_off_an_image_url():
    assert parse_seed(f"https:{IMAGE_URL}") == SEED


def test_parse_seed_ignores_unscrambled_pages():
    assert parse_seed(f"https:{IMAGE_URL.replace(CHECKSUM, CHECKSUM.lower())}") is None


def test_parse_seed_needs_an_expires_stamp():
    with pytest.raises(PiccomaError, match="expires"):
        parse_seed(f"https://pcm.kakaocdn.net/dna/{CHECKSUM}/i00001.jpg")


def test_parse_seed_needs_a_checksum_segment():
    with pytest.raises(PiccomaError, match="checksum"):
        parse_seed("https://pcm.kakaocdn.net/i00001.jpg?expires=1")


# --- descrambling -------------------------------------------------------------


def test_descramble_puts_the_tiles_back():
    order = shuffle_order(12, SEED)
    inverse = [0] * 12
    for destination, source in enumerate(order):
        inverse[source] = destination
    original = tile_image(range(12))
    scrambled = tile_image(inverse)
    assert descramble(scrambled, SEED).tobytes() == original.tobytes()


def test_descramble_leaves_the_image_size_alone():
    image = tile_image(range(12))
    assert descramble(image, SEED).size == image.size


def test_descramble_handles_edges_that_do_not_fill_a_tile():
    image = tile_image(range(12)).crop((0, 0, 137, 89))
    out = descramble(image, SEED)
    assert out.size == (137, 89)
    # Every group is permuted among itself, so no pixel value is invented.
    assert out.histogram() == image.histogram()


# --- urls ---------------------------------------------------------------------


def test_valid_hosts_is_piccoma():
    assert VALID_HOSTS == ("piccoma.com",)


@pytest.mark.parametrize(
    "url",
    [
        "https://piccoma.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/viewer/8195/1185884/",
        "https://piccoma.com/web/product/8195/episodes",
        "https://piccoma.com/web/product/8195",
    ],
)
def test_is_valid_uri_accepts_piccoma_urls(url):
    assert Piccoma.is_valid_uri(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://piccoma.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/bookshelf/history",
        "https://example.com/web/viewer/8195/1185884",
        "https://piccoma.com/web/viewer/abc/def",
        "not a url",
    ],
)
def test_is_valid_uri_rejects_everything_else(url):
    assert not Piccoma.is_valid_uri(url)


def test_parse_uri_splits_a_viewer_url():
    assert Piccoma.parse_uri("https://piccoma.com/web/viewer/8195/1185884") == ("8195", "1185884")


def test_parse_uri_splits_a_product_url():
    assert Piccoma.parse_uri("https://piccoma.com/web/product/8195/episodes?etype=E") == ("8195", None)


def test_parse_uri_rejects_anything_else():
    with pytest.raises(PiccomaError, match="not a Piccoma"):
        Piccoma.parse_uri("https://example.com/")


def test_viewer_url_builds_a_viewer_url():
    assert Piccoma.viewer_url(8195, 1185884) == f"{BASE_URL}/web/viewer/8195/1185884"


# --- reading a viewer page ----------------------------------------------------


def test_episode_info_reads_the_titles_and_the_pages():
    piccoma, _ = viewer_client()
    episode = piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/1185884")

    assert episode.product_id == "8195"
    assert episode.episode_id == "1185884"
    assert episode.series_title == "ひげ(しめさば)"
    assert episode.episode_title == "第1話 その一"
    assert episode.episode_type == "E"
    assert episode.scrambled
    assert [page["url"] for page in episode.pages] == [
        f"https:{IMAGE_URL}",
        f"https:{IMAGE_URL}".replace("i00001", "i00002"),
    ]
    assert episode.pages[0]["width"] == 1441


def test_episode_info_points_at_the_next_episode():
    piccoma, _ = viewer_client()
    episode = piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/1185884")
    assert episode.next_url == f"{BASE_URL}/web/viewer/8195/1185887"


def test_episode_info_stops_at_the_last_episode():
    piccoma, _ = viewer_client()
    episode = piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/1185887")
    assert episode.next_url is None


def test_episode_info_rejects_a_page_without_a_viewer():
    piccoma = Piccoma(FakeSession({"/web/viewer/": FakeResponse("<html></html>")}))
    with pytest.raises(NotAPiccomaPageError, match="_pdata_"):
        piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/1185884")


# --- listing a product --------------------------------------------------------


def test_entries_reads_the_episode_list():
    piccoma, _ = viewer_client()
    entries = piccoma.entries("8195")

    assert [entry.id for entry in entries] == ["1185884", "1185887"]
    assert [entry.title for entry in entries] == ["第1話 その一", "第2話 その二"]
    assert entries[0].url == f"{BASE_URL}/web/viewer/8195/1185884"
    assert entries[0].is_free
    assert entries[0].is_already_read
    assert entries[1].is_wait_until_free
    assert entries[1].is_zero_plus
    assert not entries[1].is_free


def test_entries_reads_the_volume_list():
    piccoma, _ = viewer_client()
    entries = piccoma.entries("8195", "V")

    assert entries == [
        Entry(
            id="900",
            title="1巻",
            url=f"{BASE_URL}/web/viewer/8195/900",
            is_already_read=True,
            is_purchased=True,
        ),
    ]


def test_entries_is_cached():
    piccoma, session = viewer_client()
    piccoma.entries("8195")
    piccoma.entries("8195")
    assert sum("/web/product/" in url for url in session.calls) == 1


def test_entries_survives_a_product_without_a_list():
    piccoma = Piccoma(FakeSession({"/web/product/": FakeResponse("<html></html>")}))
    assert piccoma.entries("8195") == []


def test_is_readable_flags_what_costs_nothing():
    assert Entry(id="1", title="t", url="u", is_free=True).is_readable
    assert Entry(id="1", title="t", url="u", is_purchased=True).is_readable
    assert not Entry(id="1", title="t", url="u", is_wait_until_free=True).is_readable
    # A campaign badge is not an entitlement.
    assert not Entry(id="1", title="t", url="u", is_zero_plus=True).is_readable


def test_series_title_comes_off_the_product_page():
    piccoma, _ = viewer_client()
    assert piccoma.series_title("8195") == "ひげ"


def test_series_title_falls_back_to_the_product_id():
    piccoma = Piccoma(FakeSession({"/web/product/": FakeResponse("<html></html>")}))
    assert piccoma.series_title("8195") == "8195"


def test_episode_info_reports_an_episode_it_was_bounced_off():
    signin = f"{BASE_URL}/web/acc/signin?next_url=/web/viewer/s/8195/1185887"
    piccoma, _ = viewer_client({"/web/viewer/8195/1185887": FakeResponse(SIGNIN_HTML, url=signin)})

    episode = piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/1185887")

    assert episode.pages == ()
    assert episode.episode_id == "1185887"
    # The titles come off the series' list, since the sign-in page names neither.
    assert episode.series_title == "ひげ"
    assert episode.episode_title == "第2話 その二"


def test_episode_info_names_an_unlisted_locked_episode_by_its_id():
    signin = f"{BASE_URL}/web/acc/signin"
    piccoma, _ = viewer_client({"/web/viewer/8195/999": FakeResponse(SIGNIN_HTML, url=signin)})

    episode = piccoma.episode_info(f"{BASE_URL}/web/viewer/8195/999")

    assert episode.episode_title == "999"


# --- downloading --------------------------------------------------------------


def image_routes():
    raw = BytesIO()
    Image.new("RGB", (4 * TILE_SIZE, 3 * TILE_SIZE)).save(raw, "JPEG")
    return {"kakaocdn.net": FakeResponse(content=raw.getvalue())}


def test_get_saves_every_page(tmp_path):
    piccoma, _ = viewer_client(image_routes())
    next_url, save_dir, saved = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path)

    assert saved
    assert next_url == f"{BASE_URL}/web/viewer/8195/1185887"
    assert save_dir == tmp_path / "ひげ(しめさば)" / "第1話 その一"
    assert sorted(path.name for path in save_dir.iterdir()) == ["0.jpg", "1.jpg"]


def test_get_stops_after_the_first_page_when_asked(tmp_path):
    piccoma, _ = viewer_client(image_routes())
    _, save_dir, _ = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path, only_first=True)
    assert [path.name for path in save_dir.iterdir()] == ["0.jpg"]


def test_get_skips_a_directory_that_is_already_there(tmp_path):
    piccoma, session = viewer_client(image_routes())
    (tmp_path / "ひげ(しめさば)" / "第1話 その一").mkdir(parents=True)

    _, _, saved = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path)

    assert not saved
    assert not any("kakaocdn" in url for url in session.calls)


def test_get_downloads_again_when_told_to(tmp_path):
    piccoma, _ = viewer_client(image_routes())
    (tmp_path / "ひげ(しめさば)" / "第1話 その一").mkdir(parents=True)

    _, _, saved = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path, overwrite=True)

    assert saved


def test_get_writes_metadata_when_asked(tmp_path):
    piccoma, _ = viewer_client(image_routes())
    _, save_dir, _ = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path, save_metadata=True)

    metadata = json.loads((save_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["episode_id"] == "1185884"
    assert metadata["series_title"] == "ひげ(しめさば)"
    assert len(metadata["pages"]) == 2


def test_get_warns_about_an_episode_with_no_pages(tmp_path):
    piccoma, _ = viewer_client({"/web/viewer/": FakeResponse(LOCKED_HTML)})

    with pytest.warns(NeedPurchase, match="第1話 その一"):
        _, _, saved = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185884", save_path=tmp_path)

    assert not saved


def test_get_warns_about_an_episode_it_was_bounced_off(tmp_path):
    signin = f"{BASE_URL}/web/acc/signin"
    piccoma, _ = viewer_client({"/web/viewer/8195/1185887": FakeResponse(SIGNIN_HTML, url=signin)})

    with pytest.warns(NeedPurchase, match="第2話 その二"):
        _, _, saved = piccoma.get(f"{BASE_URL}/web/viewer/8195/1185887", save_path=tmp_path)

    assert not saved


# --- logging in ---------------------------------------------------------------


def test_login_posts_the_csrf_token_back():
    session = FakeSession({"/web/acc/email/signin": [FakeResponse(SIGNIN_HTML), FakeResponse(SIGNED_IN_HTML)]})
    piccoma = Piccoma(session)

    piccoma.login("someone@example.com", "hunter2")

    assert piccoma.logged_in
    url, data = session.posts[0]
    assert url.endswith("/web/acc/email/signin")
    assert data["csrfmiddlewaretoken"] == "token-1"
    assert data["email"] == "someone@example.com"
    assert data["password"] == "hunter2"


def test_login_raises_when_piccoma_says_no():
    piccoma = Piccoma(FakeSession({"/web/acc/email/signin": FakeResponse(SIGNIN_HTML)}))

    with pytest.raises(LoginError, match="refused"):
        piccoma.login("someone@example.com", "wrong")

    assert not piccoma.logged_in


def test_login_raises_without_a_form():
    piccoma = Piccoma(FakeSession({"/web/acc/email/signin": FakeResponse("<html></html>")}))
    with pytest.raises(LoginError, match="no sign-in form"):
        piccoma.login("someone@example.com", "hunter2")


# --- packaging ----------------------------------------------------------------


def test_version_is_available():
    assert __version__


def test_a_default_client_brings_its_own_session():
    assert isinstance(Piccoma()._session, Session)  # noqa: SLF001
