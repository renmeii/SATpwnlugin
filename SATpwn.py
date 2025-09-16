import logging
import pwnagotchi.plugins as plugins
import pwnagotchi.ui.components as components
import pwnagotchi.ui.view as view
from flask import Response
import random
import json
import os
import time
import math
from concurrent.futures import ThreadPoolExecutor

class SATpwn(plugins.Plugin):
    __author__ = 'Renmeii x Mr-Cass-Ette and discoJack too '
    __version__ = 'x88.0.4-auto-geo'
    __license__ = 'GPL3'
    __description__ = 'SATpwn, the superior way to capture handshakes with auto mode and GPS deadzone'
    
    # --- Constants for configuration ---
    AP_EXPIRY_SECONDS = 3600 * 48  # 48 hours
    CLIENT_EXPIRY_SECONDS = 3600 * 24  # 24 hours
    ATTACK_SCORE_THRESHOLD = 50
    ATTACK_COOLDOWN_SECONDS = 300  # 5 minutes
    SUCCESS_BONUS_DURATION_SECONDS = 1800  # 30 minutes
    SCORE_DECAY_PENALTY_PER_HOUR = 5  # Score penalty per hour
    PMKID_FRIENDLY_APS_THRESHOLD = 3
    PMKID_FRIENDLY_BOOST_FACTOR = 1.5
    HANDSHAKE_WEIGHT = 10
    CLIENT_WEIGHT = 1
    SCORE_RECALCULATION_INTERVAL_SECONDS = 30  # 30 seconds
    EXPLORATION_PROBABILITY = 0.1  # 10% chance to explore a random channel in loose mode
    DRIVE_BY_AP_EXPIRY_SECONDS = 1800  # 30 minutes
    DRIVE_BY_CLIENT_EXPIRY_SECONDS = 900  # 15 minutes
    DRIVE_BY_ATTACK_SCORE_THRESHOLD = 20 # Lower score threshold
    DRIVE_BY_ATTACK_COOLDOWN_SECONDS = 60  # 1 minute
    
    # AUTO Mode constants
    STATIONARY_SECONDS = 3600      # 1 hour to trigger "recon" (passive/compliance)
    MOVE_SPEED_THRESHOLD = 0.5     # meters/second (for mobile detection)
    MOVE_DEBOUNCE_SECS = 20
    
    # GPS Deadzone constants
    HOME_DEADZONE_METERS = 6.0     # ~20ft buffer zone
    MOVEMENT_DISTANCE_THRESHOLD = 20.0  # Must move 20m+ to be "moving"
    
    def __init__(self):
        self.ready = False
        self.agent = None
        self.memory = {}
        self.modes = ['strict', 'loose', 'drive-by', 'recon', 'auto']
        self.memory_path = '/etc/pwnagotchi/SATpwn_memory.json'
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.mode = self.modes[0]  # Default mode, will be overridden by _load_memory()
        self.channel_stats = {}
        self.memory_is_dirty = True
        self.recon_channel_iterator = None
        self.recon_channels_tested = []
        
        # AUTO mode state
        self._last_gps = None # (time, lat, lon, speed)
        self._last_move_ok = 0
        self.home_whitelist = set()
        self._current_auto_submode = None  # Track what AUTO is currently running
        
        # GPS Deadzone state
        self._home_anchor_point = None  # (lat, lon) of detected home
        self._movement_start_point = None  # Track movement origin
        
        self._load_home_whitelist()
    
    def _load_home_whitelist(self):
        """Load home SSID/BSSID whitelist from pwnagotchi config."""
        try:
            # Try to import pwnagotchi config - adjust path as needed
            import pwnagotchi.config as config
            conf = config.config
            self.home_whitelist = set(conf.get('main', {}).get("home_whitelist", []))
            if self.home_whitelist:
                logging.info(f"[SATpwn] Loaded home whitelist: {len(self.home_whitelist)} entries")
        except Exception as e:
            logging.warning(f"[SATpwn] Could not load home whitelist from config: {e}")
            self.home_whitelist = set()
    
    def _update_gps_cache(self, gps_fix):
        """Update GPS cache with latest fix."""
        self._last_gps = (time.time(), gps_fix.get('lat', 0), gps_fix.get('lon', 0), gps_fix.get('speed', 0))
        
        # Set home anchor point if we detect we're at home and don't have one yet
        if not self._home_anchor_point and self._home_ssid_visible():
            _, lat, lon, _ = self._last_gps
            self._home_anchor_point = (lat, lon)
            logging.info(f"[SATpwn] Home anchor point set: {lat:.6f}, {lon:.6f}")
    
    def _calculate_distance(self, lat1, lon1, lat2, lon2):
        """Calculate distance between two GPS points in meters using Haversine formula"""
        if lat1 == 0 and lon1 == 0:  # Invalid GPS coordinates
            return float('inf')
        if lat2 == 0 and lon2 == 0:  # Invalid GPS coordinates
            return float('inf')
            
        # Convert latitude and longitude from degrees to radians
        lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
        
        # Haversine formula
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
        c = 2 * math.asin(math.sqrt(a))
        
        # Radius of earth in meters
        r = 6371000
        
        return c * r
    
    def _is_within_home_deadzone(self):
        """Check if current position is within home deadzone buffer"""
        if not self._last_gps or not self._home_anchor_point:
            return False
        _, lat, lon, _ = self._last_gps
        home_lat, home_lon = self._home_anchor_point
        distance = self._calculate_distance(lat, lon, home_lat, home_lon)
        return distance <= self.HOME_DEADZONE_METERS
    
    def _is_stationary(self):
        """Check if device has been stationary for STATIONARY_SECONDS."""
        if not self._last_gps:
            return False
        t, _, _, spd = self._last_gps
        return (time.time() - t >= self.STATIONARY_SECONDS) and spd < self.MOVE_SPEED_THRESHOLD
    
    def _is_moving(self):
        """Enhanced movement detection with geographic buffer"""
        if not self._last_gps:
            return False
        
        # Speed-based check (existing logic)
        t, lat, lon, spd = self._last_gps
        speed_moving = spd >= self.MOVE_SPEED_THRESHOLD
        
        # Distance-based check (new logic)
        if self._movement_start_point:
            start_lat, start_lon = self._movement_start_point
            distance_moved = self._calculate_distance(lat, lon, start_lat, start_lon)
            distance_moving = distance_moved >= self.MOVEMENT_DISTANCE_THRESHOLD
        else:
            distance_moving = False
            self._movement_start_point = (lat, lon)
        
        # Check if we're within home deadzone
        within_home_deadzone = self._is_within_home_deadzone()
        
        # Combine conditions - must be moving by speed AND distance, and NOT in home deadzone
        if speed_moving and distance_moving and not within_home_deadzone:
            if self._last_move_ok == 0:
                self._last_move_ok = time.time()
            if (time.time() - self._last_move_ok) >= self.MOVE_DEBOUNCE_SECS:
                return True
        else:
            self._last_move_ok = 0
            # Reset movement start point if we're not moving
            if not speed_moving:
                self._movement_start_point = (lat, lon)
                
        return False
    
    def _home_ssid_visible(self):
        """Check if any home SSID/BSSID is currently visible."""
        for ap_mac, ap in self.memory.items():
            if ap.get("ssid") in self.home_whitelist or ap_mac in self.home_whitelist:
                return True
        return False
    
    def _auto_mode_logic(self):
        """Decide sub-mode based on GPS & SSID visibility."""
        # Enhanced logic with deadzone consideration
        home_ssid_visible = self._home_ssid_visible()
        is_stationary = self._is_stationary()
        within_deadzone = self._is_within_home_deadzone()
        
        if home_ssid_visible or is_stationary or within_deadzone:
            return 'recon'  # when 'home', run recon mapping (passive/compliance behaviors)
        if self._is_moving():
            return 'drive-by'
        return 'loose' if len(self.memory) < 10 else 'strict'
        
    def _save_memory(self):
        """Saves the current AP/client memory and current mode to a JSON file."""
        try:
            # Create a complete memory structure that includes metadata
            memory_data = {
                "plugin_metadata": {
                    "current_mode": self.mode,
                    "last_saved": time.time(),
                    "version": self.__version__,
                    "home_anchor_point": self._home_anchor_point
                },
                "ap_data": self.memory
            }
            
            with open(self.memory_path, 'w') as f:
                json.dump(memory_data, f, indent=4)
            logging.info(f"[SATpwn] Memory and mode '{self.mode}' saved to {self.memory_path}")
        except Exception as e:
            logging.error(f"[SATpwn] Error saving memory: {e}")
    
    def _load_memory(self):
        """Loads the AP/client memory and restores the last saved mode from a JSON file."""
        if os.path.exists(self.memory_path):
            try:
                with open(self.memory_path, 'r') as f:
                    data = json.load(f)
                
                # Handle both old format (direct AP data) and new format (with metadata)
                if "plugin_metadata" in data:
                    # New format with metadata
                    metadata = data["plugin_metadata"]
                    self.memory = data.get("ap_data", {})
                    
                    # Restore the last saved mode
                    saved_mode = metadata.get("current_mode", self.modes[0])
                    if saved_mode in self.modes:
                        self.mode = saved_mode
                        logging.info(f"[SATpwn] Restored mode: {self.mode}")
                    else:
                        logging.warning(f"[SATpwn] Invalid saved mode '{saved_mode}', using default: {self.modes[0]}")
                        self.mode = self.modes[0]
                    
                    # Restore home anchor point if available
                    self._home_anchor_point = metadata.get("home_anchor_point", None)
                    if self._home_anchor_point:
                        logging.info(f"[SATpwn] Restored home anchor point: {self._home_anchor_point}")
                    
                    last_saved = metadata.get("last_saved", 0)
                    time_diff = time.time() - last_saved
                    logging.info(f"[SATpwn] Memory loaded from {self.memory_path} (last saved {time_diff:.0f}s ago)")
                else:
                    # Old format - just AP data
                    self.memory = data
                    self.mode = self.modes[0]  # Default to strict mode
                    logging.info(f"[SATpwn] Legacy memory format loaded, defaulting to mode: {self.mode}")
                    
            except Exception as e:
                logging.error(f"[SATpwn] Error loading memory: {e}")
                self.memory = {}
                self.mode = self.modes[0]
        else:
            logging.info("[SATpwn] No existing memory file found, starting fresh")
    
    def _cleanup_memory(self):
        """Removes old APs and clients from memory to keep it relevant."""
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
        """Calculates a client's score based on signal, success, and age."""
        client_data = self.memory[ap_mac]['clients'][client_mac]
        # Base score from signal strength
        score = (client_data.get('signal', -100) + 100)
        
        # Bonus for recent handshake success
        if client_data.get('last_success', 0) > time.time() - self.SUCCESS_BONUS_DURATION_SECONDS:
            score += 50
        
        # Decay score based on how long ago the client was last seen
        age_hours = (time.time() - client_data.get('last_seen', time.time())) / 3600
        decay_amount = age_hours * self.SCORE_DECAY_PENALTY_PER_HOUR
        score -= decay_amount
        
        # Ensure score doesn't go below zero
        score = max(0, score)
        
        client_data['score'] = score
        return score
    
    def _execute_attack(self, agent, ap_mac, client_mac):
        """Logs the intent to attack a high-value target."""
        # Block attacks if we're in AUTO mode and the logic says we should be passive
        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            if sub_mode == 'recon':
                logging.debug(f"[SATpwn] AUTO mode blocking attack - in recon/passive mode")
                return
                
        try:
            logging.info(f"[SATpwn] Executing tactical attack on {client_mac} via {ap_mac}")
        except Exception as e:
            logging.error(f"[SATpwn] Attack execution failed: {e}")
    
    def _get_channel_stats(self):
        """Aggregates stats per channel from memory."""
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
        """Generator that yields channels one by one and cycles through them."""
        if not channels:
            return
        while True:
            for channel in channels:
                yield channel
    
    def on_loaded(self):
        logging.info("[SATpwn] plugin loaded")
        self._load_memory()  # This now also restores the saved mode
    
    def on_unload(self, ui):
        self._save_memory()  # This now also saves the current mode
        self.executor.shutdown(wait=False)
        logging.info("[SATpwn] plugin unloaded")
    
    def on_ready(self, agent):
        self.agent = agent
        self.ready = True
        logging.info(f"[SATpwn] plugin ready in mode: {self.mode}")
    
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
        for ap in access_points:
            ap_mac = ap['mac'].lower()
            if ap_mac not in self.memory:
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
                
                # Throttle score recalculation
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
    
    def on_gps_fix(self, gps_coordinates):
        """Interface for GPS plugin (expects dict: {'lat':..., 'lon':..., 'speed':...})"""
        self._update_gps_cache(gps_coordinates)
    
    # Code for all of the modes (START)
    def _epoch_strict(self, agent, epoch, epoch_data, supported_channels):
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False
        
        # Weighted selection logic
        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No channel data, hopping to random channel {next_channel}")
            agent.set_channel(next_channel)
            return
        
        weights = []
        for ch in channels:
            stats = self.channel_stats.get(ch, {'clients': 0, 'handshakes': 0, 'aps': 0})
            weight = (stats['clients'] * self.CLIENT_WEIGHT) + (stats['handshakes'] * self.HANDSHAKE_WEIGHT)
            if stats['aps'] > self.PMKID_FRIENDLY_APS_THRESHOLD and stats['aps'] > stats['clients']:
                weight *= self.PMKID_FRIENDLY_BOOST_FACTOR
            weights.append(weight)
        
        # Filter down to only channels that are supported by the hardware
        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])
        
        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No tracked channels are supported, hopping to random supported channel {next_channel}")
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
                logging.info(f"[SATpwn] All tracked channel weights are zero, hopping to random tracked/supported channel {next_channel}")
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]
                logging.info(f"[SATpwn] Hopping to weighted-random channel {next_channel} (Mode: {self.mode})")
        
        agent.set_channel(next_channel)
    
    def _epoch_loose(self, agent, epoch, epoch_data, supported_channels):
        if self.memory_is_dirty or not self.channel_stats:
            self.channel_stats = self._get_channel_stats()
            self.memory_is_dirty = False
        
        if random.random() < self.EXPLORATION_PROBABILITY:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] Exploring random channel {next_channel} (Mode: loose)")
            agent.set_channel(next_channel)
            return
        
        # Weighted selection logic
        channels = list(self.channel_stats.keys())
        if not channels:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No channel data, hopping to random channel {next_channel}")
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
        logging.debug("[SATpwn] Applied exploration bonus for loose mode.")
        
        # Filter down to only channels that are supported by the hardware
        supported_channels_with_weights = []
        supported_weights = []
        for i, ch in enumerate(channels):
            if ch in supported_channels:
                supported_channels_with_weights.append(ch)
                supported_weights.append(weights[i])
        
        if not supported_channels_with_weights:
            next_channel = random.choice(supported_channels)
            logging.info(f"[SATpwn] No tracked channels are supported, hopping to random supported channel {next_channel}")
        else:
            total_weight = sum(supported_weights)
            if total_weight == 0:
                next_channel = random.choice(supported_channels_with_weights)
                logging.info(f"[SATpwn] All tracked channel weights are zero, hopping to random tracked/supported channel {next_channel}")
            else:
                next_channel = random.choices(supported_channels_with_weights, weights=supported_weights, k=1)[0]
                logging.info(f"[SATpwn] Hopping to weighted-random channel {next_channel} (Mode: {self.mode})")
        
        agent.set_channel(next_channel)
    
    def _epoch_driveby(self, agent, epoch, epoch_data, supported_channels):
        # Drive-by mode uses strict logic but with different timing constants
        self._epoch_strict(agent, epoch, epoch_data, supported_channels)
    
    def _epoch_recon(self, agent, epoch, epoch_data, supported_channels):
        # Initialize channel iterator if not already done
        if self.recon_channel_iterator is None:
            self.recon_channel_iterator = self._channel_iterator(supported_channels)
            self.recon_channels_tested = []
        
        # If we've tested all channels, switch to strict mode for final optimization
        if len(self.recon_channels_tested) >= len(supported_channels):
            logging.info("[SATpwn] RECON: Completed channel survey, switching to strict mode logic")
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)
            return
        
        try:
            next_channel = next(self.recon_channel_iterator)
            if next_channel not in self.recon_channels_tested:
                self.recon_channels_tested.append(next_channel)
                logging.info(f"[SATpwn] RECON: Inspecting channel {next_channel} and gathering info...")
                agent.set_channel(next_channel)
            else:
                # Skip already tested channels
                self._epoch_recon(agent, epoch, epoch_data, supported_channels)
        except StopIteration:
            # This shouldn't happen with our infinite iterator, but just in case
            logging.info("[SATpwn] RECON: Channel iterator exhausted, switching to strict mode")
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)
    
    # Code for all of the modes (END)
    
    def on_epoch(self, agent, epoch, epoch_data):
        self._cleanup_memory()
        if not self.ready:
            return Response("Plugin not ready yet.", mimetype='text/html')
        
        # Save memory and current mode every epoch
        self._save_memory()
        
        supported_channels = agent.supported_channels()
        logging.debug(f"[SATpwn] Supported channels: {supported_channels}")
        
        if not supported_channels:
            logging.warning("[SATpwn] No supported channels found.")
            return
        
        if self.mode == 'auto':
            sub_mode = self._auto_mode_logic()
            self._current_auto_submode = sub_mode
            if sub_mode == 'recon':
                logging.info("[SATpwn] AUTO ➜ RECON mode (home/stationary)")
                self._epoch_recon(agent, epoch, epoch_data, supported_channels)
            elif sub_mode == 'drive-by':
                logging.info("[SATpwn] AUTO ➜ DRIVE-BY mode (moving)")
                self._epoch_driveby(agent, epoch, epoch_data, supported_channels)
            elif sub_mode == 'loose':
                logging.info("[SATpwn] AUTO ➜ LOOSE mode (away & low data)")
                self._epoch_loose(agent, epoch, epoch_data, supported_channels)
            else:
                logging.info("[SATpwn] AUTO ➜ STRICT mode (away & data-rich)")
                self._epoch_strict(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'loose':
            logging.info("[SATpwn] Epoch done; loading loose mode")
            self._epoch_loose(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'drive-by':
            logging.info("[SATpwn] Epoch done; loading drive-by mode")
            self._epoch_driveby(agent, epoch, epoch_data, supported_channels)
        elif self.mode == 'recon':
            logging.info("[SATpwn] Epoch done; loading recon mode")
            self._epoch_recon(agent, epoch, epoch_data, supported_channels)
        else:
            logging.info("[SATpwn] Epoch done; loading strict mode")
            self._epoch_strict(agent, epoch, epoch_data, supported_channels)
    
    def on_webhook(self, path, request):
        # Handle mode toggling
        if path == 'toggle_mode':
            current_index = self.modes.index(self.mode)
            logging.debug(f"current index = {current_index}")
            next_index = (current_index + 1) % len(self.modes)
            logging.debug(f"next index = {next_index}")
            
            old_mode = self.mode
            self.mode = self.modes[next_index]
            
            # Reset recon state when switching modes
            if self.mode == 'recon':
                self.recon_channel_iterator = None
                self.recon_channels_tested = []
            
            # Reset AUTO sub-mode tracking
            if self.mode == 'auto':
                self._current_auto_submode = None
            
            # Save the new mode immediately
            self._save_memory()
            
            logging.info(f"[SATpwn] Mode changed from {old_mode} to {self.mode}")
            return Response('<html><head><meta http-equiv="refresh" content="0; url=/plugins/SATpwn/" /></head></html>', mimetype='text/html')
        
        # Main dashboard page
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
            
            # Add recon status if in recon mode
            recon_status = ""
            if self.mode == 'recon':
                channels_tested = len(self.recon_channels_tested) if self.recon_channels_tested else 0
                total_channels = len(self.agent.supported_channels()) if self.agent else 0
                recon_status = f"<p><b>Recon Progress:</b> {channels_tested}/{total_channels} channels surveyed</p>"
            
            # Add AUTO mode status with enhanced GPS info
            auto_status = ""
            if self.mode == 'auto':
                home_visible = self._home_ssid_visible()
                is_stationary = self._is_stationary()
                is_moving = self._is_moving()
                within_deadzone = self._is_within_home_deadzone()
                current_sub = self._current_auto_submode or "determining..."
                
                # GPS info
                gps_info = "No GPS data"
                if self._last_gps:
                    _, lat, lon, spd = self._last_gps
                    gps_info = f"Lat: {lat:.6f}, Lon: {lon:.6f}, Speed: {spd:.1f}m/s"
                
                home_anchor_info = "Not set"
                if self._home_anchor_point:
                    home_lat, home_lon = self._home_anchor_point
                    home_anchor_info = f"Lat: {home_lat:.6f}, Lon: {home_lon:.6f}"
                
                auto_status = f"""
                <p><b>AUTO Sub-Mode:</b> {current_sub.upper()}</p>
                <p><b>Home SSID Visible:</b> {'Yes' if home_visible else 'No'}</p>
                <p><b>Stationary (1hr):</b> {'Yes' if is_stationary else 'No'}</p>
                <p><b>Moving:</b> {'Yes' if is_moving else 'No'}</p>
                <p><b>Within Home Deadzone:</b> {'Yes' if within_deadzone else 'No'}</p>
                <p><b>Home Whitelist:</b> {len(self.home_whitelist)} entries</p>
                <p><b>GPS:</b> {gps_info}</p>
                <p><b>Home Anchor:</b> {home_anchor_info}</p>
                """
            
            html = f"""
            <html>
            <head>
                <title>Smart Auto-Tune Dashboard</title>
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
