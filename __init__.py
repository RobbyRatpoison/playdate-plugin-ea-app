import logging
import os
import sys

log = logging.getLogger(__name__)


def _find_native_launcher():
    """Return path to EADesktop.exe on Windows, or None."""
    if sys.platform != 'win32':
        return None
    from runners.windows import find_installed_exe
    exe = find_installed_exe('EA app', 'EADesktop.exe')
    if exe:
        return exe
    # Fallback: standard install-path guesses, in case the registry
    # DisplayName doesn't match ('EA app' vs an older 'EA Desktop' release).
    candidates = []
    for env in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'LOCALAPPDATA'):
        base = os.environ.get(env, '')
        if base:
            candidates.append(os.path.join(base, 'Electronic Arts', 'EA Desktop', 'EADesktop.exe'))
            candidates.append(os.path.join(base, 'EA Desktop', 'EADesktop.exe'))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _find_wine_exe(prefix, exe_name):
    """
    Return the path to exe_name inside the EA Desktop Wine prefix dir.
    Prefers the non-versioned 'EA Desktop/EA Desktop/' path for EALauncher.exe,
    then searches versioned subdirectories (excluding compatibility32).
    """
    ea_base = os.path.join(prefix, 'drive_c', 'Program Files', 'Electronic Arts', 'EA Desktop')
    if not os.path.isdir(ea_base):
        return None
    preferred = os.path.join(ea_base, 'EA Desktop', exe_name)
    if os.path.isfile(preferred):
        return preferred
    for root, dirs, files in os.walk(ea_base):
        dirs[:] = [d for d in dirs if d.lower() != 'compatibility32']
        if exe_name in files:
            return os.path.join(root, exe_name)
    return None


def _is_ea_desktop_running():
    """Return True if EALocalHostSvc has bound port 3216 (EA Desktop is up and ready).

    ss lives on the host, not inside the Flatpak sandbox -- route through
    host_run like every other process/prefix check in the codebase, or this
    raises FileNotFoundError under Flatpak every time it's called (confirmed
    live 2026-09-04: broke Start Launcher outright, and would equally have
    broken this "already open, hand off there" check during launch/uninstall)."""
    from runners.sandbox import host_run
    try:
        result = host_run(['ss', '-tlnp'], capture_output=True, text=True)
    except Exception as e:
        log.warning(f'EA: _is_ea_desktop_running check failed: {e}')
        return False
    return ':3216' in result.stdout


def _handoff_result(action, message):
    """
    A successful hand-off to EA App is not a completed launch/install/
    uninstall, so it must not be reported as 'status': 'success' -- core's
    two consumers of this both treat that literally:
    launchGame() (base.html) only shows the message and skips its
    post-launch side effects (gamepad suppression, card patch, last-played
    reorder) for 'installing'/'not_installed'/'error'/'not_supported';
    doPluginUninstall() (_ctx_menu.html) shows "Game uninstalled" and
    reloads the page on 'success', for anything else it just shows our
    message. 'installing' satisfies both (it's in the first set, and isn't
    literal 'success' for the second) -- it's not a perfect semantic fit
    for launch/uninstall, but it's the established token other plugins
    already use for "handed off elsewhere, not done yet", and reusing it
    beats inventing a one-off status nothing else recognizes.
    """
    return {'status': 'installing', 'message': message}


def _open_ea_app(action, game_name):
    """
    Install, launch, and uninstall for EA games are all the same action
    from PlayDate's side: open EA App and let the user do it there (same
    approach Lutris takes). Earlier versions tried to do more -- a
    from-scratch native downloader, a native LSX launch bypass with
    OOA/Denuvo licensing, an offerId-keyed origin2:// protocol hand-off to
    EA Desktop -- and each ran into a launcher-specific dead end (legacy
    manifests with no resolvable exe; EA Desktop itself rejecting the
    offerId hand-off for reissued classic titles with a "belongs to
    another user" error, confirmed live, unrelated to actual ownership).
    None of that is needed just to get the user in front of their library.

    action is a verb for the returned message ('launch'/'install'/'uninstall').
    """
    log.info(f'EA {action}: {game_name!r} -- opening EA App')

    try:
        if sys.platform == 'win32':
            native = _find_native_launcher()
            if not native:
                return {'status': 'error', 'message': 'EA App not installed'}
            os.startfile(native)
        else:  # Linux — Wine (EA App has no macOS version)
            import json
            from config import CONFIG_PATH
            try:
                with open(CONFIG_PATH, 'r') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            launcher_cfg = cfg.get('launchers', {}).get('ea_app', {})
            prefix   = launcher_cfg.get('prefix', '').strip()
            wine_bin = launcher_cfg.get('wine_bin', '').strip() or None

            if not prefix:
                log.warning(f'EA {action}: no Wine prefix configured')
                return {
                    'status':  'error',
                    'message': 'EA App not configured. Open Plugins → Manage to set up Wine.',
                }
            if not os.path.isdir(prefix):
                log.warning(f'EA {action}: prefix does not exist: {prefix!r}')
                return {
                    'status':  'error',
                    'message': f'Wine prefix not found: {prefix}',
                }
            from runners.wine import find_wine_binary, find_proton_wine, is_proton_wine, install_dxvk, run_in_prefix
            if not wine_bin:
                wine_bin = find_proton_wine() or find_wine_binary()
            if not wine_bin:
                log.warning(f'EA {action}: no Wine binary found')
                return {'status': 'error', 'message': 'No Wine or Proton binary found'}

            ea_desktop_exe = _find_wine_exe(prefix, 'EADesktop.exe')
            if not ea_desktop_exe:
                log.warning(f'EA {action}: EADesktop.exe not found in prefix {prefix!r}')
                return {'status': 'error', 'message': 'EA App not installed in Wine prefix'}

            if _is_ea_desktop_running():
                return _handoff_result(action, f'EA App is already open — {action} "{game_name}" from there.')

            if is_proton_wine(wine_bin):
                try:
                    install_dxvk(prefix, wine_bin)
                except Exception as e:
                    log.warning(f'EA {action}: DXVK install skipped: {e}')

            env_extra = {
                'WINEDEBUG':        '-all',
                # Disable winegstreamer to avoid aborting on unimplemented
                # winegstreamer_create_color_converter under Wine/Proton.
                'WINEDLLOVERRIDES': 'winegstreamer=',
                # DXVK tuning for CEF GPU process stability.
                'DXVK_ASYNC':       '1',
                'DXVK_LOG_LEVEL':   'none',
            }
            if is_proton_wine(wine_bin):
                env_extra.update({'WINEFSYNC': '1', 'WINEESYNC': '1'})
            # Explicitly select NVIDIA Vulkan ICD if available, to ensure
            # DXVK uses the discrete GPU rather than a software fallback.
            nvidia_icd = '/usr/share/vulkan/icd.d/nvidia_icd.x86_64.json'
            if os.path.exists(nvidia_icd):
                env_extra['VK_ICD_FILENAMES'] = nvidia_icd

            # Run directly, not wrapped in `wine explorer /desktop=...`
            # (a Wine virtual-desktop container) -- that wrapping had no
            # documented reason anywhere in this codebase, and confirmed
            # live it nests every EA Desktop window inside one big
            # container window. Running the exe directly lets Wine map
            # its windows normally.
            #
            # Goes through run_in_prefix -- not a raw subprocess.Popen(
            # [wine_bin, ...]) -- so Proton gets wrapped in umu-run (a bare
            # Proton wine_bin can't run anything standalone, see CLAUDE.md)
            # and, under Flatpak, the process actually starts on the host
            # instead of inside the sandbox. Confirmed live 2026-09-04: the
            # raw-Popen version silently launched-and-died with no error
            # (stdout/stderr went to DEVNULL) every time this ran under the
            # Flatpak build with a Proton wine_bin -- it had never been
            # exercised before because the _is_ea_desktop_running() ss bug
            # above (now also fixed) always failed first.
            log.info(f'EA {action}: starting EA App')
            run_in_prefix(prefix, ea_desktop_exe, args=['-ls=Launcher'],
                          wine_bin=wine_bin, env_extra=env_extra)
    except RuntimeError as e:
        log.error(f'EA {action}: RuntimeError: {e}')
        return {'status': 'error', 'message': str(e)}
    except Exception as e:
        log.error(f'EA {action}: unexpected error: {e}', exc_info=True)
        return {'status': 'error', 'message': f'Failed to open EA App: {e}'}

    return _handoff_result(action, f'Opening EA App — {action} "{game_name}" from there.')


class EAAppPlugin:
    id       = 'ea_app'
    name     = 'EA App'
    platform = 'ea_app'
    label    = 'EA App'

    def register(self, app):
        from .routes import bp
        app.register_blueprint(bp)
        log.info('EA App plugin registered')

    def on_startup(self):
        from .watcher import sync_ea_install_status, start_ea_watcher, _get_wine_install_base
        try:
            sync_ea_install_status()
            log.info('EA App install status synced on startup')
        except Exception as e:
            log.warning(f'Startup EA App install sync failed: {e}')
        if sys.platform != 'win32':
            install_base = _get_wine_install_base()
            if install_base:
                start_ea_watcher(install_base)

    def resync_installed(self):
        from .watcher import sync_ea_install_status
        sync_ea_install_status()

    def on_shutdown(self):
        from .watcher import stop_ea_watcher
        stop_ea_watcher()

    def on_uninstall(self):
        from .ea import clear_ea_tokens
        clear_ea_tokens()

    def launch_game(self, appid):
        from database import get_db

        db  = get_db()
        row = db.execute(
            "SELECT name, installed FROM games WHERE appid = ?", (appid,)
        ).fetchone()
        db.close()

        if not row:
            log.error(f'EA launch: appid {appid} not found in DB')
            return {'status': 'error', 'message': 'EA game not found'}

        action = 'launch' if row['installed'] else 'install'
        return _open_ea_app(action, row['name'])

    def uninstall_game(self, appid):
        from database import get_db

        db  = get_db()
        row = db.execute("SELECT name FROM games WHERE appid = ?", (appid,)).fetchone()
        db.close()

        if not row:
            log.error(f'EA uninstall: appid {appid} not found in DB')
            return {'status': 'error', 'message': 'EA game not found'}

        return _open_ea_app('uninstall', row['name'])

    def start_launcher(self):
        """Open EA App with no specific game -- same underlying open as
        launch/install/uninstall (action/game_name only affect the log
        message), exposed directly for reaching the app on its own."""
        return _open_ea_app('start', 'EA App')

    def launcher_status(self):
        if sys.platform == 'win32':
            native = _find_native_launcher()
            if native:
                return {'available': True, 'detail': 'EA App detected'}
            return {'available': False, 'detail': 'EA App not installed'}

        # Linux: require Wine prefix with EADesktop.exe
        import json
        from config import CONFIG_PATH
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        launcher_cfg = cfg.get('launchers', {}).get('ea_app', {})
        prefix   = launcher_cfg.get('prefix', '').strip()
        wine_bin = launcher_cfg.get('wine_bin', '').strip()

        from runners.wine import find_wine_binary, find_proton_wine
        if not wine_bin:
            wine_bin = find_proton_wine() or find_wine_binary()

        if not wine_bin:
            return {'available': False, 'detail': 'No Wine or Proton binary found'}
        if not prefix:
            return {'available': False, 'detail': 'Wine prefix not configured'}
        if not os.path.isdir(prefix):
            return {'available': False, 'detail': f'Prefix not found: {prefix}'}

        if _find_wine_exe(prefix, 'EADesktop.exe'):
            return {'available': True, 'detail': 'Launcher ready'}

        return {
            'available': False,
            'detail': 'EADesktop.exe not found in prefix — install EA App in Wine',
        }

    def js_api(self):
        return {
            'uninstall_url':     '/api/ea_app/uninstall/{appid}',
            'uninstall_confirm': 'Open EA App to uninstall this game? PlayDate does not uninstall EA games directly — you finish it there.',
            'scrape_url':        '/api/ea_app/scrape-single/{appid}',
            'scrape_method':     'POST',
            'store_url':         'https://www.ea.com/games/{slug}',
            'store_label':       'View on EA App ↗',
            'appid_label':       'EA Offer ID:',
            'sync_label':        'Sync EA Library',
        }

    def manage_ui(self):
        native = _find_native_launcher()
        if native:
            launcher_section = {
                'title': 'Launcher',
                'items': [
                    {'type': 'text', 'content': 'EA App is installed — no additional setup needed.'},
                    {'type': 'button', 'label': 'Start Launcher', 'action': {
                        'type': 'call', 'fn': 'eaStartLauncher',
                    }},
                ],
            }
        elif sys.platform == 'win32':
            launcher_section = {
                'title': 'Launcher',
                'items': [
                    {'type': 'text', 'content': 'EA App is not installed.'},
                    {'type': 'button', 'label': 'Download EA App', 'action': {
                        'type': 'open_url', 'url': 'https://www.ea.com/ea-app',
                    }},
                ],
            }
        else:
            items = [
                {'type': 'text', 'content': 'Set the Wine binary and prefix where EA App is installed.'},
                {'type': 'launcher_config'},
            ]
            import json
            from config import CONFIG_PATH
            try:
                with open(CONFIG_PATH, 'r') as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
            prefix = (cfg.get('launchers', {}).get('ea_app', {}).get('prefix') or '').strip()
            if prefix and _find_wine_exe(prefix, 'EADesktop.exe'):
                items.append({'type': 'text', 'content':
                              'EA App is installed. Use "Start Launcher" to open it directly -- useful to '
                              'check for updates or browse the library there.'})
                items.append({'type': 'button', 'label': 'Start Launcher', 'action': {
                    'type': 'call', 'fn': 'eaStartLauncher',
                }})
            launcher_section = {'title': 'Launcher', 'items': items}

        sections = [
            {
                'title': 'Account',
                'auth': {
                    'endpoint': '/api/ea_app/status',
                    'disconnected': [
                        {'type': 'text', 'content': 'Connect your EA account to import your library.'},
                        {'type': 'button', 'label': 'Connect EA Account', 'action': {
                            'type': 'oauth_paste',
                            'title': 'Connect EA Account',
                            'url_endpoint': '/api/ea_app/token-url',
                            'callback_endpoint': '/api/ea_app/callback',
                            'instructions': [
                                'Click <strong>Open EA Token Page</strong> — your browser opens.',
                                'If you are signed in to EA, you will see a JSON response or be redirected to an address containing <code>access_token=</code>. Paste the whole thing (or just the token) below.',
                                'If you see <code>login_required</code>, you are not signed in to EA in that browser.',
                                'If you are not logged in, sign in at <a href="https://www.ea.com" target="_blank">ea.com</a> first, then click Open again.',
                            ],
                            'input_placeholder': 'Paste access_token value here',
                            'open_label': 'Open EA Token Page',
                            'submit_label': 'Connect',
                        }},
                    ],
                    'connected': [
                        {'type': 'connected_label'},
                        {'type': 'button', 'label': 'Sync Library', 'action': {'type': 'call', 'fn': 'eaSync'}},
                        {'type': 'button', 'label': 'Import Purchase Dates', 'action': {'type': 'call', 'fn': 'eaImportDates'}},
                        {'type': 'button', 'label': 'Disconnect', 'variant': 'muted', 'action': {
                            'type': 'post', 'endpoint': '/api/ea_app/disconnect',
                            'on_success': 'refresh_auth',
                        }},
                        {'type': 'status_output', 'key': 'main'},
                    ],
                },
            },
            launcher_section,
        ]

        # Games folder button only applies to the Linux native install path
        # (install_dir lives inside the configured Wine prefix) -- Windows
        # launch is a plain os.startfile(url) with no PlayDate-managed
        # install location to open.
        if sys.platform != 'win32':
            sections.append({
                'title': 'Games Folder',
                'items': [
                    {'type': 'text', 'content': 'EA games install to EA Games\\ inside the configured Wine prefix.'},
                    {'type': 'button', 'label': 'Open Folder', 'action': {
                        'type': 'call', 'fn': 'eaOpenGamesFolder',
                    }},
                    {'type': 'status_output', 'key': 'folder'},
                ],
            })

        sections.append({
            'title': 'Library',
            'items': [
                {'type': 'text', 'content': 'Fix store page links for games where the URL does not resolve correctly. Does not require an EA account.'},
                {'type': 'button', 'label': 'Fix Store Links', 'action': {
                    'type': 'call', 'fn': 'eaFixSlugs',
                }},
            ],
        })

        return {'sections': sections}

    date_import_url = 'https://myaccount.ea.com/am/ui/payment-wallet/order-history'

    def fetch_description(self, appid, platform_id):
        from .ea import fetch_description
        return fetch_description(appid, platform_id)

    def rescrape(self, appid):
        from .ea import scrape_single
        return scrape_single(appid) or None

    art_kinds = ('vertical', 'horizontal')
    # EA's own sync tries SteamGridDB first and its CDN as the fallback; keep that.
    art_default_order = ('sgdb', 'store', 'steam')

    def art_urls(self, appid):
        from .ea import art_urls
        return art_urls(appid)

    def fragments(self):
        return {
            'tools_scripts': 'ea_app_tools_scripts.html',
        }


plugin = EAAppPlugin()
