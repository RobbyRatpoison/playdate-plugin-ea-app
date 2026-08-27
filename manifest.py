"""
Locates an installed EA game's manifest, purely to drive PlayDate's own
installed-flag/install_path detection (see watcher.py). Install, launch, and
uninstall are no longer handled by PlayDate -- all three just open EA
Desktop and let the user act there (see __init__.py's launch_game()), same
as Lutris's approach, after the native-launch bypass this file used to
support (content_id/exe resolution, OOA/Denuvo licensing) repeatedly hit
launcher-specific dead ends (legacy manifests with no resolvable exe, and
separately, EA Desktop itself rejecting the offerId-based protocol fallback
for reissued classic titles -- see PLUGINS_STATUS.md). Detection stays,
since it doesn't depend on any of that.

Installed games are found via the Wine prefix's own registry: each install
writes 'Software\\EA Games\\<Title>\\Install Dir' (or a Wow6432Node variant) --
this is EA's own manifest format's canonical discovery mechanism, not a
Maxima-specific convention. An earlier version of this file assumed
manifests lived under a central 'ProgramData/EA/EA Desktop/InstallData/
<offer_id>' directory (matching watcher.py's pre-existing, never-verified
assumption for its own installed-flag detection) -- confirmed wrong via a
real install, which never wrote anything there at all.

Manifest shape and the registry-path-expression quirk in filePath values
were derived by reading github.com/ArmchairDevelopers/Maxima (GPL-3) as a
reference; this is an independent implementation, not a port of its code.
Validated against two real installerdata.xml samples (a legacy-format
Peggle manifest and a modern DiPManifest v4.0 Battlefield V manifest,
including the Wine-registry-relative filePath resolution and Z:-drive
edge case) -- both real-world tests already caught and fixed genuine bugs
(trial detection used 'true'/'false', real manifests use '1'/'0'; drive-
letter resolution assumed everything lived under drive_c, but a game
installed outside the prefix uses Wine's Z: mapping to the host root).
"""

import logging
import os
import re
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

_REG_HEADER_RE = re.compile(r'^\[(.+?)\]')
_REG_VALUE_RE  = re.compile(r'^"((?:[^"\\]|\\.)*)"=(.*)$')


_REG_UNICODE_ESCAPE_RE = re.compile(r'\\x([0-9a-fA-F]{4})')


def _unescape_reg(s: str) -> str:
    s = s.replace('\\"', '"')
    # Confirmed against a real registry value: non-ASCII characters (e.g. the
    # (tm) symbol in "Battlefield\x2122 V") are \xHHHH-escaped, distinct from
    # the backslash-doubling used for path separators.
    s = _REG_UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
    return s.replace('\\\\', '\\')


def _read_reg_file(path: str, out: dict):
    if not os.path.isfile(path):
        return
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            section = ''
            for line in f:
                line = line.strip()
                if not line or line.startswith((';', '#')):
                    continue
                m = _REG_HEADER_RE.match(line)
                if m:
                    section = _unescape_reg(m.group(1))
                    continue
                m = _REG_VALUE_RE.match(line)
                if m and section:
                    key = _unescape_reg(m.group(1))
                    raw_value = m.group(2).strip()
                    if raw_value.startswith('"') and raw_value.endswith('"'):
                        raw_value = _unescape_reg(raw_value[1:-1])
                    full_key = f'{section}\\{key}'.replace('\\\\', '\\').lower()
                    out[full_key] = raw_value
    except OSError as e:
        log.warning('manifest: failed to read registry file %s: %s', path, e)


def _load_prefix_registry(prefix: str) -> dict:
    reg = {}
    _read_reg_file(os.path.join(prefix, 'system.reg'), reg)
    _read_reg_file(os.path.join(prefix, 'user.reg'), reg)
    return reg


def _normalize_reg_key(key: str) -> str:
    key = key.lower()
    if key.startswith('hkey_local_machine\\'):
        key = key[len('hkey_local_machine\\'):]
    elif key.startswith('hkey_current_user\\'):
        key = key[len('hkey_current_user\\'):]
    return key


def _read_reg_value(prefix: str, key: str) -> str | None:
    """key is 'Sub\\Key\\Path\\ValueName' (HKLM/HKCU prefix optional)."""
    registry = getattr(_read_reg_value, '_cache', {}).get(prefix)
    if registry is None:
        registry = _load_prefix_registry(prefix)
        cache = getattr(_read_reg_value, '_cache', {})
        cache[prefix] = registry
        _read_reg_value._cache = cache

    normalized = _normalize_reg_key(key)
    if normalized in registry:
        return registry[normalized]
    wow_key = normalized.replace('software\\', 'software\\wow6432node\\', 1)
    return registry.get(wow_key)


def invalidate_registry_cache(prefix: str):
    getattr(_read_reg_value, '_cache', {}).pop(prefix, None)


def _resolve_registry_path(prefix: str, expr: str) -> str | None:
    """
    Resolve a filePath value from installerdata.xml. Either a literal
    (Windows-style) path, or '[Sub\\Key\\Path\\ValueName]suffix\\path.exe'
    where the bracketed part is a registry value to look up first.

    Registry values holding install paths carry a drive letter (typically
    C:, but confirmed via a real install that it can be anything -- e.g. Z:
    when the game was installed outside the prefix, which Wine maps to the
    host's '/' by default). Resolved via the prefix's own dosdevices/
    symlinks rather than assuming everything lives under drive_c.
    """
    if '[' in expr and ']' in expr:
        key_part, _, suffix = expr.partition(']')
        key_part = key_part.lstrip('[')
        value = _read_reg_value(prefix, key_part)
        if value is None:
            log.warning('manifest: registry key not found for filePath expression: %r', expr)
            return None
        base = value.replace('\\', '/').rstrip('/')
        suffix = suffix.replace('\\', '/').lstrip('/')
        windows_path = f'{base}/{suffix}' if suffix else base
    else:
        windows_path = expr.replace('\\', '/')

    return _resolve_windows_path(prefix, windows_path)


def _resolve_windows_path(prefix: str, windows_path: str) -> str:
    m = re.match(r'^([A-Za-z]):/(.*)$', windows_path)
    if not m:
        # No drive letter -- treat as already relative to drive_c (the
        # common case for paths embedded directly in a manifest).
        return _case_insensitive_join(os.path.join(prefix, 'drive_c'), windows_path)

    drive, rel = m.group(1).lower(), m.group(2)
    link = os.path.join(prefix, 'dosdevices', f'{drive}:')
    try:
        drive_root = os.path.realpath(link)
    except OSError:
        drive_root = None
    if not drive_root or not os.path.isdir(drive_root):
        # Fall back to Wine's own default mappings if the symlink is
        # missing for some reason: c: -> drive_c, z: -> host root.
        drive_root = os.path.join(prefix, 'drive_c') if drive == 'c' else '/'

    return _case_insensitive_join(drive_root, rel)


def _case_insensitive_join(base: str, rel_path: str) -> str:
    """Best-effort case-insensitive resolution of rel_path under base --
    Wine's filesystem is case-sensitive but manifest paths use Windows casing
    that may not exactly match what's on disk."""
    current = base
    for part in [p for p in rel_path.split('/') if p]:
        candidate = os.path.join(current, part)
        if os.path.exists(candidate):
            current = candidate
            continue
        try:
            match = next((e.name for e in os.scandir(current) if e.name.lower() == part.lower()), None)
        except OSError:
            match = None
        current = os.path.join(current, match or part)
    return current


_INSTALL_DIR_KEY_RE = re.compile(r'^software\\(?:wow6432node\\)?[^\\]+\\[^\\]+\\install dir$')


def _iter_ea_games_install_dirs(prefix: str):
    """
    Yield resolved install directories for every 'Software\\<Company>\\<Title>'
    (or Wow6432Node variant) registry entry with an 'Install Dir' value.

    This replaces an earlier, unverified assumption that installed games are
    listed under a central 'ProgramData/EA/EA Desktop/InstallData/<offer_id>'
    directory -- confirmed via a real install that nothing gets written
    there at all (at least not by every install path); the per-title
    'Install Dir' registry key, referenced directly by both a legacy and a
    modern real installerdata.xml's own <uninstall>/filePath registry-path
    expressions, is what EA's manifest format itself treats as canonical.

    <Company> is NOT always 'EA Games' -- confirmed for real with SteamWorld
    Dig, a third-party title, which registers under
    'Software\\Wow6432Node\\Image & Form\\SteamWorld Dig' (the actual
    developer's name) rather than an EA-branded key. Matching any company
    name here is safe because the real filter is the exact-contentID
    cross-check in find_manifest_path_by_content_id() below, not this scan --
    an unrelated program's own 'Install Dir' entry simply won't have a
    matching __Installer/installerdata.xml and gets skipped harmlessly.
    """
    registry = getattr(_read_reg_value, '_cache', {}).get(prefix)
    if registry is None:
        registry = _load_prefix_registry(prefix)
        cache = getattr(_read_reg_value, '_cache', {})
        cache[prefix] = registry
        _read_reg_value._cache = cache

    for key, value in registry.items():
        if _INSTALL_DIR_KEY_RE.match(key) and value:
            resolved = _resolve_windows_path(prefix, value.replace('\\', '/'))
            if resolved and os.path.isdir(resolved):
                yield resolved


def find_manifest_path_by_content_id(prefix: str, content_id: str) -> tuple[str, str] | None:
    """
    Returns (install_dir, manifest_path) for the install directory whose
    __Installer/installerdata.xml lists content_id, or None. Exact numeric
    match against the manifest's own <contentIDs> list -- deliberately not
    fuzzy title matching, to avoid ever resolving to the wrong game.
    """
    if not content_id:
        return None
    for install_dir in _iter_ea_games_install_dirs(prefix):
        manifest_path = _case_insensitive_join(install_dir, '__Installer/installerdata.xml')
        if not os.path.isfile(manifest_path):
            continue
        try:
            root = ET.parse(manifest_path).getroot()
        except ET.ParseError:
            continue
        ids = {el.text.strip() for el in root.iter('contentID') if el.text}
        if content_id in ids:
            return install_dir, manifest_path
    return None


_TITLE_NORM_RE = re.compile(r'[^\w\s]', re.UNICODE)


def _normalize_title(name: str) -> str:
    s = _TITLE_NORM_RE.sub('', name or '')
    return re.sub(r'\s+', ' ', s).strip().lower()


def find_install_dir_by_name(prefix: str, game_name: str) -> str | None:
    """Session-free fallback for find_manifest_path_by_content_id(): match an
    install's own local manifest <title> (normalized -- manifests carry the
    branded form, e.g. 'Bejeweled® 3', against a DB name like 'Bejeweled 3')
    instead of the strict content_id cross-check, which needs a live,
    authenticated content_id lookup that install/launch no longer require.
    Purely local (registry read + XML parse, no network), so cheap enough to
    run on every install-status sync (watcher event, startup, or
    resync_installed()) unlike the content_id path.

    Used only to set the installed flag/install_path for display -- never to
    decide which exe actually runs. A real launch still goes through the
    exact content_id match when a session is available (native launch
    bypass), or the EA Desktop protocol hand-off otherwise, neither of which
    uses this function."""
    target = _normalize_title(game_name)
    if not target:
        return None
    for install_dir in _iter_ea_games_install_dirs(prefix):
        manifest_path = _case_insensitive_join(install_dir, '__Installer/installerdata.xml')
        if not os.path.isfile(manifest_path):
            continue
        try:
            root = ET.parse(manifest_path).getroot()
        except ET.ParseError:
            continue
        # Two different manifest schemas carry the display title -- a flat
        # <title> (e.g. Bejeweled 3's manifest) and a multi-locale
        # <gameTitles><gameTitle locale="..."> (e.g. Dungeon Keeper's, a
        # different DiPManifest v4.0 variant despite the identical version
        # number) -- confirmed live that relying on only one missed the other.
        titles = {el.text.strip() for el in root.iter('title') if el.text}
        titles |= {el.text.strip() for el in root.iter('gameTitle') if el.text}
        if any(_normalize_title(t) == target for t in titles):
            return install_dir
    return None


