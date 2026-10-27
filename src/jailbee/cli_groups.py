"""Small reusable CLI groups with migration hints."""

from typer._click import Command, Context
from typer._click.exceptions import NoSuchOption, UsageError
from typer.core import TyperGroup


def hinting_group(push_path: str) -> type[TyperGroup]:
    """Return a group that explains the former positional/options syntax."""

    class HintingGroup(TyperGroup):
        def resolve_command(
            self, ctx: Context, args: list[str]
        ) -> tuple[str | None, Command | None, list[str]]:
            if args and self.get_command(ctx, args[0]) is None:
                word = args[0]
                raise UsageError(
                    f"Unknown subcommand '{word}'. Did you mean: {push_path} {word}", ctx
                )
            return super().resolve_command(ctx, args)

        def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
            try:
                return super().parse_args(ctx, args)
            except NoSuchOption as exc:
                group_path = push_path.removesuffix(" push")
                raise UsageError(
                    f"options moved to `{push_path}`; `--open` → `{group_path} open`", ctx
                ) from exc

    return HintingGroup
