"""Command line entry point for getpiccoma."""

from __future__ import annotations

import getpass
import shutil
import sys
import warnings
from argparse import (
    ArgumentDefaultsHelpFormatter,
    ArgumentParser,
    ArgumentTypeError,
    Namespace,
    RawDescriptionHelpFormatter,
)

from . import __version__
from .piccoma import NeedPurchase, Piccoma, PiccomaError

_URL_FORMS = (
    "https://piccoma.com/web/viewer/<product-id>/<episode-id>",
    "https://piccoma.com/web/product/<product-id>/episodes",
)


class HelpFormatter(ArgumentDefaultsHelpFormatter, RawDescriptionHelpFormatter):
    """Show argument defaults while keeping the description's own line breaks."""


def available_list() -> str:
    """Render the accepted URL forms for the help epilog."""
    return "accepted urls:\n  - " + "\n  - ".join(_URL_FORMS)


def check_url(url: str) -> str:
    """Reject anything that is not a Piccoma viewer or product URL.

    Args:
        url: The URL given on the command line.

    Returns:
        The URL unchanged.

    Raises:
        ArgumentTypeError: The URL is not one getpiccoma can read.
    """
    if not Piccoma.is_valid_uri(url):
        msg = f"'{url}' is not a Piccoma url.\n{available_list()}"
        raise ArgumentTypeError(msg)
    return url


def parse_args(args: list[str] | None = None) -> Namespace:
    """Parse the command line.

    Args:
        args: Arguments to parse instead of `sys.argv[1:]`. Used by the tests.

    Returns:
        The parsed arguments.
    """
    parser = ArgumentParser(
        prog="getpiccoma",
        description="Retrieve and save images from manga distribution sites using Piccoma.",
        epilog=available_list(),
        formatter_class=lambda prog: HelpFormatter(
            prog,
            width=shutil.get_terminal_size(fallback=(120, 50)).columns,
            max_help_position=40,
        ),
    )
    parser.add_argument("url", type=check_url, help="viewer url, or a product url to start from its first episode")
    parser.add_argument("-b", "--bulk", action="store_true", help="follow every next episode")
    parser.add_argument("-d", "--savedir", metavar="DIR", default=".", help="directory to save into")
    parser.add_argument("-f", "--first", action="store_true", help="download only the first page")
    parser.add_argument("-o", "--overwrite", action="store_true", help="download again if it exists")
    parser.add_argument("-m", "--metadata", action="store_true", help="save episode metadata as json")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-u", "--username", metavar="MAIL", help="email address to log in with")
    parser.add_argument("-p", "--password", metavar="PW", help="password (prompted for if -u is given without it)")
    parser.add_argument("-t", "--volumes", action="store_true", help="read a product url's volume list, not episodes")
    parser.add_argument("-q", "--quiet", action="store_true", help="disable console output")
    return parser.parse_args(args)


def _start_url(piccoma: Piccoma, parsed: Namespace) -> str | None:
    """Resolve a product URL to the first episode to download from it.

    Args:
        piccoma: The client to list the product with.
        parsed: The parsed command line.

    Returns:
        The viewer URL to start at, or None when the product lists nothing.
    """
    product_id, episode_id = Piccoma.parse_uri(parsed.url)
    if episode_id is not None:
        return parsed.url

    entries = piccoma.entries(product_id, "V" if parsed.volumes else "E")
    if not entries:
        return None
    if not parsed.quiet:
        print(f"list: {len(entries)} entries")
    return entries[0].url


def main(args: list[str] | None = None) -> None:  # noqa: C901, PLR0912
    """Run the command."""
    parsed = parse_args(args)
    piccoma = Piccoma()

    if parsed.username:
        password = parsed.password or getpass.getpass("password: ")
        try:
            piccoma.login(parsed.username, password)
        except PiccomaError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        if not parsed.quiet:
            print("logged in as:", parsed.username)
    elif parsed.password:
        print("warning: -p without -u does nothing.", file=sys.stderr)

    try:
        next_url = _start_url(piccoma, parsed)
        if next_url is None:
            print("error: that product lists no episodes.", file=sys.stderr)
            raise SystemExit(1)
        if next_url != parsed.url:
            # A product url means the whole series, however `-b` was given.
            parsed.bulk = True

        while next_url:
            if not parsed.quiet:
                print("get:", next_url)
            with warnings.catch_warnings():
                warnings.simplefilter("error", NeedPurchase)
                try:
                    next_url, save_dir, saved = piccoma.get(
                        next_url,
                        save_path=parsed.savedir,
                        overwrite=parsed.overwrite,
                        only_first=parsed.first,
                        save_metadata=parsed.metadata,
                        print_log=not parsed.quiet,
                    )
                except NeedPurchase as exc:
                    print(f"stop: '{exc.args[0]}' needs a purchase, a wait or a login.", file=sys.stderr)
                    break
            if not parsed.quiet:
                print("saved:" if saved else "skipped (already there):", save_dir)
            if not parsed.bulk:
                break
    except PiccomaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not parsed.quiet:
        print("done.")


if __name__ == "__main__":
    main()
