import logging
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.components as components
import pwnagotchi.ui.view as view
from flask import Response
import random
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        import toml as tomllib

class SATpwn(plugins.Plugin):
    __author__ = 'Renmeii x Mr-Cass-Ette and discoJack too '
    __version__ = 'x88.1.0-fixed'
    __license__ = 'GPL3'
    __description__ = 'SATpwn intelligent targeting system with exclusive mode and proper plugin registration'

    # Class constants
    AP_EXPIRY_SECONDS = 3600 * 48
    CLIENT_EXPIRY_SECONDS = 3600 * 24
    ATTACK_SCORE_THRESHOLD = 50
    ATTACK_COOLDOWN_SECONDS = 300
    SUCCESS_BONUS_DURATION_SECONDS = 1800
    SCORE_DECAY_PENALTY_PER_HOUR = 5
    PMKID_FRIENDLY_APS_THRESHOLD = 3
    PMKID_FRIENDLY_BOOST_FACTOR = 1.5
    HANDSHAKE_WEIGHT = 10
    CLIENT_WEIGHT = 1
    SCORE_RECALCULATION_INTERVAL_SECONDS = 30
    EXPLORATION_PROBABILITY = 0.1
    DRIVE_BY_AP_EXPIRY_SECONDS = 1800
    DRIVE_BY_CLIENT_EXPIRY_SECONDS = 900
    DRIVE_BY_ATTACK_SCORE_THRESHOLD = 20
    DRIVE_BY_ATTACK_COOLDOWN_SECONDS = 60

    STATIONARY_SECONDS = 3600
    ACTIVITY_THRESHOLD = 5
    ACTIVITY_WINDOW_SECONDS = 300

    def __init__(self):
        # Initialize plugin state
        self.ready = False
        self.agent = None
        self.memory = {}
        self.modes = ['strict', 'loose', 'drive-by', 'recon', 'auto']
        self.memory_path = '/etc/pwnagotchi/SATpwn_memory.json'
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.mode = self.modes[0]
        self.channel_stats = {}
        self.memory_is_dirty = True
        self.recon_channel_iterator = None
        self.recon_channels_tested = []

        # Activity tracking
        self._last_activity_check = 0
        self._activity_history = []
        self.home_whitelist = set()
        self._current_auto_submode = None
        self._stationary_start = None

        # Plugin control
        self.plugin_enabled = True
        self.disable_defaults = False
        self.disabled_plugins = []

        # Initialize plugin with logging
        self.running = False
        logging.info("[SATpwn] Plugin initializing...")
        self._load_config()

        # Mark as properly initialized
        if self.plugin_enabled:
            self.running = True
            logging.info("[SATpwn] Plugin initialization complete")
        else:
            logging.info("[SATpwn] Plugin disabled via configuration")

    def _load_config(self):
        """Load configuration from TOML including plugin enable/disable, whitelist and disable_defaults setting."""
        try:
            config_path = "/etc/pwnagotchi/config.toml"

            if not os.path.exists(config_path):
                logging.info("[SATpwn] No config.toml found - using defaults")
                self.home_whitelist = set()
                self.plugin_enabled = True
                self.disable_defaults = False
                return

            with open(config_path, "rb") as f:
                conf = tomllib.load(f)

            # Load plugin enable/disable setting FIRST
            if 'main.plugins.SATpwn' in conf:
                self.plugin_enabled = bool(conf['main.plugins.SATpwn'])
                logging.info(f"[SATpwn] Plugin enabled status: {self.plugin_enabled}")
            else:
                self.plugin_enabled = True
                logging.info("[SATpwn] Plugin enabled by default (no config key found)")

            # If plugin is disabled, skip loading other configs but still allow web interface
            if not self.plugin_enabled:
                logging.info("[SATpwn] Plugin disabled via config - limited functionality")
                self.home_whitelist = set()
                self.disable_defaults = False
                return

            # Load home whitelist
            if 'main.home_whitelist' in conf:
                raw = conf['main.home_whitelist']
                if isinstance(raw, str):
                    entries = [x.strip() for x in raw.split(',') if x.strip()]
                elif isinstance(raw, list):
                    entries = [str(x).strip() for x in raw if str(x).strip()]
                else:
                    entries = []
                self.home_whitelist = set(entries)
                logging.info(f"[SATpwn] Loaded home whitelist: {self.home_whitelist}")
            else:
                self.home_whitelist = set()

            # Load disable_defaults setting
            if 'main.disable_defaults' in conf:
                self.disable_defaults = bool(conf['main.disable_defaults'])
                logging.info(f"[SATpwn] Exclusive mode enabled: {self.disable_defaults}")
            else:
                self.disable_defaults = False

        except Exception as e:
            logging.error(f"[SATpwn] Error loading config: {e}")
            self.home_whitelist = set()
            self.plugin_enabled = True
            self.disable_defaults = False

    def _disable_other_plugins(self, agent):
        """Disable all other plugins except SATpwn if disable_defaults is True."""
        if not self.disable_defaults or not self.plugin_enabled:
            return

        try:
            # Get list of all loaded plugins
            loaded_plugins = list(agent._plugins.keys()) if hasattr(agent, '_plugins') else []

            for plugin_name in loaded_plugins:
                if plugin_name.lower() != 'satpwn':
                    try:
                        # Attempt to disable the plugin
                        if hasattr(agent, 'unload_plugin'):
                            agent.unload_plugin(plugin_name)
                        elif hasattr(agent, '_plugins') and plugin_name in agent._plugins:
                            # Try to remove from plugins dict
                            del agent._plugins[plugin_name]

                        self.disabled_plugins.append(plugin_name)
                        logging.info(f"[SATpwn] Disabled plugin: {plugin_name}")
                    except Exception as e:
                        logging.error(f"[SATpwn] Failed to disable plugin {plugin_name}: {e}")

            if self.disabled_plugins:
                logging.info(f"[SATpwn] Exclusive mode - disabled {len(self.disabled_plugins)} plugins: {self.disabled_plugins}")
            else:
                logging.info("[SATpwn] Exclusive mode enabled - no other plugins found to disable")

        except Exception as e:
            logging.error(f"[SATpwn] Error disabling plugins: {e}")

    def _update_activity_history(self, new_ap_count):
        """Track AP discovery activity for movement detection."""
        now = time.time()
        self._activity_history.append((now, new_ap_count))
        cutoff = now - self.ACTIVITY_WINDOW_SECONDS
        self._activity_history = [(t, count) for t, count in self._activity_history if t > cutoff]

    def _is_stationary(self):
        """Check if device appears stationary based on AP discovery patterns."""
        now = time.time()
        recent_activity = sum(count for t, count in self._activity_history 
                             if now - t <= self.ACTIVITY_WINDOW_SECONDS)
        low_activity = recent_activity < self.ACTIVITY_THRESHOLD

        if low_activity:
            if self._stationary_start is None:
                self._stationary_start = now
            elapsed = now - self._stationary_start
            return elapsed >= self.STATIONARY_SECONDS
        else:
            if self._stationary_start is not None:
                self._stationary_start = None
            return False

    def _is_moving(self):
        """Detect movement based on high AP discovery rate."""
        now = time.time()
        recent_activity = sum(count for t, count in self._activity_history 
                             if now - t <= self.ACTIVITY_WINDOW_SECONDS)
        return recent_activity >= self.ACTIVITY_THRESHOLD

    def _home_ssid_visible(self):
        """Check if any home SSID/BSSID is currently visible."""
        if not self.home_whitelist:
            return False

        for ap_mac, ap in self.memory.items():
            ssid = ap.get("ssid", "")
            if ssid in self.home_whitelist or ap_mac in self.home_whitelist:
                return True
        return False

    def _auto_mode_logic(self):
        """Decide sub-mode based on activity patterns and SSID visibility."""
        home_ssid_visible = self._home_ssid_visible()
        is_stationary = self._is_stationary()
        is_moving = self._is_moving()

        if home_ssid_visible or is_stationary:
            return 'recon'
        if is_moving:
            return 'drive-by'
        return 'loose' if len(self.memory) < 10 else 'strict'

    def _save_memory(self):
        """Save current AP/client memory and mode to JSON file."""
        try:
            memory_data = {
                "plugin_metadata": {
                    "current_mode": self.mode,
                    "last_saved": time.time(),
                    "version": self.__version__,
                    "stationary_start": self._stationary_start,
                    "plugin_enabled": self.plugin_enabled,
                    "disable_defaults": self.disable_defaults,
                    "disabled_plugins": self.disabled_plugins
                },
                "ap_data": self.memory
            }

            with open(self.memory_path, 'w') as f:
                json.dump(memory_data, f, indent=4)
        except Exception as e:
            logging.error(f"[SATpwn] Error saving memory: {e}")

    def _load_memory(self):
        """Load AP/client memory and restore saved mode from JSON file."""
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, 'r') as f:
                    data = json.load(f)

                if "plugin_metadata" in data:
                    metadata = data["plugin_metadata"]
                    self.memory = data.get("ap_data", {})
                    saved_mode = metadata.get("current_mode", self.modes[0])
                    if saved_mode in self.modes:
                        self.mode = saved_mode
                    else:
                        self.mode = self.modes[0]
                    self._stationary_start = metadata.get("stationary_start", None)
                    self.disabled_plugins = metadata.get("disabled_plugins", [])
                else:
                    self.memory = data
                    self.mode = self.modes[0]

            except Exception as e:
                logging.error(f"[SATpwn] Error loading memory: {e}")
                self.memory = {}
                self.mode = self.modes[0]
        else:
            logging.info("[SATpwn] No existing memory file found")

    def _cleanup_memory(self):
        """Remove old APs and clients from memory."""
        if not self.plugin_enabled:
            return

        self.memory_is_dirty = True
        now = time.time()
        ap_expiry = self.DRIVE_BY_AP_EXPIRY_SECONDS if self.mode == 'drive-by' else self.AP_EXPIRY_SECONDS
        client_expiry = self.DRIVE_BY_CLIENT_EXPIRY_SECONDS if self.mode == 'drive-by' else self.CLIENT_EXPIRY_SECONDS

        expired_aps = [ap_mac for ap_mac, data in self.memory.items()
                       if now - data.get("last_seen", 0) > ap_expiry]
        for ap_mac in expired_aps:
            if ap_mac in self.memory:
                del self.memory[ap_mac]

        for ap_mac in list(self.memory.keys()):
            if ap_mac not in self.memory: 
                continue
            clients = self.memory[ap_mac].get("clients", {})
            expired_clients = [client_mac for client_mac, data in clients.items()
                               if now - data.get("last_seen", 0) > client_expiry]
            for client_mac in expired_clients:
                if client_mac in clients:
                    del clients[client_mac]

    def _recalculate_client_score(self, ap_mac, client_mac):
        """Calculate client's score based on signal, success, and age."""
        client_data = self.memory[ap_mac]['clients'][client_mac]
        score = (client_data.get('signal', -100) + 100)

        if client_data.get('last_success', 0) > time.time() - self.SUCCESS_BONUS_DURATION_SECONDS:
            score += 50

        age_hours = (time.time() - client_data.get('last_seen', time.time())) / 3600
        decay_amount = age_hours * self.SCORE_DECAY_PENALTY_PER_HOUR
        score -= decay_amount
        score = max(0, score)

        client_data['score'] = score
        return score

    def _execute_attack(self, agent, ap_mac, client_mac):
        """Execute attack on high-value target."""
        if not self.plugin_enabled:
            return

        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            if sub_mode == 'recon':
                return

        # Block attacks in recon mode
        if self.mode == 'recon':
            return

        try:
            logging.info(f"[SATpwn] Executing attack on {client_mac} via {ap_mac}")
            # In a real implementation, this would call agent.deauth() or similar
        except Exception as e:
            logging.error(f"[SATpwn] Attack execution failed: {e}")

    def _get_channel_stats(self):
        """Aggregate stats per channel from memory."""
        channel_stats = {}
        for ap_mac, ap_data in self.memory.items():
            ch = ap_data.get("channel")
            if ch is None: 
                continue
            if ch not in channel_stats:
                channel_stats[ch] = {'aps': 0, 'clients': 0, 'handshakes': 0}
            channel_stats[ch]['aps'] += 1
            channel_stats[ch]['clients'] += len(ap_data.get('clients', {}))
            channel_stats[ch]['handshakes'] += ap_data.get('handshakes', 0)
        return channel_stats

    def _channel_iterator(self, channels):
        """Generator that cycles through channels."""
        if not channels:
            return
        while True:
            for channel in channels:
                yield channel

    # Plugin lifecycle methods
    def on_loaded(self):
        """Called when plugin is loaded."""
        logging.info("[SATpwn] Plugin loaded")
        self._load_memory()

        if not self.plugin_enabled:
            logging.info("[SATpwn] Plugin disabled via config - limited functionality")

    def on_unload(self, ui):
        """Called when plugin is unloaded."""
        self._save_memory()
        self.executor.shutdown(wait=False)
        logging.info("[SATpwn] Plugin unloaded")

    def on_ready(self, agent):
        """Called when agent is ready."""
        if not self.plugin_enabled:
            logging.info("[SATpwn] Plugin disabled - on_ready skipped")
            return

        self.agent = agent
        self.ready = True

        # Disable other plugins if configured to do so
        self._disable_other_plugins(agent)

        exclusive_status = "EXCLUSIVE" if self.disable_defaults else "SHARED"
        logging.info(f"[SATpwn] Plugin ready - Mode: {self.mode} ({exclusive_status})")

    def on_ui_setup(self, ui):
        """Setup UI elements."""
        if not self.plugin_enabled:
            return

        ui.add_element('sat_mode', components.Text(
            color=view.WHITE,
            value=f'SAT Mode: {self.mode.capitalize()}',
            position=(5,13)
        ))

    def on_ui_update(self, ui):
        """Update UI elements."""
        if not self.plugin_enabled:
            return

        mode_text = self.mode.capitalize()
        if self.mode == 'auto' and self._current_auto_submode:
            mode_text += f" ({self._current_auto_submode})"
        if self.disable_defaults:
            mode_text += " [EXCL]"
        ui.set('sat_mode', f'SAT Mode: {mode_text}')

    def on_wifi_update(self, agent, access_points):
        """Process WiFi scan results."""
        if not self.plugin_enabled:
            return

        now = time.time()
        new_ap_count = 0

        for ap in access_points:
            ap_mac = ap['mac'].lower()
            if ap_mac not in self.memory:
                new_ap_count += 1
                self.memory[ap_mac] = {
                    "ssid": ap['hostname'], 
                    "channel": ap['channel'], 
                    "clients": {}, 
                    "last_seen": now, 
                    "handshakes": 0
                }
            else:
                self.memory[ap_mac].update(
                    last_seen=now, 
                    ssid=ap['hostname'], 
                    channel=ap['channel']
                )

            for client in ap['clients']:
                client_mac = client['mac'].lower()

                if client_mac not in self.memory[ap_mac]['clients']:
                    self.memory[ap_mac]['clients'][client_mac] = {
                        "last_seen": now, 
                        "signal": client['rssi'], 
                        "score": 0, 
                        "last_attempt": 0, 
                        "last_success": 0,
                        "last_recalculated": 0
                    }
                else:
                    self.memory[ap_mac]['clients'][client_mac].update(
                        last_seen=now, 
                        signal=client['rssi']
                    )

                client_data = self.memory[ap_mac]['clients'][client_mac]

                last_recalculated = client_data.get('last_recalculated', 0)
                if now - last_recalculated > self.SCORE_RECALCULATION_INTERVAL_SECONDS:
                    score = self._recalculate_client_score(ap_mac, client_mac)
                    client_data['last_recalculated'] = now
                else:
                    score = client_data.get('score', 0)

                last_attempt = client_data.get('last_attempt', 0)
                attack_score_threshold = self.DRIVE_BY_ATTACK_SCORE_THRESHOLD if self.mode == 'drive-by' else self.ATTACK_SCORE_THRESHOLD
                attack_cooldown = self.DRIVE_BY_ATTACK_COOLDOWN_SECONDS if self.mode == 'drive-by' else self.ATTACK_COOLDOWN_SECONDS

                if score >= attack_score_threshold and (now - last_attempt > attack_cooldown):
                    client_data['last_attempt'] = now
                    self.executor.submit(self._execute_attack, agent, ap_mac, client_mac)

        self._update_activity_history(new_ap_count)
        self.memory_is_dirty = True

    def on_handshake(self, agent, filename, ap, client):
        """Process captured handshakes."""
        if not self.plugin_enabled:
            return

        ap_mac = ap['mac'].lower()
        if ap_mac in self.memory:
            self.memory[ap_mac]['handshakes'] = self.memory[ap_mac].get('handshakes', 0) + 1

        client_mac = client['mac'].lower()
        if ap_mac in self.memory and client_mac in self.memory[ap_mac]['clients']:
            self.memory[ap_mac]['clients'][client_mac]['last_success'] = time.time()
            self._recalculate_client_score(ap_mac, client_mac)

        self.memory_is_dirty = True

    # Channel selection methods for different modes
    def _epoch_strict(self, agent, epoch, epoch_data, supported_channels):
        """Strict mode channel selection."""
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            agent.set_channel(next_channel)
            return

        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)

        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])

        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]

        agent.set_channel(next_channel)

    def _epoch_loose(self, agent, epoch, epoch_data, supported_channels):
        """Loose mode channel selection with exploration."""
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        if random.random() < self.EXPLORATION_PROBABILITY:
            next_channel = random.choice(supported_channels)
            agent.set_channel(next_channel)
            return

        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            agent.set_channel(next_channel)
            return

        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)

        exploration_bonus = 1.0
        weights = [w + exploration_bonus for w in weights]

        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])

        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]

        agent.set_channel(next_channel)

    def _epoch_driveby(self, agent, epoch, epoch_data, supported_channels):
        """Drive-by mode uses strict logic but with different timing constants."""
        self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def _epoch_recon(self, agent, epoch, epoch_data, supported_channels):
        """Recon mode systematically scans all channels."""
        if self.recon_channel_iterator is None:
            self.recon_channel_iterator = self._channel_iterator(supported_channels)
            self.recon_channels_tested = []

        if len(self.recon_channels_tested) >= len(supported_channels):
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)
            return

        try:
            next_channel = next(self.recon_channel_iterator)
            if next_channel not in self.recon_channels_tested:
                self.recon_channels_tested.append(next_channel)
                agent.set_channel(next_channel)
            else:
                self._epoch_recon(agent, epoch, epoch_data, supported_channels)
        except StopIteration:
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def on_epoch(self, agent, epoch, epoch_data):
        """Called every epoch to decide channel hopping."""
        if not self.plugin_enabled:
            return

        self._cleanup_memory()
        if not self.ready:
            return

        self._save_memory()

        supported_channels = agent.supported_channels()

        if not supported_channels:
            logging.warning("[SATpwn] No supported channels found.")
            return

        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            self._current_auto_submode = sub_mode
            if sub_mode == 'recon':
                self._epoch_recon(agent, epoch, epoch_data, supported_channels)
            elif sub_mode == 'drive-by':
                self._epoch_driveby(agent, epoch, epoch_data, supported_channels)
            elif sub_mode == 'loose':
                self._epoch_loose(agent, epoch, epoch_data, supported_channels)
            else:
                self._epoch_strict(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'loose':
            self._epoch_loose(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'drive-by':
            self._epoch_driveby(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'recon':
            self._epoch_recon(agent, epoch, epoch_data, supported_channels)
        else:
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def on_webhook(self, path, request):
        """Handle web dashboard requests - always available even when plugin disabled."""
        # Handle mode toggle (only if plugin enabled)
        if path == 'toggle_mode':
            if not self.plugin_enabled:
                return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')

            current_index = self.modes.index(self.mode)
            next_index = (current_index + 1) % len(self.modes)

            old_mode = self.mode
            self.mode = self.modes[next_index]

            if self.mode == 'recon':
                self.recon_channel_iterator = None
                self.recon_channels_tested = []

            if self.mode == 'auto':
                self._current_auto_submode = None

            self._save_memory()

            logging.info(f"[SATpwn] Mode changed from {old_mode} to {self.mode}")
            return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')

        # Main dashboard - always available
        if path == '/' or not path:
            # Show disabled interface if plugin is disabled
            if not self.plugin_enabled:
                disabled_html = """
                <html>
                <head>
                    <title>SATpwn Dashboard - DISABLED</title>
                    <style>
                        body { font-family: monospace; background-color: #1e1e1e; color: #d4d4d4; margin: 0; padding: 20px; }
                        .card { background-color: #252526; border: 1px solid #333; border-radius: 5px; padding: 15px; margin: 10px 0; }
                        h1 { color: #f44336; }
                        h2 { color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px; }
                        p { color: #FFA726; }
                        code { background-color: #333; padding: 2px 4px; border-radius: 3px; }
                        .status { color: #f44336; font-weight: bold; }
                    </style>
                </head>
                <body>
                    <h1>SATpwn Plugin Dashboard</h1>
                    <div class="card">
                        <h2>Plugin Status</h2>
                        <p class="status">PLUGIN DISABLED</p>
                        <p>The SATpwn plugin is currently disabled via configuration.</p>
                        <p>To enable, set: <code>main.plugins.SATpwn = true</code> in config.toml</p>
                        <p>Then restart the pwnagotchi service: <code>sudo systemctl restart pwnagotchi</code></p>
                    </div>
                    <div class="card">
                        <h2>Configuration</h2>
                        <p>Required configuration keys in /etc/pwnagotchi/config.toml:</p>
                        <p><code>main.plugins.SATpwn = true</code> - Enable the plugin</p>
                        <p><code>main.disable_defaults = false</code> - Shared mode (optional)</p>
                        <p><code>main.home_whitelist = "HomeSSID"</code> - Home networks (optional)</p>
                    </div>
                </body>
                </html>
                """
                return Response(disabled_html, mimetype='text/html')

            # Generate active dashboard
            if self.memory_is_dirty or not self.channel_stats:
                self.channel_stats = self._get_channel_stats()
                self.memory_is_dirty = False

            total_aps = len(self.memory)
            total_clients = sum(len(ap.get('clients', {})) for ap in self.memory.values())

            # Channel stats table
            channel_html = "<table><tr><th>Channel</th><th>APs</th><th>Clients</th><th>Handshakes</th></tr>"
            if self.channel_stats:
                for ch, stats in sorted(self.channel_stats.items()):
                    channel_html += f"<tr><td>{ch}</td><td>{stats['aps']}</td><td>{stats['clients']}</td><td>{stats['handshakes']}</td></tr>"
            else:
                channel_html += "<tr><td colspan='4'>No data yet - plugin is starting up</td></tr>"
            channel_html += "</table>"

            # Memory table
            memory_html = "<table><tr><th>AP (SSID/MAC)</th><th>Ch</th><th>Last Seen</th><th>Clients</th></tr>"
            if self.memory:
                sorted_aps = sorted(self.memory.items(), key=lambda item: item[1].get('last_seen', 0), reverse=True)
                for ap_mac, ap_data in sorted_aps[:20]:  # Limit to first 20 for performance
                    last_seen_ap = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ap_data.get('last_seen', 0)))
                    client_count = len(ap_data.get('clients', {}))
                    memory_html += f"<tr><td>{ap_data.get('ssid', 'N/A')}<br><small>{ap_mac}</small></td><td>{ap_data.get('channel', 'N/A')}</td><td>{last_seen_ap}</td><td>{client_count} clients</td></tr>"
                if len(self.memory) > 20:
                    memory_html += f"<tr><td colspan='4'><i>... and {len(self.memory) - 20} more APs</i></td></tr>"
            else:
                memory_html += "<tr><td colspan='4'>No APs discovered yet</td></tr>"
            memory_html += "</table>"

            # Mode controls
            next_mode_index = (self.modes.index(self.mode) + 1) % len(self.modes)
            next_mode_name = self.modes[next_mode_index].replace('-', ' ').title()
            mode_toggle_button = f"<a href='/plugins/SATpwn/toggle_mode' style='display:inline-block;padding:10px;background-color:#569cd6;color:#fff;text-decoration:none;border-radius:5px;margin-top:10px;'>Switch to {next_mode_name} Mode</a>"

            # Recon status
            recon_status = ""
            if self.mode == 'recon':
                channels_tested = len(self.recon_channels_tested) if self.recon_channels_tested else 0
                total_channels = len(self.agent.supported_channels()) if self.agent else 0
                recon_status = f"<p><b>Recon Progress:</b> {channels_tested}/{total_channels} channels surveyed</p>"

            # Plugin status
            if self.disable_defaults:
                exclusive_color = "#4CAF50"
                exclusive_text = "EXCLUSIVE MODE"
                disabled_count = len(self.disabled_plugins)
                plugin_status = f"""
                <p style="color: #4CAF50;"><b>Plugin Status: ENABLED</b></p>
                <p style="color: {exclusive_color};"><b>{exclusive_text}</b></p>
                <p><b>Disabled Plugins:</b> {disabled_count}</p>
                <p><small>SATpwn is the primary plugin</small></p>
                """
            else:
                plugin_status = """
                <p style="color: #4CAF50;"><b>Plugin Status: ENABLED</b></p>
                <p style="color: #FFA726;"><b>SHARED MODE</b></p>
                <p><small>Other plugins may also be active</small></p>
                """

            # AUTO mode status
            auto_status = ""
            if self.mode == 'auto':
                home_visible = self._home_ssid_visible()
                is_stationary = self._is_stationary()
                is_moving = self._is_moving()
                current_sub = self._current_auto_submode or "determining..."

                recent_activity = sum(count for t, count in self._activity_history 
                                    if time.time() - t <= self.ACTIVITY_WINDOW_SECONDS)

                auto_status = f"""
                <p><b>AUTO Sub-Mode:</b> {current_sub.upper()}</p>
                <p><b>Home SSID Visible:</b> {'Yes' if home_visible else 'No'}</p>
                <p><b>Stationary (1hr):</b> {'Yes' if is_stationary else 'No'}</p>
                <p><b>Moving:</b> {'Yes' if is_moving else 'No'}</p>
                <p><b>Recent Activity:</b> {recent_activity} new APs (5min)</p>
                <p><b>Home Whitelist:</b> {len(self.home_whitelist)} entries</p>
                """

            # Configuration display
            config_display = f"""
            <p><code>main.plugins.SATpwn = {str(self.plugin_enabled).lower()}</code></p>
            <p><code>main.disable_defaults = {str(self.disable_defaults).lower()}</code></p>
            <p><code>main.home_whitelist = "{','.join(sorted(self.home_whitelist))}"</code></p>
            """

            # Main dashboard HTML
            html = f"""
            <html>
            <head>
                <title>SATpwn Dashboard</title>
                <style>
                    body {{ font-family: monospace; background-color: #1e1e1e; color: #d4d4d4; margin: 0; padding: 20px; }}
                    .container {{ display: grid; grid-template-columns: 1fr; gap: 20px; max-width: 1400px; }}
                    .grid-2-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
                    .grid-3-col {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; }}
                    .card {{ background-color: #252526; border: 1px solid #333; border-radius: 5px; padding: 15px; }}
                    h1 {{ color: #569cd6; margin-bottom: 20px; }}
                    h2 {{ color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px; margin-top: 0; }}
                    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
                    th, td {{ border: 1px solid #444; padding: 6px; text-align: left; vertical-align: top; }}
                    th {{ background-color: #333; font-weight: bold; }}
                    code {{ background-color: #333; padding: 2px 4px; border-radius: 3px; }}
                    small {{ color: #888; }}
                    a {{ color: #569cd6; }}
                </style>
            </head>
            <body>
                <h1>SATpwn Dashboard v{self.__version__}</h1>
                <div class="container">
                    <div class="grid-3-col">
                        <div class="card">
                            <h2>Live Stats</h2>
                            <p><b>Total APs:</b> {total_aps}</p>
                            <p><b>Total Clients:</b> {total_clients}</p>
                            <p><b>Memory File:</b> {self.memory_path}</p>
                        </div>
                        <div class="card">
                            <h2>Controls</h2>
                            <p><b>Current Mode:</b> {self.mode.upper()}</p>
                            {recon_status}
                            {mode_toggle_button}
                        </div>
                        <div class="card">
                            <h2>Plugin Status</h2>
                            {plugin_status}
                        </div>
                    </div>
                    <div class="grid-2-col">
                        <div class="card">
                            <h2>Configuration</h2>
                            {config_display}
                        </div>
                        <div class="card">
                            <h2>AUTO Mode Status</h2>
                            {auto_status if auto_status else '<p>Not in AUTO mode</p>'}
                        </div>
                    </div>
                    <div class="card">
                        <h2>Channel Statistics</h2>
                        {channel_html}
                    </div>
                    <div class="card">
                        <h2>Recent Access Points</h2>
                        {memory_html}
                    </div>
                </div>
            </body>
            </html>
            """
            return Response(html, mimetype='text/html')

        return Response("Not Found", status=404, mimetype='text/html')
