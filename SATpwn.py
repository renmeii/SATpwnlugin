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
    __version__ = 'x88.0.8'
    __license__ = 'GPL3'
    __description__ = 'SATpwn intelligent targeting system'
    
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
        
        self._last_activity_check = 0
        self._activity_history = []
        self.home_whitelist = set()
        self._current_auto_submode = None
        self._stationary_start = None
        
        logging.info("[SATpwn] Plugin initializing...")
        self._load_home_whitelist()
    
    def _load_home_whitelist(self):
        """Load home SSID/BSSID whitelist from flat TOML key."""
        try:
            config_path = "/etc/pwnagotchi/config.toml"
            
            if not os.path.exists(config_path):
                self.home_whitelist = set()
                return
            
            with open(config_path, "rb") as f:
                conf = tomllib.load(f)
            
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
                
        except Exception as e:
            logging.error(f"[SATpwn] Error loading home whitelist: {e}")
            self.home_whitelist = set()
    
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
                    "stationary_start": self._stationary_start
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
                else:
                    self.memory = data
                    self.mode = self.modes[0]
                    
            except Exception as e:
                logging.error(f"[SATpwn] Error loading memory: {e}")
                self.memory = {}
                self.mode = self.modes[0]
    
    def _cleanup_memory(self):
        """Remove old APs and clients from memory."""
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
        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            if sub_mode == 'recon':
                return
                
        try:
            logging.info(f"[SATpwn] Executing attack on {client_mac} via {ap_mac}")
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
    
    def on_loaded(self):
        logging.info("[SATpwn] Plugin loaded")
        self._load_memory()
    
    def on_unload(self, ui):
        self._save_memory()
        self.executor.shutdown(wait=False)
        logging.info("[SATpwn] Plugin unloaded")
    
    def on_ready(self, agent):
        self.agent = agent
        self.ready = True
        logging.info(f"[SATpwn] Plugin ready in mode: {self.mode}")
    
    def on_ui_setup(self, ui):
        ui.add_element('sat_mode', components.Text(
        color=view.WHITE,
        value=f'SAT Mode: {self.mode.capitalize()}',
        position=(5,13)))
    
    def on_ui_update(self, ui):
        mode_text = self.mode.capitalize()
        if self.mode == 'auto' and self._current_auto_submode:
            mode_text += f" ({self._current_auto_submode})"
        ui.set('sat_mode', f'SAT Mode: {mode_text}')
    
    def on_wifi_update(self, agent, access_points):
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
        ap_mac = ap['mac'].lower()
        if ap_mac in self.memory:
            self.memory[ap_mac]['handshakes'] = self.memory[ap_mac].get('handshakes', 0) + 1
        
        client_mac = client['mac'].lower()
        if ap_mac in self.memory and client_mac in self.memory[ap_mac]['clients']:
            self.memory[ap_mac]['clients'][client_mac]['last_success'] = time.time()
            self._recalculate_client_score(ap_mac, client_mac)
        
        self.memory_is_dirty = True
    
    def _epoch_strict(self, agent, epoch, epoch_data, supported_channels):
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
        self._epoch_strict(agent, epoch, epoch_data, supported_channels)
    
    def _epoch_recon(self, agent, epoch, epoch_data, supported_channels):
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
        self._cleanup_memory()
        if not self.ready:
            return Response("Plugin not ready yet.", mimetype='text/html')
        
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
        if path == 'toggle_mode':
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
        
        if path == '/' or not path:
            if self.memory_is_dirty or not self.channel_stats:
                self.channel_stats = self._get_channel_stats()
                self.memory_is_dirty = False
            
            total_aps = len(self.memory)
            total_clients = sum(len(ap.get('clients', {})) for ap in self.memory.values())
            
            channel_html = "<table><tr><th>Ch</th><th>APs</th><th>Clients</th><th>Handshakes</th></tr>"
            for ch, stats in sorted(self.channel_stats.items()):
                channel_html += f"<tr><td>{ch}</td><td>{stats['aps']}</td><td>{stats['clients']}</td><td>{stats['handshakes']}</td></tr>"
            channel_html += "</table>"
            
            memory_html = "<table><tr><th>AP (SSID/MAC)</th><th>Ch</th><th>Last Seen</th><th>Clients (MAC / Score / Last Seen)</th></tr>"
            sorted_aps = sorted(self.memory.items(), key=lambda item: item[1].get('last_seen', 0), reverse=True)
            for ap_mac, ap_data in sorted_aps:
                last_seen_ap = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ap_data.get('last_seen', 0)))
                client_html = "<ul style='margin:0;padding-left:15px;'>"
                sorted_clients = sorted(ap_data.get('clients', {}).items(), key=lambda item: item[1].get('score', 0), reverse=True)
                for client_mac, client_data in sorted_clients:
                    last_seen_client = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(client_data.get('last_seen', 0)))
                    score = client_data.get('score', 0)
                    client_html += f"<li><small>{client_mac} | Score: {score:.2f} | Seen: {last_seen_client}</small></li>"
                client_html += "</ul>"
                memory_html += f"<tr><td>{ap_data.get('ssid', 'N/A')}<br><small>{ap_mac}</small></td><td>{ap_data.get('channel', 'N/A')}</td><td>{last_seen_ap}</td><td>{client_html}</td></tr>"
            memory_html += "</table>"
            
            next_mode_index = (self.modes.index(self.mode) + 1) % len(self.modes)
            next_mode_name = self.modes[next_mode_index].replace('-', ' ').title()
            mode_toggle_button = f"<a href='/plugins/SATpwn/toggle_mode' style='display:inline-block;padding:10px;background-color:#569cd6;color:#fff;text-decoration:none;border-radius:5px;'>Switch to {next_mode_name} Mode</a>"
            
            recon_status = ""
            if self.mode == 'recon':
                channels_tested = len(self.recon_channels_tested) if self.recon_channels_tested else 0
                total_channels = len(self.agent.supported_channels()) if self.agent else 0
                recon_status = f"<p><b>Recon Progress:</b> {channels_tested}/{total_channels} channels surveyed</p>"
            
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
            
            html = f"""
            <html>
            <head>
                <title>SATpwn Dashboard</title>
                <style>
                    body {{ font-family: monospace; background-color: #1e1e1e; color: #d4d4d4; margin: 0; padding: 20px; }}
                    .container {{ display: grid; grid-template-columns: 1fr; gap: 20px; }}
                    .grid-2-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
                    .card {{ background-color: #252526; border: 1px solid #333; border-radius: 5px; padding: 15px; }}
                    h1, h2 {{ color: #569cd6; border-bottom: 1px solid #333; padding-bottom: 5px;}}
                    table {{ width: 100%; border-collapse: collapse; }}
                    th, td {{ border: 1px solid #444; padding: 8px; text-align: left; vertical-align: top;}}
                    th {{ background-color: #333; }}
                    ul {{ list-style-type: none; margin: 0; padding-left: 15px;}}
                </style>
            </head>
            <body>
                <h1>SATpwn Dashboard</h1>
                <div class="container">
                    <div class="grid-2-col">
                        <div class="card">
                            <h2>Live Stats</h2>
                            <p>Total APs Tracked: {total_aps}</p>
                            <p>Total Clients Tracked: {total_clients}</p>
                        </div>
                        <div class="card">
                            <h2>Controls</h2>
                            <p><b>Current Mode:</b> {self.mode.upper()}</p>
                            {recon_status}
                            {auto_status}
                            {mode_toggle_button}
                        </div>
                    </div>
                    <div class="card">
                        <h2>Channel Weights</h2>
                        {channel_html}
                    </div>
                    <div class="card">
                        <h2>AP & Client Memory</h2>
                        {memory_html}
                    </div>
                </div>
            </body>
            </html>
            """
            return Response(html, mimetype='text/html')
        
        return Response("Not Found", status=404, mimetype='text/html')
