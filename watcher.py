import json
import logging
import os

from runners.watcher import PluginInstallWatcher

log = logging.getLogger(__name__)

# EA App's own docs describe install manifests living under
# %PROGRAMDATA%\EA\EA Desktop\InstallData\, but nothing has ever been
# observed to write there in practice (see manifest.py's module docstring).
# The reliable per-game location is install_path/platform_executable,
# stored on the games row itself at install time by download.py (the same
# generic columns other plugins like GOG already use for this).
_EA_GAMES_SUBPATH = os.path.join(
    'drive_c', 'Program Files', 'EA Games'
)


def _get_wine_prefix():
    try:
        from config import CONFIG_PATH
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
        return cfg.get('launchers', {}).get('ea_app', {}).get('prefix', '').strip() or None
    except Exception:
        return None


def _get_wine_install_base():
    """Real EA Games directory inside the prefix -- unlike InstallData, this
    one actually receives filesystem events when a game installs/uninstalls."""
    prefix = _get_wine_prefix()
    return os.path.join(prefix, _EA_GAMES_SUBPATH) if prefix else None


def _install_present(install_path, exe_rel=None):
    """Whether an EA game is *actually* installed at install_path -- not just
    whether the directory still exists.

    EA's Cleanup.exe removes the game payload and the __Installer/ metadata
    dir on uninstall, but routinely leaves the top-level game folder in place
    afterwards -- as an empty shell (just game.ico) or leftover DRM-activation
    stubs. A bare os.path.isdir() check therefore reports these classic titles
    as installed forever; confirmed for Bejeweled 3, Crusader: No Remorse,
    SimCity 2000, Syndicate (1993) and Theme Hospital.

    Positive signals, strongest first:
      1. the recorded platform_executable still exists;
      2. __Installer/installerdata.xml exists -- EA's own canonical "installed"
         artifact (see manifest.py: both install-dir matchers require it),
         which Cleanup.exe deletes.
    Absence of both means uninstalled.
    """
    if not install_path or not os.path.isdir(install_path):
        return False
    if exe_rel and os.path.isfile(os.path.join(install_path, exe_rel)):
        return True
    from .manifest import _case_insensitive_join
    return os.path.isfile(_case_insensitive_join(install_path, '__Installer/installerdata.xml'))


def sync_ea_install_status():
    """
    Verify each EA game's installed flag against install_path/platform_executable
    (set at install time by download.py's native install path -- see its own
    comment: "Stored so uninstall_game() ... can find this install later
    without needing an EA session/content_id API round trip"). This detects
    both installs AND uninstalls for anything installed through PlayDate
    itself.

    Games with no stored install_path (installed via the real EA Desktop
    client directly -- now the default install path, see __init__.py's
    launch_game -- or pre-existing installs from before this project touched
    them) are matched two ways, both purely local (registry read + XML
    parse, no network -- cheap enough to run on every filesystem event):

    1. Exact content_id match (manifest.find_manifest_path_by_content_id)
       when platform_appname is populated -- the Juno library sync already
       fetches each game's contentId in the same batch query as its name/
       art, it just wasn't being persisted before; now stored in
       platform_appname (same generic column Epic uses for its appName) and
       backfilled opportunistically by ea.scrape_single() for games synced
       before this. As precise as the native-launch exe-decision check.
    2. Fuzzy manifest-<title>/<gameTitle> name match
       (manifest.find_install_dir_by_name) when no content_id is on record
       yet. Confirmed live this misses titles where the DB's disambiguated
       display name (e.g. "SimCity 2000" + a franchise-reboot suffix EA's
       catalog adds) differs from the manifest's own plainer internal title
       (e.g. "SimCity 2000 Special Edition") -- a real, accepted gap that
       content_id backfilling resolves over time as games get rescraped.

    Once matched either way, install_path is backfilled so future watcher
    events / resync_installed() calls use the cheap direct check above
    instead of re-scanning every time.

    Called: once at startup (before the watcher attaches), on every
    filesystem event under the EA Games dir (see _watcher below), and via
    resync_installed() -- which bulk_rescrape_games() calls for every
    touched non-Steam platform once metadata (e.g. platform_appname/
    content_id) has actually changed, since that's not a filesystem event
    the watcher would ever see.
    """
    from database import get_db
    from . import manifest

    prefix = _get_wine_prefix()

    db = get_db()
    try:
        rows = db.execute(
            "SELECT appid, name, install_path, platform_executable, platform_appname, installed "
            "FROM games WHERE platform = 'ea_app'"
        ).fetchall()
        for row in rows:
            install_path = row['install_path']
            exe_rel      = row['platform_executable']

            if install_path:
                still_installed = _install_present(install_path, exe_rel)
                new_flag = 1 if still_installed else 0
                if new_flag != row['installed']:
                    db.execute("UPDATE games SET installed = ? WHERE appid = ?", (new_flag, row['appid']))
                if not still_installed:
                    # Drop the stale path so the manifest-name re-match branch
                    # below can re-detect a later reinstall (EA may put it in a
                    # different dir). The leftover shell Cleanup.exe leaves
                    # behind has no installerdata.xml, so it won't re-match.
                    db.execute("UPDATE games SET install_path = NULL WHERE appid = ?", (row['appid'],))
            elif prefix and not row['installed']:
                found = None
                if row['platform_appname']:
                    match = manifest.find_manifest_path_by_content_id(prefix, row['platform_appname'])
                    found = match[0] if match else None
                    if found:
                        log.info(f'EA install sync: matched {row["name"]!r} by content_id -> {found}')
                if not found:
                    found = manifest.find_install_dir_by_name(prefix, row['name'])
                    if found:
                        log.info(f'EA install sync: matched {row["name"]!r} by manifest title -> {found}')
                if found:
                    db.execute(
                        "UPDATE games SET installed = 1, install_path = ? WHERE appid = ?",
                        (found, row['appid']),
                    )
        db.commit()
    finally:
        db.close()


# recursive + file events: EA's Cleanup.exe uninstalls a classic title by
# gutting its game folder (removing __Installer/ and the payload) while
# leaving the now-empty folder in place -- a top-level directory-only watch
# never sees that. debounce coalesces the event storm from a game
# install/uninstall into one sync once the filesystem settles.
_watcher = PluginInstallWatcher(
    'ea_app', sync_ea_install_status,
    recursive=True, watch_files=True, debounce_seconds=1.5,
)


def start_ea_watcher(watch_path: str):
    _watcher.start(watch_path)


def stop_ea_watcher():
    _watcher.stop()
