"""Draw the README's demo: an agent asked for a free GPU, finding one and using it.

    uv run python scripts/readme_demo.py

One animated SVG, about a minute long, looping. The fleet is the one in the README's
screenshots (`readme_screenshots.py`: invented machines, no addresses), and every table
in it is drawn by fleet's own renderers from probe snapshots in the real wire format --
so when the output changes, re-run this and the demo changes with it. What the person
and the agent say is scripted; the commands are ones the skill tells agents to run.

No player, no JavaScript, no fonts to fetch: text is placed cell by cell and revealed
with CSS keyframes, which GitHub renders inside an <img>.

Writes docs/assets/fleet-demo.svg.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readme_screenshots as shots  # noqa: E402  (sets up the throwaway fleet first)

from rich.console import Console, RenderableType  # noqa: E402
from rich.segment import Segment  # noqa: E402
from rich.style import Style  # noqa: E402
from rich.terminal_theme import SVG_EXPORT_THEME as THEME  # noqa: E402
from rich.text import Text  # noqa: E402

from fleet.models import ProbeResult, Status  # noqa: E402
from fleet.probe.parse import parse_payload  # noqa: E402
from fleet.render.top import render_ls  # noqa: E402
from fleet.render.view import fleet_view, matches_tag  # noqa: E402
from fleet.state import store  # noqa: E402

COLS, ROWS = 150, 30            # the terminal: wide enough for `fleet ls` unwrapped
CW, LH, FONT = 8.4, 18.0, 14    # cell width, line height, font size (px)
PAD, BAR = 16.0, 34.0           # inner padding; the window's title bar
FG, BG = THEME.foreground_color.hex, THEME.background_color.hex


@dataclass
class Line:
    segments: list[Segment]
    at: float                     # seconds into the loop when it appears
    typed: float = 0.0            # > 0: typed out over this many seconds
    row: int = 0                  # absolute row, filled in by the timeline


@dataclass
class Timeline:
    console: Console
    t: float = 0.0
    lines: list[Line] = field(default_factory=list)
    scrolls: list[tuple[float, int]] = field(default_factory=list)   # (when, top row)

    def _add(self, segs: list[Segment], typed: float = 0.0) -> None:
        line = Line(segs, self.t, typed, len(self.lines))
        self.lines.append(line)
        top = self.scrolls[-1][1] if self.scrolls else 0
        if line.row - top >= ROWS:                 # off the bottom: scroll, as a terminal does
            self.scrolls.append((self.t, line.row - ROWS + 1))

    def show(self, renderable: RenderableType, *, hold: float = 0.0, step: float = 0.0) -> None:
        """Print something at once (or `step` seconds per line), then wait `hold`."""
        for segs in self.console.render_lines(renderable, pad=False):
            self._add(segs)
            self.t += step
        self.t += hold

    def say(self, markup: str, *, hold: float = 1.2) -> None:
        self.show(Text.from_markup(markup), hold=hold)

    def type(self, markup: str, *, cps: float = 22.0, hold: float = 0.5) -> None:
        """One line typed out, as a person (or an agent's tool call) would."""
        (segs,) = self.console.render_lines(Text.from_markup(markup), pad=False)
        dur = max(0.4, sum(s.cell_length for s in segs) / cps)
        self._add(segs, typed=dur)
        self.t += dur + hold

    def blank(self, n: int = 1) -> None:
        for _ in range(n):
            self._add([])


def script(tl: Timeline, rows: list[dict]) -> None:
    gpus = [r for r in rows if matches_tag(r, "cuda") and matches_tag(r, "vram-24g")]

    tl.t = 0.8
    tl.type("[bold]>[/bold] train train.py on whatever has a free 24 GB card", hold=0.8)
    tl.blank()
    tl.say("[magenta]●[/magenta] Checking every machine for a free card of 24 GB or more.",
           hold=0.6)
    tl.type("  [bold green]$[/bold green] fleet ls --tag cuda --tag vram-24g", cps=30)
    tl.show(render_ls(gpus), step=0.05, hold=5.5)
    tl.blank()
    tl.say("[magenta]●[/magenta] [bold]rtx4090[/bold] has an idle 24 GB card and no "
           "hourly cost. a100-spot and h100-idle", hold=0)
    tl.say("  have idle cards too, but bill $1.89 and $2.49 an hour; lab-3090 is busy. "
           "Starting on rtx4090,", hold=0)
    tl.say("  detached, logging to train.log.", hold=1.8)
    tl.type("  [bold green]$[/bold green] fleet ssh rtx4090 -- 'cd ~/proj && nohup "
            "python train.py > train.log 2>&1 &'", cps=40, hold=2.5)

    busy(rows)                                      # the job has started on the card
    tl.type("  [bold green]$[/bold green] fleet ls rtx4090", cps=30)
    tl.show(render_ls([r for r in shots_rows() if r["name"] == "rtx4090"]), hold=1.5)
    tl.blank()
    tl.say("[magenta]●[/magenta] Running: train.py has the card on rtx4090 now, 4.2G "
           "left free. Progress goes to", hold=0)
    tl.say("  ~/proj/train.log there -- ask me any time how it is going.", hold=3.5)

    tl.blank()
    tl.type("[bold]>[/bold] anything costing me money?", hold=0.8)
    tl.blank()
    tl.say("[magenta]●[/magenta] Checking the machines that bill by the hour.", hold=0.6)
    tl.type("  [bold green]$[/bold green] fleet ls --tag rental", cps=30)
    tl.show(render_ls([r for r in shots_rows() if matches_tag(r, "rental")]), hold=2.5)
    tl.blank()
    tl.say("[magenta]●[/magenta] [bold]h100-idle[/bold] is idle at $2.49/hr. a100-spot is "
           "busy on one of its two cards. Stop h100-idle", hold=0)
    tl.say("  at your provider if you are done with it; I will not touch it without your "
           "yes.", hold=4.0)
    tl.blank()
    tl.say("[dim]fleet · github.com/lion-zhang/fleet · ask your agent: "
           "\"Install fleet from https://github.com/lion-zhang/fleet\"[/dim]", hold=5.0)


def busy(rows: list[dict]) -> None:
    """A new probe of rtx4090, taken once train.py holds the card."""
    _, text = next(m for m in shots.MACHINES if m[0].name == "rtx4090")
    text = shots.payload("rtx4090", "c3" * 16, cores=32, ram_gb=64, free_gb=38,
                         load="6.10 4.20 1.90",
                         gpus=[("NVIDIA GeForce RTX 4090", 24564, 20290, 97)],
                         procs=[(0, 860, "Xorg"), (0, 19430, "python")],
                         disks=[("/", 1800, 412)])
    conn = store.connect()
    store.record(conn, "d:4090", ProbeResult(status=Status.OK, snapshot=parse_payload(text)))
    conn.close()


def shots_rows() -> list[dict]:
    from fleet.ops import rows as rows_mod
    from fleet.render.view import Detail

    return rows_mod.snapshot(detail=Detail.FULL)


# ---- drawing --------------------------------------------------------------------------

def _color(style: Style | None, *, bg: bool = False) -> str | None:
    if style is None:
        return None
    c = style.bgcolor if bg else style.color
    if c is None or c.is_default:
        return None
    return c.get_truecolor(THEME, foreground=not bg).hex


def _runs(segs: list[Segment]) -> list[tuple[int, str, Style | None]]:
    """(column, text, style) for each run of one style, control codes dropped."""
    out: list[tuple[int, str, Style | None]] = []
    col = 0
    for seg in segs:
        if seg.control or not seg.text:
            continue
        if out and out[-1][2] == seg.style and out[-1][0] + len(out[-1][1]) == col:
            out[-1] = (out[-1][0], out[-1][1] + seg.text, seg.style)
        else:
            out.append((col, seg.text, seg.style))
        col += seg.cell_length
    return out


def _pct(t: float, total: float) -> str:
    return f"{min(100.0, max(0.0, 100 * t / total)):.3f}%"


def draw(tl: Timeline) -> str:
    total = tl.t
    width = COLS * CW + 2 * PAD
    height = BAR + ROWS * LH + 2 * PAD
    css: list[str] = [
        f"text{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',"
        f"monospace;font-size:{FONT}px;white-space:pre}}",
        ".b{font-weight:bold}.i{font-style:italic}.d{opacity:.6}",
        f".l,.c,.k,.s{{animation-duration:{total:.2f}s;animation-iteration-count:infinite}}",
        ".l{opacity:0}",
    ]
    body: list[str] = []
    end = _pct(total - 0.6, total)                  # everything fades before the loop restarts

    for n, line in enumerate(tl.lines):
        y = line.row * LH
        parts: list[str] = []
        for col, text, style in _runs(line.segments):
            x = col * CW
            if bgc := _color(style, bg=True):
                parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{len(text) * CW:.1f}" '
                             f'height="{LH}" fill="{bgc}"/>')
            if not text.strip():
                continue
            cls = " ".join(c for c, on in (("b", style and style.bold),
                                           ("i", style and style.italic),
                                           ("d", style and style.dim)) if on)
            fill = _color(style)
            parts.append(
                f'<text x="{x:.1f}" y="{y + LH * 0.75:.1f}" textLength="{len(text) * CW:.1f}" '
                f'lengthAdjust="spacingAndGlyphs"'
                + (f' class="{cls}"' if cls else "") + (f' fill="{fill}"' if fill else "")
                + f">{escape(text)}</text>")
        if not parts:
            continue
        css.append(f"@keyframes l{n}{{0%,{_pct(line.at, total)}{{opacity:0}}"
                   f"{_pct(line.at + 0.01, total)},{end}{{opacity:1}}100%{{opacity:0}}}}"
                   f"#l{n}{{animation-name:l{n}}}")
        if line.typed:
            # A cover the colour of the screen slides off the text a character at a time,
            # with the cursor riding on its left edge.
            cells = sum(len(t) for _, t, _ in _runs(line.segments))
            span = cells * CW
            a, b = _pct(line.at, total), _pct(line.at + line.typed, total)
            css.append(f"@keyframes k{n}{{0%,{a}{{transform:translateX(0);"
                       f"animation-timing-function:steps({cells},end)}}"
                       f"{b},100%{{transform:translateX({span:.1f}px)}}}}"
                       f"#k{n}{{animation-name:k{n}}}")
            off = _pct(line.at + line.typed + 0.6, total)
            css.append(f"@keyframes c{n}{{0%,{off}{{opacity:1}}"
                       f"{_pct(line.at + line.typed + 0.61, total)},100%{{opacity:0}}}}"
                       f"#c{n}{{animation-name:c{n}}}")
            parts.append(f'<g id="k{n}" class="k"><rect x="0" y="{y:.1f}" '
                         f'width="{span + CW:.1f}" height="{LH}" fill="{BG}"/>'
                         f'<rect id="c{n}" class="c" x="0" y="{y + 1:.1f}" width="{CW:.1f}" '
                         f'height="{LH - 2}" fill="{FG}" opacity=".8"/></g>')
        body.append(f'<g id="l{n}" class="l">{"".join(parts)}</g>')

    if tl.scrolls:
        frames = ["0%{transform:translateY(0)}"]
        for when, top in tl.scrolls:
            frames.append(f"{_pct(when, total)}{{transform:translateY({-top * LH:.1f}px)}}")
        frames.append(f"{end}{{transform:translateY({-tl.scrolls[-1][1] * LH:.1f}px)}}"
                      "100%{transform:translateY(0)}")
        # steps: a terminal jumps a line at a time, it does not glide
        css.append("@keyframes s{" + "".join(frames) + "}"
                   "#s{animation-name:s;animation-timing-function:steps(1,end)}")

    dots = "".join(f'<circle cx="{PAD + 6 + i * 20}" cy="{BAR / 2 + 2}" r="6" fill="{c}"/>'
                   for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")))
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" fill="{FG}" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" '
        f'aria-label="An agent asked to train on a free 24 GB GPU runs fleet ls, picks the '
        f'idle RTX 4090, starts the job there, then flags an idle rental that costs money.">'
        f"<title>fleet: your agent finds a free GPU and uses it</title>"
        f"<style>{''.join(css)}</style>"
        f'<rect width="100%" height="100%" rx="10" fill="{BG}"/>'
        f"{dots}"
        f'<text x="{width / 2:.0f}" y="{BAR / 2 + 6}" text-anchor="middle" class="d">'
        f"your agent, with fleet</text>"
        f'<clipPath id="v"><rect x="0" y="0" width="{COLS * CW:.1f}" '
        f'height="{ROWS * LH:.1f}"/></clipPath>'
        f'<g transform="translate({PAD},{BAR + PAD})" clip-path="url(#v)">'
        f'<g id="s" class="s">{"".join(body)}</g></g></svg>\n')


def main() -> None:
    rows = shots.build()
    fleet_view(rows)
    console = Console(width=COLS, force_terminal=True, color_system="truecolor",
                      legacy_windows=False, record=False)
    tl = Timeline(console)
    script(tl, rows)
    out = shots.ASSETS / "fleet-demo.svg"
    out.write_text(draw(tl), encoding="utf-8")
    print(f"wrote {out.relative_to(shots.REPO)}: {tl.t:.0f}s, {len(tl.lines)} lines, "
          f"{out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
