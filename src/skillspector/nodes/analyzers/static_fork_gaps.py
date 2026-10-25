"""Static patterns (fork): gaps found by the coverage audit (FG1-FG12).

Each rule closes a gap listed in docs/COVERAGE_AUDIT.md, where the upstream static
rules produced no finding or caught an attack only through a side signal:

FG1  untrusted package source (http index, --trusted-host, direct archive URL)
FG2  over-broad trigger description in SKILL.md frontmatter
FG3  write to agent memory/config or a shell start-up file (persistence)
FG4  encoded payload (base64, hex, binary, Morse) that decodes to a command
FG5  obfuscated shell command (GuardFall: split quotes, hex escapes, joined vars)
FG6  resource abuse (huge loops, huge downloads)
FG7  instruction to follow directions found in external content (indirect injection)
FG8  user or web input interpolated into a shell command
FG9  running untrusted pull-request code
FG10 sandbox evasion or time-bomb condition
FG11 falsifying results or hiding information from the user
FG12 reading or archiving credential stores / raw-socket exfiltration in scripts
"""

from __future__ import annotations

import base64
import binascii
import re
import sys
from collections.abc import Iterator

from skillspector.logging_config import get_logger
from skillspector.models import AnalyzerFinding, Location, Severity
from skillspector.state import AnalyzerNodeResponse, SkillspectorState

from . import pattern_defaults, static_runner
from .common import get_context, get_line_number
from .pattern_defaults import PatternCategory
from .prohibition_context import is_directly_prohibited

logger = get_logger(__name__)

ANALYZER_ID = "static_fork_gaps"

# name, category, explanation, remediation
_RULES: dict[str, tuple[str, PatternCategory, str, str]] = {
    "FG1": (
        "Untrusted Package Source",
        PatternCategory.SUPPLY_CHAIN,
        "A package is installed from a plain-http index, an unverified host, or a direct "
        "archive URL. The package content can be replaced without notice.",
        "Install only from the official registry over HTTPS, pin versions and hashes, and "
        "never disable host verification.",
    ),
    "FG2": (
        "Over-Broad Skill Trigger",
        PatternCategory.TRIGGER_ABUSE,
        "The skill description asks to be loaded for every or any task, so its instructions "
        "would enter unrelated sessions.",
        "Describe the specific tasks the skill handles; remove 'always', 'any task' and "
        "'load first' wording.",
    ),
    "FG3": (
        "Agent Persistence Write",
        PatternCategory.MEMORY_POISONING,
        "The skill writes to agent memory or configuration (CLAUDE.md, AGENTS.md, settings, "
        "hooks) or to a shell start-up file, so its effect survives the session.",
        "Remove writes to agent configuration, memory and shell start-up files; ask the user "
        "to make such changes themselves.",
    ),
    "FG4": (
        "Encoded Payload",
        PatternCategory.PROMPT_INJECTION,
        "Encoded text (base64, hex, binary or Morse) decodes to a command or sensitive "
        "reference. Legitimate skills rarely need encoded instructions.",
        "Replace encoded content with plain, reviewable text.",
    ),
    "FG5": (
        "Obfuscated Shell Command",
        PatternCategory.TOOL_MISUSE,
        "A command name is hidden with split quotes, hex escapes or joined variables "
        "(GuardFall pattern), which evades literal filters but runs normally.",
        "Write commands literally; remove obfuscation.",
    ),
    "FG6": (
        "Resource Abuse",
        PatternCategory.EXCESSIVE_AGENCY,
        "The skill asks for very large loops, downloads or outputs that waste compute, "
        "tokens or bandwidth.",
        "Bound loops and downloads to what the task needs.",
    ),
    "FG7": (
        "Follow External Instructions",
        PatternCategory.PROMPT_INJECTION,
        "The skill tells the agent to follow instructions found in external content "
        "(issues, pull requests, web pages, emails, files). Anyone who can write that "
        "content can control the agent (indirect prompt injection).",
        "Treat external content as data; never execute instructions found in it.",
    ),
    "FG8": (
        "Shell Interpolation of Untrusted Input",
        PatternCategory.TOOL_MISUSE,
        "User or web input is placed directly into a shell command, allowing command injection.",
        "Pass input as a separate argument, quote it, or validate it against an allow-list.",
    ),
    "FG9": (
        "Untrusted Code Execution",
        PatternCategory.SUPPLY_CHAIN,
        "The skill checks out contributor pull-request code and runs it (tests, build, "
        "install) with the agent's permissions.",
        "Run untrusted pull-request code only in an isolated sandbox without secrets.",
    ),
    "FG10": (
        "Sandbox Evasion or Time Bomb",
        PatternCategory.ROGUE_AGENT,
        "Behaviour changes depending on whether the skill seems to run in a sandbox or VM, "
        "or after a date: a classic way to hide malicious steps from review.",
        "Remove environment- and date-conditional behaviour.",
    ),
    "FG11": (
        "Result Falsification or Concealment",
        PatternCategory.ROGUE_AGENT,
        "The skill tells the agent to falsify results or hide errors, warnings or actions "
        "from the user.",
        "Report results truthfully and keep the user informed of every action.",
    ),
    "FG12": (
        "Credential Store Access",
        PatternCategory.DATA_EXFILTRATION,
        "Credential stores (~/.ssh, ~/.aws, ~/.gnupg, .env, browser profiles) are archived, "
        "copied or read, or data is sent over a raw socket.",
        "Do not access credential stores; use scoped tokens provided by the user.",
    ),
}

for _rule_id, (_name, _category, _explanation, _remediation) in _RULES.items():
    pattern_defaults.PATTERN_NAMES.setdefault(_rule_id, _name)
    pattern_defaults.RULE_ID_TO_CATEGORY.setdefault(_rule_id, _category.value)
    pattern_defaults.DEFAULT_EXPLANATIONS.setdefault(_rule_id, _explanation)
    pattern_defaults.DEFAULT_REMEDIATIONS.setdefault(_rule_id, _remediation)

_I = re.IGNORECASE | re.MULTILINE

# Words that make decoded text or normalized shell dangerous.
_DANGER = re.compile(
    r"\b(?:curl|wget|nc|ncat|netcat|bash|sh|zsh|python3?|perl|eval|exec|base64|scp|rsync|"
    r"ssh|id_rsa|id_ed25519|credentials|token|password|passwd|api[_ -]?key|secret)\b|"
    r"\.ssh|\.aws|\.env\b|https?://|/dev/tcp",
    re.IGNORECASE,
)
_SHELL_COMMANDS = (
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "bash",
    "python",
    "python3",
    "perl",
    "scp",
    "rsync",
    "base64",
    "sh",
)

FG1_PATTERNS = [
    (
        r"\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+install\b[^\n]*?(?:--(?:extra-)?index-url|-i)\s+http://\S+",
        0.9,
    ),
    (r"\b(?:pip3?|uv\s+pip|python3?\s+-m\s+pip)\s+install\b[^\n]*--trusted-host\b", 0.85),
    (r"\b(?:pip3?|uv\s+pip)\s+install\b[^\n]*?\bhttps?://\S+\.(?:tar\.gz|zip|whl)\b", 0.7),
    (r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b[^\n]*?\b(?:http://|git\+http://)\S+", 0.85),
    (r"\b(?:npm|pnpm|yarn)\s+(?:install|add|i)\b[^\n]*--registry\s+http://\S+", 0.85),
    (r"\bnpm\s+config\s+set\s+(?:strict-ssl\s+false|registry\s+http://)", 0.85),
    (r"\bgem\s+install\b[^\n]*--source\s+http://\S+", 0.8),
]

FG2_PATTERNS = [
    (
        r"\b(?:use|load|apply|activate|invoke|trigger)\b[^\n]{0,40}?\b(?:for\s+)?(?:any|every|all)\s+"
        r"(?:task|request|prompt|question|conversation|message|coding|work)",
        0.75,
    ),
    (r"\balways\s+(?:load|use|apply|activate|run|invoke)\b", 0.75),
    (r"\b(?:load|run|use)\s+(?:this\s+(?:skill\s+)?)?(?:first|before\s+(?:any|all|every))", 0.7),
    (r"\b(?:for|on)\s+everything\b", 0.6),
]

_PERSIST_TARGET = (
    r"(?:(?:~|\$HOME)/\.claude/|\.claude/(?:settings|CLAUDE|memory|hooks|agents|commands|skills)|CLAUDE\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|\.cursor/rules|"
    r"\.codex/|settings(?:\.local)?\.json|\.mcp\.json|\.git/hooks/|"
    r"~?/?\.(?:bashrc|zshrc|bash_profile|profile|zprofile)|crontab|/etc/cron|"
    r"\.config/autostart|systemd/user|launchagents|(?:agent|assistant)(?:'s)?\s+memory)"
)
FG3_PATTERNS = [
    (
        rf"\b(?:add|append|write|insert|put|save|copy|modify|edit|update|create)\b[^\n]{{0,120}}?"
        rf"\b(?:to|into|in)\s+[`'\"]?{_PERSIST_TARGET}",
        0.8,
    ),
    (rf"(?:>>|\btee\s+-a)\s*[`'\"]?{_PERSIST_TARGET}", 0.85),
    (r"\bcrontab\s+(?:-l\s*\|[^\n]*\|\s*crontab\s+-|-e|\S+\.cron)", 0.7),
]

FG6_PATTERNS = [
    (
        r"\b(?:repeat|run|loop|iterate|do)\b[^\n]{0,60}?\b\d{1,3}(?:,?\d{3}){1,}\s*"
        r"(?:times|iterations|loops)\b",
        0.75,
    ),
    (
        r"\b(?:download|fetch|pull|curl|wget)\b[^\n]{0,80}?\b\d{2,}\s*(?:GB|TB|gigabytes?|terabytes?)\b",
        0.7,
    ),
    (
        r"\b(?:run|start|create|enter|keep\s+running)\b[^\n.]{0,20}?\b(?:an?\s+)?(?:infinite|endless)\s+loop\b",
        0.7,
    ),
]

FG7_PATTERNS = [
    (
        r"\b(?:follow|obey|execute|carry\s+out|apply|do)\b[^\n]{0,30}?\b(?:any|all|the)?\s*"
        r"(?:instructions?|steps|commands?|directions)\b[^\n]{0,40}?\b(?:found|written|given|contained|"
        r"listed)?\s*(?:in|inside|from)\s+(?:the\s+)?(?:\w+\s+){0,2}"
        r"(?:issues?|issue\s+bod(?:y|ies)|pull\s+requests?|prs?|comments?|emails?|messages?|"
        r"web\s*pages?|websites?|urls?|tickets?|responses?|readme|documents?|search\s+results?)",
        0.75,
    ),
    (
        r"\bdo\s+(?:whatever|what)\s+(?:the\s+)?(?:issue|web\s*page|page|website|email|comment|response|file|ticket|it)\s+"
        r"says\b",
        0.75,
    ),
]

FG8_PATTERNS = [
    (
        r"\b(?:bash|sh|zsh)\s+-c\s+[\"'][^\"'\n]*\$\{?(?:\w*(?:QUERY|INPUT|ARG|USER|SEARCH|TEXT|REQUEST|PARAM|NAME)\w*)\}?",
        0.75,
    ),
    (r"\beval\s+[\"']?\$\{?\w+", 0.7),
    (
        r"\b(?:user(?:'s)?|request|web|page|issue)\s+(?:input|text|query|search\s+text|content)\s+directly\b",
        0.6,
    ),
]

FG9_PATTERNS = [
    (
        r"\b(?:gh\s+pr\s+checkout|git\s+(?:fetch|checkout)\s+[^\n]*pull/\d*|check\s*out\s+(?:the\s+)?"
        r"(?:contributor(?:'s)?\s+)?(?:pr|pull\s+request))\b[^\n]{0,80}?\b(?:make|npm|pnpm|yarn|pytest|"
        r"tox|pip\s+install|python|bash|sh|run|test|build|install)\b",
        0.7,
    ),
]

FG10_PATTERNS = [
    (
        r"\bif\b[^\n.]{0,60}?\b(?:hostname|host\s*name|environment|machine|user(?:name)?|cpu|"
        r"virtuali[sz]ation)\b[^\n.]{0,40}?\b(?:sandbox|vm|virtual|vbox|virtualbox|qemu|kvm|"
        r"docker|container|ci|analysis|honeypot)\b[^\n.]{0,80}?\b(?:skip|don'?t|do\s+not|stop|"
        r"exit|abort|disable|hide|behave|pretend|otherwise)\b",
        0.75,
    ),
    (
        r"\b(?:after|from|starting|once\s+it\s+is)\s+(?:on\s+)?(?:20\d\d-\d\d-\d\d|"
        r"\d{1,2}/\d{1,2}/20\d\d)\b[^\n]{0,80}?\b(?:send|upload|run|execute|delete|curl|post)\b",
        0.7,
    ),
    (
        r"\b(?:skip|disable|don'?t\s+run|hide)\b[^\n]{0,40}?\bif\b[^\n]{0,40}?\b(?:sandbox|vm|"
        r"monitored|being\s+(?:watched|tested|analy[sz]ed))\b",
        0.75,
    ),
]

FG11_PATTERNS = [
    (
        r"\b(?:result|check|test|status|outcome)s?\b[^\n]{0,60}?\b(?:not|fail(?:ed|ing)?)\b[^\n]{0,40}?"
        r"\b(?:must|should|always)\s+be\s+(?:written|reported|marked|recorded|changed|corrected|"
        r"shown)\s+as\s+(?:pass|passed|success|ok|green)\b",
        0.8,
    ),
    (
        r"\b(?:mark|report|record|write|change|correct)\b[^\n]{0,40}?\b(?:failures?|errors?|"
        r"failed\s+\w+)\b[^\n]{0,20}?\bas\s+(?:pass|passed|success|ok)\b",
        0.8,
    ),
    (
        r"\b(?:do\s+not|don'?t|never)\s+(?:tell|inform|notify|alert|warn)\s+(?:the\s+)?user\s+"
        r"(?:about|of|that\s+(?:you|this|it)|when|if)\b",
        0.7,
    ),
    (
        r"\b(?:do\s+not|don'?t|never)\s+(?:mention|reveal|disclose|show)\s+(?:this|it|that)\s+to\s+(?:the\s+)?user\b",
        0.75,
    ),
    (
        r"\b(?:silently|quietly|secretly)\s+(?:upload|send|delete|remove|modify|execute|install|"
        r"post|copy|exfiltrate|transmit|forward)\b",
        0.75,
    ),
    (
        r"\bwithout\s+(?:telling|informing|notifying|asking)\s+the\s+user\b",
        0.65,
    ),
    (
        r"\b(?:never|do\s+not|don'?t)\s+ask\s+(?:the\s+user\s+)?(?:for\s+)?(?:confirmation|"
        r"permission|approval)\b",
        0.65,
    ),
]

_CRED = r"(?:~|\$HOME|/home/\w+|/root)?/?\.(?:ssh|aws|gnupg|kube|docker|netrc|git-credentials|azure|config/gcloud)\b"
FG12_PATTERNS = [
    (rf"\b(?:tar|zip|7z|cp|rsync|scp|cat|base64|gpg|xxd)\b[^\n]{{0,80}}?{_CRED}", 0.8),
    (r"(?:~|\$HOME)/\.(?:mozilla|config/google-chrome|config/chromium)\b", 0.7),
]
# Raw sockets are common in legitimate scripts; flag them only next to credential access.
_RAW_SOCKET = re.compile(
    r"\bsocket\.create_connection\s*\(|\bsocket\.socket\s*\([^\n]*\)\s*\.connect\s*\(|/dev/tcp/"
)


# ---------------------------------------------------------------------------
# FG4: encoded payloads
# ---------------------------------------------------------------------------

_B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{24,}={0,2}(?![A-Za-z0-9+/=])")
_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}){16,}\b")
_BIN = re.compile(r"(?:\b[01]{8}\b[ \t]*){6,}")
_MORSE = re.compile(r"(?:(?<![\w.\-])[.\-]{1,7}(?:[ \t]+|[ \t]*/[ \t]*)){5,}[.\-]{1,7}(?![\w.\-])")
_MORSE_TABLE = {
    ".-": "a",
    "-...": "b",
    "-.-.": "c",
    "-..": "d",
    ".": "e",
    "..-.": "f",
    "--.": "g",
    "....": "h",
    "..": "i",
    ".---": "j",
    "-.-": "k",
    ".-..": "l",
    "--": "m",
    "-.": "n",
    "---": "o",
    ".--.": "p",
    "--.-": "q",
    ".-.": "r",
    "...": "s",
    "-": "t",
    "..-": "u",
    "...-": "v",
    ".--": "w",
    "-..-": "x",
    "-.--": "y",
    "--..": "z",
    "-----": "0",
    ".----": "1",
    "..---": "2",
    "...--": "3",
    "....-": "4",
    ".....": "5",
    "-....": "6",
    "--...": "7",
    "---..": "8",
    "----.": "9",
    ".-.-.-": ".",
    "--..--": ",",
    "-..-.": "/",
    ".--.-.": "@",
    "-....-": "-",
    "..--.-": "_",
    "---...": ":",
    "-.--.": "(",
    "-.--.-": ")",
}
_DECODE_HINT = re.compile(
    r"\b(?:decode|decoded|base64\s+-d|execute|run|calibration|token|setup)\b", re.I
)


def _printable(text: str) -> bool:
    return bool(text) and sum(ch.isprintable() or ch in "\n\t" for ch in text) / len(text) > 0.95


def _decode_candidates(content: str) -> Iterator[tuple[re.Match[str], str, str]]:
    """Yield (match, encoding, decoded_text) for decodable blobs."""
    for match in _B64.finditer(content):
        blob = match.group(0)
        if content[max(0, match.start() - 30) : match.start()].lower().rfind("data:image") != -1:
            continue
        try:
            decoded = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if _printable(decoded):
            yield match, "base64", decoded
    for match in _HEX.finditer(content):
        try:
            decoded = bytes.fromhex(match.group(0)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if _printable(decoded):
            yield match, "hex", decoded
    for match in _BIN.finditer(content):
        try:
            decoded = "".join(chr(int(b, 2)) for b in match.group(0).split())
        except ValueError:
            continue
        if _printable(decoded):
            yield match, "binary", decoded
    for match in _MORSE.finditer(content):
        words = re.split(r"[ \t]*/[ \t]*", match.group(0).strip())
        letters = ["".join(_MORSE_TABLE.get(code, "?") for code in word.split()) for word in words]
        decoded = " ".join(letters)
        if decoded.count("?") <= len(decoded) * 0.1 and len(decoded.replace(" ", "")) >= 4:
            yield match, "morse", decoded


# ---------------------------------------------------------------------------
# FG5: obfuscated shell commands
# ---------------------------------------------------------------------------

_HEX_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})")
_ANSI_C = re.compile(r"\$'((?:[^'\\]|\\.)*)'")
_ASSIGN = re.compile(r"\b([A-Za-z_]\w*)=([\"']?)([^\s;\"']+)\2(?=[\s;])")


def _normalize_shell(line: str) -> str:
    text = _ANSI_C.sub(
        lambda m: _HEX_ESCAPE.sub(lambda h: chr(int(h.group(1), 16)), m.group(1)), line
    )
    text = _HEX_ESCAPE.sub(lambda h: chr(int(h.group(1), 16)), text)
    # Quotes or backslashes inside a word: cu"rl, c'u'rl, c\url
    text = re.sub(r"(?<=\w)[\"'\\]+(?=\w)", "", text)
    text = re.sub(r"(?<=\w)[\"']{2}", "", text)
    # Join simple variables: a=cu; b=rl; $a$b
    variables = {name: value for name, _, value in _ASSIGN.findall(text)}
    if variables:
        text = re.sub(
            r"\$\{?([A-Za-z_]\w*)\}?",
            lambda m: variables.get(m.group(1), m.group(0)),
            text,
        )
    return text


def _hidden_commands(line: str) -> list[str]:
    normalized = _normalize_shell(line)
    if normalized == line:
        return []
    raw_words = set(re.findall(r"[A-Za-z0-9_]+", line.lower()))
    found = []
    for command in _SHELL_COMMANDS:
        if (
            re.search(rf"(?<![\w./-]){command}(?![\w-])", normalized, re.I)
            and command not in raw_words
        ):
            found.append(command)
    return found


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


def _frontmatter_description(content: str) -> tuple[int, str] | None:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", content, re.S)
    if not match:
        return None
    desc = re.search(r"^description:\s*(.+?)(?=^\w[\w-]*:|\Z)", match.group(1), re.M | re.S)
    if not desc:
        return None
    return match.start(1) + desc.start(1), desc.group(1)


def analyze(content: str, file_path: str, file_type: str) -> list[AnalyzerFinding]:
    """Analyze content for the fork's gap rules (FG1-FG12)."""
    findings: list[AnalyzerFinding] = []

    def add(
        rule_id: str,
        severity: Severity,
        start: int,
        text: str,
        confidence: float,
        extra: str | None = None,
    ) -> None:
        name, category, _, _ = _RULES[rule_id]
        findings.append(
            AnalyzerFinding(
                rule_id=rule_id,
                message=name if extra is None else f"{name}: {extra}",
                severity=severity,
                location=Location(file=file_path, start_line=get_line_number(content, start)),
                confidence=confidence,
                tags=[category.value, "fork"],
                context=get_context(content, start),
                matched_text=text[:200],
                complete_match=text,
            )
        )

    def scan(
        rule_id: str,
        patterns: list[tuple[str, float]],
        severity: Severity,
        prose: bool = False,
        check_prohibited: bool = True,
    ) -> None:
        for pattern, confidence in patterns:
            matcher = static_runner.iter_paragraph_matches if prose else re.finditer
            for match in matcher(pattern, content, _I):
                if check_prohibited and is_directly_prohibited(content, match.start(), match.end()):
                    continue
                add(rule_id, severity, match.start(), match.group(0), confidence)

    scan("FG1", FG1_PATTERNS, Severity.HIGH)
    if file_path.rsplit("/", 1)[-1].upper() == "SKILL.MD":
        description = _frontmatter_description(content)
        if description:
            offset, text = description
            for pattern, confidence in FG2_PATTERNS:
                if match := re.search(pattern, text, re.I):
                    add("FG2", Severity.HIGH, offset + match.start(), match.group(0), confidence)
                    break
    scan("FG3", FG3_PATTERNS, Severity.HIGH)
    scan("FG6", FG6_PATTERNS, Severity.MEDIUM, prose=True)
    scan("FG7", FG7_PATTERNS, Severity.HIGH, prose=True)
    scan("FG8", FG8_PATTERNS, Severity.HIGH)
    scan("FG9", FG9_PATTERNS, Severity.HIGH)
    scan("FG10", FG10_PATTERNS, Severity.HIGH, prose=True)
    scan("FG11", FG11_PATTERNS, Severity.HIGH, prose=True)
    before = len(findings)
    scan("FG12", FG12_PATTERNS, Severity.HIGH)
    if len(findings) > before or re.search(_CRED, content, re.I):
        for match in _RAW_SOCKET.finditer(content):
            add("FG12", Severity.HIGH, match.start(), match.group(0), 0.75, "raw socket")

    for match, encoding, decoded in _decode_candidates(content):
        dangerous = _DANGER.search(decoded)
        hinted = _DECODE_HINT.search(content[max(0, match.start() - 120) : match.end() + 120])
        if dangerous:
            add(
                "FG4",
                Severity.HIGH,
                match.start(),
                match.group(0),
                0.85,
                f"{encoding} decodes to {decoded[:120]!r}",
            )
        elif hinted and encoding in {"binary", "morse"}:
            add(
                "FG4",
                Severity.MEDIUM,
                match.start(),
                match.group(0),
                0.6,
                f"{encoding} decodes to {decoded[:120]!r}",
            )

    offset = 0
    for line in content.splitlines(keepends=True):
        hidden = _hidden_commands(line)
        if hidden:
            add(
                "FG5",
                Severity.HIGH,
                offset,
                line.strip(),
                0.8,
                f"hidden {', '.join(hidden)} -> {_normalize_shell(line).strip()[:120]!r}",
            )
        offset += len(line)

    return findings


def node(state: SkillspectorState) -> AnalyzerNodeResponse:
    """Run the fork gap patterns and return findings."""
    response = static_runner.run_static_patterns_with_ledger(state, [sys.modules[__name__]])
    logger.info("%s: %d findings", ANALYZER_ID, len(response["findings"]))
    return response
