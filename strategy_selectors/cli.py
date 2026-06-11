"""Shared CLI helpers for strategy selector scripts."""

from __future__ import annotations

import argparse

_AUTO_HELP = "\u200b"


class SelectorHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show default values in ``-h`` output even when an option has no help text."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_str = action.help
        if help_str == _AUTO_HELP:
            help_str = ""
        elif help_str is None:
            help_str = ""
        if "%(default)" not in help_str and action.default is not argparse.SUPPRESS:
            defaulting_nargs = [argparse.OPTIONAL, argparse.ZERO_OR_MORE]
            if action.option_strings or action.nargs in defaulting_nargs:
                if help_str:
                    help_str += " "
                help_str += "(default: %(default)s)"
        return help_str

    def _format_action(self, action: argparse.Action) -> str:
        original_help = action.help
        if original_help is None or (isinstance(original_help, str) and not original_help.strip()):
            action.help = _AUTO_HELP
        try:
            return super()._format_action(action)
        finally:
            action.help = original_help


def selector_argument_parser(**kwargs) -> argparse.ArgumentParser:
    """Build an ArgumentParser that includes default values in ``-h`` output."""
    return argparse.ArgumentParser(
        formatter_class=SelectorHelpFormatter,
        **kwargs,
    )


def help_requested(argv: list[str] | None = None) -> bool:
    """Return True when argv asks for usage help."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    return "-h" in args or "--help" in args


def print_help_and_exit(build_parser) -> None:
    """Parse argv and print help. argparse exits the process on success."""
    build_parser().parse_args()
