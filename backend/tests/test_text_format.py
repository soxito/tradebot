r"""Guards on Telegram message formatting.

Telegram sends the model's text straight through with ``parse_mode="HTML"``,
so a maths answer arrived as raw ``\frac{\partial \mathbf{u}}{\partial t}``,
bold showed its literal asterisks, comparison tables arrived as pipe rows, and
``reply[:3800]`` cut the last answer off mid-word at "via diff".

The tests use real reported replies. Note the raw strings: writing these
through a shell mangles the double backslashes that matter most.
"""

from __future__ import annotations

import pytest

from app.services.text_format import (
    TELEGRAM_LIMIT,
    convert_latex,
    flatten_tables,
    format_for_telegram,
    latex_to_unicode,
    to_telegram_html,
    truncate_for_telegram,
)


# ── LaTeX → Unicode ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "expr,expected",
    [
        (r"\frac{1}{2}", "½"),
        (r"\frac{dy}{dx}", "dy/dx"),
        (r"\tfrac12", "½"),
        (r"\sqrt{x^2+1}", "√(x²+1)"),
        (r"x^{n+1}", "xⁿ⁺¹"),
        (r"a_{ij}", "aᵢⱼ"),
        (r"\mathbb{R}^3", "ℝ³"),
        (r"s\in\mathbb{C}", "s∈ℂ"),
        (r"\ln(x)", "ln(x)"),
        (r"\stackrel{?}{=}", "≟"),
        (r"\int", "∫"),
        (r"\in", "∈"),
    ],
)
def test_expressions_convert(expr, expected):
    assert latex_to_unicode(expr) == expected


def test_symbol_survives_a_following_styling_wrapper():
    r"""\Delta\mathbf{u} must not collapse to \Deltau."""
    assert latex_to_unicode(r"\Delta\mathbf{u}") == "Δu"
    assert latex_to_unicode(r"\nu\,\Delta\mathbf{u}") == "νΔu"


def test_operator_binds_to_its_term():
    assert latex_to_unicode(r"\frac{\partial \mathbf{u}}{\partial t}") == "∂u/∂t"
    assert latex_to_unicode(r"-\nabla p") == "-∇p"


def test_negative_and_thin_spaces_tighten():
    r"""\! is a NEGATIVE space; widening it says the opposite of the source."""
    assert latex_to_unicode(r"a\!\cdot\!b") == "a·b"


def test_script_fallback_is_not_re_superscripted():
    """∞ has no superscript glyph; the fallback bracket must stay a bracket."""
    out = latex_to_unicode(r"\sum_{n=1}^{\infty}")
    assert "^(∞)" in out
    assert "⁽" not in out


def test_row_break_with_explicit_gap_is_removed():
    r"""`\\[4pt]` is a row break; the bracket must not strand as "[4pt]"."""
    out = latex_to_unicode(r"a = b,\\[4pt] c = 0")
    assert "4pt" not in out
    assert "\\" not in out


def test_norm_bars_convert():
    out = latex_to_unicode(r"\|\omega(t)\|")
    assert "‖" in out
    assert "\\" not in out


def test_aligned_environment_scaffolding_is_dropped():
    out = latex_to_unicode(r"\begin{aligned} a &= b \end{aligned}")
    assert "begin" not in out and "aligned" not in out and "&" not in out


def test_never_raises_on_malformed_input():
    for bad in [r"\frac{1", r"\sqrt{", "^{", "{{{", "\\", r"\frac{}{}", r"\boxed{"]:
        latex_to_unicode(bad)  # must not raise


# ── Span detection ───────────────────────────────────────────────────────────

def test_display_and_inline_spans_convert():
    assert "∂u/∂t" in convert_latex(r"\[\frac{\partial \mathbf{u}}{\partial t}\]")
    assert "½" in convert_latex(r"zero at \(\tfrac12\)")


@pytest.mark.parametrize(
    "line",
    [
        "would earn a $1 million prize and a place in history",
        "entry at $64,000 with a stop at $62,500",
        "it cost $5 and returned $12 in profit",
    ],
)
def test_currency_amounts_are_left_alone(line):
    """This is a trading bot — a naive $…$ rule eats the words between amounts."""
    assert convert_latex(line) == line


def test_dollar_maths_still_converts_when_it_is_really_notation():
    assert "x²" in convert_latex("take $x^2$ here")


def test_stray_commands_outside_delimiters_are_converted():
    """Models emit bare \\alpha constantly, with no surrounding $ or \\(."""
    assert "α" in convert_latex(r"the \alpha term")


# ── Tables ───────────────────────────────────────────────────────────────────

def test_tables_become_readable_lines():
    table = (
        "| Challenge | Explanation |\n"
        "|-----------|-------------|\n"
        "| Non-linearity | Couples all velocity components. |\n"
        "| Critical spaces | Scaling leaves L3 invariant. |"
    )
    out = flatten_tables(table)
    assert "|" not in out
    assert "Non-linearity" in out
    assert "Couples all velocity components." in out
    assert out.count("•") == 2
    # A two-column table's second header is a generic label; repeating it on
    # every row adds nothing.
    assert "Explanation:" not in out


def test_wide_tables_keep_their_column_names():
    table = (
        "| Pair | Entry | Stop | Target |\n"
        "|------|-------|------|--------|\n"
        "| XAUUSD | 2400 | 2380 | 2450 |"
    )
    out = flatten_tables(table)
    assert "Entry: 2400" in out
    assert "Target: 2450" in out


def test_text_without_a_table_is_untouched():
    text = "P&L is |negative| today"
    assert flatten_tables(text) == text


# ── Telegram HTML ────────────────────────────────────────────────────────────

def test_markdown_becomes_telegram_tags():
    out = to_telegram_html("a **bold** and `code` and [link](https://x.com)")
    assert "<b>bold</b>" in out
    assert "<code>code</code>" in out
    assert '<a href="https://x.com">link</a>' in out
    assert "**" not in out and "`" not in out


def test_headings_become_bold_without_hashes():
    out = to_telegram_html("## Section\nbody")
    assert "<b>Section</b>" in out
    assert "#" not in out


def test_html_special_characters_are_escaped():
    """An unescaped < makes Telegram reject the whole message with a 400."""
    out = to_telegram_html("if a < b & c > d, use <script>")
    assert "&lt;" in out and "&amp;" in out and "&gt;" in out
    assert "<script>" not in out


def test_code_contents_are_not_reinterpreted():
    out = to_telegram_html("run ```\nif a < b: x = **2\n```")
    assert "<pre>" in out
    assert "&lt; b" in out
    assert "<b>" not in out  # the ** inside code must stay literal


def test_equation_lines_are_not_turned_into_bullets():
    r"""A wrapped equation line starting "+" was rewritten to "• ", which
    silently changed the maths."""
    out = to_telegram_html(
        "\\[\n\\frac{\\partial \\mathbf{u}}{\\partial t}\n"
        "+ (\\mathbf{u}\\!\\cdot\\!\\nabla)\\mathbf{u} = 0\n\\]"
    )
    assert "+ (u·∇)u" in out
    assert "• (u·∇)u" not in out


def test_real_bullets_still_render():
    out = to_telegram_html("- first\n- second")
    assert out.count("•") == 2


# ── Length ───────────────────────────────────────────────────────────────────

def test_short_messages_are_untouched():
    assert truncate_for_telegram("hello") == "hello"


def test_truncation_breaks_on_a_boundary_not_mid_word():
    text = ("This is a complete sentence about fluid dynamics. " * 200)
    out = truncate_for_telegram(text, limit=500)
    assert len(out) <= 500 + 40           # + the trim notice
    assert "trimmed" in out
    # The visible body must end at a sentence, not part-way through a word.
    body = out.split("\n\n…")[0]
    assert body.endswith(".")


def test_truncation_closes_open_tags():
    """An unclosed <b> makes Telegram reject the message outright."""
    text = "<b>" + ("word " * 400) + "</b>"
    out = truncate_for_telegram(text, limit=300)
    assert out.count("<b>") == out.count("</b>")


def test_truncation_keeps_most_of_the_budget():
    """A boundary search must not collapse a long block to a few words."""
    out = truncate_for_telegram("x" * 200 + ". " + "y" * 2000, limit=1000)
    assert len(out) > 600


# ── The reported reply, end to end ───────────────────────────────────────────

REPORTED = r"""Sir, I'll give you a concise yet thorough overview of the **three-dimensional Navier-Stokes equations**.

---

## 1. Navier-Stokes Existence & Smoothness (3-D)

### The equations
\[
\boxed{
\begin{aligned}
\frac{\partial \mathbf{u}}{\partial t}
+ (\mathbf{u}\!\cdot\!\nabla)\mathbf{u}
&= -\nabla p + \nu\,\Delta\mathbf{u} + \mathbf{f},\\[4pt]
\nabla\!\cdot\!\mathbf{u} &= 0,
\end{aligned}}
\]
* \(\mathbf{u}(\mathbf{x},t)\) - velocity field (vector)
* \(\nu>0\) - kinematic viscosity
* Domain: usually \(\mathbb{R}^3\).

### Why it is hard
| Challenge | Explanation |
|-----------|-------------|
| **Non-linearity** | The convection term \((\mathbf{u}\cdot\nabla)\mathbf{u}\) couples components. |
| **Critical spaces** | The scaling leaves \(L^3(\mathbb{R}^3)\) invariant. |

Blow-up needs \(\int_0^T \|\omega(t)\|_{L^\infty}\,dt=\infty\). It would earn a $1 million prize."""


def test_reported_reply_has_no_latex_left():
    out = format_for_telegram(REPORTED)
    for artefact in [
        r"\frac", r"\partial", r"\mathbf", r"\nabla", r"\qquad", r"\boxed",
        r"\begin", r"\end", r"\mathbb", r"\[", r"\]", r"\(", r"\)", "4pt",
    ]:
        assert artefact not in out, f"{artefact!r} survived formatting"


def test_reported_reply_reads_as_maths():
    out = format_for_telegram(REPORTED)
    assert "∂u/∂t" in out
    assert "-∇p + νΔu + f" in out
    assert "∇·u = 0" in out
    assert "ℝ³" in out


def test_reported_reply_has_no_raw_markdown_or_tables():
    out = format_for_telegram(REPORTED)
    assert "**" not in out
    assert "|" not in out
    assert "##" not in out


def test_reported_reply_keeps_the_prize_amount():
    assert "$1 million prize" in format_for_telegram(REPORTED)


def test_reported_reply_is_valid_telegram_html():
    out = format_for_telegram(REPORTED)
    for tag in ("b", "i", "code", "pre", "a"):
        assert out.count(f"<{tag}>") + out.count(f'<{tag} ') == out.count(f"</{tag}>")
    assert len(out) <= TELEGRAM_LIMIT + 40


def test_plain_trading_replies_pass_through_unchanged():
    plain = "BTCUSDT is at 64,120, up 2.1% on the day. No open positions."
    assert format_for_telegram(plain) == plain


def test_analysis_detail_pipes_are_not_read_as_a_table():
    """/analyze lays levels out with pipes; only a divider row makes a table."""
    detail = (
        "PROPOSED LONG SETUP (LIVE DATA via Bitget — NOT EXECUTED)\n"
        "Entry : 2400  |  SL : 2380  |  TP1 : 2450 (R:R 2.5x)  |  TP2 : 2500\n"
        'To execute say:\n  "execute XAUUSD long 1"'
    )
    out = format_for_telegram(detail)
    assert "Entry : 2400" in out
    assert "SL : 2380" in out
    assert "•" not in out


def test_comparison_operators_are_escaped_not_dropped():
    """A bare < under parse_mode=HTML makes Telegram reject the message."""
    out = format_for_telegram("Enter if RSI < 30 & price > EMA200")
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out


def test_formatting_never_raises():
    for bad in ["", "\\[", "**unclosed", "```unclosed", "|||", "\\frac{1", "<b>"]:
        format_for_telegram(bad)


# ── Stray commands in otherwise plain replies ────────────────────────────────

def test_sizing_commands_are_stripped_outside_delimiters():
    r"""A reply with no \[ … \] still arrives full of \bigl and \displaystyle."""
    out = convert_latex(r"So \displaystyle \frac{d}{dx}x^x=x^x\bigl(\ln x + 1\bigr).")
    assert "\\" not in out
    assert "bigl" not in out and "displaystyle" not in out


def test_a_fraction_does_not_run_into_the_next_token():
    """d/dxx^x gives the reader no way to see where the operator ends."""
    assert "d/dx x" in latex_to_unicode(r"\frac{d}{dx}x^x")


def test_stray_roots_and_sets_convert_too():
    out = convert_latex(r"bounded by \sqrt{2} on \mathbb{R}")
    assert "√2" in out and "ℝ" in out and "\\" not in out


# ── Bare accents (no braces) ─────────────────────────────────────────────────
# A forecast question came back as "\hat pₜ₊ₕ=pₜ exp(∑ᵢ₌₁^(h)\hat rₜ₊ᵢ)": the
# brace-aware pass needs a {…} to read, so `\hat p` kept its backslash.

def test_bare_style_command_loses_the_backslash_not_the_operand():
    # In prose the ^/_ passes deliberately stay out; the backslash must still go.
    assert format_for_telegram(r"\hat p_{t+h} = p_t") == "p_{t+h} = p_t"
    assert format_for_telegram(r"\bar x and \vec u") == "x and u"
    # Inside a maths span the whole thing renders.
    assert format_for_telegram(r"\[ \hat p_{t+h} = p_t \]").strip() == "pₜ₊ₕ = pₜ"


def test_longest_style_name_wins():
    assert format_for_telegram(r"\textbf{bold}") == "bold"
    assert "\\" not in format_for_telegram(r"E[\hat r_{t+1}]")


def test_a_superscript_horizon_renders():
    """∑_{i=1}^{h} used to fall back to the literal '^(h)'."""
    assert format_for_telegram(r"\[ \sum_{i=1}^{h} r_i \]").strip() == "∑ᵢ₌₁ʰ rᵢ"
