"""Turn model output into something a chat client can actually display.

Models answer a maths question with LaTeX and a comparison with a markdown
table. The web chat now converts those in the browser, but Telegram sends the
model's text straight through with ``parse_mode="HTML"``, so the user saw raw
``\\frac{\\partial \\mathbf{u}}{\\partial t}``, literal ``**bold**`` asterisks,
and pipe-delimited table rows — plus a reply chopped off mid-word at the
character cap.

This module is the server-side counterpart to ``frontend/src/utils/mathText.ts``
and covers the same ground: LaTeX to Unicode, markdown to the small tag set
Telegram accepts, tables to lines that read on a phone, and truncation on a
sentence boundary rather than mid-word.

Everything here is defensive. Badly-formed input must come out readable, never
raise, and never emit HTML that Telegram will reject — a formatting bug that
returns a 400 costs the user the whole message.
"""

from __future__ import annotations

import html
import re
from typing import Callable

# ── LaTeX → Unicode ──────────────────────────────────────────────────────────

_SYMBOLS: dict[str, str] = {
    # Greek — lower case
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    # Greek — upper case
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Operators and relations
    "partial": "∂", "nabla": "∇", "infty": "∞", "sum": "∑", "prod": "∏",
    "int": "∫", "oint": "∮", "pm": "±", "mp": "∓", "times": "×", "div": "÷",
    "cdot": "·", "ast": "∗", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼",
    "simeq": "≃", "cong": "≅", "propto": "∝", "in": "∈", "notin": "∉",
    "ni": "∋", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "nexists": "∄", "neg": "¬", "land": "∧",
    "lor": "∨", "to": "→", "rightarrow": "→", "Rightarrow": "⇒",
    "leftarrow": "←", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "mapsto": "↦", "implies": "⇒", "iff": "⇔",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "prime": "′",
    "aleph": "ℵ", "hbar": "ℏ", "ell": "ℓ", "circ": "∘", "bullet": "•",
    "star": "⋆", "dagger": "†", "oplus": "⊕", "otimes": "⊗",
    "lVert": "‖", "rVert": "‖", "lvert": "|", "rvert": "|",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋",
}

#: Escaped delimiters: `\|` is the norm bars, `\{`/`\}` literal braces.
_ESCAPED_DELIMS = {r"\|": "‖", r"\{": "{", r"\}": "}", r"\%": "%", r"\&": "&", r"\#": "#"}

_BLACKBOARD = {
    "A": "𝔸", "C": "ℂ", "D": "𝔻", "F": "𝔽", "G": "𝔾", "H": "ℍ", "K": "𝕂",
    "N": "ℕ", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "S": "𝕊", "T": "𝕋", "Z": "ℤ",
}

_SUPERSCRIPT = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶",
    "7": "⁷", "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽",
    ")": "⁾", "n": "ⁿ", "i": "ⁱ", "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ",
    "e": "ᵉ", "k": "ᵏ", "m": "ᵐ", "p": "ᵖ", "s": "ˢ", "t": "ᵗ", "x": "ˣ", "y": "ʸ",
    # A forecast horizon is written p̂ₜ₊ₕ = pₜ exp(∑ᵢ₌₁ʰ r̂ₜ₊ᵢ): without "h" the
    # sum's limit fell back to the literal "^(h)".
    "f": "ᶠ", "g": "ᵍ", "h": "ʰ", "j": "ʲ", "l": "ˡ", "o": "ᵒ", "r": "ʳ",
    "u": "ᵘ", "v": "ᵛ", "w": "ʷ", "z": "ᶻ",
}

_SUBSCRIPT = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆",
    "7": "₇", "8": "₈", "9": "₉", "+": "₊", "-": "₋", "=": "₌", "(": "₍",
    ")": "₎", "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ", "k": "ₖ",
    "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ", "p": "ₚ", "r": "ᵣ", "s": "ₛ",
    "t": "ₜ", "u": "ᵤ", "v": "ᵥ", "x": "ₓ",
}

_VULGAR = {
    "1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾",
    "1/5": "⅕", "1/8": "⅛", "3/8": "⅜", "5/8": "⅝", "7/8": "⅞",
}

_FUNCTIONS = (
    "arcsin|arccos|arctan|limsup|liminf|sinh|cosh|tanh|ln|log|exp|sin|cos|tan|"
    "sec|csc|cot|lim|max|min|sup|inf|det|dim|gcd|arg|deg|ker|hom|Pr|mod"
)


def _map_all(text: str, table: dict[str, str]) -> str | None:
    """Map every character, or return None if any has no equivalent."""
    out = []
    for ch in text:
        mapped = table.get(ch)
        if mapped is None:
            return None
        out.append(mapped)
    return "".join(out)


def _read_group(src: str, open_idx: int) -> tuple[str, int] | None:
    """Read a balanced ``{...}`` starting at ``open_idx``. None if unbalanced."""
    if open_idx >= len(src) or src[open_idx] != "{":
        return None
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx + 1:i], i + 1
    return None


def _replace_command(
    src: str, name: str, arity: int, render: Callable[[str, str], str],
    *, separate: bool = False,
) -> str:
    """Replace every ``\\name{...}`` (brace-aware, so nesting survives).

    ``separate`` adds a space when the replacement would otherwise run into the
    next token: ``\\frac{d}{dx}x^x`` becomes "d/dx xˣ" rather than "d/dxxˣ",
    where the reader cannot tell where the operator ends.
    """
    marker = "\\" + name
    out: list[str] = []
    i = 0
    while i < len(src):
        at = src.find(marker, i)
        if at == -1:
            out.append(src[i:])
            break
        # \nu must not match inside \nabla: the name may not run on.
        after = src[at + len(marker):at + len(marker) + 1]
        if after and after.isalpha():
            out.append(src[i:at + len(marker)])
            i = at + len(marker)
            continue
        cursor = at + len(marker)
        while cursor < len(src) and src[cursor] == " ":
            cursor += 1
        first = _read_group(src, cursor)
        if first is None:
            out.append(src[i:at + len(marker)])
            i = at + len(marker)
            continue
        second: tuple[str, int] | None = None
        if arity == 2:
            c2 = first[1]
            while c2 < len(src) and src[c2] == " ":
                c2 += 1
            second = _read_group(src, c2)
            if second is None:
                out.append(src[i:at + len(marker)])
                i = at + len(marker)
                continue
        out.append(src[i:at])
        rendered = render(first[0], second[0] if second else "")
        i = second[1] if second else first[1]
        if (
            separate and rendered and rendered[-1].isalnum()
            and i < len(src) and (src[i].isalnum() or src[i] == "\\")
        ):
            rendered += " "
        out.append(rendered)
    return "".join(out)


def _is_atomic(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9∂π]{1,3}", text.strip()))


def _render_fraction(numerator: str, denominator: str) -> str:
    a = latex_to_unicode(numerator).strip()
    b = latex_to_unicode(denominator).strip()
    vulgar = _VULGAR.get(f"{a}/{b}")
    if vulgar:
        return vulgar
    left = a if _is_atomic(a) else f"({a})"
    right = b if _is_atomic(b) else f"({b})"
    return f"{left}/{right}"


def _replace_script(src: str, sigil: str, table: dict[str, str]) -> str:
    """Replace ``^{...}`` / ``_{...}`` with Unicode scripts."""
    out: list[str] = []
    i = 0
    while i < len(src):
        at = src.find(sigil + "{", i)
        if at == -1:
            out.append(src[i:])
            break
        group = _read_group(src, at + 1)
        if group is None:
            out.append(src[i:at + 1])
            i = at + 1
            continue
        body = latex_to_unicode(group[0]).strip()
        out.append(src[i:at])
        out.append(_map_all(body, table) or f"{sigil}({body})")
        i = group[1]
    return "".join(out)


_SYMBOL_RE = re.compile(
    r"\\(" + "|".join(sorted(_SYMBOLS, key=len, reverse=True)) + r")(?![a-zA-Z])"
)
_FUNCTION_RE = re.compile(r"\\(" + _FUNCTIONS + r")(?![a-zA-Z])")
#: Pure sizing/layout directives that take no braced argument.
#: `[bB]igg?[lrm]?` covers \big \bigg \bigl \bigr \Bigl \biggr and friends —
#: models reach for these constantly and a bare \bigl( was left on screen.
_SIZING_RE = re.compile(
    r"\\(?:left|right|[bB]igg?[lrm]?|displaystyle|textstyle|scriptstyle"
    r"|limits|nolimits|notag|nonumber)\b"
)
_STYLE_COMMANDS = (
    "mathbf", "mathrm", "mathit", "mathcal", "mathsf", "boldsymbol",
    "vec", "bar", "hat", "text", "textbf", "textrm", "operatorname", "mbox",
    "boxed", "displaystyle",
)

#: The same commands written without braces — ``\hat p``, ``\vec u``. The
#: brace-aware pass above needs a ``{…}`` to read and leaves these untouched, so
#: a forecast written ``\hat p_{t+h}`` reached the user with the backslash still
#: on screen. The accent has no phone-legible form anyway: drop the command and
#: keep its operand. Longest name first, or ``\textbf`` loses only its ``\text``.
_BARE_STYLE_RE = re.compile(
    r"\\(?:" + "|".join(sorted(_STYLE_COMMANDS, key=len, reverse=True)) + r")"
    r"(?![a-zA-Z])[ \t]*"
)


def latex_to_unicode(expr: str) -> str:
    """Convert a LaTeX expression body to readable Unicode. Never raises."""
    if not expr:
        return ""
    s = expr
    try:
        # Escaped delimiters first: `\|` must become the norm bars before the
        # backslash-stripping passes below turn it into a stray pipe.
        for escaped, glyph in _ESCAPED_DELIMS.items():
            s = s.replace(escaped, glyph)

        # Structural commands first; the renderers recurse for nesting.
        s = re.sub(r"\\[dt]?frac(\d)(\d)",
                   lambda m: _render_fraction(m.group(1), m.group(2)), s)
        for name in ("frac", "dfrac", "tfrac"):
            s = _replace_command(s, name, 2, _render_fraction, separate=True)
        s = _replace_command(
            s, "sqrt", 1,
            lambda a, _b: (lambda body: f"√{body}" if _is_atomic(body) else f"√({body})")(
                latex_to_unicode(a).strip()
            ),
        )
        s = _replace_command(s, "mathbb", 1, lambda a, _b: _BLACKBOARD.get(a.strip(), a))
        s = _replace_command(
            s, "stackrel", 2,
            lambda a, b: "≟" if (latex_to_unicode(a).strip() == "?"
                                 and latex_to_unicode(b).strip() == "=")
            else latex_to_unicode(b).strip(),
        )

        # Symbols BEFORE the styling wrappers are unwrapped. The other order
        # breaks `\Delta\mathbf{u}`: dropping \mathbf first leaves `\Deltau`,
        # and the trailing letter then defeats the \Delta lookahead.
        s = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(1)], s)
        s = _FUNCTION_RE.sub(r"\1", s)
        for cmd in _STYLE_COMMANDS:
            s = _replace_command(s, cmd, 1, lambda a, _b: latex_to_unicode(a))
        s = _BARE_STYLE_RE.sub("", s)

        # Bare single-char scripts BEFORE braced ones: the braced pass falls
        # back to `^(…)` for characters with no Unicode form, and running the
        # bare pass after would superscript that bracket into `⁽`.
        s = re.sub(r"\^([A-Za-z0-9+\-])",
                   lambda m: _SUPERSCRIPT.get(m.group(1), m.group(0)), s)
        s = re.sub(r"_([A-Za-z0-9+\-])",
                   lambda m: _SUBSCRIPT.get(m.group(1), m.group(0)), s)
        s = _replace_script(s, "^", _SUPERSCRIPT)
        s = _replace_script(s, "_", _SUBSCRIPT)

        # Environments and alignment scaffolding. `\\[4pt]` is a row break with
        # an explicit gap; the optional bracket must go with it, or it is left
        # stranded in the text as a literal "[4pt]".
        s = re.sub(r"\\(?:begin|end)\{[^{}]*\}", "", s)
        s = re.sub(r"\\\\\s*\[[^\]]*\]", "\n", s)
        s = s.replace("\\\\", "\n")
        s = re.sub(r"\{([^{}]*)\}", r"\1", s)

        # \! is a NEGATIVE space and \, \; \: are thin spaces: they were written
        # to pull terms together, so a full space says the opposite.
        s = re.sub(r"\\[!,;:]", "", s)
        s = re.sub(r"\\(?:qquad|quad|\s)", " ", s)
        s = _SIZING_RE.sub("", s)
        s = re.sub(r"[&$]", " ", s)

        # An operator binds to the term it applies to: `∂ u` → `∂u`.
        s = re.sub(r"([∂∇√∑∏∫ΔδπΓΛΩ])[ \t]+(?=[A-Za-z(])", r"\1", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()
    except Exception:  # noqa: BLE001 — unreadable maths beats a lost message
        return expr


# Display forms first, then inline. `$…$` is filtered separately below.
_MATH_SPANS: list[tuple[re.Pattern, bool]] = [
    (re.compile(r"\\\[(.*?)\\\]", re.S), True),
    (re.compile(r"\$\$(.*?)\$\$", re.S), True),
    (re.compile(
        r"\\begin\{(equation|align|aligned|gather|displaymath|array)\*?\}"
        r"(.*?)\\end\{\1\*?\}", re.S), True),
    (re.compile(r"\\\((.*?)\\\)", re.S), False),
]


def _is_math_dollar(body: str) -> bool:
    """Is a ``$…$`` span maths, or two currency amounts in one sentence?

    This is a trading bot: "$5" and "$2,400" are everywhere, and a naive rule
    turns "costs $5 and earns $10" into a maths span, deleting the words
    between. Missing a plain ``$x$`` is cosmetic; eating a sentence is not.
    """
    if not body or body[0].isspace() or body[-1].isspace():
        return False
    if re.match(r"^[\d,.]+\s", body):
        return False
    return bool(re.search(r"\\[a-zA-Z]|[\^_]", body))


def convert_latex(text: str, on_math: Callable[[str, bool], str] | None = None) -> str:
    """Replace every LaTeX span in a message with readable Unicode.

    ``on_math(converted, is_display)`` lets a caller take custody of each
    converted span. Telegram formatting uses it to park equations somewhere the
    later line-based passes cannot reach: a wrapped equation line beginning
    "+ (u·∇)u" is otherwise read as a markdown bullet and rewritten to
    "• (u·∇)u", quietly changing the maths.
    """
    if not text:
        return ""

    def _emit(converted: str, display: bool) -> str:
        if on_math is not None:
            return on_math(converted, display)
        return f"\n{converted}\n" if display else converted

    out = text
    for pattern, display in _MATH_SPANS:
        def _sub(m: re.Match, _display: bool = display) -> str:
            body = m.group(2) if m.re.groups > 1 else m.group(1)
            converted = latex_to_unicode(body)
            return _emit(converted, _display) if converted else ""
        out = pattern.sub(_sub, out)
    out = re.sub(
        r"\$([^$\n]+?)\$",
        lambda m: _emit(latex_to_unicode(m.group(1)), False)
        if _is_math_dollar(m.group(1)) else m.group(0),
        out,
    )
    # Stray commands outside any delimiter — models emit these constantly, and
    # a reply with no \[ … \] at all still arrives full of \frac and \bigl.
    # Only backslash-led forms are touched here: the surrounding text is prose,
    # so the ^/_ and brace passes must not run over it.
    if "\\" in out:
        for escaped, glyph in _ESCAPED_DELIMS.items():
            out = out.replace(escaped, glyph)
        out = _SYMBOL_RE.sub(lambda m: _SYMBOLS[m.group(1)], out)
        out = _FUNCTION_RE.sub(r"\1", out)
        for name in ("frac", "dfrac", "tfrac"):
            out = _replace_command(out, name, 2, _render_fraction, separate=True)
        out = _replace_command(
            out, "sqrt", 1,
            lambda a, _b: (lambda b: f"√{b}" if _is_atomic(b) else f"√({b})")(
                latex_to_unicode(a).strip()
            ),
        )
        out = _replace_command(out, "mathbb", 1, lambda a, _b: _BLACKBOARD.get(a.strip(), a))
        for cmd in _STYLE_COMMANDS:
            out = _replace_command(out, cmd, 1, lambda a, _b: latex_to_unicode(a))
        out = _BARE_STYLE_RE.sub("", out)
        out = _SIZING_RE.sub("", out)
        out = re.sub(r"\\[!,;:]", "", out)
    return re.sub(r"\n{3,}", "\n\n", out)


# ── Markdown tables → lines that read on a phone ─────────────────────────────

_TABLE_DIVIDER = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def flatten_tables(text: str) -> str:
    """Rewrite markdown tables as ``header — value`` lines.

    Telegram renders no tables, and a phone is too narrow for aligned columns
    anyway, so the pipes arrived as visual noise. Pairing each cell with its
    column header keeps the information and loses only the grid.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_row = line.count("|") >= 2
        divider_next = i + 1 < len(lines) and _TABLE_DIVIDER.match(lines[i + 1] or "")
        if not (is_row and divider_next):
            out.append(line)
            i += 1
            continue

        headers = _split_row(line)
        i += 2  # header + divider
        while i < len(lines) and lines[i].count("|") >= 2:
            cells = _split_row(lines[i])
            if any(cells):
                first = cells[0] if cells else ""
                # On a two-column table the second header is almost always a
                # generic label ("Explanation", "Detail"), so repeating it on
                # every row is pure noise. Name the column only when there are
                # several and the reader would otherwise lose track.
                label = len(headers) > 2
                rest = [
                    f"{headers[j]}: {cells[j]}"
                    if label and j < len(headers) and headers[j] else cells[j]
                    for j in range(1, len(cells)) if cells[j]
                ]
                out.append(f"• {first}" + (f" — {'; '.join(rest)}" if rest else ""))
            i += 1
    return "\n".join(out)


# ── Markdown → the tag set Telegram accepts ──────────────────────────────────

_PLACEHOLDER = "\x00F{}\x00"
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def to_telegram_html(text: str) -> str:
    """Convert markdown to the small HTML subset Telegram supports.

    Telegram accepts only b/i/u/s/code/pre/a/blockquote and rejects the whole
    message with a 400 if anything else looks like a tag — so every other ``<``
    must be escaped, and code has to be lifted out before escaping so its
    contents survive intact.
    """
    if not text:
        return ""

    stash: list[str] = []

    def _stash(rendered: str) -> str:
        stash.append(rendered)
        return _PLACEHOLDER.format(len(stash) - 1)

    # 1. Lift code out first so nothing below rewrites its contents.
    body = _FENCE_RE.sub(
        lambda m: _stash(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text
    )
    body = _INLINE_CODE_RE.sub(
        lambda m: _stash(f"<code>{html.escape(m.group(1))}</code>"), body
    )

    # 2. Maths and tables, while the text is still plain. Converted equations
    #    are stashed like code: they are literal from here on, so no later pass
    #    can mistake a leading "+" or "-" in a wrapped equation for a bullet.
    def _stash_math(converted: str, display: bool) -> str:
        token = _stash(html.escape(converted, quote=False))
        return f"\n{token}\n" if display else token

    body = convert_latex(body, on_math=_stash_math)
    body = flatten_tables(body)

    # 3. Escape everything that remains, then add our own tags.
    body = html.escape(body, quote=False)

    lines: list[str] = []
    for raw in body.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        # Headings become bold; Telegram has no heading tag.
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            lines.append(f"<b>{heading.group(2).strip()}</b>")
            continue
        # Horizontal rules become a thin divider.
        if re.fullmatch(r"(?:[-*_]\s*){3,}", stripped):
            lines.append("──────────")
            continue
        # Unordered bullets get a real bullet glyph.
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if bullet:
            indent = "  " * min(len(bullet.group(1)) // 2, 3)
            lines.append(f"{indent}• {bullet.group(2)}")
            continue
        lines.append(line)
    body = "\n".join(lines)

    # 4. Inline emphasis. Bold before italic, so **x** is not read as *(*x*)*.
    body = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", body, flags=re.S)
    body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body, flags=re.S)
    body = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", body)
    body = re.sub(r"(?<![\w_])__(?!\s)([^_\n]+?)(?<!\s)__(?![\w_])", r"<b>\1</b>", body)
    # Markdown links → Telegram anchors.
    body = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', body)

    # 5. Put the code back.
    for idx, rendered in enumerate(stash):
        body = body.replace(_PLACEHOLDER.format(idx), rendered)

    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


# ── Length ───────────────────────────────────────────────────────────────────

#: Telegram's hard limit is 4096 characters; leave room for the trim notice.
TELEGRAM_LIMIT = 3900

_OPEN_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|pre|a|blockquote)\b[^>]*>")


def _close_open_tags(fragment: str) -> str:
    """Close any tag still open, so a trimmed message is not rejected as 400."""
    open_stack: list[str] = []
    for m in _OPEN_TAG_RE.finditer(fragment):
        if m.group(1):
            if open_stack and open_stack[-1] == m.group(2):
                open_stack.pop()
        else:
            open_stack.append(m.group(2))
    return fragment + "".join(f"</{tag}>" for tag in reversed(open_stack))


def truncate_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Trim to ``limit`` on a boundary, never mid-word.

    The previous ``reply[:3800]`` cut the last answer off at "via diff", which
    reads as the bot crashing. Breaking at a paragraph — or failing that a
    sentence — and saying so is honest about what happened.
    """
    if len(text) <= limit:
        return text

    window = text[:limit]
    for boundary in ("\n\n", "\n", ". ", "; ", " "):
        cut = window.rfind(boundary)
        # Only accept a boundary that keeps most of the budget, or a long
        # unbroken block would collapse to a couple of sentences.
        if cut > limit * 0.6:
            # Keep the punctuation, drop the whitespace: cutting at `cut` for
            # ". " would end the message on "dynamics" instead of "dynamics.".
            window = window[:cut + len(boundary.rstrip())]
            break
    return _close_open_tags(window.rstrip()) + "\n\n… (trimmed — ask me to continue)"


def format_for_telegram(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    """Full pipeline: maths, tables, markdown, then a clean length trim."""
    if not text:
        return ""
    try:
        return truncate_for_telegram(to_telegram_html(text), limit)
    except Exception:  # noqa: BLE001
        # A formatting bug must not cost the user their answer. Fall back to
        # escaped plain text, which Telegram always accepts.
        return truncate_for_telegram(html.escape(text, quote=False), limit)
