# getpiccoma

[![PyPI](
  <https://img.shields.io/pypi/v/getpiccoma?color=blue>
  )](
  <https://pypi.org/project/getpiccoma/>
) [![CI](
  <https://github.com/eggplants/getpiccoma/actions/workflows/ci.yml/badge.svg>
  )](
  <https://github.com/eggplants/getpiccoma/actions/workflows/ci.yml>
)

[![ghcr size](
  <https://ghcr-badge.egpl.dev/eggplants/getpiccoma/size>
)](
  <https://github.com/eggplants/getpiccoma/pkgs/container/getpiccoma>
)

Retrieve and save images from manga distribution sites using Piccoma.

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

docker run --rm ghcr.io/eggplants/getpiccoma eggplant
```

## CLI

```shellsession
$ getpiccoma
Hello, world!

$ getpiccoma eggplant
Hello, eggplant!
```

## Library

```python
import getpiccoma

print(getpiccoma.__version__)
```

## License

[MIT License](
  <https://github.com/eggplants/getpiccoma/blob/master/LICENSE.txt>
)
