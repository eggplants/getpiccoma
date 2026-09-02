# getpiccoma

[![PyPI](
  <https://img.shields.io/pypi/v/getpiccoma?color=blue>
  )](
  <https://pypi.org/project/getpiccoma/>
) [![CI](
  <https://github.com/eggplants/getpiccoma/actions/workflows/ci.yml/badge.svg>
  )](
  <https://github.com/eggplants/getpiccoma/actions/workflows/ci.yml>
) [![ghcr size](
  <https://ghcr-badge.egpl.dev/eggplants/getpiccoma/size>
)](
  <https://github.com/eggplants/getpiccoma/pkgs/container/getpiccoma>
)

Retrieve and save images from manga distribution sites using
[Piccoma](https://piccoma.com).

_Note: Redistribution of downloaded image data is prohibited. Please keep it to private use._

## Valid URL Formats

- `piccoma.com/web/viewer/<product-id>/<episode-id>` -- one episode
  - e.g. <https://piccoma.com/web/viewer/8195/1185884>
- `piccoma.com/web/product/<product-id>/episodes` -- the whole series, from its
  first entry
  - e.g. <https://piccoma.com/web/product/8195/episodes>

## Installation

```bash
# mise via github release
mise use -g github:eggplants/getpiccoma

# mise via pipx
mise use -g pipx:getpiccoma

# pipx
pipx install getpiccoma

# pip
pip install getpiccoma
```

### Docker

```bash
docker pull ghcr.io/eggplants/getpiccoma

docker run --rm -v "$PWD:/work" -w /work \
  ghcr.io/eggplants/getpiccoma https://piccoma.com/web/viewer/8195/1185884
```

## CLI

```shellsession
$ pget https://piccoma.com/web/viewer/8195/1185884
get: https://piccoma.com/web/viewer/8195/1185884
  Downloading... ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% ( 14/14 pages ) remain: 0:00:00 spent: 0:00:03
saved: ひげを剃る。そして女子高生を拾う。(しめさば ぶーた 足立いまる)/第1話 失恋と女子高生 (1)
done.
```

`-b` follows every next episode, and stops on the first one the account may not
read. A product URL implies it:

```bash
pget https://piccoma.com/web/product/8195/episodes -d out
```

Log in with `-u` to reach episodes the account has bought or unlocked. The
password is prompted for when `-p` is left out:

```bash
pget https://piccoma.com/web/viewer/8195/1185884 -u you@example.com
```

| Option | Description |
| --- | --- |
| `-b`, `--bulk` | follow every next episode |
| `-d DIR`, `--savedir DIR` | directory to save into |
| `-f`, `--first` | download only the first page |
| `-o`, `--overwrite` | download again if it exists |
| `-m`, `--metadata` | save episode metadata as json |
| `-u MAIL`, `--username MAIL` | email address to log in with |
| `-p PW`, `--password PW` | password |
| `-t`, `--volumes` | read a product url's volume list, not episodes |
| `-q`, `--quiet` | disable console output |

## Library

```python
from getpiccoma import Piccoma

piccoma = Piccoma()
next_url, save_dir, saved = piccoma.get(
    "https://piccoma.com/web/viewer/8195/1185884",
    save_path="out",
)
```

`episode_info` reads a viewer page without downloading anything, and `entries`
lists a series:

```python
episode = piccoma.episode_info("https://piccoma.com/web/viewer/8195/1185884")
print(episode.series_title, episode.episode_title, len(episode.pages))

for entry in piccoma.entries(episode.product_id):
    print(entry.title, entry.is_readable, entry.url)
```

`descramble` and `parse_seed` are exposed on their own, for pages fetched some
other way:

```python
from PIL import Image
from getpiccoma import descramble, parse_seed

seed = parse_seed(image_url)
page = descramble(Image.open(path), seed) if seed else Image.open(path)
```

## Acknowledgements

- [catsital/pyccoma](https://github.com/catsital/pyccoma)
- [catsital/pycasso](https://github.com/catsital/pycasso)

## License

[MIT License](
  <https://github.com/eggplants/getpiccoma/blob/master/LICENSE.txt>
)
