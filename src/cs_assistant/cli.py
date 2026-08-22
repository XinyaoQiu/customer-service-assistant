"""Command line: setup commands and an interactive conversation."""

import sys
from pathlib import Path

import typer

from .config import Settings

app = typer.Typer(help="Publisher support assistant", no_args_is_help=True)


@app.command()
def init_db(seed: bool = typer.Option(True, help="Load sample publishers and articles")):
    """Create the business schema, optionally with sample data."""
    from .sources.business_db import BusinessDB

    db = BusinessDB(Settings.from_env().mysql_dsn)
    db.init_schema(seed=seed)
    typer.secho("schema ready" + (" with seed data" if seed else ""), fg=typer.colors.GREEN)


@app.command()
def index(
    policy_dir: Path = typer.Option(None, help="Directory of policy markdown"),
    variants: bool = typer.Option(True, help="Generate colloquial phrasings (one model call per chunk)"),
):
    """Rebuild the policy index from the markdown source."""
    from .sources.policy import PolicyIndex

    settings = Settings.from_env()
    target = policy_dir or Path(settings.policy_dir)

    typer.echo(f"indexing {target}…")
    count = PolicyIndex(settings).rebuild(target, with_variants=variants)
    typer.secho(f"indexed {count} chunks", fg=typer.colors.GREEN)


@app.command()
def search(query: str, locale: str = "en", limit: int = 5):
    """Search policy directly, to inspect what the assistant would retrieve."""
    from .sources.policy import PolicyIndex

    for hit in PolicyIndex(Settings.from_env()).search(query, locale=locale, limit=limit):
        score = hit.get("rerank_score", hit.get("score", 0))
        typer.echo(f"{score:+.2f}  {hit['title']} > {hit['heading_path']}  [{hit['source_file']}]")


@app.command()
def ask(
    message: str,
    publisher: str = typer.Option("pub_001", help="Publisher id from the request context"),
    locale: str = "en",
):
    """Send one message and print the reply."""
    from .conversation import Conversation

    turn = Conversation(publisher, locale=locale).send(message)
    _render(turn)


@app.command()
def chat(
    publisher: str = typer.Option("pub_001", help="Publisher id from the request context"),
    locale: str = "en",
):
    """Interactive conversation. State persists across turns within the session."""
    from .conversation import Conversation

    typer.echo("Loading…")
    conversation = Conversation(publisher, locale=locale)
    typer.secho(f"Talking as {publisher}. Ctrl-D to quit.\n", fg=typer.colors.BRIGHT_BLACK)

    while True:
        try:
            message = typer.prompt("you", prompt_suffix=" > ")
        except (EOFError, typer.Abort):
            typer.echo()
            break
        if not message.strip():
            continue

        try:
            for kind, text in conversation.stream(message):
                if kind == "progress":
                    typer.secho(f"  {text}", fg=typer.colors.BRIGHT_BLACK)
                else:
                    typer.echo(f"\n{text or '(no reply)'}\n")
        except Exception as exc:
            typer.secho(f"failed: {exc}", fg=typer.colors.RED)


def _render(turn) -> None:
    typer.secho(
        f"\n[{turn.intent or 'unrouted'}]"
        + (f" escalated → {turn.ticket}" if turn.escalated else ""),
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.echo(turn.reply or "(no reply)")
    if turn.escalated and turn.escalation_reason:
        typer.secho(f"reason: {turn.escalation_reason}", fg=typer.colors.BRIGHT_BLACK)
    typer.echo()


def main():
    sys.exit(app())


if __name__ == "__main__":
    main()
